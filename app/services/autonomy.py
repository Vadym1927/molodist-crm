"""Режими роботи CRM.

• Автономний режим — CRM працює без CleverBox: жодних запитів до API CleverBox (синк, вебхуки, запис), облік,
  історія й звітність ведуться в CRM з дати «since». Дані CleverBox (чеки, візити) у звітах — лише ДО цієї дати;
  все, що раніше, довантажується звітами CleverBox або вручну.
• Тестовий режим — фіскалізацію відв'язано (чеки не йдуть у Checkbox), оплата карткою не чекає термінала —
  гроші не реальні. Такі чеки позначаються «ТЕСТ» (Sale.fiscal.test_sale) і їх видно окремо.
Налаштування — SyncState "work_mode" (JSON)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import SessionLocal, SyncState

KEY = "work_mode"
DEFAULT = {"autonomous": False, "since": None, "test": False, "at": None}
_CACHE: dict = {"cfg": None}          # один процес app — режим тримається в пам'яті (оновлюється при збереженні / читанні з БД)


def _read(s: Session) -> dict:
    st = s.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {**DEFAULT, **(d if isinstance(d, dict) else {})}


def cfg(db: Session | None = None) -> dict:
    """З db — читає з БД (і оновлює кеш); без db — з кешу (без окремої сесії посеред чужої транзакції)."""
    if db is not None:
        _CACHE["cfg"] = _read(db)
        return dict(_CACHE["cfg"])
    if _CACHE["cfg"] is None:
        with SessionLocal() as s:
            _CACHE["cfg"] = _read(s)
    return dict(_CACHE["cfg"])


def save(db: Session, **kw) -> dict:
    d = {**cfg(db), **kw, "at": datetime.now().isoformat(timespec="seconds")}
    if d.get("autonomous") and not d.get("since"):
        d["since"] = date.today().isoformat()
    db.merge(SyncState(key=KEY, value=json.dumps(d, ensure_ascii=False)))
    db.commit()
    _CACHE["cfg"] = dict(d)
    return d


def offline(db: Session | None = None) -> bool:
    """CleverBox вимкнено (автономний режим)."""
    try:
        return bool(cfg(db).get("autonomous"))
    except Exception:  # noqa: BLE001 — немає таблиці на старті
        return False


def test_mode(db: Session | None = None) -> bool:
    try:
        return bool(cfg(db).get("test"))
    except Exception:  # noqa: BLE001
        return False


def since(db: Session | None = None) -> date | None:
    d = cfg(db)
    if not d.get("autonomous") or not d.get("since"):
        return None
    try:
        return date.fromisoformat(d["since"])
    except ValueError:
        return None


def cb_until(db: Session, b: datetime) -> datetime:
    """Верхня межа для даних CleverBox: в автономному режимі — не пізніше дати переходу на CRM."""
    s = since(db)
    if not s:
        return b
    lim = datetime.combine(s, datetime.min.time())
    return min(b, lim - timedelta(microseconds=1))
