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
GROUPS_KEY = "patient_groups"
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


# ---------------- статуси (матриця) ----------------
CATS = {"active": "Активні", "sleep": "Сплячі", "lost": "Втрачені"}
INF = 9999
# за замовчуванням — як «Матриця статусів» CleverBox: візити (успішні), заплановані, днів від останнього візиту
DEFAULT_MATRIX = [
    {"name": "Потенційний", "color": "#E8B931", "cat": "active", "v_from": 0, "v_to": 0, "p_from": 1, "p_to": INF, "l_from": 0, "l_to": INF},
    {"name": "Первинний пацієнт", "color": "#C9B037", "cat": "active", "v_from": 1, "v_to": 1, "p_from": 0, "p_to": INF, "l_from": 0, "l_to": 179},
    {"name": "Постійний пацієнт", "color": "#4FC3D9", "cat": "active", "v_from": 2, "v_to": 9, "p_from": 0, "p_to": INF, "l_from": 0, "l_to": 179},
    {"name": "ФАНАТ", "color": "#7CB342", "cat": "active", "v_from": 10, "v_to": INF, "p_from": 0, "p_to": INF, "l_from": 0, "l_to": 179},
    {"name": "Давно не бачилися", "color": "#A0522D", "cat": "sleep", "v_from": 1, "v_to": INF, "p_from": 0, "p_to": 0, "l_from": 180, "l_to": 359},
    {"name": "Колишній", "color": "#C0392B", "cat": "lost", "v_from": 1, "v_to": INF, "p_from": 0, "p_to": 0, "l_from": 360, "l_to": INF},
]
_RANGES = (("v_from", "v_to"), ("p_from", "p_to"), ("l_from", "l_to"))


def _int(v, d):
    try:
        return max(0, min(int(float(v)), INF))
    except (TypeError, ValueError):
        return d


def _norm(r: dict) -> dict | None:
    n = " ".join(str(r.get("name") or "").split())[:40]
    if not n:
        return None
    x = {"name": n, "color": r.get("color") if _HEX.match(str(r.get("color") or "")) else "#8a8496",
         "cat": r.get("cat") if r.get("cat") in CATS else "active"}
    if "min" in r and "v_from" not in r:                        # старий формат {name, min, color}: від N візитів
        x.update(v_from=_int(r.get("min"), 0), v_to=INF, p_from=0, p_to=INF, l_from=0, l_to=INF)
    else:
        for a, b in _RANGES:
            x[a], x[b] = _int(r.get(a), 0), _int(r.get(b), INF)
            if x[b] < x[a]:
                x[b] = x[a]
    x["min"] = x["v_from"]                                      # сумісність зі старими шаблонами
    return x


def tags(db: Session) -> list[dict]:
    """Статуси в порядку пріоритету: пацієнт отримує ОСТАННІЙ у списку статус, умови якого виконані (плаваючі —
    за днями від останнього візиту: статус змінюється сам, коли пацієнт зникає й повертається)."""
    rows = _get(db, TAGS_KEY)
    if rows is None:
        rows = DEFAULT_TAGS
    out = [x for x in (_norm(r) for r in rows) if x]
    if rows and "min" in rows[0] and "v_from" not in rows[0]:   # старий формат — за порогом
        out.sort(key=lambda x: x["v_from"])
    return out or [_norm(r) for r in DEFAULT_MATRIX]


def save_tags(db: Session, rows: list[dict]) -> None:
    _put(db, TAGS_KEY, [x for x in (_norm(r) for r in rows) if x])


def reset_tags(db: Session) -> None:
    _put(db, TAGS_KEY, [dict(r) for r in DEFAULT_MATRIX])


def matches(t: dict, visits: int, planned: int | None = None, last_days: int | None = None) -> bool:
    if not (t["v_from"] <= visits <= t["v_to"]):
        return False
    if planned is not None and not (t["p_from"] <= planned <= t["p_to"]):
        return False
    if last_days is not None and not (t["l_from"] <= last_days <= t["l_to"]):
        return False
    if last_days is None and t["l_from"] > 0:                   # умова за останнім візитом, а дати немає — не підходить
        return False
    if planned is None and t["p_from"] > 0:
        return False
    return True


def tag_for(levels: list[dict], visits: int, planned: int | None = None, last_days: int | None = None) -> str:
    """Статус за візитами (+ заплановані, + днів від останнього візиту, якщо відомі). Останній відповідний у списку."""
    cur = ""
    for t in levels:
        if matches(t, visits, planned, last_days):
            cur = t["name"]
    if not cur and levels:                                      # нічого не підійшло (напр. повернувся після паузи з записом) — лише за візитами
        for t in levels:
            if matches(t, visits):
                cur = t["name"]
        cur = cur or levels[0]["name"]
    return cur


def colors(levels: list[dict]) -> dict[str, str]:
    return {t["name"]: t["color"] for t in levels}


def cat_of(levels: list[dict]) -> dict[str, str]:
    return {t["name"]: t["cat"] for t in levels}


def range_text(t: dict) -> str:
    def rng(a, b, unit):
        if a == 0 and b >= INF:
            return ""
        if b >= INF:
            return f"від {a} {unit}"
        if a == b:
            return f"{a} {unit}"
        return f"{a}–{b} {unit}"
    parts = [rng(t["v_from"], t["v_to"], "візитів"), rng(t["p_from"], t["p_to"], "запланов."), rng(t["l_from"], t["l_to"], "дн. від візиту")]
    return " · ".join(p for p in parts if p) or "без умов"


def visit_days(db: Session, pids) -> dict[int, int]:
    """Кількість різних днів із завершеним візитом: історія CleverBox (Visit) + записи CRM «Візит завершено»."""
    return {k: v["visits"] for k, v in stats(db, pids).items()}


def stats(db: Session, pids) -> dict[int, dict]:
    """pid → {visits: різних днів із завершеним візитом, planned: майбутніх записів, last_days: днів від останнього візиту | None}."""
    from datetime import date as _date, datetime as _dt
    from ..models import Appointment, Visit
    pids = list({p for p in pids if p})
    if not pids:
        return {}
    days: dict[int, set] = {}
    for pid, d in db.execute(select(Visit.patient_id, func.date(Visit.starts_at)).where(
            Visit.patient_id.in_(pids), Visit.status.in_(("attended", "done"))).distinct()):
        days.setdefault(pid, set()).add(str(d)[:10])
    for pid, d in db.execute(select(Appointment.patient_id, func.date(Appointment.start)).where(
            Appointment.patient_id.in_(pids), Appointment.status.in_(("done", "paid"))).distinct()):
        days.setdefault(pid, set()).add(str(d)[:10])
    now = _dt.now()
    planned: dict[int, int] = {}
    for pid, n in db.execute(select(Appointment.patient_id, func.count()).where(
            Appointment.patient_id.in_(pids), Appointment.start > now, Appointment.status.notin_(("cancelled", "no_show")))
            .group_by(Appointment.patient_id)):
        planned[pid] = int(n)
    today = _date.today()
    out = {}
    for pid in pids:
        ds = days.get(pid) or set()
        last = None
        if ds:
            try:
                last = (today - _date.fromisoformat(max(ds))).days
            except ValueError:
                last = None
        out[pid] = {"visits": len(ds), "planned": planned.get(pid, 0), "last_days": max(0, last) if last is not None else None}
    return out


def tag_of(db: Session, levels: list[dict], pid: int, extra_visit: int = 0) -> str:
    st = stats(db, [pid]).get(pid) or {"visits": 0, "planned": 0, "last_days": None}
    return tag_for(levels, st["visits"] + extra_visit, st["planned"], st["last_days"])


# ---------------- групи пацієнтів ----------------
def group_name(g) -> str:
    v = (g.get("name") or g.get("title") or "") if isinstance(g, dict) else g
    return " ".join(str(v or "").split())[:60]


def group_counts(db: Session) -> dict[str, int]:
    from ..models import Patient
    cnt: dict[str, int] = {}
    for gl in db.scalars(select(Patient.client_groups).where(Patient.client_groups.is_not(None))):
        for g in gl or []:
            n = group_name(g)
            if n:
                cnt[n] = cnt.get(n, 0) + 1
    return cnt


def groups(db: Session) -> list[dict]:
    """Довідник груп (Налаштування → Пацієнти). Якщо ще не збережено — з груп, що вже є в пацієнтів (CleverBox)."""
    saved = _get(db, GROUPS_KEY)
    if saved is None:
        cnt = group_counts(db)
        return [{"name": k, "color": "#5C4B82"} for k, _ in sorted(cnt.items(), key=lambda x: (-x[1], x[0].lower()))]
    out, seen = [], set()
    for r in saved:
        n = group_name(r.get("name"))
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append({"name": n, "color": r.get("color") if _HEX.match(str(r.get("color") or "")) else "#5C4B82"})
    return out


def save_groups(db: Session, rows: list[dict]) -> None:
    _put(db, GROUPS_KEY, [{"name": group_name(r.get("name")), "color": r.get("color")} for r in rows if group_name(r.get("name"))])


def rename_group(db: Session, old: str, new: str) -> int:
    from ..models import Patient
    n = 0
    for p in db.scalars(select(Patient).where(Patient.client_groups.is_not(None))):
        names = [group_name(g) for g in p.client_groups or []]
        if any(x.lower() == old.lower() for x in names):
            p.client_groups = list(dict.fromkeys(new if x.lower() == old.lower() else x for x in names if x))
            n += 1
    db.commit()
    return n


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
