"""Звіти блоку «Візити»: відгуки й NPS, завантаженість кабінетів і апаратів."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appointment, Doctor, Patient, Review
from .reports import Col, Report, _rng

WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
LOST = ("cancelled", "no_show")


def _loc(v):
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


def _pct(a: float, b: float) -> float | None:
    return round(100 * a / b, 1) if b else None


def nps(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from . import reviews as rv
    a, b = _rng(d_from, d_to)
    view = view if view in ("list", "questions", "complaints") else ""
    asked = [r for r in db.scalars(select(Review).where(Review.created_at >= a - timedelta(hours=4), Review.created_at <= b + timedelta(hours=4)))
             if a <= _loc(r.created_at) <= b]
    got = [r for r in asked if r.answered_at]
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    qt = {q["key"]: q["title"] for q in rv.cfg(db)["questions"]}
    sent = [r for r in asked if r.sent_at]
    five = [r for r in got if not r.complaint]
    r = Report("nps", "Відгуки й NPS",
               "Опитування після візитів за період (за датою візиту). NPS = % тих, хто поставив 9–10 на «чи порадите друзям», мінус % тих, "
               "хто поставив 0–6. Рекламація — відгук, де хоч одна оцінка нижча за 5. «У Google» — пацієнт натиснув кнопку й відкрив "
               "форму відгуку Google (чи опублікував — CRM не знає).",
               empty="За період опитувань не було. Увімкнути — «Налаштування → Відгуки й NPS».")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За лікарями"), ("questions", "За питаннями"), ("list", "Усі відгуки"), ("complaints", "Рекламації")]}]
    n = rv.nps_of(got)
    r.tiles = [("NPS", n if n is not None else 0, None, "num", f"{sum(1 for x in got if x.nps is not None)} відповідей на «чи порадите»"),
               ("Середня оцінка", round(sum(x.avg or 0 for x in got) / len(got), 2) if got else 0, None, "num", f"з 5 · {len(got)} відгуків"),
               ("Відповіли", _pct(len(got), len(sent)) or 0, None, "pct", f"{len(got)} з {len(sent)} надісланих · не доставлено {len(asked) - len(sent)}"),
               ("Рекламацій", len(got) - len(five), None, "int", f"у Google перейшли {sum(1 for x in got if x.google_at)} з {len(five)} «пʼятірок»")]
    if view in ("list", "complaints"):
        rows = [x for x in got if view == "list" or x.complaint]
        pn = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_({x.patient_id for x in rows} or {0})))}
        r.cols = [Col("name", "Пацієнт"), Col("at", "Дата", "date"), Col("doc", "Лікар"), Col("avg", "Оцінка", "num"), Col("nps", "Порадить, 0–10", "int"),
                  Col("low", "Нижче 5"), Col("text", "Відгук"), Col("g", "Google")]
        for x in sorted(rows, key=lambda x: x.answered_at, reverse=True):
            r.rows.append({"name": pn.get(x.patient_id, f"#{x.patient_id}"), "at": _loc(x.answered_at), "doc": docs.get(x.doctor_id, "—"), "avg": x.avg,
                           "nps": x.nps, "low": ", ".join(f"{qt.get(k, k)}: {v}" for k, v in (x.scores or {}).items() if v < 5),
                           "text": (x.text or "")[:160], "g": "так" if x.google_at else "", "_href": f"/patients/{x.patient_id}#t-reviews"})
        r.empty = "Рекламацій за період немає." if view == "complaints" else r.empty
        return r
    if view == "questions":
        G = defaultdict(list)
        for x in got:
            for k, v in (x.scores or {}).items():
                G[k].append(v)
        r.cols = [Col("name", "Питання"), Col("n", "Оцінок", "int"), Col("avg", "Середня", "num"), Col("five", "«5»", "pct"), Col("low", "Нижче 5", "int", True)]
        for k, v in G.items():
            r.rows.append({"name": qt.get(k, k), "n": len(v), "avg": round(sum(v) / len(v), 2), "five": _pct(sum(1 for z in v if z == 5), len(v)),
                           "low": sum(1 for z in v if z < 5)})
        r.rows.sort(key=lambda z: z["avg"])
        r.total = {"low": sum(z["low"] for z in r.rows)}
        return r
    G = defaultdict(lambda: {"asked": 0, "got": []})
    for x in asked:
        G[x.doctor_id]["asked"] += bool(x.sent_at)
    for x in got:
        G[x.doctor_id]["got"].append(x)
    r.cols = [Col("name", "Лікар"), Col("asked", "Надіслано", "int", True), Col("n", "Відгуків", "int", True), Col("resp", "Відповіли", "pct"),
              Col("avg", "Середня оцінка", "num"), Col("nps", "NPS", "num"), Col("five", "Усі «5»", "int", True), Col("bad", "Рекламацій", "int", True),
              Col("g", "У Google", "int", True)]
    for did, g in G.items():
        v = g["got"]
        r.rows.append({"name": docs.get(did, "Без лікаря"), "asked": g["asked"], "n": len(v), "resp": _pct(len(v), g["asked"]),
                       "avg": round(sum(x.avg or 0 for x in v) / len(v), 2) if v else None, "nps": rv.nps_of(v),
                       "five": sum(1 for x in v if not x.complaint), "bad": sum(1 for x in v if x.complaint), "g": sum(1 for x in v if x.google_at),
                       "_href": f"/reports/doctors-kpi?doctor={did}" if did else None})
    r.rows.sort(key=lambda z: -(z["n"]))
    r.total = {c.key: sum(z.get(c.key) or 0 for z in r.rows) for c in r.cols if c.total}
    r.chart = [(z["name"][:16], z["avg"] or 0) for z in r.rows]
    r.chart_title = "Середня оцінка за лікарями"
    return r


def rooms(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Зайнятість кабінетів і апаратів за записами: години, записи, піки по годинах дня."""
    a, b = _rng(d_from, d_to)
    view = view if view in ("res", "hours", "wd") else ""
    appts = list(db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b, Appointment.status.notin_(LOST))))
    days = max((d_to - d_from).days + 1, 1)
    r = Report("rooms", "Завантаженість кабінетів і апаратів",
               "За записами періоду (без скасованих і неявок): скільки годин зайнятий кожен кабінет і апарат, у які години й дні тижня піки. "
               "Кабінет і апарат беруться з запису; записи без них — у рядку «не вказано».",
               empty="За період записів не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "Кабінети"), ("res", "Апарати"), ("hours", "За годинами дня"), ("wd", "За днями тижня")]}]

    def dur(x):
        return max(0.0, ((x.end or x.start) - x.start).total_seconds() / 3600)
    tot_h = sum(dur(x) for x in appts)
    no_place = sum(1 for x in appts if not x.place)
    places = {x.place for x in appts if x.place}
    r.tiles = [("Записів", len(appts), None, "int", f"{round(tot_h, 1)} годин"),
               ("Кабінетів у роботі", len(places), None, "int", f"без кабінету: {no_place} записів"),
               ("Годин на день", round(tot_h / days, 1), None, "num", "у середньому по клініці"),
               ("З апаратом", sum(1 for x in appts if x.resource), None, "int", f"{_pct(sum(1 for x in appts if x.resource), len(appts)) or 0}% записів")]
    if view in ("hours", "wd"):
        G = defaultdict(lambda: {"h": 0.0, "n": 0, "pl": set()})
        for x in appts:
            t, e = x.start, x.end or x.start
            first = True
            while t < e:
                nx = min(e, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                g = G[t.hour if view == "hours" else t.weekday()]
                g["h"] += (nx - t).total_seconds() / 3600
                if first:
                    g["n"] += 1
                    first = False
                if x.place:
                    g["pl"].add((x.place, t.date()))
                t = nx
        r.cols = [Col("name", "Година" if view == "hours" else "День тижня"), Col("n", "Записів", "int", True), Col("h", "Зайнято годин", "num", True),
                  Col("share", "Частка", "pct")]
        for k in sorted(G):
            g = G[k]
            r.rows.append({"name": f"{k:02d}:00–{k + 1:02d}:00" if view == "hours" else WD[k], "n": g["n"], "h": round(g["h"], 1), "share": _pct(g["h"], tot_h)})
        r.chart = [(z["name"][:5], z["h"]) for z in r.rows]
        r.chart_title = "Зайняті години"
    else:
        key = (lambda x: x.resource or "Апарат не вказано") if view == "res" else (lambda x: x.place or "Кабінет не вказано")
        G = defaultdict(lambda: {"h": 0.0, "n": 0, "d": set(), "sum": 0.0, "hours": defaultdict(float)})
        for x in appts:
            if view == "res" and not x.resource:
                continue
            g = G[key(x)]
            g["h"] += dur(x)
            g["n"] += 1
            g["d"].add(x.start.date())
            g["sum"] += sum(float(s.get("price") or 0) for s in (x.services or []) if isinstance(s, dict))
            g["hours"][x.start.hour] += dur(x)
        r.cols = [Col("name", "Апарат" if view == "res" else "Кабінет"), Col("n", "Записів", "int", True), Col("h", "Зайнято годин", "num", True),
                  Col("d", "Днів у роботі", "int"), Col("per", "Годин на робочий день", "num"), Col("peak", "Пікова година"),
                  Col("sum", "Сума записів", "uah", True), Col("uah_h", "₴ за годину", "uah")]
        for k, g in sorted(G.items(), key=lambda kv: -kv[1]["h"]):
            pk = max(g["hours"], key=g["hours"].get) if g["hours"] else None
            r.rows.append({"name": k, "n": g["n"], "h": round(g["h"], 1), "d": len(g["d"]), "per": round(g["h"] / len(g["d"]), 1) if g["d"] else None,
                           "peak": f"{pk:02d}:00" if pk is not None else "", "sum": round(g["sum"]), "uah_h": round(g["sum"] / g["h"]) if g["h"] else None})
        r.chart = [(z["name"][:16], z["h"]) for z in r.rows[:12]]
        r.chart_title = "Зайняті години"
        r.empty = "Записів з апаратом за період не було." if view == "res" else r.empty
    r.total = {c.key: round(sum(z.get(c.key) or 0 for z in r.rows), 1) for c in r.cols if c.total}
    return r


BUILDERS = {"nps": nps, "rooms": rooms}
