"""Звіти блоку «Співробітники»: завантаження спеціалістів (графік ↔ записи), KPI адміністраторів
(дзвінки, звернення, чати, записи, каса, задачі), звіт по задачах, скасування й неявки."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appeal, Appointment, AuditLog, Call, ChatMessage, Doctor, Patient, Sale, StaffShift, Task, User
from .reports import Col, Report, _rng

WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
LOST = ("cancelled", "no_show")
REPLY_MAX_MIN = 12 * 60          # відповідь у чаті пізніше, ніж за 12 год, — це вже нова розмова, а не швидкість реакції


def _utc(a: datetime, b: datetime):
    """Межі періоду (київський час) → UTC: created_at записів, звернень, задач і журнал зберігаються в UTC."""
    z = ZoneInfo(settings.timezone)
    f = lambda v: v.replace(tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None)
    return f(a), f(b)


def _loc(v: datetime | None) -> datetime | None:
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


def _hm(s: str | None) -> float:
    try:
        h, m = (s or "").split(":")[:2]
        return int(h) + int(m) / 60
    except ValueError:
        return 0.0


def _pct(a: float, b: float) -> float | None:
    return round(100 * a / b, 1) if b else None


def _price(ap: Appointment) -> float:
    return sum(float(s.get("price") or 0) for s in (ap.services or []) if isinstance(s, dict))


def _doctors(db: Session) -> dict[int, Doctor]:
    return {d.id: d for d in db.scalars(select(Doctor))}


def _users(db: Session) -> dict[int, User]:
    return {u.id: u for u in db.scalars(select(User))}


# ---------------- завантаження спеціалістів ----------------

def load(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    a, b = _rng(d_from, d_to)
    view = view if view in ("wd", "hours") else ""
    docs = _doctors(db)
    shifts = list(db.scalars(select(StaffShift).where(StaffShift.day >= d_from, StaffShift.day <= d_to)))
    appts = list(db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b,
                                                      Appointment.status.notin_(LOST), Appointment.doctor_id.is_not(None))))
    r = Report("load", "Завантаження спеціалістів",
               "Години за графіком («Спеціалісти → Графік») проти годин у записах (без скасованих і неявок). "
               "Завантаження = зайняті години ÷ години графіка. Якщо графік не заповнений — завантаження не рахується, "
               "а записи показані як «поза графіком».",
               empty="За період немає ні змін у графіку, ні записів.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За спеціалістами"), ("wd", "За днями тижня"), ("hours", "За годинами дня")]}]

    def dur(ap):
        return max(0.0, ((ap.end or ap.start) - ap.start).total_seconds() / 3600)

    sh_total = sum(max(0.0, _hm(s.end) - _hm(s.start)) for s in shifts)
    shift_keys = {(s.doctor_id, s.day) for s in shifts}
    busy_in = sum(dur(x) for x in appts if (x.doctor_id, x.start.date()) in shift_keys)
    busy_out = sum(dur(x) for x in appts if (x.doctor_id, x.start.date()) not in shift_keys)
    r.tiles = [("Завантаження", _pct(busy_in, sh_total) or 0, None, "pct", "зайняті години ÷ графік"),
               ("Години за графіком", round(sh_total, 1), None, "num", f"{len(shift_keys)} змін"),
               ("Зайнято годин", round(busy_in, 1), None, "num", f"{len(appts)} записів"),
               ("Вільно годин", round(max(0.0, sh_total - busy_in), 1), None, "num",
                f"поза графіком: {round(busy_out, 1)} год" if busy_out else "")]
    if view == "hours":
        H = defaultdict(lambda: {"sh": 0.0, "busy": 0.0})
        for s in shifts:
            x, y = _hm(s.start), _hm(s.end)
            for h in range(int(x), int(y + 0.999)):
                H[h]["sh"] += max(0.0, min(y, h + 1) - max(x, h))
        for ap in appts:
            t, e = ap.start, ap.end or ap.start
            while t < e:
                nxt = min(e, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                H[t.hour]["busy"] += (nxt - t).total_seconds() / 3600
                t = nxt
        r.cols = [Col("name", "Година"), Col("sh", "Годин графіка", "num", True), Col("busy", "Зайнято", "num", True),
                  Col("load", "Завантаження", "pct"), Col("free", "Вільно", "num", True)]
        for h in sorted(H):
            g = H[h]
            r.rows.append({"name": f"{h:02d}:00–{h + 1:02d}:00", "sh": round(g["sh"], 1), "busy": round(g["busy"], 1),
                           "load": _pct(g["busy"], g["sh"]), "free": round(max(0.0, g["sh"] - g["busy"]), 1)})
        r.chart = [(f"{h:02d}", round(H[h]["busy"], 1)) for h in sorted(H)]
        r.chart_title = "Зайняті години за часом доби"
    else:
        G = defaultdict(lambda: {"sh": 0.0, "busy": 0.0, "out": 0.0, "days": set(), "n": 0, "sum": 0.0})
        key = (lambda did, day: day.weekday()) if view == "wd" else (lambda did, day: did)
        for s in shifts:
            g = G[key(s.doctor_id, s.day)]
            g["sh"] += max(0.0, _hm(s.end) - _hm(s.start))
            g["days"].add((s.doctor_id, s.day))
        for ap in appts:
            g = G[key(ap.doctor_id, ap.start.date())]
            g["busy" if (ap.doctor_id, ap.start.date()) in shift_keys else "out"] += dur(ap)
            g["n"] += 1
            g["sum"] += _price(ap)
        r.cols = [Col("name", "День тижня" if view == "wd" else "Спеціаліст"), Col("days", "Змін", "int", True),
                  Col("sh", "Годин графіка", "num", True), Col("busy", "Зайнято", "num", True), Col("load", "Завантаження", "pct"),
                  Col("free", "Вільно", "num", True), Col("out", "Поза графіком", "num", True), Col("n", "Записів", "int", True),
                  Col("sum", "Сума записів", "uah", True), Col("per_h", "₴ за год графіка", "uah")]
        for k, g in G.items():
            if view != "wd" and k in docs and docs[k].active is False and not g["n"]:
                continue
            r.rows.append({"name": WD[k] if view == "wd" else (docs[k].name if k in docs else f"#{k}"), "_k": k,
                           "days": len(g["days"]), "sh": round(g["sh"], 1), "busy": round(g["busy"], 1),
                           "load": _pct(g["busy"], g["sh"]), "free": round(max(0.0, g["sh"] - g["busy"]), 1),
                           "out": round(g["out"], 1), "n": g["n"], "sum": round(g["sum"]),
                           "per_h": round(g["sum"] / g["sh"]) if g["sh"] else None,
                           "_href": None if view == "wd" else f"/reports/doctors-kpi?doctor={k}"})
        r.rows.sort(key=(lambda x: x["_k"]) if view == "wd" else (lambda x: -(x["load"] or 0)))
        r.chart = [(x["name"], x["load"] or 0) for x in r.rows]
        r.chart_title = "Завантаження, %"
    r.total = {c.key: round(sum(x.get(c.key) or 0 for x in r.rows), 1) for c in r.cols if c.total}
    return r


# ---------------- KPI адміністраторів ----------------

def _reply_times(msgs: list) -> tuple[list[float], dict[int, list[float]], int]:
    """Хвилини від першого повідомлення пацієнта до нашої відповіді. Повертає (усі, за співробітником, без відповіді)."""
    th = defaultdict(list)
    for m in msgs:
        th[(m.channel, m.thread)].append(m)
    all_, by, unanswered = [], defaultdict(list), 0
    for ms in th.values():
        ms.sort(key=lambda m: m.at)
        wait = None
        for m in ms:
            if m.direction == "in":
                wait = wait or m.at
            elif wait and (m.status or "sent") == "sent":
                mins = (m.at - wait).total_seconds() / 60
                if mins <= REPLY_MAX_MIN:
                    all_.append(mins)
                    if m.user_id:
                        by[m.user_id].append(mins)
                wait = None
        if wait:
            unanswered += 1
    return all_, by, unanswered


def admin_kpi(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from .appeals import dirs, status_type
    a, b = _rng(d_from, d_to)
    ua, ub = _utc(a, b)
    users = _users(db)
    G = defaultdict(lambda: defaultdict(float))
    calls = list(db.scalars(select(Call).where(Call.at >= a, Call.at <= b)))
    scores = defaultdict(list)
    c_in = [c for c in calls if c.direction == "in"]
    missed = [c for c in c_in if (c.status or "") != "answered"]
    back = 0                                      # пропущені, яким передзвонили (вихідний на той самий номер пізніше)
    outs = defaultdict(list)
    for c in calls:
        if c.direction == "out" and c.phone:
            outs[c.phone[-9:]].append(c.at)
    for c in missed:
        if c.phone and any(t > c.at for t in outs.get(c.phone[-9:], ())):
            back += 1
    for c in calls:
        if not c.user_id or (c.status or "") != "answered":
            continue
        g = G[c.user_id]
        g["c_in" if c.direction == "in" else "c_out"] += 1
        g["talk"] += (c.duration or 0) / 60
        if c.ai_score is not None:
            scores[c.user_id].append(float(c.ai_score))
    d = dirs(db)
    appeals = list(db.scalars(select(Appeal).where(Appeal.created_at >= ua, Appeal.created_at <= ub, Appeal.deleted_at.is_(None))))
    ok_all = 0
    for ap in appeals:
        t = status_type(d, ap.status)
        ok_all += t == "success"
        if ap.responsible_id:
            g = G[ap.responsible_id]
            g["ap"] += 1
            g["ap_ok"] += t == "success"
            g["ap_fail"] += t == "fail"
    for x in db.scalars(select(Appointment).where(Appointment.created_at >= ua, Appointment.created_at <= ub,
                                                  Appointment.created_by.is_not(None))):
        G[x.created_by]["rec"] += 1
        G[x.created_by]["rec_sum"] += _price(x)
    for lg in db.scalars(select(AuditLog).where(AuditLog.at >= ua, AuditLog.at <= ub, AuditLog.action == "appointment.status",
                                                AuditLog.user_id.is_not(None))):
        if (lg.details or {}).get("status") == "confirmed":
            G[lg.user_id]["conf"] += 1
    for s in db.scalars(select(Sale).where(Sale.at >= a, Sale.at <= b, Sale.status == "paid", Sale.created_by.is_not(None))):
        G[s.created_by]["sales"] += 1
        G[s.created_by]["sales_sum"] += float(s.total or 0)
    msgs = list(db.scalars(select(ChatMessage).where(ChatMessage.at >= ua, ChatMessage.at <= ub)))
    rt_all, rt_by, unanswered = _reply_times(msgs)
    for m in msgs:
        if m.direction == "out" and m.user_id and (m.status or "sent") == "sent":
            G[m.user_id]["chat"] += 1
    for t in db.scalars(select(Task).where(Task.done_at >= ua, Task.done_at <= ub, Task.status == "done")):
        if t.assignee_id:
            G[t.assignee_id]["tasks"] += 1
    r = Report("admin_kpi", "KPI адміністраторів",
               "Хто що зробив за період: прийняті й вихідні дзвінки (за привʼязкою телефонії до користувача CRM), звернення, де "
               "співробітник відповідальний, і скільки з них дійшло до запису; записи, створені в календарі; підтверджені записи; "
               "чеки в касі; відповіді в Direct / Messenger і час до відповіді; закриті задачі. Пропущені дзвінки нікому "
               "не приписуються — вони в плитці зверху. Боти й автоматичні записи тут не рахуються.",
               empty="За період дій адміністраторів не знайдено.")
    r.tiles = [("Пропущені дзвінки", _pct(len(missed), len(c_in)) or 0, None, "pct",
                f"{len(missed)} з {len(c_in)} вхідних · передзвонили {back}"),
               ("Звернення → запис", _pct(ok_all, len(appeals)) or 0, None, "pct", f"{ok_all} з {len(appeals)} звернень"),
               ("Відповідь у чаті", round(median(rt_all)) if rt_all else 0, None, "int",
                f"хв, медіана · {len(rt_all)} відповідей · без відповіді {unanswered}"),
               ("Записів створено", int(sum(g["rec"] for g in G.values())), None, "int",
                f"підтверджено {int(sum(g['conf'] for g in G.values()))}")]
    r.cols = [Col("name", "Співробітник"), Col("c_in", "Прийнято дзвінків", "int", True), Col("c_out", "Вихідних", "int", True),
              Col("talk", "Хв розмов", "int", True), Col("score", "Оцінка дзвінків", "num"),
              Col("ap", "Звернень", "int", True), Col("ap_ok", "Записано", "int", True), Col("conv", "Конверсія", "pct"),
              Col("rec", "Створено записів", "int", True), Col("conf", "Підтверджено", "int", True),
              Col("sales", "Чеків", "int", True), Col("sales_sum", "Сума чеків", "uah", True),
              Col("chat", "Відповідей у чаті", "int", True), Col("rt", "Відповідь, хв", "int"),
              Col("tasks", "Задач закрито", "int", True)]
    for uid, g in G.items():
        u = users.get(uid)
        if not u or u.role == "doctor":
            continue
        sc, rt = scores.get(uid), rt_by.get(uid)
        r.rows.append({"name": u.name, "c_in": int(g["c_in"]), "c_out": int(g["c_out"]), "talk": round(g["talk"]),
                       "score": round(sum(sc) / len(sc), 1) if sc else None, "ap": int(g["ap"]), "ap_ok": int(g["ap_ok"]),
                       "conv": _pct(g["ap_ok"], g["ap"]), "rec": int(g["rec"]), "conf": int(g["conf"]), "sales": int(g["sales"]),
                       "sales_sum": round(g["sales_sum"]), "chat": int(g["chat"]), "rt": round(median(rt)) if rt else None,
                       "tasks": int(g["tasks"])})
    r.rows.sort(key=lambda x: -(x["c_in"] + x["c_out"] + x["rec"] + x["sales"] + x["chat"]))
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    return r


# ---------------- задачі ----------------

def tasks(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from .tasks import KINDS
    a, b = _rng(d_from, d_to)
    ua, ub = _utc(a, b)
    view = view if view in ("kinds", "overdue") else ""
    users = _users(db)
    today = date.today()
    made = list(db.scalars(select(Task).where(Task.created_at >= ua, Task.created_at <= ub)))
    done = list(db.scalars(select(Task).where(Task.done_at >= ua, Task.done_at <= ub, Task.status == "done")))
    overdue = list(db.scalars(select(Task).where(Task.status == "open", Task.due_date.is_not(None), Task.due_date < today)))
    opened = db.scalars(select(Task).where(Task.status == "open")).all()

    def late(t):
        return bool(t.due_date and t.done_at and _loc(t.done_at).date() > t.due_date)

    def who(t):
        if t.assignee_id:
            return users[t.assignee_id].name if t.assignee_id in users else f"#{t.assignee_id}"
        return {"owner": "Черга власника", "admin": "Черга адміністраторів"}.get(t.assignee_role or "admin", "Без виконавця")

    hrs = [(t.done_at - t.created_at).total_seconds() / 3600 for t in done if t.created_at]
    r = Report("tasks", "Звіт по задачах",
               "Створені й закриті за період задачі; «Зараз відкрито» і «Прострочено» — стан на сьогодні, незалежно від періоду. "
               "«Вчасно» — закрито не пізніше терміну (задачі без терміну вважаються вчасними).",
               empty="За період задач не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За виконавцями"), ("kinds", "За типами"), ("overdue", "Прострочені зараз")]}]
    r.tiles = [("Створено", len(made), None, "int", ""),
               ("Закрито", len(done), None, "int", f"вчасно {_pct(sum(not late(t) for t in done), len(done)) or 0}%"),
               ("Прострочено зараз", len(overdue), None, "int", f"з {len(opened)} відкритих"),
               ("До закриття", round(median(hrs), 1) if hrs else 0, None, "num", "годин, медіана")]
    if view == "overdue":
        pn = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_({t.patient_id for t in overdue if t.patient_id})))}
        r.cols = [Col("name", "Задача"), Col("kind", "Тип"), Col("who", "Виконавець"), Col("patient", "Пацієнт"),
                  Col("due", "Термін", "date"), Col("days", "Днів прострочки", "int")]
        for t in sorted(overdue, key=lambda t: t.due_date):
            r.rows.append({"name": t.title, "kind": KINDS.get(t.kind, t.kind), "who": who(t), "patient": pn.get(t.patient_id, ""),
                           "due": t.due_date, "days": (today - t.due_date).days, "_href": f"/tasks?open={t.id}"})
        r.empty = "Прострочених задач немає."
        return r
    key = (lambda t: KINDS.get(t.kind, t.kind)) if view == "kinds" else who
    G = defaultdict(lambda: defaultdict(float))
    H = defaultdict(list)
    for t in made:
        G[key(t)]["made"] += 1
    for t in done:
        g = G[key(t)]
        g["done"] += 1
        g["late"] += late(t)
        if t.created_at:
            H[key(t)].append((t.done_at - t.created_at).total_seconds() / 3600)
    for t in opened:
        G[key(t)]["open"] += 1
    for t in overdue:
        G[key(t)]["over"] += 1
    r.cols = [Col("name", "Тип задачі" if view == "kinds" else "Виконавець"), Col("made", "Створено", "int", True),
              Col("done", "Закрито", "int", True), Col("ontime", "Вчасно", "pct"), Col("late", "Із запізненням", "int", True),
              Col("hours", "Годин до закриття", "num"), Col("open", "Зараз відкрито", "int", True),
              Col("over", "Прострочено", "int", True)]
    for k, g in G.items():
        r.rows.append({"name": k, "made": int(g["made"]), "done": int(g["done"]), "ontime": _pct(g["done"] - g["late"], g["done"]),
                       "late": int(g["late"]), "hours": round(median(H[k]), 1) if H[k] else None, "open": int(g["open"]),
                       "over": int(g["over"])})
    r.rows.sort(key=lambda x: -(x["done"] + x["open"]))
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    return r


# ---------------- скасування й неявки ----------------

def cancels(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    a, b = _rng(d_from, d_to)
    view = view if view in ("reasons", "list", "wd") else ""
    docs = _doctors(db)
    appts = list(db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b)))
    lost = [x for x in appts if x.status in LOST]
    nxt: dict[int, datetime] = {}                 # чи пацієнт після зриву має новий запис
    pids = {x.patient_id for x in lost}
    if pids:
        for x in db.scalars(select(Appointment).where(Appointment.patient_id.in_(pids), Appointment.start > a,
                                                      Appointment.status.notin_(LOST)).order_by(Appointment.start)):
            nxt[x.patient_id] = x.start

    def rebooked(x):
        return x.patient_id in nxt and nxt[x.patient_id] > x.start

    n_c = sum(x.status == "cancelled" for x in lost)
    n_n = len(lost) - n_c
    r = Report("cancels", "Скасування й неявки",
               "Записи з датою візиту в періоді. «Втрачено» — сума послуг у скасованих записах і неявках за цінами запису. "
               "«Перезаписались» — пацієнт після зриву має пізніший дійсний запис або візит.",
               empty="За період скасувань і неявок не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За спеціалістами"), ("reasons", "За причинами"), ("wd", "За днями тижня"), ("list", "Список записів")]}]
    r.tiles = [("Зірвано записів", _pct(len(lost), len(appts)) or 0, None, "pct", f"{len(lost)} з {len(appts)}"),
               ("Скасовано", n_c, None, "int", f"{_pct(n_c, len(appts)) or 0}% записів"),
               ("Неявки", n_n, None, "int", f"{_pct(n_n, len(appts)) or 0}% записів"),
               ("Втрачено", round(sum(_price(x) for x in lost)), None, "uah",
                f"перезаписались {sum(rebooked(x) for x in lost)} з {len(lost)}")]
    if view == "list":
        pn = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_(pids)))} if pids else {}
        r.cols = [Col("name", "Пацієнт"), Col("at", "Дата", "date"), Col("doc", "Спеціаліст"), Col("st", "Статус"),
                  Col("reason", "Причина"), Col("svc", "Послуги"), Col("sum", "Сума", "uah", True), Col("re", "Новий запис", "date")]
        for x in sorted(lost, key=lambda x: x.start, reverse=True):
            r.rows.append({"name": pn.get(x.patient_id, f"#{x.patient_id}"), "at": x.start, "doc": docs[x.doctor_id].name if x.doctor_id in docs else "—",
                           "st": "Неявка" if x.status == "no_show" else "Скасовано", "reason": x.cancel_reason or "",
                           "svc": ", ".join(s.get("name") or "" for s in (x.services or []) if isinstance(s, dict))[:120],
                           "sum": round(_price(x)), "re": nxt[x.patient_id] if rebooked(x) else None,
                           "_href": f"/patients/{x.patient_id}"})
        r.total = {"sum": sum(x["sum"] for x in r.rows)}
        return r
    if view == "reasons":
        key = lambda x: ("Неявка" if x.status == "no_show" else (x.cancel_reason or "").strip() or "Скасовано без причини")
    elif view == "wd":
        key = lambda x: x.start.weekday()
    else:
        key = lambda x: x.doctor_id
    G = defaultdict(lambda: defaultdict(float))
    for x in appts:
        if view != "reasons":
            G[key(x)]["all"] += 1
    for x in lost:
        g = G[key(x)]
        g["c" if x.status == "cancelled" else "n"] += 1
        g["sum"] += _price(x)
        g["re"] += rebooked(x)
    r.cols = [Col("name", {"reasons": "Причина", "wd": "День тижня"}.get(view, "Спеціаліст"))] + \
             ([] if view == "reasons" else [Col("all", "Записів", "int", True)]) + \
             [Col("c", "Скасовано", "int", True), Col("n", "Неявки", "int", True)] + \
             ([Col("share", "Частка зривів", "pct")] if view == "reasons" else [Col("share", "% зривів", "pct")]) + \
             [Col("sum", "Втрачено", "uah", True), Col("re", "Перезаписались", "int", True), Col("re_pct", "% перезапису", "pct")]
    for k, g in G.items():
        n = g["c"] + g["n"]
        if not n and view == "":
            continue
        name = k if view == "reasons" else (WD[k] if view == "wd" else (docs[k].name if k in docs else "Без спеціаліста"))
        r.rows.append({"name": name, "_k": k if view == "wd" else 0, "all": int(g["all"]), "c": int(g["c"]), "n": int(g["n"]),
                       "share": _pct(n, len(lost) if view == "reasons" else g["all"]), "sum": round(g["sum"]),
                       "re": int(g["re"]), "re_pct": _pct(g["re"], n)})
    r.rows.sort(key=(lambda x: x["_k"]) if view == "wd" else (lambda x: -(x["c"] + x["n"])))
    r.chart = [(x["name"], x["c"] + x["n"]) for x in r.rows[:12]]
    r.chart_title = "Зірвані записи"
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    return r


BUILDERS = {"load": load, "admin_kpi": admin_kpi, "tasks": tasks, "cancels": cancels}
