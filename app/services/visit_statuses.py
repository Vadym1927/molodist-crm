"""Статуси візитів (Налаштування → Клініка → Статуси візитів): назва, колір у календарі, увімкнено чи ні.
Системні статуси мають фіксовані ключі (логіка CRM спирається на них), назву й колір можна змінити.
«Оплачено ₴» — статус запису після оплати в касі CRM (чек); «Візит завершено» адміністратор ставить вручну.
Якщо «Оплачено ₴» вимкнено — оплата одразу завершує візит (як раніше).
Власні статуси (ключ c1, c2…) поводяться як активний запис (як «Записаний»)."""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "visit_statuses"
# key, назва, колір, системний, можна вимкнути
BUILTIN = [
    ("booked", "Записаний", "#F5B940", False),
    ("confirmed", "Підтверджено", "#9B4DE0", True),
    ("arrived", "Прийшов", "#3B8FD9", True),
    ("paid", "Оплачено ₴", "#2E9E5B", True),
    ("done", "Візит завершено", "#4CAF50", False),
    ("no_show", "Не прийшов", "#C0392B", True),
    ("cancelled", "Скасовано", "#9E9AA8", False),
]
SYSTEM = {k for k, *_ in BUILTIN}
PSEUDO: set = set()                       # (раніше «paid» був лише кольором; тепер — справжній статус запису)
CANCEL = {"cancelled", "no_show"}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _raw(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def all_(db: Session) -> list[dict]:
    """Усі статуси в порядку показу: [{key, label, color, on, system, can_off, cal}]."""
    raw = _raw(db)
    saved = {x.get("key"): x for x in raw.get("items", []) if x.get("key")}
    out = []
    for k, lb, col, can_off in BUILTIN:
        s = saved.get(k, {})
        out.append({"key": k, "label": (s.get("label") or lb), "color": s.get("color") if _HEX.match(str(s.get("color") or "")) else col,
                    "on": bool(s.get("on", True)) if can_off else True, "system": True, "can_off": can_off,
                    "cal": bool(s.get("cal", False)) if k == "cancelled" else True, "default": lb})
    for s in raw.get("items", []):
        if s.get("key") in SYSTEM or not re.match(r"^c\d{1,4}$", str(s.get("key") or "")):
            continue
        out.append({"key": s["key"], "label": s.get("label") or s["key"], "color": s.get("color") if _HEX.match(str(s.get("color") or "")) else "#8a8496",
                    "on": bool(s.get("on", True)), "system": False, "can_off": True, "cal": True, "default": ""})
    order = {k: i for i, k in enumerate(raw.get("order") or [])}
    out.sort(key=lambda x: order.get(x["key"], 1000 + [b[0] for b in BUILTIN].index(x["key"]) if x["key"] in SYSTEM else 2000))
    return out


def save(db: Session, items: list[dict]) -> None:
    clean, order = [], []
    n = max([int(x["key"][1:]) for x in all_(db) if not x["system"]] + [0])
    for it in items:
        k = str(it.get("key") or "").strip()
        lb = str(it.get("label") or "").strip()[:40]
        if not k:
            if not lb:
                continue
            n += 1
            k = f"c{n}"
        if k not in SYSTEM and not re.match(r"^c\d{1,4}$", k):
            continue
        col = str(it.get("color") or "")
        clean.append({"key": k, "label": lb, "color": col if _HEX.match(col) else None,
                      "on": bool(it.get("on")), "cal": bool(it.get("cal"))})
        order.append(k)
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value = json.dumps({"items": clean, "order": order}, ensure_ascii=False)
    st.updated_at = datetime.utcnow()
    db.merge(st)
    db.commit()


def labels(db: Session, only_on: bool = False, real: bool = True) -> dict[str, str]:
    """key → назва (real=True — без «Оплачено», тобто значення, які можна поставити запису)."""
    return {x["key"]: x["label"] for x in all_(db) if (not real or x["key"] not in PSEUDO) and (x["on"] or not only_on)}


def paid_on(db: Session) -> bool:
    """Чи вмикнено статус «Оплачено ₴»: тоді оплата в касі ставить «Оплачено», а «Завершено» — вручну."""
    return next((x["on"] for x in all_(db) if x["key"] == "paid"), False)


def after_payment(db: Session, current: str | None) -> str:
    """Статус запису після оплати чека: завершений лишається завершеним; інакше «Оплачено» (або «Завершено», якщо «Оплачено» вимкнено)."""
    if current == "done":
        return "done"
    return "paid" if paid_on(db) else "done"


def colors(db: Session) -> dict[str, str]:
    return {x["key"]: x["color"] for x in all_(db)}


def valid(db: Session, status: str) -> bool:
    return status in labels(db)


def css(db: Session) -> str:
    """Правила .s-KEY для блоків календаря: смуга — колір статусу, фон — світлий відтінок."""
    return "".join(f".s-{x['key']}{{--bc:{x['color']};--bg:color-mix(in srgb,{x['color']} 16%,#fff);--c:{x['color']}}}" for x in all_(db))


def show_cancelled(db: Session) -> bool:
    return next((x["cal"] for x in all_(db) if x["key"] == "cancelled"), False)


def display(db: Session, status: str | None, paid: bool) -> str:
    """Ключ кольору візиту в календарі: оплачений (і не скасований) — «Оплачено ₴», якщо цей статус увімкнено."""
    st = status or "booked"
    if st in ("done", "paid"):
        return st
    if paid and st not in CANCEL and next((x["on"] for x in all_(db) if x["key"] == "paid"), False):
        return "paid"
    return st
