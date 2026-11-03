"""Налаштування → Пацієнти: статуси пацієнтів (Первинний / Постійний / ФАНАТ …) і поля картки пацієнта в календарі.

Статус пацієнта визначається кількістю візитів: різних днів із завершеним візитом (історія CleverBox + записи CRM
зі статусом «Візит завершено»), для запланованого візиту — разом із ним. Пацієнт отримує найвищий статус, поріг якого
(«від N візитів») він досяг."""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import SyncState

TAGS_KEY = "patient_tags"
FIELDS_KEY = "calendar_patient_fields"
DEFAULT_TAGS = [{"name": "Первинний пацієнт", "min": 0, "color": "#E8B931"},
                {"name": "Постійний пацієнт", "min": 2, "color": "#4FC3D9"},
                {"name": "ФАНАТ", "min": 10, "color": "#7CB342"}]
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# поля картки пацієнта, які можна вивести в блок візиту в календарі: key → (назва, лише для medical)
FIELDS = {
    "phone": ("Телефон", False), "comment": ("Замітка до запису", False), "cb_note": ("Замітка пацієнта", False),
    "instagram": ("Instagram", False), "drink": ("Напій, якому надає перевагу", False),
    "communication_channel": ("Основний канал комунікації", False), "preferred_doctor": ("Улюблений лікар", False),
    "directions": ("Напрямки", False), "source": ("Джерело залучення", False), "birthday": ("День народження", False),
    "viber": ("Viber", False), "telegram_contact": ("Telegram", False), "email": ("Email", False),
    "client_groups": ("Групи пацієнта", False), "notes": ("Нотатки", False),
    "cb_anamnesis": ("Медичний анамнез", True), "med_allergies": ("Алергії", True), "med_contra": ("Протипоказання", True),
}
MODES = {"off": "Не показувати", "on": "Завжди", "hover": "При наведенні"}
DEFAULT_FIELDS = {"phone": "on", "comment": "on"}          # те, що видно зараз


def _get(db: Session, key: str):
    st = db.get(SyncState, key)
    try:
        return json.loads(st.value) if st and st.value else None
    except ValueError:
        return None


def _put(db: Session, key: str, val) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = json.dumps(val, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


# ---------------- статуси ----------------
def tags(db: Session) -> list[dict]:
    rows = _get(db, TAGS_KEY) or DEFAULT_TAGS
    out = []
    for r in rows:
        n = " ".join(str(r.get("name") or "").split())[:40]
        if n:
            try:
                mn = max(0, int(float(r.get("min") or 0)))
            except (TypeError, ValueError):
                mn = 0
            out.append({"name": n, "min": mn, "color": r.get("color") if _HEX.match(str(r.get("color") or "")) else "#8a8496"})
    out.sort(key=lambda x: x["min"])
    return out or list(DEFAULT_TAGS)


def save_tags(db: Session, rows: list[dict]) -> None:
    _put(db, TAGS_KEY, [r for r in rows if str(r.get("name") or "").strip()])


def tag_for(levels: list[dict], visits: int) -> str:
    cur = levels[0]["name"] if levels else ""
    for t in levels:
        if visits >= t["min"]:
            cur = t["name"]
    return cur


def colors(levels: list[dict]) -> dict[str, str]:
    return {t["name"]: t["color"] for t in levels}


def visit_days(db: Session, pids) -> dict[int, int]:
    """Кількість різних днів із завершеним візитом: історія CleverBox (Visit) + записи CRM «Візит завершено»."""
    from ..models import Appointment, Visit
    pids = list({p for p in pids if p})
    if not pids:
        return {}
    days: dict[int, set] = {}
    for pid, d in db.execute(select(Visit.patient_id, func.date(Visit.starts_at)).where(
            Visit.patient_id.in_(pids), Visit.status.in_(("attended", "done"))).distinct()):
        days.setdefault(pid, set()).add(str(d))
    for pid, d in db.execute(select(Appointment.patient_id, func.date(Appointment.start)).where(
            Appointment.patient_id.in_(pids), Appointment.status == "done").distinct()):
        days.setdefault(pid, set()).add(str(d))
    return {k: len(v) for k, v in days.items()}


# ---------------- поля в календарі ----------------
def fields(db: Session) -> dict[str, str]:
    saved = _get(db, FIELDS_KEY)
    base = dict(DEFAULT_FIELDS) if saved is None else {}
    for k, v in (saved or {}).items():
        if k in FIELDS and v in MODES:
            base[k] = v
    return {k: base.get(k, "off") for k in FIELDS}


def save_fields(db: Session, vals: dict) -> None:
    _put(db, FIELDS_KEY, {k: v for k, v in vals.items() if k in FIELDS and v in MODES})


def _val(p, k: str, comment: str | None):
    if k == "comment":
        return comment
    v = getattr(p, k, None)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    if hasattr(v, "strftime"):
        return v.strftime("%d.%m")
    return v


def calendar_info(db: Session, events, medical: bool) -> dict:
    """{(patient_id, appt_id): {"on": [(назва, значення)], "hover": [...]}} для блоків календаря."""
    from ..models import Patient
    cfg = {k: m for k, m in fields(db).items() if m != "off" and (medical or not FIELDS[k][1])}
    if not cfg:
        return {}
    pids = {e.patient_id for e in events if e.patient_id}
    pts = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(pids)))} if pids else {}
    out = {}
    for e in events:
        p = pts.get(e.patient_id)
        if not p:
            continue
        r = {"on": [], "hover": []}
        for k, m in cfg.items():
            v = _val(p, k, e.note)
            if v not in (None, ""):
                r[m].append((k, FIELDS[k][0], str(v)))
        out[id(e)] = r
    return out
