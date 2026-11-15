"""Звіти блоку «Комунікації»: статистика по чатах (Direct / Messenger) і повідомлення пацієнтам (SMS / Viber / Telegram)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appeal, Appointment, Campaign, ChatMessage, MessageLog, Patient, User
from .reports import Col, Report, _rng
from .reports_staff import REPLY_MAX_MIN

WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
CH = {"instagram": "Instagram", "facebook": "Facebook", "telegram": "Telegram", "viber": "Viber", "sms": "SMS"}
LOST = ("cancelled", "no_show")


def _loc(v: datetime | None) -> datetime | None:
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


def _utc(v: datetime) -> datetime:
    return v.replace(tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc).replace(tzinfo=None)


def _pct(a: float, b: float) -> float | None:
    return round(100 * a / b, 1) if b else None


# ---------------- статистика по чатах ----------------

def chat_stats(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from . import appeals as ap
    a, b = _rng(d_from, d_to)
    view = view if view in ("staff", "hours", "wd", "open") else ""
    msgs = list(db.scalars(select(ChatMessage).where(ChatMessage.at >= _utc(a), ChatMessage.at <= _utc(b)).order_by(ChatMessage.at)))
    th = defaultdict(list)
    for m in msgs:
        th[(m.channel, m.thread)].append(m)
    # нова переписка — до періоду від цієї людини повідомлень не було
    old = set()
    if th:
        for ch_, t_ in db.execute(select(ChatMessage.channel, ChatMessage.thread).where(ChatMessage.at < _utc(a)).distinct()):
            old.add((ch_, t_))
    d = ap.dirs(db)
    ap_ids = {m.appeal_id for m in msgs if m.appeal_id}
    appeals = {x.id: x for x in db.scalars(select(Appeal).where(Appeal.id.in_(ap_ids)))} if ap_ids else {}
    users = {u.id: u.name for u in db.scalars(select(User))}
    rows_t, waits, by_user = [], [], defaultdict(list)
    for key, ms in th.items():
        ins = [m for m in ms if m.direction == "in"]
        outs = [m for m in ms if m.direction == "out" and (m.status or "sent") == "sent"]
        wait, first_wait, pending = None, None, None
        for m in ms:
            if m.direction == "in":
                wait = wait or m.at
            elif wait and (m.status or "sent") == "sent":
                mins = (m.at - wait).total_seconds() / 60
                if mins <= REPLY_MAX_MIN:
                    waits.append((wait, mins))
                    if m.user_id:
                        by_user[m.user_id].append(mins)
                    first_wait = first_wait if first_wait is not None else mins
                wait = None
        pending = wait
        apl = next((appeals[m.appeal_id] for m in ms if m.appeal_id in appeals), None)
        rows_t.append({"key": key, "ins": len(ins), "outs": len(outs), "new": key not in old and bool(ins), "first_wait": first_wait,
                       "pending": pending, "name": next((m.name for m in ms if m.name), None), "patient_id": next((m.patient_id for m in ms if m.patient_id), None),
                       "last_in": ins[-1] if ins else None, "ad": any(m.ad for m in ms),
                       "type": ap.status_type(d, apl.status) if apl else None})
    active = [t for t in rows_t if t["ins"]]
    n_in, n_out = sum(t["ins"] for t in rows_t), sum(t["outs"] for t in rows_t)
    wv = [w for _, w in waits]
    pend = [t for t in active if t["pending"]]
    booked = sum(1 for t in active if t["type"] == "success")
    r = Report("chat_stats", "Статистика по чатах",
               "Instagram Direct і Facebook Messenger за період. «Нові» — люди, які раніше не писали. Час відповіді — від першого повідомлення "
               "пацієнта без відповіді до нашої відповіді (відповіді пізніше ніж за 12 годин у швидкість не входять). «Без відповіді» — останнє "
               "слово за пацієнтом. «Записались» — звернення цієї переписки у статусі «успіх».",
               empty="За період повідомлень у чатах не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За днями"), ("staff", "За співробітниками"), ("hours", "За годинами дня"), ("wd", "За днями тижня"),
                              ("open", "Без відповіді")]}]
    r.tiles = [("Переписок", len(active), None, "int", f"нових {sum(t['new'] for t in active)} · з реклами {sum(t['ad'] for t in active)}"),
               ("Повідомлень", n_in + n_out, None, "int", f"вхідних {n_in} · наших {n_out}"),
               ("Відповідь", round(median(wv)) if wv else 0, None, "int",
                f"хв, медіана · до 15 хв: {_pct(sum(1 for w in wv if w <= 15), len(wv)) or 0}%"),
               ("Без відповіді", len(pend), None, "int", f"записались {booked} з {len(active)} ({_pct(booked, len(active)) or 0}%)")]
    if view == "open":
        pn = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_({t["patient_id"] for t in pend if t["patient_id"]} or {0})))}
        r.cols = [Col("name", "Хто"), Col("ch", "Канал"), Col("at", "Останнє повідомлення", "date"), Col("hours", "Годин без відповіді", "num"),
                  Col("text", "Текст")]
        now = datetime.utcnow()
        for t in sorted(pend, key=lambda t: t["pending"]):
            m = t["last_in"]
            r.rows.append({"name": pn.get(t["patient_id"]) or t["name"] or "Без імені", "ch": CH.get(t["key"][0], t["key"][0]), "at": _loc(m.at),
                           "hours": round((now - t["pending"]).total_seconds() / 3600, 1), "text": (m.text or "вкладення")[:90],
                           "_href": f"/chats?t={t['key'][1]}"})
        r.empty = "Усі переписки з відповіддю."
        return r
    if view == "staff":
        G = defaultdict(lambda: {"out": 0, "th": set()})
        for m in msgs:
            if m.direction == "out" and (m.status or "sent") == "sent":
                g = G[m.user_id or 0]
                g["out"] += 1
                g["th"].add((m.channel, m.thread))
        r.cols = [Col("name", "Співробітник"), Col("out", "Відповідей", "int", True), Col("th", "Переписок", "int", True),
                  Col("rt", "Відповідь, хв (медіана)", "int"), Col("fast", "До 15 хв", "pct"), Col("slow", "Понад годину", "int", True)]
        for uid, g in sorted(G.items(), key=lambda kv: -kv[1]["out"]):
            w = by_user.get(uid, [])
            r.rows.append({"name": users.get(uid) or "У самому Instagram / Facebook (не з CRM)", "out": g["out"], "th": len(g["th"]),
                           "rt": round(median(w)) if w else None, "fast": _pct(sum(1 for x in w if x <= 15), len(w)),
                           "slow": sum(1 for x in w if x > 60)})
        r.total = {"out": sum(x["out"] for x in r.rows), "th": len({k for g in G.values() for k in g["th"]}), "slow": sum(x["slow"] for x in r.rows)}
        return r
    key = {"hours": lambda t: t.hour, "wd": lambda t: t.weekday()}.get(view, lambda t: t.date())
    G = defaultdict(lambda: {"in": 0, "out": 0, "th": set(), "new": set(), "w": []})
    newk = {t["key"] for t in rows_t if t["new"]}
    for m in msgs:
        g = G[key(_loc(m.at))]
        if m.direction == "in":
            g["in"] += 1
            g["th"].add((m.channel, m.thread))
            if (m.channel, m.thread) in newk:
                g["new"].add((m.channel, m.thread))
        elif (m.status or "sent") == "sent":
            g["out"] += 1
    for at, w in waits:
        G[key(_loc(at))]["w"].append(w)
    r.cols = [Col("name", {"hours": "Година", "wd": "День тижня"}.get(view, "Дата")), Col("th", "Переписок", "int"), Col("new", "Нових", "int", True),
              Col("in", "Вхідних", "int", True), Col("out", "Наших відповідей", "int", True), Col("rt", "Відповідь, хв (медіана)", "int"),
              Col("fast", "До 15 хв", "pct"), Col("slow", "Понад годину", "int", True)]
    for k in sorted(G, reverse=not view):
        g = G[k]
        name = f"{k:02d}:00–{k + 1:02d}:00" if view == "hours" else (WD[k] if view == "wd" else f"{k:%d.%m.%Y} {WD[k.weekday()]}")
        r.rows.append({"name": name, "th": len(g["th"]), "new": len(g["new"]), "in": g["in"], "out": g["out"],
                       "rt": round(median(g["w"])) if g["w"] else None, "fast": _pct(sum(1 for x in g["w"] if x <= 15), len(g["w"])),
                       "slow": sum(1 for x in g["w"] if x > 60)})
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    ch_rows = r.rows if view else list(reversed(r.rows))
    r.chart = [(x["name"][:5], x["in"]) for x in ch_rows[-31:]]
    r.chart_title = "Вхідні повідомлення"
    return r


# ---------------- повідомлення пацієнтам ----------------

def messages(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    a, b = _rng(d_from, d_to)
    view = view if view in ("kind", "staff", "errors") else ""
    logs = list(db.scalars(select(MessageLog).where(MessageLog.at >= _utc(a), MessageLog.at <= _utc(b)).order_by(MessageLog.at.desc())))
    users = {u.id: u.name for u in db.scalars(select(User))}
    camps = {c.id: c.name for c in db.scalars(select(Campaign))}
    ok = [m for m in logs if m.status != "failed"]
    # запис після повідомлення: пацієнт створив запис протягом 7 днів
    pids = {m.patient_id for m in ok}
    made = defaultdict(list)
    if pids:
        for pid, at in db.execute(select(Appointment.patient_id, Appointment.created_at).where(
                Appointment.patient_id.in_(list(pids)), Appointment.status.notin_(LOST), Appointment.created_at >= _utc(a))):
            made[pid].append(at)

    def booked(m) -> bool:
        return any(m.at <= t <= m.at + timedelta(days=7) for t in made.get(m.patient_id, ()))

    def kind(m) -> str:
        return "Розсилка" if m.campaign_id else ("Із задачі" if m.task_id else "Вручну з картки")
    r = Report("messages", "Повідомлення пацієнтам",
               "Усе, що CRM надіслала пацієнтам за період: SMS, Viber, Telegram — з розсилок, із задач і вручну з картки. «Не надіслано» — "
               "помилка каналу (немає Telegram, невірний номер, збій). «Запис після» — пацієнт записався протягом 7 днів після повідомлення. "
               "Статус «доставлено / прочитано» є лише в розсилках — у звіті «Розсилки: ефективність».",
               empty="За період повідомлень пацієнтам не надсилали.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За каналами"), ("kind", "За типом"), ("staff", "За відправниками"), ("errors", "Помилки")]}]
    bk = {m.patient_id for m in ok if booked(m)}
    r.tiles = [("Надіслано", len(ok), None, "int", f"пацієнтам: {len(pids)}"),
               ("Не надіслано", len(logs) - len(ok), None, "int", f"{_pct(len(logs) - len(ok), len(logs)) or 0}% спроб"),
               ("Маркетингових", sum(1 for m in ok if m.marketing), None, "int", "розсилки й акції"),
               ("Записались після", len(bk), None, "int", f"{_pct(len(bk), len(pids)) or 0}% пацієнтів, протягом 7 днів")]
    if view == "errors":
        pn = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_({m.patient_id for m in logs if m.status == "failed"} or {0})))}
        r.cols = [Col("name", "Пацієнт"), Col("at", "Коли", "date"), Col("ch", "Канал"), Col("kind", "Тип"), Col("err", "Помилка")]
        for m in logs:
            if m.status == "failed":
                r.rows.append({"name": pn.get(m.patient_id, f"#{m.patient_id}"), "at": _loc(m.at), "ch": CH.get(m.channel, m.channel), "kind": kind(m),
                               "err": (m.error or "")[:120], "_href": f"/patients/{m.patient_id}"})
        r.empty = "Помилок надсилання за період не було."
        return r
    key = {"kind": lambda m: (camps.get(m.campaign_id, "Розсилка") if m.campaign_id else kind(m)),
           "staff": lambda m: users.get(m.user_id) or ("Розсилка / автоматично" if not m.user_id else f"#{m.user_id}")}.get(
        view, lambda m: CH.get(m.channel, m.channel))
    G = defaultdict(lambda: {"ok": 0, "fail": 0, "p": set(), "bk": set()})
    for m in logs:
        g = G[key(m)]
        if m.status == "failed":
            g["fail"] += 1
        else:
            g["ok"] += 1
            g["p"].add(m.patient_id)
            if m.patient_id in bk and booked(m):
                g["bk"].add(m.patient_id)
    r.cols = [Col("name", {"kind": "Тип / розсилка", "staff": "Відправник"}.get(view, "Канал")), Col("ok", "Надіслано", "int", True),
              Col("fail", "Не надіслано", "int", True), Col("fail_pct", "% помилок", "pct"), Col("p", "Пацієнтів", "int"),
              Col("bk", "Записались після", "int", True), Col("conv", "Конверсія", "pct")]
    for k, g in sorted(G.items(), key=lambda kv: -kv[1]["ok"]):
        r.rows.append({"name": k, "ok": g["ok"], "fail": g["fail"], "fail_pct": _pct(g["fail"], g["ok"] + g["fail"]), "p": len(g["p"]),
                       "bk": len(g["bk"]), "conv": _pct(len(g["bk"]), len(g["p"]))})
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    r.chart = [(x["name"][:18], x["ok"]) for x in r.rows[:10]]
    r.chart_title = "Надіслано повідомлень"
    return r


BUILDERS = {"chat_stats": chat_stats, "messages": messages}
