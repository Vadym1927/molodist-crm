"""Meta Ads: центр моніторингу реклами в Instagram / Facebook.

Статистика кабінету по днях і оголошеннях (витрати, покази, кліки, 3-сек. перегляди) потрапляє в CRM двома шляхами:
  • CRM сама щогодини читає Marketing API (токен з правом ads_read і номер рекламного кабінету — «Підключення» на сторінці);
  • або агент на сервері надсилає ті самі рядки POST-ом на вебхук (той самий формат, що й у Google Ads).
Ліди рахуються автоматично: коли людина пише в Direct / Messenger з реклами («Написати нам»), Meta додає до повідомлення
referral з ad_id — CRM зберігає його в переписці й у зверненні. Далі воронка по кожному ліду: пацієнт → запис → візит → оплати.
Витрати — у валюті кабінету (USD); у гривні перераховуються за курсом на день (курс із «Планування ціни» / monobank)."""
from __future__ import annotations

import json
import secrets as _secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AdsStat, Appeal, ChatMessage, SyncState

KEY = "meta_ads"
REF_KEY = "meta_ad_refs"            # клік по рекламі без повідомлення: співрозмовник → оголошення (до першого повідомлення)
SRC = "meta"
GRAPH = "https://graph.facebook.com/v19.0"
DEFAULT = {"hook": "", "token": "", "account": "", "currency": "USD", "last_pull": None, "last_error": None, "margin_pct": 60.0}


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    out = {**DEFAULT, **d}
    if not out["hook"]:
        out["hook"] = _secrets.token_urlsafe(18)
        save_cfg(db, out)
        db.commit()
    return out


def save_cfg(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ".").replace(" ", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _rate(db: Session) -> float:
    from .pricing import rates
    return float(rates(db).get("USD") or 0) or 41.5


# ---------------- статистика кабінету ----------------

def ingest(db: Session, rows: list[dict], rate: float | None = None) -> dict:
    """rows: [{day, ad_id, ad_name, adset, campaign, status?, impressions, clicks, spend, views3?, rate?}] — рядок на день і оголошення.
    Повторне надсилання оновлює. Курс USD→UAH фіксується на рядку (для минулих днів не перераховується)."""
    c = cfg(db)
    rate = rate or _rate(db)
    days = []
    for r in rows:
        try:
            days.append(date.fromisoformat(str(r.get("day"))[:10]))
        except ValueError:
            pass
    have = {}
    if days:
        for s_ in db.scalars(select(AdsStat).where(AdsStat.source == SRC, AdsStat.level == "ad", AdsStat.day >= min(days), AdsStat.day <= max(days))):
            have[(s_.day, s_.key)] = s_
    n_new = n_upd = 0
    for r in rows:
        try:
            day = date.fromisoformat(str(r.get("day"))[:10])
        except ValueError:
            continue
        key = str(r.get("ad_id") or "")[:200]
        if not key:
            continue
        s_ = have.get((day, key))
        if s_ is None:
            s_ = AdsStat(source=SRC, level="ad", day=day, key=key)
            db.add(s_)
            have[(day, key)] = s_
            n_new += 1
        else:
            n_upd += 1
        s_.name = str(r.get("ad_name") or "")[:400] or s_.name
        s_.parent = str(r.get("campaign") or "")[:400] or s_.parent
        s_.status = str(r.get("status") or "")[:30] or s_.status
        s_.clicks, s_.impressions, s_.cost = _f(r.get("clicks")), _f(r.get("impressions")), round(_f(r.get("spend")), 2)
        old = s_.extra or {}
        s_.extra = {"adset": str(r.get("adset") or old.get("adset") or "")[:300], "views3": _f(r.get("views3")),
                    "rate": _f(r.get("rate")) or old.get("rate") or rate, "thumb": r.get("thumb") or old.get("thumb")}
        s_.updated_at = datetime.utcnow()
    c["last_pull"], c["last_error"] = datetime.now().isoformat(timespec="minutes"), None
    save_cfg(db, c)
    db.commit()
    return {"ok": True, "new": n_new, "updated": n_upd}


def pull(db: Session, days: int = 7, http: httpx.Client | None = None) -> dict:
    """Прочитати статистику з Marketing API за останні days днів (рівень — оголошення, по днях)."""
    c = cfg(db)
    tok, acc = (c.get("token") or "").strip(), (c.get("account") or "").strip().replace("act_", "")
    if not tok or not acc:
        return {"ok": False, "error": "Не вказано токен або номер рекламного кабінету"}
    today = date.today()
    params = {"access_token": tok, "level": "ad", "time_increment": 1, "limit": 500,
              "time_range": json.dumps({"since": (today - timedelta(days=days)).isoformat(), "until": today.isoformat()}),
              "fields": "ad_id,ad_name,adset_name,campaign_name,impressions,clicks,spend,actions,account_currency"}
    own = http is None
    http = http or httpx.Client(timeout=40)
    rows, url = [], f"{GRAPH}/act_{acc}/insights"
    try:
        for _ in range(20):
            js = http.get(url, params=params).json()
            if js.get("error"):
                c["last_error"] = str((js["error"] or {}).get("message") or js["error"])[:300]
                save_cfg(db, c)
                db.commit()
                return {"ok": False, "error": c["last_error"]}
            for x in js.get("data") or []:
                v3 = next((_f(a.get("value")) for a in x.get("actions") or [] if a.get("action_type") == "video_view"), 0.0)
                rows.append({"day": x.get("date_start"), "ad_id": x.get("ad_id"), "ad_name": x.get("ad_name"), "adset": x.get("adset_name"),
                             "campaign": x.get("campaign_name"), "impressions": x.get("impressions"), "clicks": x.get("clicks"),
                             "spend": x.get("spend"), "views3": v3})
                if x.get("account_currency"):
                    c["currency"] = x["account_currency"]
            nxt = (js.get("paging") or {}).get("next")
            if not nxt:
                break
            url, params = nxt, None
    except (httpx.HTTPError, ValueError) as e:
        c["last_error"] = f"Немає звʼязку з Meta: {type(e).__name__}"
        save_cfg(db, c)
        db.commit()
        return {"ok": False, "error": c["last_error"]}
    finally:
        if own:
            http.close()
    save_cfg(db, c)
    return ingest(db, rows)


# ---------------- ліди з реклами (referral у повідомленнях) ----------------

def referral_of(ev: dict) -> dict | None:
    """Referral з події вебхука Meta: у повідомленні (Instagram), окремою подією або в postback (Messenger)."""
    msg = ev.get("message") or {}
    r = msg.get("referral") or ev.get("referral") or (ev.get("postback") or {}).get("referral")
    if not isinstance(r, dict):
        return None
    ad_id = str(r.get("ad_id") or "").strip()
    ctx = r.get("ads_context_data") or {}
    if not ad_id and (r.get("source") or "").upper() != "ADS":
        return None
    return {"ad_id": ad_id or None, "title": str(ctx.get("ad_title") or "")[:200] or None, "ref": str(r.get("ref") or "")[:120] or None,
            "source": str(r.get("source") or "ADS")[:30]}


def remember(db: Session, sid: str, ad: dict) -> None:
    """Клік по рекламі прийшов окремою подією — запамʼятати до першого повідомлення цього співрозмовника."""
    st = db.get(SyncState, REF_KEY) or SyncState(key=REF_KEY)
    try:
        d = json.loads(st.value) if st.value else {}
    except ValueError:
        d = {}
    d[sid] = {**ad, "at": datetime.utcnow().isoformat(timespec="seconds")}
    if len(d) > 500:
        d = dict(sorted(d.items(), key=lambda kv: kv[1].get("at") or "")[-400:])
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def recall(db: Session, sid: str, days: int = 7) -> dict | None:
    st = db.get(SyncState, REF_KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        return None
    x = d.get(sid)
    if not x:
        return None
    try:
        if datetime.utcnow() - datetime.fromisoformat(x.get("at")) > timedelta(days=days):
            return None
    except (TypeError, ValueError):
        return None
    return {k: v for k, v in x.items() if k != "at"}


def mark_appeal(db: Session, appeal_id: int | None, ad: dict) -> None:
    """Звернення з реклами: мітка в utm (джерело meta, ad_id) — за нею ліда видно у звітах і фільтрах."""
    a = db.get(Appeal, appeal_id) if appeal_id else None
    if a and "ad_id=" not in (a.utm or ""):
        a.utm = (f"utm_source=meta&utm_medium=paid&ad_id={ad.get('ad_id') or ''}" + (f"&ad={ad['title']}" if ad.get("title") else ""))[:300]


def _loc(v: datetime) -> datetime:
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None)


def leads(db: Session, d_from: date, d_to: date) -> list[dict]:
    """Унікальні ліди з реклами Meta за період: переписки з referral (перше повідомлення з рекламною міткою)."""
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    out, seen = [], set()
    q = select(ChatMessage).where(ChatMessage.ad.is_not(None), ChatMessage.direction == "in").order_by(ChatMessage.at)
    for m in db.scalars(q):
        if m.thread in seen:
            continue
        seen.add(m.thread)
        at = _loc(m.at)
        if not (a <= at <= b):
            continue
        ap_ = db.get(Appeal, m.appeal_id) if m.appeal_id else None
        out.append({"at": at, "phone": ap_.phone if ap_ else None, "kind": "chat", "campaign": None, "term": None,
                    "status": ap_.status if ap_ else None, "ad_id": (m.ad or {}).get("ad_id"), "ad_title": (m.ad or {}).get("title"),
                    "name": m.name or (ap_.name if ap_ else None), "channel": m.channel, "thread": m.thread,
                    "patient_id": m.patient_id or (ap_.patient_id if ap_ else None), "call_id": None, "appeal_id": m.appeal_id,
                    "answered": True, "touches": 1})
    return out


# ---------------- зведення ----------------

def _ratios(t: dict) -> dict:
    t["ctr"] = round(100 * t["clicks"] / t["impressions"], 2) if t["impressions"] else None
    t["cpc"] = round(t["cost"] / t["clicks"], 2) if t["clicks"] else None
    t["cpm"] = round(1000 * t["cost"] / t["impressions"], 2) if t["impressions"] else None
    t["hook"] = round(100 * t["views3"] / t["impressions"], 2) if t["impressions"] and t["views3"] else None
    n, bk, pay = t.get("leads", 0), t.get("booked", 0), t.get("payers", 0)
    t["cpl"] = round(t["uah"] / n, 2) if n else None
    t["cost_booking"] = round(t["uah"] / bk, 2) if bk else None
    t["cost_sale"] = round(t["uah"] / pay, 2) if pay else None
    t["cr_click_lead"] = round(100 * n / t["clicks"], 2) if t["clicks"] else None
    t["cr_lead_book"] = round(100 * bk / n, 1) if n else None
    t["cr_lead_sale"] = round(100 * pay / n, 1) if n else None
    t["roas"] = round(t.get("revenue", 0) / t["uah"], 2) if t["uah"] else None
    for k in ("cost", "uah", "revenue"):
        t[k] = round(t.get(k, 0), 2)
    return t


def _blank() -> dict:
    return {"clicks": 0.0, "impressions": 0.0, "cost": 0.0, "uah": 0.0, "views3": 0.0, "leads": 0, "booked": 0, "came": 0, "payers": 0,
            "revenue": 0.0}


def dashboard(db: Session, d_from: date, d_to: date, ad: str = "", campaign: str = "", group: str = "ad") -> dict:
    """group: ad — по оголошеннях (сума за період), day — щоденний реєстр (день × оголошення), campaign — по кампаніях."""
    from .google_ads import funnel
    c = cfg(db)
    stats = list(db.scalars(select(AdsStat).where(AdsStat.source == SRC, AdsStat.level == "ad", AdsStat.day >= d_from, AdsStat.day <= d_to)
                            .order_by(AdsStat.day.desc())))
    names = {}
    for s_ in db.scalars(select(AdsStat).where(AdsStat.source == SRC, AdsStat.level == "ad").order_by(AdsStat.day)):
        names[s_.key] = s_
    ls = funnel(db, leads(db, d_from, d_to))
    ads_all = sorted({(s_.key, s_.name or s_.key) for s_ in stats} | {(r["ad_id"], (names[r["ad_id"]].name if r["ad_id"] in names else r["ad_title"]) or r["ad_id"])
                                                                   for r in ls if r["ad_id"]}, key=lambda x: x[1].lower())
    camps_all = sorted({s_.parent for s_ in stats if s_.parent})
    if ad:
        stats, ls = [s_ for s_ in stats if s_.key == ad], [r for r in ls if r["ad_id"] == ad]
    if campaign:
        stats = [s_ for s_ in stats if s_.parent == campaign]
        ls = [r for r in ls if r["ad_id"] in names and names[r["ad_id"]].parent == campaign]
    group = group if group in ("ad", "day", "campaign") else "ad"

    def gkey(day, ad_id):
        s_ = names.get(ad_id)
        return {"ad": (ad_id,), "day": (day, ad_id), "campaign": ((s_.parent if s_ else None) or "Без кампанії",)}[group]
    G, tot = defaultdict(_blank), _blank()
    for s_ in stats:
        rate = float((s_.extra or {}).get("rate") or 0) or _rate(db)
        for t in (G[gkey(s_.day, s_.key)], tot):
            t["clicks"] += s_.clicks or 0
            t["impressions"] += s_.impressions or 0
            t["cost"] += s_.cost or 0
            t["uah"] += (s_.cost or 0) * rate
            t["views3"] += float((s_.extra or {}).get("views3") or 0)
    for r in ls:
        for t in (G[gkey(r["at"].date(), r["ad_id"] or "—")], tot):
            t["leads"] += 1
            t["booked"] += bool(r["booked"])
            t["came"] += bool(r["came"])
            t["payers"] += r["revenue"] > 0
            t["revenue"] += r["revenue"]
    rows = []
    for k, t in G.items():
        ad_id = k[-1] if group != "campaign" else None
        s_ = names.get(ad_id) if ad_id else None
        rows.append({**_ratios(t), "day": k[0] if group == "day" else None, "ad_id": ad_id,
                     "name": (k[0] if group == "campaign" else ((s_.name if s_ else None) or next((r["ad_title"] for r in ls if r["ad_id"] == ad_id and r["ad_title"]), None)
                                                               or ("Реклама без номера оголошення" if ad_id == "—" else f"Оголошення {ad_id}"))),
                     "campaign": s_.parent if s_ else None, "adset": (s_.extra or {}).get("adset") if s_ else None})
    rows.sort(key=(lambda x: (x["day"], x["uah"])) if group == "day" else (lambda x: x["uah"]), reverse=True)
    return {"cfg": {k: v for k, v in c.items() if k != "token"}, "connected": bool(c.get("token") and c.get("account")), "has_token": bool(c.get("token")),
            "d_from": d_from, "d_to": d_to, "tot": _ratios(tot), "rows": rows, "leads": sorted(ls, key=lambda r: r["at"], reverse=True),
            "ads": ads_all, "campaigns": camps_all, "ad": ad, "campaign": campaign, "group": group, "rate": _rate(db),
            "n_stats": len(stats)}
