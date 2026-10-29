"""Права доступу: ролі (вбудовані owner / admin / doctor + свої), набір дозволів кожної. Зберігається в SyncState roles_cfg
й застосовується до PERMS / ROLES у памʼяті процесу (один процес app)."""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ROLES, SyncState, User
from ..security import BASE_PERMS, PERMS

KEY = "roles_cfg"
BUILTIN = {"owner": "Власник", "admin": "Адміністратор", "doctor": "Лікар"}
BASE_ROLES = dict(BUILTIN)
GROUPS = [
    ("Пацієнти", [("patients_all", "Бачити всіх пацієнтів (без цього — лише своїх)"), ("patient_edit", "Редагувати картку пацієнта"),
                  ("medical", "Медична картка й медичні нотатки"), ("upload", "Завантажувати плани й фото"),
                  ("files_assign", "Розбирати файли без пацієнта"), ("message", "Писати пацієнтам (повідомлення)"),
                  ("loyalty_patient", "Бонуси й персональна знижка пацієнта")]),
    ("Звернення й запис", [("appeals", "Звернення (заявки)"), ("book", "Записувати пацієнтів (CRM і CleverBox)"),
                           ("reminders", "Нагадування"), ("send_digest", "Надсилати зведення")]),
    ("Бюті-плани", [("plan_edit", "Редагувати плани"), ("plan_delete", "Видаляти плани"), ("mapping", "Зіставлення послуг")]),
    ("Каса й фінанси", [("sales", "Каса: продаж, чеки"), ("sale_refund", "Повернення чека"),
                        ("finance", "Фінанси, звіти, зарплата, ключі інтеграцій"), ("kpi_own", "Свій KPI")]),
    ("Склад і товари", [("products", "Продукти"), ("stock", "Склад: поставки, списання, постачальники")]),
    ("Команда", [("specialists", "Команда (картки спеціалістів)"), ("specialists_edit", "Додавати / змінювати спеціалістів"),
                 ("schedule", "Змінювати графік роботи"), ("tasks_all", "Усі задачі (не лише свої)")]),
    ("Комунікації", [("campaigns", "Готувати розсилки"), ("campaigns_send", "Запускати розсилки на базу"), ("scripts_edit", "Скрипти адмінів")]),
    ("Дані", [("sync", "Синхронізація з CleverBox"), ("import", "Імпорт"), ("export_reports", "Експорт звітів"),
              ("export_files", "Експорт файлів"), ("sheets", "Google Sheets"), ("migrate", "Перенос даних"), ("backup", "Резервні копії")]),
    ("Адміністрування", [("users", "Користувачі, права доступу, налаштування"), ("api", "REST API: ключі й вебхуки"), ("audit", "Журнал дій")]),
]
ALL_PERMS = [p for _g, items in GROUPS for p, _l in items]


def _cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def roles(db: Session) -> list[dict]:
    """[{key, name, builtin, hidden, perms}] — owner має все й не змінюється."""
    cfg = _cfg(db)
    out = []
    for k, name in BUILTIN.items():
        c = (cfg.get("roles") or {}).get(k, {})
        perms = set(ALL_PERMS) if k == "owner" else set(c.get("perms") if "perms" in c else [p for p, rs in BASE_PERMS.items() if k in rs])
        out.append({"key": k, "name": c.get("name") or name, "builtin": True, "hidden": bool(c.get("hidden")), "perms": perms})
    for k, c in (cfg.get("roles") or {}).items():
        if k in BUILTIN:
            continue
        out.append({"key": k, "name": c.get("name") or k, "builtin": False, "hidden": bool(c.get("hidden")), "perms": set(c.get("perms") or []),
                    "base": c.get("base") or "admin"})
    return out


def apply(db: Session) -> None:
    rs = roles(db)
    for p in set(PERMS) | set(ALL_PERMS):
        PERMS[p] = {"owner"} | {r["key"] for r in rs if p in r["perms"]}
    ROLES.clear()
    ROLES.update({r["key"]: r["name"] for r in rs})


def save_role(db: Session, key: str | None, name: str, perms: list[str], hidden: bool = False, base: str = "admin") -> str:
    cfg = _cfg(db)
    rs = cfg.setdefault("roles", {})
    if not key:
        base_key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "role"
        key = f"r_{base_key[:20]}"
        i = 2
        while key in rs or key in BUILTIN:
            key = f"r_{base_key[:18]}{i}"
            i += 1
    if key == "owner":
        rs["owner"] = {"name": name or BUILTIN["owner"]}
    else:
        rs[key] = {"name": name.strip()[:60] or key, "perms": [p for p in perms if p in ALL_PERMS], "hidden": hidden, "base": base}
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(cfg, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()
    apply(db)
    return key


def reset_role(db: Session, key: str) -> None:
    cfg = _cfg(db)
    if key in BUILTIN and key in (cfg.get("roles") or {}):
        cfg["roles"].pop(key)
        st = db.get(SyncState, KEY)
        st.value = json.dumps(cfg, ensure_ascii=False)
        db.commit()
        apply(db)


def delete_role(db: Session, key: str) -> str | None:
    if key in BUILTIN:
        return "Вбудовану роль не видаляють — її можна приховати або змінити права"
    if db.scalar(select(User.id).where(User.role == key, User.active.is_(True)).limit(1)):
        return "Роль має активних користувачів — спершу змініть їм роль"
    cfg = _cfg(db)
    (cfg.get("roles") or {}).pop(key, None)
    st = db.get(SyncState, KEY)
    if st:
        st.value = json.dumps(cfg, ensure_ascii=False)
        db.commit()
    apply(db)
    return None


def users_by_role(db: Session) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in db.scalars(select(User).where(User.active.is_(True))):
        out[u.role] = out.get(u.role, 0) + 1
    return out
