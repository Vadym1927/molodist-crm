"""Основні налаштування клініки (як у CleverBox): загальні, звіти, локації з ресурсами, режимом роботи й робочими місцями.
Зберігаються одним JSON у SyncState clinic_settings; стартові значення — як у CleverBox MOLODIST."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState

KEY = "clinic_settings"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
COUNTRIES = ["Україна", "Польща", "Молдова", "Румунія", "Чехія", "Німеччина", "Інша"]
TIMEZONES = ["Europe/Kyiv", "Europe/Warsaw", "Europe/Chisinau", "Europe/Bucharest", "Europe/Prague", "Europe/Berlin"]
LANGUAGES = ["Українська", "English", "Polski"]
CURRENCIES = ["UAH", "EUR", "USD", "PLN"]

DEFAULT = {
    "general": {"country": "Україна", "timezone": "Europe/Kyiv", "language": "Українська", "currency": "UAH", "vat": False},
    "reports": {"exclude_income": ["Внесення коштів в касу"]},
    "locations": [{
        "id": 114, "name": "Molodist_", "active": True, "address": "", "phone": "",
        "hours": {str(i): {"on": True, "from": "10:00", "to": "20:00"} for i in range(7)},
        "resources": [{"name": n, "active": True} for n in
                      ("IPL", "RF", "Мікротоки", "Лазер", "Дерматоскоп", "Стратосфера", "Центрифуга", "Стратосфера 2")],
        "places": [{"name": n, "active": True} for n in
                   ("Кабінет 1 (косметолог)", "Кабінет 2 (лазер/дерматолог)", "Кабінет 3 (масаж)")],
    }],
}


def get(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    data = copy.deepcopy(DEFAULT)
    if st and st.value:
        try:
            saved = json.loads(st.value)
            for k, v in saved.items():
                data[k] = {**data[k], **v} if isinstance(data.get(k), dict) and isinstance(v, dict) else v
        except ValueError:
            pass
    return data


def save(db: Session, data: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(data, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()
    apply(data)


def apply(data: dict) -> None:
    """Налаштування, які діють на весь процес (один процес app): часовий пояс."""
    tz = (data.get("general") or {}).get("timezone")
    if tz:
        settings.timezone = tz


def location(data: dict, lid: int) -> dict | None:
    return next((x for x in data["locations"] if int(x.get("id", 0)) == int(lid)), None)


def hours_for(db: Session, day: date) -> tuple[int, int] | None:
    """Години роботи першої активної локації в цей день: (з, до) — цілі години для сітки календаря; None — вихідний."""
    data = get(db)
    loc = next((x for x in data["locations"] if x.get("active")), None)
    if not loc:
        return None
    h = (loc.get("hours") or {}).get(str(day.weekday())) or {}
    if not h.get("on"):
        return None
    try:
        f, t = int(h.get("from", "10:00")[:2]), int(h.get("to", "20:00")[:2])
        t += 1 if h.get("to", "20:00")[3:5] not in ("", "00") else 0
        return f, t
    except ValueError:
        return None


def excluded_income(db: Session) -> set[str]:
    return {x.strip().lower() for x in (get(db).get("reports") or {}).get("exclude_income") or [] if x.strip()}
