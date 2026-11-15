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


# ---------------- робочий час і завантаження ----------------

def _hours(t: datetime, e: datetime):
    """Інтервал → (дата, година, частка години) по годинних відрізках."""
    while t < e:
        nxt = min(e, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        yield t.date(), t.hour, (nxt - t).total_seconds() / 3600
        t = nxt


def load(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from ..models import TimeBlock
    a, b = _rng(d_from, d_to)
    view = view if view in ("wd", "days", "hours") else ""
    docs = _doctors(db)
    shifts = {(s.doctor_id, s.day): s for s in db.scalars(select(StaffShift).where(StaffShift.day >= d_from, StaffShift.day <= d_to))}
    appts = list(db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b,
                                                      Appointment.status.notin_(LOST), Appointment.doctor_id.is_not(None))))
    blocks = list(db.scalars(select(TimeBlock).where(TimeBlock.start <= b, TimeBlock.end >= a)))
    # лише ті, хто веде прийом: адміністратори, прибирання тощо мають зміни, але не мають записів — вони б занижували зайнятість
    seen = {did for (did,) in db.execute(select(Appointment.doctor_id).where(
        Appointment.start >= a - timedelta(days=90), Appointment.start <= b, Appointment.status.notin_(LOST),
        Appointment.doctor_id.is_not(None)).distinct())}
    shifts = {k: v for k, v in shifts.items() if k[0] in seen}
    r = Report("load", "Робочий час і завантаження",
               "Години за графіком («Спеціалісти → Графік») мінус заблокований час (блокування в календарі в межах зміни) = доступні години. "
               "«У записах» — години записів без скасованих і неявок у дні зі зміною. Простій = доступні − у записах; "
               "зайнятість = у записах ÷ доступні. Записи в дні без зміни показані окремо як «поза графіком». "
               "У звіті лише ті, хто веде прийом (мав записи за останні 90 днів) — зміни адміністраторів і прибирання не враховуються.",
               empty="За період немає ні змін у графіку, ні записів.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За спеціалістами"), ("wd", "За днями тижня"), ("days", "За датами"), ("hours", "За годинами дня")]}]
    # клітинка (спеціаліст, дата, година) → графік / блок / записи
    C = defaultdict(lambda: {"sh": 0.0, "blk": 0.0, "busy": 0.0, "out": 0.0, "n": 0, "sum": 0.0})
    for (did, day), s in shifts.items():
        x, y = _hm(s.start), _hm(s.end)
        for h in range(int(x), int(y + 0.999)):
            C[(did, day, h)]["sh"] += max(0.0, min(y, h + 1) - max(x, h))
    for bl in blocks:
        for did in ([bl.doctor_id] if bl.doctor_id else {k[0] for k in shifts}):
            for day, h, part in _hours(max(bl.start, a), min(bl.end, b)):
                c = C.get((did, day, h))
                if c and c["sh"]:
                    c["blk"] = min(c["sh"], c["blk"] + part)
    for ap in appts:
        on = (ap.doctor_id, ap.start.date()) in shifts
        first = True
        for day, h, part in _hours(ap.start, ap.end or ap.start):
            c = C[(ap.doctor_id, day, h)]
            c["busy" if on else "out"] += part
            if first:
                c["n"] += 1
                c["sum"] += _price(ap)
                first = False
    key = {"wd": lambda k: k[1].weekday(), "days": lambda k: k[1], "hours": lambda k: k[2]}.get(view, lambda k: k[0])
    G = defaultdict(lambda: defaultdict(float))
    days = defaultdict(set)
    for k, c in C.items():
        g = G[key(k)]
        for f in ("sh", "blk", "busy", "out", "n", "sum"):
            g[f] += c[f]
        if c["sh"]:
            days[key(k)].add((k[0], k[1]))
    tot = {f: sum(g[f] for g in G.values()) for f in ("sh", "blk", "busy", "out", "n")}
    avail = max(0.0, tot["sh"] - tot["blk"])
    W = defaultdict(lambda: [0.0, 0.0])               # день тижня → [доступно, зайнято]
    for k, c in C.items():
        W[k[1].weekday()][0] += c["sh"] - c["blk"]
        W[k[1].weekday()][1] += c["busy"]
    wl = sorted(((v[1] / v[0], w) for w, v in W.items() if v[0] > 0))
    r.tiles = [("Зайнятість", _pct(tot["busy"], avail) or 0, None, "pct", f"{round(tot['busy'], 1)} з {round(avail, 1)} доступних год"),
               ("Години за графіком", round(tot["sh"], 1), None, "num", f"{len(shifts)} змін · заблоковано {round(tot['blk'], 1)} год"),
               ("Простій", round(max(0.0, avail - tot["busy"]), 1), None, "num",
                f"годин · {_pct(max(0.0, avail - tot['busy']), avail) or 0}% вільного часу"),
               ("Найвільніший день", round(100 - 100 * wl[0][0], 1) if wl else 0, None, "pct",
                (f"вільно в {WD[wl[0][1]]} · найзайнятіший — {WD[wl[-1][1]]} ({round(100 * wl[-1][0])}%)" if wl else "") +
                (f" · поза графіком {round(tot['out'], 1)} год" if tot["out"] else ""))]
    r.cols = [Col("name", {"wd": "День тижня", "days": "Дата", "hours": "Година"}.get(view, "Спеціаліст")), Col("days", "Змін", "int", True),
              Col("sh", "За графіком", "num", True), Col("blk", "Заблоковано", "num", True), Col("avail", "Доступно", "num", True),
              Col("busy", "У записах", "num", True), Col("load", "Зайнятість", "pct"), Col("free", "Простій", "num", True),
              Col("free_pct", "Вільно", "pct"), Col("out", "Поза графіком", "num", True), Col("n", "Записів", "int", True),
              Col("sum", "Сума записів", "uah", True), Col("per_h", "₴ за доступну год", "uah")]
    for k, g in G.items():
        if not view and k in docs and docs[k].active is False and not g["n"]:
            continue
        av = max(0.0, g["sh"] - g["blk"])
        name = {"wd": lambda: WD[k], "days": lambda: f"{k:%d.%m} {WD[k.weekday()]}", "hours": lambda: f"{k:02d}:00–{k + 1:02d}:00"}.get(
            view, lambda: docs[k].name if k in docs else f"#{k}")()
        r.rows.append({"name": name, "_k": k, "days": len(days[k]), "sh": round(g["sh"], 1), "blk": round(g["blk"], 1), "avail": round(av, 1),
                       "busy": round(g["busy"], 1), "load": _pct(g["busy"], av), "free": round(max(0.0, av - g["busy"]), 1),
                       "free_pct": _pct(max(0.0, av - g["busy"]), av), "out": round(g["out"], 1), "n": int(g["n"]), "sum": round(g["sum"]),
                       "per_h": round(g["sum"] / av) if av else None,
                       "_href": f"/reports/doctors-kpi?doctor={k}" if not view else None})
    r.rows.sort(key=(lambda x: x["_k"]) if view else (lambda x: -(x["load"] or 0)))
    r.chart = [(x["name"], x["load"] or 0) for x in r.rows] if view != "days" or len(r.rows) <= 31 else []
    r.chart_title = "Зайнятість, %"
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
    missed = [c for c in c_in if c.status == "missed"]
    back = 0                                      # пропущені, яким передзвонили (вихідний на той самий номер пізніше)
    outs = defaultdict(list)
    for c in calls:
        if c.direction == "out" and c.phone:
            outs[c.phone[-9:]].append(c.at)
    for c in missed:
        if c.phone and any(t > c.at for t in outs.get(c.phone[-9:], ())):
            back += 1
    from .telephony import _emp
    by_name = {(u.name or "").strip().lower(): u.id for u in users.values()}
    for c in calls:
        uid = c.user_id or by_name.get((_emp(c.employee) or "").strip().lower())   # як у «Телефонія → KPI»: за користувачем або імʼям
        if not uid or c.status == "missed":
            continue
        g = G[uid]
        g["c_in" if c.direction == "in" else "c_out"] += 1
        g["talk"] += (c.duration or 0) / 60
        if c.ai_score is not None:
            scores[uid].append(float(c.ai_score))
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


# ---------------- поверненість пацієнтів по спеціалістах ----------------

def returns(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Когорта — пацієнти, які були в спеціаліста в періоді. Чи повернулись після цього: до нього ж, до іншого, взагалі в клініку."""
    from . import doctor_kpi as dk
    view = view if view in ("flow", "lost") else ""
    today = date.today()
    docs = _doctors(db)
    hist_from = d_from - timedelta(days=365)                       # рік історії — звідки пацієнт прийшов
    agg = dk._collect(db, hist_from, max(today, d_to), list(docs.values()))
    V: dict[int, list] = defaultdict(list)                         # пацієнт → [(час, лікар)]
    for did, x in agg.items():
        for v in x["visits"]:
            if v["patient_id"] and v["at"].date() <= today:
                V[v["patient_id"]].append((v["at"], did))
    for lst in V.values():
        lst.sort()
    cohort = {pid for pid, lst in V.items() if any(d_from <= at.date() <= d_to for at, _ in lst)}
    nb = dk._next_booked(db, cohort, datetime.now())
    first = dk._first_activity(db, cohort)
    nb_doc: dict[int, set] = defaultdict(set)                      # майбутні записи: пацієнт → лікарі
    if cohort:
        for pid, did in db.execute(select(Appointment.patient_id, Appointment.doctor_id).where(
                Appointment.patient_id.in_(cohort), Appointment.start > datetime.now(), Appointment.status.notin_(LOST))):
            nb_doc[pid].add(did)
    G = defaultdict(lambda: {"pat": set(), "new": set(), "same": set(), "other": set(), "clinic": set(), "new_back": set(),
                             "from_other": set(), "days": [], "visits": 0})
    flow = defaultdict(set)                                        # (від лікаря, до лікаря) → пацієнти
    lost_rows = []
    for pid in cohort:
        lst = V[pid]
        for did in {d for at, d in lst if d_from <= at.date() <= d_to}:
            g = G[did]
            mine = [at for at, d in lst if d == did and d_from <= at.date() <= d_to]
            t0 = mine[0]                                           # перший візит до цього лікаря в періоді
            g["pat"].add(pid)
            g["visits"] += len(mine)
            is_new = bool(first.get(pid) and first[pid] >= d_from)
            if is_new:
                g["new"].add(pid)
            prev = [d for at, d in lst if at < t0]
            if prev and prev[-1] != did:
                g["from_other"].add(pid)                           # прийшов від іншого спеціаліста клініки
                flow[(prev[-1], did)].add(pid)
            later = [(at, d) for at, d in lst if at.date() > t0.date()]
            same = any(d == did for _, d in later) or did in nb_doc.get(pid, ())
            other = any(d != did for _, d in later) or bool(nb_doc.get(pid, set()) - {did})
            back = bool(later) or pid in nb
            if same:
                g["same"].add(pid)
            if other:
                g["other"].add(pid)
            if back:
                g["clinic"].add(pid)
                if is_new:
                    g["new_back"].add(pid)
                if later:
                    g["days"].append((later[0][0].date() - t0.date()).days)
            elif view == "lost":
                lost_rows.append((pid, did, mine[-1], is_new))
    n_all, n_back = len(cohort), len({p for g in G.values() for p in g["clinic"]})
    n_new = {p for g in G.values() for p in g["new"]}
    n_new_back = {p for g in G.values() for p in g["new_back"]}
    same_all = sum(len(g["same"]) for g in G.values())
    pat_all = sum(len(g["pat"]) for g in G.values())
    r = Report("returns", "Поверненість по спеціалістах",
               "Когорта — пацієнти, які були в спеціаліста в обраному періоді. «Повернулись» — після першого візиту в періоді був ще візит "
               "в інший день або є майбутній запис: до цього ж спеціаліста, до іншого чи взагалі в клініку. «Прийшли від колег» — попередній "
               "візит пацієнта (за рік до періоду) був в іншого спеціаліста клініки. Свіжі періоди показують занижену поверненість — "
               "пацієнти ще не встигли повернутись; для висновків беріть період, що закінчився 1–2 місяці тому.",
               empty="За період візитів не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За спеціалістами"), ("flow", "Переходи між спеціалістами"), ("lost", "Хто не повернувся")]}]
    r.tiles = [("Поверненість у клініку", _pct(n_back, n_all) or 0, None, "pct", f"{n_back} з {n_all} пацієнтів періоду"),
               ("До того ж спеціаліста", _pct(same_all, pat_all) or 0, None, "pct", f"{same_all} з {pat_all}"),
               ("Поверненість нових", _pct(len(n_new_back), len(n_new)) or 0, None, "pct", f"{len(n_new_back)} з {len(n_new)} нових"),
               ("Переходів між спеціалістами", sum(len(v) for v in flow.values()), None, "int", "пацієнтів прийшли від колег")]
    name = lambda k: docs[k].name if k in docs else "Без спеціаліста"
    if view == "flow":
        r.cols = [Col("name", "Від кого"), Col("to", "До кого"), Col("n", "Пацієнтів", "int", True)]
        r.rows = sorted(({"name": name(a_), "to": name(b_), "n": len(v)} for (a_, b_), v in flow.items()), key=lambda x: -x["n"])
        r.total = {"n": sum(x["n"] for x in r.rows)}
        r.empty = "Переходів між спеціалістами за період не було."
        return r
    if view == "lost":
        pn = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_({x[0] for x in lost_rows} or {0})))}
        r.cols = [Col("name", "Пацієнт"), Col("phone", "Телефон"), Col("doc", "Спеціаліст"), Col("last", "Останній візит", "date"),
                  Col("days", "Днів тому", "int"), Col("kind", "Тип")]
        for pid, did, at, is_new in sorted(lost_rows, key=lambda x: x[2], reverse=True):
            p = pn.get(pid)
            r.rows.append({"name": p.full_name if p else f"#{pid}", "phone": (p.phone or "") if p else "", "doc": name(did), "last": at,
                           "days": (today - at.date()).days, "kind": "новий" if is_new else "постійний", "_href": f"/patients/{pid}"})
        r.empty = "Усі пацієнти періоду повернулись або записані."
        return r
    r.cols = [Col("name", "Спеціаліст"), Col("pat", "Пацієнтів", "int", True), Col("new", "з них нових", "int", True),
              Col("same", "Повернулись до нього", "int", True), Col("same_pct", "% до нього", "pct"),
              Col("other", "Пішли до інших", "int", True), Col("clinic", "Повернулись у клініку", "int", True),
              Col("clinic_pct", "% у клініку", "pct"), Col("new_pct", "% нових повернулось", "pct"),
              Col("lost", "Не повернулись", "int", True), Col("from_other", "Прийшли від колег", "int", True),
              Col("days", "Днів до повернення", "int"), Col("vpp", "Візитів на пацієнта", "num")]
    for did, g in G.items():
        n = len(g["pat"])
        r.rows.append({"name": name(did), "pat": n, "new": len(g["new"]), "same": len(g["same"]), "same_pct": _pct(len(g["same"]), n),
                       "other": len(g["other"]), "clinic": len(g["clinic"]), "clinic_pct": _pct(len(g["clinic"]), n),
                       "new_pct": _pct(len(g["new_back"]), len(g["new"])), "lost": n - len(g["clinic"]),
                       "from_other": len(g["from_other"]), "days": round(median(g["days"])) if g["days"] else None,
                       "vpp": round(g["visits"] / n, 1) if n else None, "_href": f"/reports/doctors-kpi?doctor={did}"})
    r.rows.sort(key=lambda x: -x["pat"])
    r.chart = [(x["name"], x["same_pct"] or 0) for x in r.rows]
    r.chart_title = "Поверненість до того ж спеціаліста, %"
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    return r


BUILDERS = {"load": load, "admin_kpi": admin_kpi, "tasks": tasks, "cancels": cancels, "returns": returns}
