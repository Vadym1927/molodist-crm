"""Google Ads: центр моніторингу реклами.

Дані кабінету надсилає скрипт Google Ads (Інструменти → Масові дії → Скрипти) — щогодини POST на вебхук CRM:
кампанії, групи, ключові слова, пошукові запити, пристрої, години — по днях. Developer-token і OAuth не потрібні.
Ліди з реклами — дзвінки з колтрекінгу (utm / gclid з Ringostat або окремий номер лінії) і звернення з utm.
Далі воронка по кожному ліду: пацієнт → запис → візит → оплати; показники CPL, CAC, ROAS, ROI.
Баланс кабінету: якщо Google його не віддає (передоплата) — рахуємо самі: внесений залишок + поповнення − витрати;
коли грошей лишається менше ніж на N днів — у платіжному календарі зʼявляється «Поповнення Google Ads»."""
from __future__ import annotations

import json
import secrets as _secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AdsStat, AdsTopup, Appeal, Appointment, BankTxn, Call, Check, PayItem, Patient, Sale, SyncState, Visit
from . import appeals as ap

KEY = "google_ads"
SRC = "google"
DEFAULT = {"hook": "", "account": {}, "last_push": None, "vat_pct": 20.0, "margin_pct": 60.0, "alert_days": 5, "alert_amount": 0.0,
           "topup_amount": 10000.0, "cashbox": "", "article": "Маркетинг", "balance_base": None,      # {"day": iso, "amount": float}
           "lines": [], "appeal_sources": ["Google Ads"], "real_balance": None, "bank_words": ["google"],
           "offline_on": False, "offline_name": "CRM — оплачений візит", "offline_sent": []}
LEVELS = ("account", "campaign", "adgroup", "keyword", "term", "device", "hour")


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    out = {**DEFAULT, **d}
    if not out["hook"]:
        out["hook"] = _secrets.token_urlsafe(24)
        save_cfg(db, out)
    return out


def save_cfg(db: Session, d: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(d, ensure_ascii=False, default=str), updated_at=datetime.utcnow()))
    db.commit()


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- прийом даних зі скрипта ----------------
def ingest(db: Session, payload: dict) -> dict:
    """payload: {"account": {...}, "rows": [[level, day, key, name, parent, status, clicks, impr, cost, conv, value, extra?], …],
    "balance": {"limit": …, "served": …}?}. Повторне надсилання оновлює рядки (унікальність: рівень + день + ключ)."""
    c = cfg(db)
    rows = payload.get("rows") or []
    n_new = n_upd = 0
    by_key: dict = {}
    days = {str(r[1])[:10] for r in rows if isinstance(r, list) and len(r) >= 11}
    levels = {str(r[0]) for r in rows if isinstance(r, list) and len(r) >= 11}
    if days:
        dd = [date.fromisoformat(x) for x in days]
        for s_ in db.scalars(select(AdsStat).where(AdsStat.source == SRC, AdsStat.level.in_(list(levels)), AdsStat.day >= min(dd), AdsStat.day <= max(dd))):
            by_key[(s_.level, s_.day, s_.key)] = s_
    for r in rows:
        if not isinstance(r, list) or len(r) < 11 or r[0] not in LEVELS:
            continue
        try:
            day = date.fromisoformat(str(r[1])[:10])
        except ValueError:
            continue
        key = str(r[2] or "")[:200]
        s_ = by_key.get((r[0], day, key))
        if s_ is None:
            s_ = AdsStat(source=SRC, level=r[0], day=day, key=key)
            db.add(s_)
            by_key[(r[0], day, key)] = s_
            n_new += 1
        else:
            n_upd += 1
        s_.name, s_.parent, s_.status = str(r[3] or "")[:400] or None, str(r[4] or "")[:400] or None, str(r[5] or "")[:30] or None
        s_.clicks, s_.impressions, s_.cost, s_.conversions, s_.conv_value = _f(r[6]), _f(r[7]), round(_f(r[8]), 2), _f(r[9]), round(_f(r[10]), 2)
        s_.extra = r[11] if len(r) > 11 and isinstance(r[11], dict) else None
        s_.updated_at = datetime.utcnow()
    if isinstance(payload.get("account"), dict) and payload["account"]:
        c["account"] = {k: str(v)[:120] for k, v in payload["account"].items()}
    b = payload.get("balance")
    if isinstance(b, dict) and b.get("limit") is not None:
        c["real_balance"] = {"amount": round(_f(b.get("limit")) - _f(b.get("served")), 2), "at": datetime.now().isoformat(timespec="minutes")}
    c["last_push"] = datetime.now().isoformat(timespec="minutes")
    save_cfg(db, c)
    db.commit()
    refresh(db)
    return {"ok": True, "new": n_new, "updated": n_upd}


# ---------------- баланс ----------------
def bank_topups(db: Session, c: dict | None = None) -> int:
    """Платежі з виписки банку на Google → поповнення (зараховано = сума без ПДВ)."""
    c = c or cfg(db)
    words = [w.lower() for w in c.get("bank_words") or [] if w]
    if not words:
        return 0
    have = {t.ref for t in db.scalars(select(AdsTopup).where(AdsTopup.source == SRC, AdsTopup.ref.is_not(None)))}
    n = 0
    since = datetime.now() - timedelta(days=120)
    for t in db.scalars(select(BankTxn).where(BankTxn.amount < 0, BankTxn.at >= since)):
        text = f"{t.counter_name or ''} {t.description or ''}".lower()
        if not any(w in text for w in words) or f"bank:{t.id}" in have:
            continue
        amt = abs(float(t.amount))
        db.add(AdsTopup(source=SRC, day=t.at.date(), amount=amt, credited=round(amt / (1 + _f(c["vat_pct"]) / 100), 2), origin="bank",
                        ref=f"bank:{t.id}", note=(t.counter_name or t.description or "")[:300]))
        n += 1
    db.commit()
    return n


def add_topup(db: Session, day: date, amount: float, note: str = "", c: dict | None = None) -> AdsTopup:
    c = c or cfg(db)
    t = AdsTopup(source=SRC, day=day, amount=round(amount, 2), credited=round(amount / (1 + _f(c["vat_pct"]) / 100), 2), origin="manual", note=note[:300] or None)
    db.add(t)
    db.commit()
    return t


def daily_cost(db: Session, d_from: date, d_to: date) -> dict[date, float]:
    out: dict = defaultdict(float)
    for s_ in db.scalars(select(AdsStat).where(AdsStat.source == SRC, AdsStat.level == "campaign", AdsStat.day >= d_from, AdsStat.day <= d_to)):
        out[s_.day] += s_.cost
    return out


def balance(db: Session, c: dict | None = None, today: date | None = None) -> dict:
    c = c or cfg(db)
    today = today or date.today()
    week = daily_cost(db, today - timedelta(days=7), today - timedelta(days=1))
    avg = round(sum(week.values()) / 7, 2)
    base = c.get("balance_base")
    real = c.get("real_balance")
    out = {"known": False, "amount": None, "avg_day": avg, "days_left": None, "source": None, "topups": [], "spent": 0.0, "base": base}
    if real and real.get("amount") is not None:
        out.update(known=True, amount=round(_f(real["amount"]), 2), source="google", at=real.get("at"))
    elif base and base.get("day"):
        b0 = date.fromisoformat(base["day"])
        tops = db.scalars(select(AdsTopup).where(AdsTopup.source == SRC, AdsTopup.day >= b0).order_by(AdsTopup.day.desc())).all()
        spent = round(sum(daily_cost(db, b0, today).values()), 2)
        out.update(known=True, amount=round(_f(base.get("amount")) + sum(t.credited for t in tops) - spent, 2), source="calc", topups=tops, spent=spent)
    if out["known"] and avg > 0:
        out["days_left"] = round(max(out["amount"], 0) / avg, 1)
    out["low"] = bool(out["known"] and ((out["days_left"] is not None and out["days_left"] <= _f(c["alert_days"])) or out["amount"] <= _f(c["alert_amount"])))
    out["until"] = (today + timedelta(days=int(out["days_left"]))) if out["days_left"] is not None else None
    return out


def refresh(db: Session, today: date | None = None) -> dict:
    """Поповнення з банку й з платіжного календаря → баланс; мало грошей → платіж «Поповнення Google Ads» у платіжному календарі."""
    c = cfg(db)
    today = today or date.today()
    bank_topups(db, c)
    for it in db.scalars(select(PayItem).where(PayItem.ref.like("gads:topup%"), PayItem.status == "paid")):   # оплатили з календаря → поповнення
        ref = f"payitem:{it.id}"
        amt = float(it.amount_fact or it.amount or 0)
        day = it.paid_at or it.due
        if not amt or db.scalar(select(AdsTopup).where(AdsTopup.ref == ref)):
            continue
        near = db.scalars(select(AdsTopup).where(AdsTopup.source == SRC, AdsTopup.day >= day - timedelta(days=3), AdsTopup.day <= day + timedelta(days=3))).all()
        if any(abs(t.amount - amt) < 1 for t in near):          # той самий платіж уже прийшов з банку
            continue
        db.add(AdsTopup(source=SRC, day=day, amount=amt, credited=round(amt / (1 + _f(c["vat_pct"]) / 100), 2), origin="paycal", ref=ref, note="з платіжного календаря"))
    db.commit()
    try:                                               # GA4: свіжі кліки по номеру → мітки дзвінків
        from . import ga4 as _ga4
        from . import site_leads as _sl                # кліки з тегу (gclid) — точніше за GA4, тому першими
        _sl.match_clicks(db, today - timedelta(days=3), today)
        _ga4.refresh(db)
    except Exception:      # noqa: BLE001
        pass
    b = balance(db, c, today)
    made = None
    if b["low"] and _f(c["topup_amount"]) > 0:
        open_ = db.scalar(select(PayItem).where(PayItem.ref.like("gads:topup%"), PayItem.status.in_(("planned", "approved"))))
        if not open_:
            due = today if (b["days_left"] or 0) <= 2 else today + timedelta(days=max(int(b["days_left"]) - 2, 0))
            made = PayItem(due=due, name="Поповнення Google Ads", group="marketing", article=c.get("article") or None, cashbox=c.get("cashbox") or None,
                           counterparty="Google Ads", amount=round(_f(c["topup_amount"]), 2), source="manual", ref=f"gads:topup:{today.isoformat()}",
                           note=f"Баланс ≈ {b['amount']:.0f}, вистачить на {b['days_left'] if b['days_left'] is not None else '?'} дн. (середня витрата {b['avg_day']:.0f}/день)")
            db.add(made)
            db.commit()
    return {"balance": b, "pay_item": made.id if made else None}


# ---------------- ліди з реклами ----------------
def _is_google(source: str | None, medium: str | None, gclid: str | None) -> bool:
    s, m = (source or "").lower(), (medium or "").lower()
    return bool(gclid) or ("google" in s and (not m or any(x in m for x in ("cpc", "ppc", "paid", "ads"))) and "organic" not in m) or "adwords" in s


def leads(db: Session, d_from: date, d_to: date, c: dict | None = None) -> list[dict]:
    """Унікальні ліди з Google Ads за період (перший дотик за телефоном): дзвінки з колтрекінгу + звернення з utm."""
    c = c or cfg(db)
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    lines = {ap.phone_key(x) for x in c.get("lines") or [] if x}
    srcs = {s.strip().lower() for s in c.get("appeal_sources") or [] if s.strip()}
    raw = []
    for cl in db.scalars(select(Call).where(Call.at >= a, Call.at <= b, Call.direction == "in").order_by(Call.at)):
        if _is_google(cl.utm_source, cl.utm_medium, cl.gclid) or (lines and ap.phone_key(cl.line) in lines):
            raw.append({"at": cl.at, "phone": cl.phone, "kind": "call", "campaign": cl.utm_campaign, "term": cl.utm_term, "status": cl.status, "attr": cl.attr, "gclid": cl.gclid,
                        "patient_id": cl.patient_id, "call_id": cl.id, "appeal_id": cl.appeal_id, "duration": cl.duration, "landing": cl.landing})
    for x in db.scalars(select(Appeal).where(Appeal.created_at >= a, Appeal.created_at <= b, Appeal.deleted_at.is_(None)).order_by(Appeal.created_at)):
        u = (x.utm or "").lower()
        if (x.source or "").strip().lower() in srcs or ("google" in u and ("cpc" in u or "gclid" in u or "ppc" in u)) or "gclid=" in u:
            camp = next((p.split("=", 1)[1] for p in (x.utm or "").replace("?", "&").split("&") if p.lower().startswith("utm_campaign=")), None)
            term = next((p.split("=", 1)[1] for p in (x.utm or "").replace("?", "&").split("&") if p.lower().startswith("utm_term=")), None)
            q = dict(parse_qsl((x.utm or "").replace("?", "&")))
            attr = q.get("attr") or None
            raw.append({"at": x.created_at, "phone": x.phone, "kind": "appeal", "campaign": camp, "term": term, "status": x.status, "attr": attr, "source": x.source,
                        "gclid": q.get("gclid") or None,
                        "patient_id": x.patient_id, "call_id": None, "appeal_id": x.id, "duration": None, "landing": None})
    raw.sort(key=lambda r: r["at"])
    out, seen = [], {}
    for r in raw:
        k = ap.phone_key(r["phone"]) or f"id{r['call_id'] or r['appeal_id']}"
        if k in seen:
            first = seen[k]
            first["touches"] += 1
            if r["kind"] == "call" and r["status"] == "answered":
                first["answered"] = True
            first["campaign"] = first["campaign"] or r["campaign"]
            first["term"] = first["term"] or r["term"]
            first["appeal_id"] = first["appeal_id"] or r["appeal_id"]
            first["gclid"] = first.get("gclid") or r.get("gclid")
            continue
        r["touches"], r["answered"] = 1, (r["kind"] != "call" or r["status"] == "answered")
        seen[k] = r
        out.append(r)
    return out


def funnel(db: Session, ls: list[dict], today: date | None = None) -> list[dict]:
    """По кожному ліду: пацієнт, чи новий, запис після звернення, візит, оплати (усе після дати ліда)."""
    from . import doctor_kpi as dk
    today = today or date.today()
    by_phone = {}
    for p in db.scalars(select(Patient).where(Patient.phone.is_not(None))):
        by_phone.setdefault(ap.phone_key(p.phone), p)
    for r in ls:
        p = db.get(Patient, r["patient_id"]) if r.get("patient_id") else by_phone.get(ap.phone_key(r["phone"]))
        r["patient"] = p
        r["patient_id"] = p.id if p else None
    pids = {r["patient_id"] for r in ls if r["patient_id"]}
    first = dk._first_activity(db, pids)
    appts, visits, checks, sales = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    if pids:
        for x in db.scalars(select(Appointment).where(Appointment.patient_id.in_(list(pids)), Appointment.status.notin_(("cancelled", "no_show")))):
            appts[x.patient_id].append(x)
        for x in db.scalars(select(Visit).where(Visit.patient_id.in_(list(pids)), Visit.status != "cancelled")):
            visits[x.patient_id].append(x)
        for x in db.scalars(select(Check).where(Check.patient_id.in_(list(pids)), Check.deleted.is_(False))):
            checks[x.patient_id].append(x)
        for x in db.scalars(select(Sale).where(Sale.patient_id.in_(list(pids)), Sale.status == "paid")):
            sales[x.patient_id].append(x)
    for r in ls:
        pid, t0 = r["patient_id"], r["at"]
        d0 = t0.date()
        r.update(new=False, booked=None, came=None, revenue=0.0, revenue30=0.0, n_pay=0)
        if not pid:
            continue
        fa = first.get(pid)
        r["new"] = bool(fa is None or fa >= d0 - timedelta(days=1))
        bk = sorted([x.start for x in appts[pid] if x.start >= t0 - timedelta(hours=1)] +
                    [x.starts_at for x in visits[pid] if x.starts_at >= t0 - timedelta(hours=1)])
        r["booked"] = bk[0] if bk else None
        done = sorted([x.start for x in appts[pid] if x.status in ("done", "paid", "arrived") and x.start >= t0 - timedelta(hours=1)] +
                      [x.starts_at for x in visits[pid] if x.status == "done" and x.starts_at >= t0 - timedelta(hours=1)])
        r["came"] = done[0] if done else None
        pays = [(x.date, float(x.amount or 0)) for x in checks[pid] if x.date.date() >= d0] + [(x.at, float(x.total or 0)) for x in sales[pid] if x.at.date() >= d0]
        r["revenue"] = round(sum(v for _, v in pays), 2)
        r["revenue30"] = round(sum(v for t, v in pays if t.date() <= d0 + timedelta(days=30)), 2)
        r["n_pay"] = len(pays)
        if pays and not r["came"]:                 # оплата є, а візит у базі не закритий — пацієнт був
            r["came"] = min(t for t, _ in pays)
        if r["came"] and not r["booked"]:
            r["booked"] = r["came"]
    return ls


EV_LABEL = {"phone_click": "клік по номеру", "click_telegram": "Telegram", "click_viber": "Viber", "click_booking": "запис онлайн",
            "generate_lead": "форма заявки", "click_whatsapp": "WhatsApp", "click_instagram": "Instagram"}


def _agg(rows) -> dict:
    t = {"clicks": 0.0, "impressions": 0.0, "cost": 0.0, "conversions": 0.0, "conv_value": 0.0}
    for s_ in rows:
        for k in t:
            t[k] += getattr(s_, k) or 0
    return t


def _ratios(t: dict, vat: float, margin: float) -> dict:
    cash = t["cost"] * (1 + vat / 100)
    t["cost"] = round(t["cost"], 2)
    t["cash"] = round(cash, 2)
    t["ctr"] = round(100 * t["clicks"] / t["impressions"], 2) if t["impressions"] else None
    t["cpc"] = round(t["cost"] / t["clicks"], 2) if t["clicks"] else None
    n = t.get("leads", 0)
    t["cpl"] = round(cash / n, 2) if n else None
    t["cpa"] = round(cash / t["conversions"], 2) if t.get("conversions") else None
    t["cr_click_lead"] = round(100 * n / t["clicks"], 2) if t["clicks"] else None
    t["cr_lead_book"] = round(100 * t.get("booked", 0) / n, 1) if n else None
    t["cr_lead_came"] = round(100 * t.get("came", 0) / n, 1) if n else None
    t["cac"] = round(cash / t["new_patients"], 2) if t.get("new_patients") else None
    t["cost_per_visit"] = round(cash / t["came"], 2) if t.get("came") else None
    t["cost_per_booking"] = round(cash / t["booked"], 2) if t.get("booked") else None
    rev = t.get("revenue", 0.0)
    t["roas"] = round(rev / cash, 2) if cash else None
    t["roi"] = round(100 * (rev - cash) / cash, 1) if cash else None
    t["roi_margin"] = round(100 * (rev * margin / 100 - cash) / cash, 1) if cash else None
    t["avg_check"] = round(rev / t["payers"], 2) if t.get("payers") else None
    return t


def _lead_sums(ls: list[dict]) -> dict:
    return {"leads": len(ls), "calls": sum(1 for r in ls if r["kind"] == "call"), "missed": sum(1 for r in ls if not r["answered"]),
            "booked": sum(1 for r in ls if r["booked"]), "came": sum(1 for r in ls if r["came"]),
            "new_patients": sum(1 for r in ls if r["new"] and r["came"]), "payers": sum(1 for r in ls if r["revenue"] > 0),
            "revenue": round(sum(r["revenue"] for r in ls), 2), "revenue30": round(sum(r["revenue30"] for r in ls), 2)}


def dashboard(db: Session, d_from: date, d_to: date, today: date | None = None) -> dict:
    c = cfg(db)
    vat, margin = _f(c["vat_pct"]), _f(c["margin_pct"])
    stats = db.scalars(select(AdsStat).where(AdsStat.source == SRC, AdsStat.day >= d_from, AdsStat.day <= d_to)).all()
    camp_rows = [s_ for s_ in stats if s_.level == "campaign"]
    ls = funnel(db, leads(db, d_from, d_to, c), today)
    tot = _ratios({**_agg(camp_rows), **_lead_sums(ls)}, vat, margin)
    from . import ga4 as _ga4
    gsum = _ga4.summary(db, d_from, d_to)
    gcfg = _ga4.cfg(db)
    tot["ga_clicks"], tot["ga_all"], tot["ga_other"], tot["ga_events"] = gsum["paid"], gsum["total"], gsum["paid_other"], gsum["by_event"]
    _cash = tot["cash"]
    tot["ga_cpa"] = round(_cash / (gsum["paid"] + gsum["paid_other"]), 2) if (gsum["paid"] + gsum["paid_other"]) else None
    tot["probable"] = sum(1 for r in ls if r.get("attr") == "ga4?")
    # кампанії
    camps: dict = {}
    for s_ in camp_rows:
        k = camps.setdefault(s_.key, {"key": s_.key, "name": s_.name or s_.key, "status": s_.status, "rows": [], "budget": None, "type": None})
        k["rows"].append(s_)
        k["name"], k["status"] = s_.name or k["name"], s_.status or k["status"]
        if s_.extra:
            k["budget"], k["type"] = s_.extra.get("budget") or k["budget"], s_.extra.get("type") or k["type"]
    idx = {}
    for k in camps.values():
        idx[(k["name"] or "").strip().lower()] = k["key"]
        idx[k["key"]] = k["key"]
    by_c: dict = defaultdict(list)
    for r in ls:
        by_c[idx.get((r["campaign"] or "").strip().lower(), "")].append(r)
    camp_list = []
    for key, k in camps.items():
        camp_list.append({"key": key, "name": k["name"], "status": k["status"], "budget": k["budget"], "type": k["type"],
                          "ga_clicks": gsum["by_campaign"].get((k["name"] or "").strip().lower(), 0),
                          "ga_other": gsum["by_campaign_other"].get((k["name"] or "").strip().lower(), 0),
                          "ga_events": gsum["by_campaign_event"].get((k["name"] or "").strip().lower(), {}),
                          **_ratios({**_agg(k["rows"]), **_lead_sums(by_c.get(key, []))}, vat, margin)})
    camp_list.sort(key=lambda x: -x["cost"])
    unmatched = by_c.get("", [])
    if unmatched:
        camp_list.append({"key": "", "name": "Кампанію не визначено (немає utm_campaign)", "status": None, "budget": None, "type": None,
                          **_ratios({"clicks": 0.0, "impressions": 0.0, "cost": 0.0, "conversions": 0.0, "conv_value": 0.0, **_lead_sums(unmatched)}, vat, margin)})
    # по днях
    days = []
    cost_d, click_d = defaultdict(float), defaultdict(float)
    for s_ in camp_rows:
        cost_d[s_.day] += s_.cost
        click_d[s_.day] += s_.clicks
    lead_d = defaultdict(int)
    for r in ls:
        lead_d[r["at"].date()] += 1
    d = d_from
    while d <= d_to:
        days.append({"day": d, "cost": round(cost_d.get(d, 0), 2), "clicks": int(click_d.get(d, 0)), "leads": lead_d.get(d, 0)})
        d += timedelta(days=1)

    def top(level, n=25):
        g: dict = {}
        for s_ in stats:
            if s_.level != level:
                continue
            x = g.setdefault(s_.key, {"name": s_.name or s_.key, "parent": s_.parent, "clicks": 0.0, "impressions": 0.0, "cost": 0.0, "conversions": 0.0, "extra": s_.extra})
            for f_ in ("clicks", "impressions", "cost", "conversions"):
                x[f_] += getattr(s_, f_) or 0
        out = sorted(g.values(), key=lambda x: -x["cost"])[:n]
        for x in out:
            x["cost"] = round(x["cost"], 2)
            x["ctr"] = round(100 * x["clicks"] / x["impressions"], 2) if x["impressions"] else None
            x["cpc"] = round(x["cost"] / x["clicks"], 2) if x["clicks"] else None
        return out
    term_leads = defaultdict(int)
    for r in ls:
        if r["term"]:
            term_leads[r["term"].strip().lower()] += 1
    kws = top("keyword")
    for x in kws:
        x["leads"] = term_leads.get((x["name"] or "").strip().lower().strip('[]"+'), 0)
    hours = [0.0] * 24
    for s_ in stats:
        if s_.level == "hour":
            try:
                hours[int(s_.key.split("|")[-1])] += s_.cost
            except (ValueError, IndexError):
                pass
    lead_h = [0] * 24
    for r in ls:
        lead_h[r["at"].hour] += 1
    last = db.scalar(select(AdsStat.day).where(AdsStat.source == SRC).order_by(AdsStat.day.desc()).limit(1))
    from ..models import GaEvent
    ga_paid = [e for e in db.scalars(select(GaEvent).where(GaEvent.at >= datetime.combine(d_from, datetime.min.time()),
                                                          GaEvent.at <= datetime.combine(d_to, datetime.max.time())).order_by(GaEvent.at.desc()))
               if "google" in (e.source or "").lower() and (e.medium or "").lower() in ("cpc", "ppc", "paid")][:300]
    return {"cfg": c, "tot": tot, "camps": camp_list, "days": days, "leads": sorted(ls, key=lambda r: r["at"], reverse=True),
            "keywords": kws, "terms": top("term"), "ga_paid": ga_paid, "ev_label": EV_LABEL, "devices": top("device", 6), "hours": hours, "lead_hours": lead_h,
            "balance": balance(db, c, today), "connected": bool(c.get("last_push")), "last_day": last,
            "has_utm": any(r["campaign"] for r in ls), "d_from": d_from, "d_to": d_to,
            "ga4": {"on": bool(gcfg.get("property") and (gcfg.get("events") or gcfg.get("lead_events"))), "last_sync": gcfg.get("last_sync"), "error": gcfg.get("last_error")},
            "pay_item": db.scalar(select(PayItem).where(PayItem.ref.like("gads:topup%"), PayItem.status.in_(("planned", "approved"))))}


# ---------------- офлайн-конверсії назад у Google Ads ----------------
def _tz(dt: datetime) -> str:
    off = datetime.now() - datetime.utcnow()
    mins = round(off.total_seconds() / 60)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + ("+" if mins >= 0 else "-") + f"{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def offline_rows(db: Session, today: date | None = None) -> list[dict]:
    """Ліди з gclid, які прийшли й заплатили → конверсія «CRM — оплачений візит» з цінністю = оплати за 30 днів від звернення
    (надсилаємо, коли минув день візиту; один раз на gclid). Google навчається на реальних грошах, а не на кліках."""
    c = cfg(db)
    if not c.get("offline_on"):
        return []
    today = today or date.today()
    sent = set(c.get("offline_sent") or [])
    ls = funnel(db, leads(db, today - timedelta(days=88), today, c), today)
    out = []
    for r in ls:
        g = r.get("gclid")
        if not g or not r.get("came") or r["revenue30"] <= 0 or g in sent:
            continue
        came = r["came"] if isinstance(r["came"], datetime) else datetime.combine(r["came"], datetime.min.time()).replace(hour=12)
        if came.date() >= today:                             # чекаємо, поки закриється день візиту
            continue
        out.append({"key": g, "gclid": g, "name": c.get("offline_name") or DEFAULT["offline_name"], "time": _tz(max(came, r["at"] + timedelta(minutes=1))),
                    "value": round(r["revenue30"], 2), "currency": (c.get("account") or {}).get("currency") or "UAH"})
    return out[:500]


def offline_ack(db: Session, keys: list) -> int:
    c = cfg(db)
    sent = list(dict.fromkeys((c.get("offline_sent") or []) + [str(k)[:200] for k in keys or [] if k]))
    c["offline_sent"] = sent[-5000:]
    c["offline_last"] = {"at": datetime.now().isoformat(timespec="minutes"), "n": len(keys or [])}
    save_cfg(db, c)
    return len(keys or [])


# ---------------- скрипт для кабінету Google Ads ----------------
def script(hook_url: str) -> str:
    return SCRIPT.replace("__HOOK__", hook_url)


def script_body(hook_url: str) -> str:
    """Актуальна логіка для завантажувача: та сама, що й повний скрипт, але головна функція зветься run()."""
    return script(hook_url).replace("function main() {", "function run() {", 1)


LOADER = r"""/**
 * MOLODIST CRM — завантажувач. Вставляється в Google Ads один раз: актуальну логіку (статистика, погоджені зміни,
 * офлайн-конверсії) щоразу бере з CRM, тож після оновлень CRM скрипт міняти не треба. Розклад: щогодини.
 */
var HOOK = '__HOOK__';
function main() {
  var res = UrlFetchApp.fetch(HOOK + '/script', { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error('CRM не віддала скрипт: ' + res.getResponseCode());
  eval(res.getContentText());
  run();
}
"""


def loader(hook_url: str) -> str:
    return LOADER.replace("__HOOK__", hook_url)


SCRIPT = r"""/**
 * MOLODIST CRM — вивантаження статистики Google Ads у CRM.
 * Встановлення: Google Ads → Інструменти → Масові дії → Скрипти → «+» → вставити цей код → Авторизувати → Зберегти.
 * Розклад: щогодини. Перший запуск вивантажує історію з початку року.
 * Версія 2: також застосовує зміни, погоджені власником у CRM / Telegram (мінус-слова, паузи, бюджети, ключі, оголошення).
 */
var HOOK = '__HOOK__';
var DAYS_DETAIL = 30;   // групи, ключові слова, запити, пристрої, години — за останні N днів

function main() {
  var tz = AdsApp.currentAccount().getTimeZone();
  var today = new Date();
  var fmt = function (d) { return Utilities.formatDate(d, tz, 'yyyy-MM-dd'); };
  var yearStart = fmt(new Date(today.getFullYear(), 0, 1));
  var detailStart = fmt(new Date(today.getTime() - DAYS_DETAIL * 86400000));
  var end = fmt(today);
  var acc = AdsApp.currentAccount();
  var account = { id: acc.getCustomerId(), name: acc.getName(), currency: acc.getCurrencyCode(), tz: tz };
  var rows = [];
  var m = 'metrics.clicks, metrics.impressions, metrics.cost_micros, metrics.conversions, metrics.conversions_value';
  var num = function (r) { return [Number(r.metrics.clicks || 0), Number(r.metrics.impressions || 0), Number(r.metrics.costMicros || 0) / 1e6,
                                   Number(r.metrics.conversions || 0), Number(r.metrics.conversionsValue || 0)]; };
  var run = function (q, fn) { try { var it = AdsApp.search(q); while (it.hasNext()) { fn(it.next()); } } catch (e) { Logger.log('skip: ' + e); } };

  run('SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.amount_micros, segments.date, ' + m +
      " FROM campaign WHERE segments.date BETWEEN '" + yearStart + "' AND '" + end + "' AND metrics.impressions > 0", function (r) {
    rows.push(['campaign', r.segments.date, String(r.campaign.id), r.campaign.name, '', r.campaign.status].concat(num(r),
      [{ type: r.campaign.advertisingChannelType, budget: Number((r.campaignBudget || {}).amountMicros || 0) / 1e6 }]));
  });
  run('SELECT ad_group.id, ad_group.name, ad_group.status, campaign.name, segments.date, ' + m +
      " FROM ad_group WHERE segments.date BETWEEN '" + detailStart + "' AND '" + end + "' AND metrics.impressions > 0", function (r) {
    rows.push(['adgroup', r.segments.date, String(r.adGroup.id), r.adGroup.name, r.campaign.name, r.adGroup.status].concat(num(r)));
  });
  run('SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group.id, campaign.name, segments.date, ' + m +
      " FROM keyword_view WHERE segments.date BETWEEN '" + detailStart + "' AND '" + end + "' AND metrics.impressions > 0", function (r) {
    var k = r.adGroupCriterion.keyword || {};
    rows.push(['keyword', r.segments.date, r.adGroup.id + '~' + r.adGroupCriterion.criterionId, k.text, r.campaign.name, k.matchType].concat(num(r)));
  });
  run('SELECT search_term_view.search_term, campaign.name, segments.date, ' + m +
      " FROM search_term_view WHERE segments.date BETWEEN '" + detailStart + "' AND '" + end + "' AND metrics.clicks > 0", function (r) {
    rows.push(['term', r.segments.date, String(r.searchTermView.searchTerm).substring(0, 190), r.searchTermView.searchTerm, r.campaign.name, ''].concat(num(r)));
  });
  var dev = {}, hrs = {};
  run('SELECT segments.device, segments.date, ' + m + " FROM campaign WHERE segments.date BETWEEN '" + detailStart + "' AND '" + end + "' AND metrics.impressions > 0", function (r) {
    var k = r.segments.date + '|' + r.segments.device, n = num(r); dev[k] = dev[k] || [0, 0, 0, 0, 0]; for (var i = 0; i < 5; i++) dev[k][i] += n[i];
  });
  run('SELECT segments.hour, segments.date, ' + m + " FROM campaign WHERE segments.date BETWEEN '" + detailStart + "' AND '" + end + "' AND metrics.impressions > 0", function (r) {
    var k = r.segments.date + '|' + r.segments.hour, n = num(r); hrs[k] = hrs[k] || [0, 0, 0, 0, 0]; for (var i = 0; i < 5; i++) hrs[k][i] += n[i];
  });
  Object.keys(dev).forEach(function (k) { var p = k.split('|'); rows.push(['device', p[0], p[1], p[1], '', ''].concat(dev[k])); });
  Object.keys(hrs).forEach(function (k) { var p = k.split('|'); rows.push(['hour', p[0], 'h|' + p[1], p[1] + ':00', '', ''].concat(hrs[k])); });

  var balance = null;   // баланс доступний лише для кабінетів з лімітом рахунку (оплата за рахунками)
  run('SELECT account_budget.approved_spending_limit_micros, account_budget.amount_served_micros, account_budget.status FROM account_budget', function (r) {
    var b = r.accountBudget || {};
    if (b.status === 'APPROVED' && b.approvedSpendingLimitMicros) { balance = { limit: Number(b.approvedSpendingLimitMicros) / 1e6, served: Number(b.amountServedMicros || 0) / 1e6 }; }
  });

  var CH = 1500, sent = 0;
  for (var i = 0; i < Math.max(rows.length, 1); i += CH) {
    var body = { account: account, rows: rows.slice(i, i + CH) };
    if (i === 0 && balance) body.balance = balance;
    var res = UrlFetchApp.fetch(HOOK, { method: 'post', contentType: 'application/json', payload: JSON.stringify(body), muteHttpExceptions: true });
    if (res.getResponseCode() >= 300) throw new Error('CRM відповіла ' + res.getResponseCode() + ': ' + res.getContentText().substring(0, 200));
    sent += body.rows.length;
  }
  Logger.log('Надіслано рядків: ' + sent);
  applyQueue();
  uploadOffline();
}

/* Офлайн-конверсії з CRM: gclid пацієнтів, які прийшли й заплатили, з цінністю = оплати. Дія-конверсія з такою назвою
   має бути створена в кабінеті («Цілі → Конверсії → Імпорт → Кліки / CRM»). */
function uploadOffline() {
  var res = UrlFetchApp.fetch(HOOK + '/offline', { muteHttpExceptions: true });
  if (res.getResponseCode() >= 300) return;
  var rows = (JSON.parse(res.getContentText()).rows) || [];
  if (!rows.length) return;
  var cols = ['Google Click ID', 'Conversion Name', 'Conversion Time', 'Conversion Value', 'Conversion Currency'];
  var up = AdsApp.bulkUploads().newCsvUpload(cols, { moneyInMicros: false });
  rows.forEach(function (r) { up.append({ 'Google Click ID': r.gclid, 'Conversion Name': r.name, 'Conversion Time': r.time,
                                          'Conversion Value': r.value, 'Conversion Currency': r.currency }); });
  up.forOfflineConversions();
  up.setFileName('MOLODIST CRM ' + Utilities.formatDate(new Date(), 'Europe/Kyiv', 'yyyy-MM-dd HH:mm'));
  up.apply();
  UrlFetchApp.fetch(HOOK + '/offline', { method: 'post', contentType: 'application/json',
                    payload: JSON.stringify({ keys: rows.map(function (r) { return r.key; }) }), muteHttpExceptions: true });
  Logger.log('Офлайн-конверсій надіслано: ' + rows.length);
}

/* Погоджені в CRM зміни (мінус-слова, паузи, бюджети, ключі, оголошення) → кабінет. Без погодження скрипт нічого не змінює. */
function applyQueue() {
  var res = UrlFetchApp.fetch(HOOK + '/queue', { muteHttpExceptions: true });
  if (res.getResponseCode() >= 300) { Logger.log('Черга змін: ' + res.getResponseCode()); return; }
  var list = (JSON.parse(res.getContentText()).changes) || [], out = [];
  var camp = function (c) {
    var it = c.campaign_id ? AdsApp.campaigns().withIds([Number(c.campaign_id)]).get() : AdsApp.campaigns().withCondition("campaign.name = '" + String(c.campaign).replace(/'/g, "\\'") + "'").get();
    if (it.hasNext()) return it.next();
    var p = c.campaign_id ? AdsApp.performanceMaxCampaigns().withIds([Number(c.campaign_id)]).get() : null;
    return p && p.hasNext() ? p.next() : null;
  };
  var group = function (c) {
    var it = c.adgroup_id ? AdsApp.adGroups().withIds([Number(c.adgroup_id)]).get()
      : AdsApp.adGroups().withCondition("campaign.name = '" + String(c.campaign).replace(/'/g, "\\'") + "'").withCondition("ad_group.name = '" + String(c.adgroup || '').replace(/'/g, "\\'") + "'").get();
    return it.hasNext() ? it.next() : null;
  };
  list.forEach(function (c) {
    var r = { id: c.id, ok: false, note: '' };
    try {
      if (c.kind === 'negative') {
        var cp = camp(c); if (!cp) throw new Error('кампанію не знайдено');
        var t = c.match === 'EXACT' ? '[' + c.target + ']' : '"' + c.target + '"';
        cp.createNegativeKeyword(t); r.ok = true; r.note = 'додано ' + t;
      } else if (c.kind === 'pause_campaign' || c.kind === 'enable_campaign') {
        var cp2 = camp(c); if (!cp2) throw new Error('кампанію не знайдено');
        if (c.kind === 'pause_campaign') cp2.pause(); else cp2.enable(); r.ok = true;
      } else if (c.kind === 'budget') {
        var cp3 = camp(c); if (!cp3) throw new Error('кампанію не знайдено');
        cp3.getBudget().setAmount(Number(c.amount)); r.ok = true; r.note = 'бюджет ' + c.amount;
      } else if (c.kind === 'pause_keyword' || c.kind === 'enable_keyword') {
        var ids = String(c.keyword_id || c.target).split('~');
        var kw = AdsApp.keywords().withIds([[Number(ids[0]), Number(ids[1])]]).get();
        if (!kw.hasNext()) throw new Error('ключ не знайдено');
        var k = kw.next(); if (c.kind === 'pause_keyword') k.pause(); else k.enable(); r.ok = true;
      } else if (c.kind === 'add_keyword') {
        var g = group(c); if (!g) throw new Error('групу не знайдено');
        var txt = c.match === 'EXACT' ? '[' + c.target + ']' : (c.match === 'BROAD' ? c.target : '"' + c.target + '"');
        var op = g.newKeywordBuilder().withText(txt); if (c.final_url) op = op.withFinalUrl(c.final_url);
        var kr = op.build(); r.ok = kr.isSuccessful(); r.note = r.ok ? 'додано ' + txt : kr.getErrors().join('; ');
      } else if (c.kind === 'remove_negative') {
        var cp5 = camp(c); if (!cp5) throw new Error('кампанію не знайдено');
        var ni = cp5.negativeKeywords().get(), rm = 0;
        while (ni.hasNext()) { var nk = ni.next(); if (nk.getText().replace(/[\"\[\]+]/g, '').trim().toLowerCase() === String(c.target).toLowerCase()) { nk.remove(); rm++; } }
        r.ok = rm > 0; r.note = rm ? 'прибрано ' + rm : 'мінус-слово не знайдено';
      } else if (c.kind === 'broad_to_phrase') {
        var cp4 = camp(c); if (!cp4) throw new Error('кампанію не знайдено');
        var ki = AdsApp.keywords().withCondition('campaign.id = ' + cp4.getId())
          .withCondition('ad_group_criterion.keyword.match_type = BROAD').withCondition('ad_group_criterion.status = ENABLED').get();
        var moved = 0, bad = 0;
        while (ki.hasNext()) {
          var kw2 = ki.next(), tx = kw2.getText().replace(/[\"\[\]+]/g, '').trim();
          var kb = kw2.getAdGroup().newKeywordBuilder().withText('"' + tx + '"');
          var fu = kw2.urls().getFinalUrl(); if (fu) kb = kb.withFinalUrl(fu);
          var kres = kb.build();
          if (kres.isSuccessful() || /DUPLICATE|already|існує/i.test(kres.getErrors().join(' '))) { kw2.pause(); moved++; } else { bad++; }
        }
        r.ok = moved > 0 || bad === 0; r.note = 'переведено ' + moved + (bad ? ', помилок ' + bad : '');
      } else if (c.kind === 'rsa') {
        var g2 = group(c); if (!g2) throw new Error('групу не знайдено');
        var old = [];
        if (c.pause_text) {
          var ai = g2.ads().withCondition("ad_group_ad.status = ENABLED").get();
          while (ai.hasNext()) { var ad = ai.next(); var t = ad.isType().responsiveSearchAd() ? JSON.stringify(ad.asType().responsiveSearchAd().getDescriptions()) : '';
            if (t.indexOf(c.pause_text) >= 0) { old.push(ad); if (!c.final_url) c.final_url = ad.urls().getFinalUrl(); } }
        }
        var b = g2.newAd().responsiveSearchAdBuilder().withHeadlines((c.headlines || []).map(function (h) { return { text: h }; }))
          .withDescriptions((c.descriptions || []).map(function (d) { return { text: d }; })).withFinalUrl(c.final_url);
        if (c.path1) b = b.withPath1(c.path1); if (c.path2) b = b.withPath2(c.path2);
        var ar = b.build(); r.ok = ar.isSuccessful(); r.note = r.ok ? 'оголошення створено' + (c.paused ? ' (на паузі)' : '') : ar.getErrors().join('; ');
        if (r.ok && c.paused) ar.getResult().pause();
        if (r.ok && !c.paused && old.length) { old.forEach(function (a) { a.pause(); }); r.note += '; старих на паузу: ' + old.length; }
      } else { r.note = 'невідомий тип ' + c.kind; }
    } catch (e) { r.ok = false; r.note = String(e).substring(0, 300); }
    out.push(r);
  });
  if (out.length) {
    UrlFetchApp.fetch(HOOK + '/applied', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ results: out }), muteHttpExceptions: true });
    Logger.log('Застосовано змін: ' + out.filter(function (x) { return x.ok; }).length + ' з ' + out.length);
  }
}
"""
