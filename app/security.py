"""Користувачі, ролі, API-ключі, журнал дій.

Ролі:
  owner  — повний доступ: фінанси/прогноз, KPI всіх лікарів, користувачі, API-ключі, вебхуки, журнал, видалення.
  admin  — робота з пацієнтами: картки, плани, файли, нагадування, зіставлення з CleverBox. Без фінпрогнозу,
           без медичних нотаток, без керування доступами.
  doctor — лише свої пацієнти (план, де він лікар), свій KPI, медичні нотатки своїх пацієнтів.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime

from fastapi import Request, HTTPException, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import SessionLocal, User, ApiKey, AuditLog, Patient, BeautyPlan

# capability -> ролі
PERMS: dict[str, set[str]] = {
    "finance":        {"owner"},                     # прогноз, суми по клініці, KPI всіх лікарів
    "kpi_own":        {"owner", "doctor"},
    "patients_all":   {"owner", "admin"},
    "patient_edit":   {"owner", "admin", "doctor"},
    "plan_edit":      {"owner", "admin", "doctor"},
    "plan_delete":    {"owner"},
    "upload":         {"owner", "admin", "doctor"},
    "medical":        {"owner", "doctor"},
    "mapping":        {"owner", "admin"},
    "files_assign":   {"owner", "admin"},
    "reminders":      {"owner", "admin"},
    "send_digest":    {"owner", "admin"},
    "sync":           {"owner", "admin"},
    "sheets":         {"owner"},
    "users":          {"owner"},
    "api":            {"owner"},
    "audit":          {"owner"},
    "export_reports": {"owner", "admin", "doctor"},  # у межах своєї видимості
    "export_files":   {"owner", "admin"},
    "import":         {"owner", "admin"},
    "migrate":        {"owner"},
    "backup":         {"owner"},
    "products":       {"owner", "admin"},
    "stock":          {"owner", "admin"},
    "tasks_all":      {"owner"},
    "book":           {"owner", "admin"},            # запис у CleverBox з бюті-плану
    "message":        {"owner", "admin"},            # повідомлення пацієнту з задачі/картки
    "campaigns":      {"owner", "admin"},            # готувати розсилки, превʼю, тест
    "campaigns_send": {"owner"},                     # запускати розсилку на базу
    "scripts_edit":   {"owner"},
    "specialists":    {"owner", "admin"},            # картки спеціалістів, їх статистика
    "specialists_edit": {"owner"},
}


def can(user: User | None, perm: str) -> bool:
    return bool(user and user.active and user.role in PERMS.get(perm, set()))


# ---------------- паролі (PBKDF2, без зовнішніх залежностей) ----------------

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000)
    return f"pbkdf2_sha256$260000${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:   # noqa: BLE001
        return False


def ensure_owner(db: Session):
    """Перший запуск: створюємо власника з OWNER_LOGIN / ADMIN_PASSWORD з .env."""
    if db.scalar(select(User).limit(1)):
        return
    db.add(User(login=settings.owner_login, name=settings.owner_name, role="owner",
                password_hash=hash_password(settings.admin_password)))
    db.commit()


# ---------------- сесія веб-інтерфейсу ----------------

def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(db_dep)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    request.state.user = user
    return user


def require(perm: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not can(user, perm):
            raise HTTPException(status_code=403, detail="Недостатньо прав")
        return user
    return dep


def patient_visible(user: User, patient: Patient) -> bool:
    if can(user, "patients_all"):
        return True
    if user.role == "doctor" and user.doctor_id:
        return patient.doctor_id == user.doctor_id or any(p.doctor_id == user.doctor_id for p in patient.plans)
    return False


def plan_visible(user: User, plan: BeautyPlan) -> bool:
    if can(user, "patients_all"):
        return True
    return user.role == "doctor" and user.doctor_id is not None and plan.doctor_id == user.doctor_id


# ---------------- API-ключі ----------------

def new_api_key() -> tuple[str, str, str]:
    """-> (повний ключ для показу один раз, prefix, hash)."""
    raw = "mcrm_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hashlib.sha256(raw.encode()).hexdigest()


def api_key_dep(request: Request, x_api_key: str | None = Header(default=None),
                authorization: str | None = Header(default=None), db: Session = Depends(db_dep)) -> ApiKey:
    ip = "api:" + client_ip(request)
    if api_limiter.blocked_for(ip):
        raise HTTPException(429, "Забагато невдалих спроб з цієї адреси — спробуйте пізніше")
    raw = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    if not raw:
        raise HTTPException(401, "Потрібен API-ключ: заголовок X-API-Key або Authorization: Bearer")
    h = hashlib.sha256(raw.encode()).hexdigest()
    key = db.scalar(select(ApiKey).where(ApiKey.prefix == raw[:12], ApiKey.active.is_(True)))
    if not key or not hmac.compare_digest(key.key_hash, h):
        if api_limiter.fail(ip):
            audit(db, "api.blocked", ip=ip[4:])
        raise HTTPException(401, "Невірний або відкликаний API-ключ")
    key.last_used = datetime.utcnow()
    db.commit()
    return key


def scope(name: str):
    def dep(key: ApiKey = Depends(api_key_dep)) -> ApiKey:
        if name not in (key.scopes or []):
            raise HTTPException(403, f"Ключу бракує дозволу «{name}»")
        return key
    return dep


# ---------------- журнал ----------------

def audit(db: Session, action: str, entity: str | None = None, entity_id: int | None = None,
          user: User | None = None, key: ApiKey | None = None, **details):
    db.add(AuditLog(action=action, entity=entity, entity_id=entity_id, user_id=user.id if user else None,
                    api_key_id=key.id if key else None, details=details or None))
    db.commit()


# ---------------- захист від перебору паролів / ключів ----------------

import threading as _threading
from collections import defaultdict as _dd
from datetime import timedelta as _td


class Limiter:
    """N невдалих спроб за вікно → блок на той самий час. Окремо рахує IP і логін
    (перебір паролів до одного логіна з різних IP теж ловиться). Памʼять процесу — app працює одним процесом."""

    def __init__(self, max_attempts: int, minutes: int):
        self.max, self.win = max_attempts, _td(minutes=minutes)
        self.fails: dict[str, list[datetime]] = _dd(list)
        self.lock = _threading.Lock()

    def _clean(self, key: str, now: datetime):
        self.fails[key] = [t for t in self.fails[key] if now - t < self.win]

    def blocked_for(self, *keys: str) -> int:
        """Скільки хвилин ще діє блок (0 — не заблоковано)."""
        now = datetime.utcnow()
        with self.lock:
            left = 0
            for k in keys:
                self._clean(k, now)
                if len(self.fails[k]) >= self.max:
                    left = max(left, int((self.fails[k][-1] + self.win - now).total_seconds() // 60) + 1)
            return left

    def fail(self, *keys: str) -> bool:
        """Зареєструвати невдачу; True — якщо саме зараз спрацювало блокування."""
        now = datetime.utcnow()
        with self.lock:
            hit = False
            for k in keys:
                self._clean(k, now)
                self.fails[k].append(now)
                hit |= len(self.fails[k]) == self.max
            return hit

    def reset(self, *keys: str):
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)


login_limiter = Limiter(settings.login_max_attempts, settings.login_block_minutes)          # на логін
# на IP — поріг у 4 рази вищий: уся клініка може виходити з однієї адреси, одна помилка колеги не блокує всіх
ip_limiter = Limiter(settings.login_max_attempts * 4, settings.login_block_minutes)
api_limiter = Limiter(20, settings.login_block_minutes)       # невірні API-ключі з одного IP


def client_ip(request: Request) -> str:
    # за Caddy uvicorn запущений з --proxy-headers, тож request.client — реальна адреса клієнта
    return request.client.host if request.client else "?"
