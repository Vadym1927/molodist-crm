"""Звіти блоку «Маркетинг»: зведення по каналах (витрати → ліди → нові пацієнти → виручка), конверсія звернень у запис,
ефективність розсилок, вартість залучення й окупність нового пацієнта."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AdsStat, Appeal, Appointment, Campaign, Check, FinOp, Patient, Sale, Visit
from .reports import Col, Report, _rng

LOST = ("cancelled", "no_show")
ADS_RE = re.compile(r"google|гугл|meta|facebook|фейсбук|instagram|інстаграм|таргет.*бюджет", re.I)
MKT_RE = re.compile(r"маркет|реклам", re.I)


def _loc(v: datetime | None) -> datetime | None:
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


def _utc(v: datetime) -> datetime:
    return v.replace(tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc).replace(tzinfo=None)


def _pct(a: float, b: float) -> float | None:
    return round(100 * a / b, 1) if b else None


def _div(a: float, b: float) -> float | None:
    return round(a / b, 2) if b else None


def ad_costs(db: Session, d_from: date, d_to: date) -> dict:
    """Витрати рекламних кабінетів у гривні: Google (з ПДВ) і Meta (за курсом на день)."""
    from . import google_ads as ga, meta_ads as ma
    vat = float(ga.cfg(db).get("vat_pct") or 0)
    g = sum(float(s.cost or 0) for s in db.scalars(select(AdsStat).where(AdsStat.source == "google", AdsStat.level == "campaign",
                                                                        AdsStat.day >= d_from, AdsStat.day <= d_to)))
    rate = ma._rate(db)
    m = sum(float(s.cost or 0) * (float((s.extra or {}).get("rate") or 0) or rate)
            for s in db.scalars(select(AdsStat).where(AdsStat.source == "meta", AdsStat.level == "ad", AdsStat.day >= d_from, AdsStat.day <= d_to)))
    return {"google": round(g * (1 + vat / 100), 2), "meta": round(m, 2)}


def other_costs(db: Session, d_from: date, d_to: date) -> float:
    """Інші маркетингові витрати з фіноперацій (статті з «маркет» / «реклам»), без поповнень рекламних кабінетів."""
    tot = 0.0
    for x in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.kind == "expense", FinOp.day >= d_from, FinOp.day <= d_to)):
        if MKT_RE.search(x.category or "") and not ADS_RE.search(f"{x.category} {x.counterparty or ''} {x.comment or ''}"):
            tot += float(x.amount or 0)
    return round(tot, 2)


def _appeal_leads(db: Session, d_from: date, d_to: date, skip: set[int]) -> list[dict]:
    a, b = _rng(d_from, d_to)
    out = []
    for x in db.scalars(select(Appeal).where(Appeal.created_at >= _utc(a), Appeal.created_at <= _utc(b), Appeal.deleted_at.is_(None))
                        .order_by(Appeal.created_at)):
        if x.id in skip:
            continue
        out.append({"at": _loc(x.created_at), "phone": x.phone, "kind": "appeal", "status": x.status, "source": (x.source or "").strip() or "Без джерела",
                    "patient_id": x.patient_id, "appeal_id": x.id, "call_id": None, "answered": True, "touches": 1,
                    "deny": x.deny_reason, "closed_at": _loc(x.closed_at), "responsible_id": x.responsible_id})
    return out


def _sum(ls: list[dict]) -> dict:
    return {"leads": len(ls), "booked": sum(1 for r in ls if r["booked"]), "came": sum(1 for r in ls if r["came"]),
            "new": sum(1 for r in ls if r["new"] and r["came"]), "payers": sum(1 for r in ls if r["revenue"] > 0),
            "revenue": round(sum(r["revenue"] for r in ls), 2)}


def _first_all(db: Session) -> dict[int, date]:
    """Перша активність кожного пацієнта (чек, візит CleverBox, запис CRM)."""
    out: dict[int, date] = {}

    def put(pid, dt):
        if pid and dt:
            d = dt.date() if isinstance(dt, datetime) else datetime.fromisoformat(str(dt)).date()
            out[pid] = min(out.get(pid, d), d)
    for pid, dt in db.execute(select(Check.patient_id, func.min(Check.date)).where(Check.deleted.is_(False)).group_by(Check.patient_id)):
        put(pid, dt)
    for pid, dt in db.execute(select(Visit.patient_id, func.min(Visit.starts_at)).where(Visit.status != "cancelled").group_by(Visit.patient_id)):
        put(pid, dt)
    for pid, dt in db.execute(select(Appointment.patient_id, func.min(Appointment.start)).where(Appointment.status.notin_(LOST))
                              .group_by(Appointment.patient_id)):
        put(pid, dt)
    return out


def _payments(db: Session, pids: set[int]) -> dict[int, list]:
    out = defaultdict(list)
    if not pids:
        return out
    ids = list(pids)
    for pid, dt, amt in db.execute(select(Check.patient_id, Check.date, Check.amount).where(Check.patient_id.in_(ids), Check.deleted.is_(False))):
        out[pid].append((dt, float(amt or 0)))
    for pid, dt, amt in db.execute(select(Sale.patient_id, Sale.at, Sale.total).where(Sale.patient_id.in_(ids), Sale.status == "paid")):
        out[pid].append((dt, float(amt or 0)))
    return out


# ---------------- зведення по каналах ----------------

def channels(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from . import google_ads as ga, meta_ads as ma
    cost = ad_costs(db, d_from, d_to)
    g = ga.funnel(db, ga.leads(db, d_from, d_to))
    m = ga.funnel(db, ma.leads(db, d_from, d_to))
    used = {r["appeal_id"] for r in g + m if r.get("appeal_id")}
    from . import appeals as ap
    dd = ap.dirs(db)
    rest = ga.funnel(db, [x for x in _appeal_leads(db, d_from, d_to, used) if ap.status_type(dd, x["status"]) != "other"])   # без «не пацієнтів»
    by_src = defaultdict(list)
    for r in rest:
        by_src[r["source"]].append(r)
    lead_pids = {r["patient_id"] for r in g + m + rest if r.get("patient_id")}
    # нові пацієнти без жодного звернення — записались напряму (за джерелом запису першого візиту)
    first = _first_all(db)
    direct = {pid for pid, d in first.items() if d_from <= d <= d_to and pid not in lead_pids}
    pays = _payments(db, direct)
    bsrc = {}
    if direct:
        for ap in db.scalars(select(Appointment).where(Appointment.patient_id.in_(list(direct)), Appointment.status.notin_(LOST))
                             .order_by(Appointment.start.desc())):
            bsrc[ap.patient_id] = ap.booking_source or "не вказано"
    D = defaultdict(lambda: {"leads": 0, "booked": 0, "came": 0, "new": 0, "payers": 0, "revenue": 0.0})
    for pid in direct:
        t = D["Запис напряму · " + bsrc.get(pid, "історія CleverBox")]
        rev = sum(v for dt, v in pays.get(pid, []) if dt.date() >= first[pid])
        t["leads"] += 1
        t["booked"] += 1
        t["came"] += 1
        t["new"] += 1
        t["payers"] += rev > 0
        t["revenue"] += rev
    rows = [("Google Ads", cost["google"], _sum(g), "/marketing/google"), ("Meta Ads", cost["meta"], _sum(m), "/marketing/meta")]
    rows += [(f"Звернення · {k}", 0.0, _sum(v), "/reports?r=appeals_conv") for k, v in by_src.items()]
    rows += [(k, 0.0, {**v, "revenue": round(v["revenue"], 2)}, None) for k, v in D.items()]
    other = other_costs(db, d_from, d_to)
    spend = cost["google"] + cost["meta"]
    r = Report("channels", "Зведення по каналах",
               "Звідки приходять пацієнти і скільки це коштує. Google Ads — дзвінки з колтрекінгу й звернення з міткою реклами; Meta Ads — "
               "переписки в Direct з рекламною міткою; «Звернення» — решта звернень за їхнім джерелом; «Запис напряму» — нові пацієнти без "
               "звернення, за джерелом запису першого візиту. Виручка — оплати пацієнта після дати ліда (на сьогодні). Витрати Google — з ПДВ, "
               "Meta — за курсом на день. Нові пацієнти — ті, хто вперше прийшов у клініку після цього ліда.",
               empty="За період лідів і нових пацієнтів немає.")
    r.cols = [Col("name", "Канал"), Col("cost", "Витрати", "uah", True), Col("leads", "Лідів", "int", True), Col("booked", "Записались", "int", True),
              Col("came", "Прийшли", "int", True), Col("conv", "Лід → візит", "pct"), Col("new", "Нових пацієнтів", "int", True),
              Col("revenue", "Виручка", "uah", True), Col("cpl", "Ціна ліда", "uah"), Col("cac", "Ціна нового пацієнта", "uah"),
              Col("roas", "ROAS", "num"), Col("avg", "Виручка на пацієнта", "uah")]
    T = defaultdict(float)
    for name, c, s, href in rows:
        if not (c or s["leads"]):
            continue
        r.rows.append({"name": name, "cost": round(c, 2), "leads": s["leads"], "booked": s["booked"], "came": s["came"],
                       "conv": _pct(s["came"], s["leads"]), "new": s["new"], "revenue": s["revenue"], "cpl": _div(c, s["leads"]) if c else None,
                       "cac": _div(c, s["new"]) if c else None, "roas": _div(s["revenue"], c) if c else None,
                       "avg": _div(s["revenue"], s["payers"]), "_href": href})
        for k in ("leads", "booked", "came", "new", "revenue"):
            T[k] += s[k]
    r.rows.sort(key=lambda x: (-x["cost"], -x["revenue"]))
    r.total = {"cost": round(spend, 2), **{k: (round(T[k], 2) if k == "revenue" else int(T[k])) for k in ("leads", "booked", "came", "new", "revenue")}}
    paid_rev = sum(x["revenue"] for x in r.rows if x["cost"])
    r.tiles = [("Витрати на рекламу", round(spend), None, "uah", f"Google {round(cost['google'])} ₴ · Meta {round(cost['meta'])} ₴" +
                (f" · інший маркетинг {round(other)} ₴" if other else "")),
               ("Нових пацієнтів", int(T["new"]), None, "int", f"з реклами {sum(x['new'] for x in r.rows if x['cost'])}"),
               ("Ціна нового пацієнта", _div(spend + other, T["new"]) or 0, None, "uah", "усі маркетингові витрати ÷ усі нові"),
               ("ROAS реклами", _div(paid_rev, spend) or 0, None, "num", f"виручка з реклами {round(paid_rev)} ₴")]
    r.chart = [(x["name"][:22], x["new"]) for x in sorted(r.rows, key=lambda x: -x["new"])[:10]]
    r.chart_title = "Нові пацієнти за каналами"
    return r


# ---------------- звернення: конверсія в запис ----------------

def appeals_conv(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from . import appeals as ap, google_ads as ga
    from ..models import User
    view = view if view in ("deny", "staff") else ""
    d = ap.dirs(db)
    ls = ga.funnel(db, _appeal_leads(db, d_from, d_to, set()))
    for x in ls:
        x["type"] = ap.status_type(d, x["status"])
    real = [x for x in ls if x["type"] != "other"]                 # «не пацієнти» (спам, постачальники) — поза воронкою
    r = Report("appeals_conv", "Звернення: конверсія в запис",
               "Звернення, створені в періоді: скільки дійшло до запису (статус «успіх»), до візиту й оплати. Конверсія — записані ÷ закриті "
               "(успіх + відмова); звернення в роботі в неї ще не входять. «До запису» — медіана днів від звернення до закриття записом. "
               "Звернення зі статусами «інше» (не пацієнти) не враховуються.",
               empty="За період звернень не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За джерелами"), ("staff", "За відповідальними"), ("deny", "Причини відмов")]}]
    ok = [x for x in real if x["type"] == "success"]
    fail = [x for x in real if x["type"] == "fail"]
    days = [(x["closed_at"] - x["at"]).total_seconds() / 86400 for x in ok if x["closed_at"]]
    r.tiles = [("Звернень", len(real), None, "int", f"не пацієнти: {len(ls) - len(real)}"),
               ("Конверсія в запис", _pct(len(ok), len(ok) + len(fail)) or 0, None, "pct", f"{len(ok)} записано · {len(fail)} відмов · {len(real) - len(ok) - len(fail)} в роботі"),
               ("Прийшли й оплатили", sum(1 for x in real if x["revenue"] > 0), None, "int", f"виручка {round(sum(x['revenue'] for x in real))} ₴"),
               ("До запису", round(median(days), 1) if days else 0, None, "num", "днів, медіана")]
    if view == "deny":
        G = defaultdict(lambda: defaultdict(int))
        for x in fail:
            G[x["deny"] or "Без причини"][x["source"]] += 1
        r.cols = [Col("name", "Причина відмови"), Col("n", "Відмов", "int", True), Col("share", "Частка", "pct"), Col("top", "Найчастіше з джерела")]
        for k, v in sorted(G.items(), key=lambda kv: -sum(kv[1].values())):
            n = sum(v.values())
            r.rows.append({"name": k, "n": n, "share": _pct(n, len(fail)), "top": max(v, key=v.get)})
        r.total = {"n": len(fail)}
        r.empty = "Відмов за період не було."
        return r
    users = {u.id: u.name for u in db.scalars(select(User))}
    key = (lambda x: users.get(x["responsible_id"], "Без відповідального")) if view == "staff" else (lambda x: x["source"])
    G = defaultdict(list)
    for x in real:
        G[key(x)].append(x)
    r.cols = [Col("name", "Відповідальний" if view == "staff" else "Джерело"), Col("n", "Звернень", "int", True), Col("ok", "Записано", "int", True),
              Col("fail", "Відмова", "int", True), Col("work", "В роботі", "int", True), Col("conv", "Конверсія", "pct"),
              Col("came", "Прийшли", "int", True), Col("new", "Нових пацієнтів", "int", True), Col("payers", "Оплатили", "int", True),
              Col("revenue", "Виручка", "uah", True), Col("days", "Днів до запису", "num")]
    for k, v in G.items():
        o, f = [x for x in v if x["type"] == "success"], [x for x in v if x["type"] == "fail"]
        dd = [(x["closed_at"] - x["at"]).total_seconds() / 86400 for x in o if x["closed_at"]]
        r.rows.append({"name": k, "n": len(v), "ok": len(o), "fail": len(f), "work": len(v) - len(o) - len(f), "conv": _pct(len(o), len(o) + len(f)),
                       "came": sum(1 for x in v if x["came"]), "new": sum(1 for x in v if x["new"] and x["came"]),
                       "payers": sum(1 for x in v if x["revenue"] > 0), "revenue": round(sum(x["revenue"] for x in v), 2),
                       "days": round(median(dd), 1) if dd else None})
    r.rows.sort(key=lambda x: -x["n"])
    r.chart = [(x["name"][:20], x["n"]) for x in r.rows[:12]]
    r.chart_title = "Звернень за джерелами" if not view else "Звернень за відповідальними"
    r.total = {c.key: round(sum(x.get(c.key) or 0 for x in r.rows), 2) for c in r.cols if c.total}
    return r


# ---------------- розсилки ----------------

def mailings(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    a, b = _rng(d_from, d_to)
    camps = [c for c in db.scalars(select(Campaign).where(Campaign.sent_at.is_not(None)).order_by(Campaign.sent_at.desc()))
             if a <= c.sent_at <= b or a <= (_loc(c.sent_at) or c.sent_at) <= b]
    r = Report("mailings", "Розсилки: ефективність",
               "Розсилки, надіслані в періоді. Запис / візит / оплата рахуються пацієнту, якому повідомлення надіслано, якщо вони сталися "
               "протягом вікна розсилки (днів після надсилання — задається в розсилці, типово 14). Це звʼязок за часом, а не доказ, що "
               "пацієнт прийшов саме через розсилку.",
               empty="За період розсилок не було.")
    r.cols = [Col("name", "Розсилка"), Col("at", "Надіслано", "date"), Col("total", "Адресатів", "int", True), Col("sent", "Надіслано", "int", True),
              Col("deliv", "Доставлено", "int", True), Col("read", "Прочитано", "int", True), Col("failed", "Помилок", "int", True),
              Col("booked", "Записались", "int", True), Col("conv", "Конверсія", "pct"), Col("came", "Прийшли", "int", True),
              Col("payers", "Оплатили", "int", True), Col("revenue", "Виручка", "uah", True), Col("per", "₴ на повідомлення", "uah")]
    for c in camps:
        got = [x for x in c.recipients if x.status in ("sent", "delivered", "read") and x.sent_at]
        pids = {x.patient_id for x in got}
        win = timedelta(days=c.attribution_days or 14)
        t0 = min((x.sent_at for x in got), default=c.sent_at)
        sent_at = {x.patient_id: x.sent_at for x in got}
        booked, came = set(), set()
        if pids:
            for ap_ in db.scalars(select(Appointment).where(Appointment.patient_id.in_(list(pids)), Appointment.status.notin_(LOST),
                                                            Appointment.created_at >= t0 - timedelta(hours=4))):
                s0 = sent_at[ap_.patient_id]
                made = _loc(ap_.created_at)
                if s0 <= made <= s0 + win:
                    booked.add(ap_.patient_id)
                    if ap_.status in ("done", "paid", "arrived") or ap_.sale_id:
                        came.add(ap_.patient_id)
            for v in db.scalars(select(Visit).where(Visit.patient_id.in_(list(pids)), Visit.status == "done", Visit.starts_at >= t0)):
                s0 = sent_at[v.patient_id]
                if s0 <= v.starts_at <= s0 + win:
                    booked.add(v.patient_id)
                    came.add(v.patient_id)
        rev, payers = 0.0, set()
        for pid, ps in _payments(db, pids).items():
            s0 = sent_at[pid]
            x = sum(v for dt, v in ps if s0 <= dt <= s0 + win)
            if x > 0:
                rev += x
                payers.add(pid)
        r.rows.append({"name": c.name, "at": c.sent_at, "total": len(c.recipients), "sent": len(got),
                       "deliv": sum(1 for x in got if x.status in ("delivered", "read")), "read": sum(1 for x in got if x.status == "read"),
                       "failed": sum(1 for x in c.recipients if x.status == "failed"), "booked": len(booked), "conv": _pct(len(booked), len(got)),
                       "came": len(came | payers), "payers": len(payers), "revenue": round(rev, 2), "per": _div(rev, len(got)),
                       "_href": f"/campaigns/{c.id}"})
    r.total = {c.key: round(sum(x.get(c.key) or 0 for x in r.rows), 2) for c in r.cols if c.total}
    T = r.total
    r.tiles = [("Розсилок", len(r.rows), None, "int", f"{int(T.get('sent', 0))} повідомлень"),
               ("Доставлено", _pct(T.get("deliv", 0), T.get("sent", 0)) or 0, None, "pct", f"прочитано {int(T.get('read', 0))}"),
               ("Записались після розсилки", int(T.get("booked", 0)), None, "int", f"{_pct(T.get('booked', 0), T.get('sent', 0)) or 0}% адресатів"),
               ("Виручка у вікні", round(T.get("revenue", 0)), None, "uah", f"оплатили {int(T.get('payers', 0))}")]
    return r


# ---------------- вартість залучення й окупність ----------------

def cac(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """По місяцях (12 місяців до кінця періоду): маркетингові витрати, нові пацієнти, їхня виручка за 30 / 90 / 180 днів."""
    today = date.today()
    months = []
    y, m = d_to.year, d_to.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    first = _first_all(db)
    by_m = defaultdict(list)
    for pid, d in first.items():
        by_m[(d.year, d.month)].append(pid)
    pays = _payments(db, {pid for k in months for pid in by_m.get(k, [])})
    r = Report("cac", "Вартість залучення й окупність",
               "12 місяців до кінця обраного періоду. Витрати = рекламні кабінети (Google з ПДВ, Meta) + інші маркетингові фіноперації. "
               "Нові — пацієнти з першою активністю в клініці цього місяця (усі канали, не лише реклама). Виручка за 30 / 90 / 180 днів — "
               "оплати цих пацієнтів від першого візиту. Окупність = виручка за 90 днів ÷ витрати; для свіжих місяців вона ще росте.",
               empty="Даних немає.")
    r.cols = [Col("name", "Місяць"), Col("ads", "Реклама", "uah", True), Col("other", "Інший маркетинг", "uah", True), Col("cost", "Витрати разом", "uah", True),
              Col("new", "Нових пацієнтів", "int", True), Col("cac", "Ціна нового пацієнта", "uah"), Col("r30", "Виручка 30 днів", "uah", True),
              Col("r90", "Виручка 90 днів", "uah", True), Col("r180", "Виручка 180 днів", "uah", True), Col("ltv90", "На пацієнта за 90 днів", "uah"),
              Col("pay", "Окупність за 90 днів", "num"), Col("back", "Повернулись удруге", "pct")]
    for (yy, mm) in months:
        a = date(yy, mm, 1)
        b = (date(yy + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1))
        if a > today:
            continue
        c = ad_costs(db, a, b)
        ads, oth = c["google"] + c["meta"], other_costs(db, a, b)
        pids = by_m.get((yy, mm), [])
        rv = {30: 0.0, 90: 0.0, 180: 0.0}
        back = 0
        for pid in pids:
            f0 = first[pid]
            ps = [(dt.date(), v) for dt, v in pays.get(pid, []) if dt.date() >= f0]
            for n in rv:
                rv[n] += sum(v for d_, v in ps if d_ <= f0 + timedelta(days=n))
            back += len({d_ for d_, _ in ps}) > 1
        cost = ads + oth
        r.rows.append({"name": f"{yy}-{mm:02d}", "ads": round(ads), "other": round(oth), "cost": round(cost), "new": len(pids),
                       "cac": _div(cost, len(pids)) if cost else None, "r30": round(rv[30]), "r90": round(rv[90]), "r180": round(rv[180]),
                       "ltv90": _div(rv[90], len(pids)), "pay": _div(rv[90], cost) if cost else None, "back": _pct(back, len(pids))})
    r.total = {c.key: sum(x.get(c.key) or 0 for x in r.rows) for c in r.cols if c.total}
    T = r.total
    r.tiles = [("Витрати за 12 міс.", T.get("cost", 0), None, "uah", f"реклама {T.get('ads', 0)} ₴"),
               ("Нових пацієнтів", T.get("new", 0), None, "int", ""),
               ("Ціна нового пацієнта", _div(T.get("cost", 0), T.get("new", 0)) or 0, None, "uah", "у середньому за 12 міс."),
               ("Окупність за 90 днів", _div(T.get("r90", 0), T.get("cost", 0)) or 0, None, "num", "виручка нових ÷ витрати")]
    r.chart = [(x["name"][2:], x["new"]) for x in r.rows]
    r.chart_title = "Нові пацієнти за місяцями"
    return r


BUILDERS = {"channels": channels, "appeals_conv": appeals_conv, "mailings": mailings, "cac": cac}
