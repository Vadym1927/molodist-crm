"""GA4 → привʼязка дзвінків до реклами за часом кліку по номеру телефону.

GA4 Data API (лише читання, сервісний акаунт з роллю «Читач» у ресурсі): події кліку по номеру / форми по хвилинах
з джерелом, каналом, кампанією й ключовим словом сеансу. Вхідний дзвінок, що почався протягом кількох хвилин після кліку,
отримує ці мітки (Call.attr = "ga4"; якщо в ті ж хвилини були кліки з різних кампаній — "ga4?", тобто імовірно).
Дзвінки, набрані вручну з іншого пристрою, так не визначаються."""
from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Call, GaEvent, SyncState

KEY = "ga4_cfg"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
DEFAULT = {"property": "", "events": [], "key_enc": "", "email": "", "window_before": 4, "window_after": 1, "last_sync": None, "last_error": None}
HINT = ("phone", "tel", "call", "дзвін", "телефон", "click_to_call")


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {**DEFAULT, **d}


def save_cfg(db: Session, d: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(d, ensure_ascii=False, default=str), updated_at=datetime.utcnow()))
    db.commit()


def set_key(db: Session, raw_json: str) -> str:
    """Ключ сервісного акаунта (JSON) — зберігаємо зашифрованим; повертаємо email акаунта."""
    try:
        info = json.loads(raw_json)
        email, pk = info["client_email"], info["private_key"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("Це не ключ сервісного акаунта Google (потрібен JSON з полями client_email і private_key)")
    if "BEGIN PRIVATE KEY" not in pk:
        raise ValueError("У ключі немає private_key")
    c = cfg(db)
    c["key_enc"] = _cipher().encrypt(json.dumps({"client_email": email, "private_key": pk, "token_uri": info.get("token_uri")}).encode()).decode()
    c["email"] = email
    save_cfg(db, c)
    return email


def _sa(db: Session, c: dict) -> dict | None:
    if c.get("key_enc"):
        try:
            return json.loads(_cipher().decrypt(c["key_enc"].encode()).decode())
        except Exception:      # noqa: BLE001
            return None
    from ..config import settings                      # той самий сервісний акаунт, що для Google Sheets (якщо заданий у .env)
    path = getattr(settings, "google_service_account_file", None)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return None


def sa_email(db: Session) -> str | None:
    sa = _sa(db, cfg(db))
    return sa.get("client_email") if sa else None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _token(sa: dict, http: httpx.Client) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    seg = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + _b64(json.dumps(
        {"iss": sa["client_email"], "scope": SCOPE, "aud": uri, "iat": now, "exp": now + 3000}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(seg.encode(), padding.PKCS1v15(), hashes.SHA256())
    r = http.post(uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": seg + "." + _b64(sig)})
    if r.status_code >= 400:
        raise ValueError(f"Google не видав доступ ({r.status_code}): {r.text[:160]}")
    return r.json()["access_token"]


def _report(db: Session, body: dict, http: httpx.Client | None = None, c: dict | None = None) -> list[dict]:
    c = c or cfg(db)
    prop = "".join(ch for ch in str(c.get("property") or "") if ch.isdigit())
    if not prop:
        raise ValueError("Вкажіть ідентифікатор ресурсу GA4 (число)")
    sa = _sa(db, c)
    if not sa:
        raise ValueError("Немає ключа сервісного акаунта Google")
    http = http or httpx.Client(timeout=60)
    tok = _token(sa, http)
    out, offset = [], 0
    while True:
        r = http.post(f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport", headers={"Authorization": f"Bearer {tok}"},
                      json={**body, "limit": 100000, "offset": offset})
        if r.status_code == 403:
            raise ValueError(f"GA4 не дає доступу (403). Додайте {sa.get('client_email')} у ресурс GA4: Адміністратор → Керування доступом до ресурсу → роль «Читач». "
                             "Також у Google Cloud проєкті сервісного акаунта має бути ввімкнено Google Analytics Data API.")
        if r.status_code >= 400:
            raise ValueError(f"GA4: {r.status_code} {r.text[:200]}")
        js = r.json()
        dims = [h["name"] for h in js.get("dimensionHeaders") or []]
        mets = [h["name"] for h in js.get("metricHeaders") or []]
        rows = js.get("rows") or []
        for row in rows:
            x = {d: v.get("value", "") for d, v in zip(dims, row.get("dimensionValues") or [])}
            x.update({m: v.get("value", "0") for m, v in zip(mets, row.get("metricValues") or [])})
            out.append(x)
        offset += len(rows)
        if not rows or offset >= int(js.get("rowCount") or 0):
            break
    return out


def events_list(db: Session, days: int = 30, http: httpx.Client | None = None) -> list[dict]:
    """Усі події ресурсу за N днів з кількістю — щоб вибрати, які з них «клік по номеру»."""
    rows = _report(db, {"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}], "dimensions": [{"name": "eventName"}],
                        "metrics": [{"name": "eventCount"}], "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}]}, http)
    return [{"name": r["eventName"], "count": int(float(r["eventCount"])), "hint": any(h in r["eventName"].lower() for h in HINT)} for r in rows]


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() in ("(not set)", "(not provided)", "") else v


def sync(db: Session, d_from: date, d_to: date, http: httpx.Client | None = None) -> dict:
    """Події з налаштувань (кліки по номеру тощо) по хвилинах з міткою сеансу → GaEvent."""
    c = cfg(db)
    names = [e for e in c.get("events") or [] if e]
    if not names:
        raise ValueError("Оберіть події GA4, які означають клік по номеру телефону")
    rep = {"rows": 0, "new": 0}
    a = d_from
    while a <= d_to:
        b = min(a + timedelta(days=29), d_to)
        rows = _report(db, {"dateRanges": [{"startDate": a.isoformat(), "endDate": b.isoformat()}],
                            "dimensions": [{"name": x} for x in ("dateHourMinute", "eventName", "sessionSource", "sessionMedium", "sessionCampaignName", "sessionGoogleAdsKeyword")],
                            "metrics": [{"name": "eventCount"}],
                            "dimensionFilter": {"filter": {"fieldName": "eventName", "inListFilter": {"values": names}}}}, http, c)
        t0, t1 = datetime.combine(a, datetime.min.time()), datetime.combine(b, datetime.max.time())
        have = {(e.at, e.event, e.source, e.medium, e.campaign, e.keyword): e for e in db.scalars(select(GaEvent).where(GaEvent.at >= t0, GaEvent.at <= t1))}
        for r in rows:
            try:
                at = datetime.strptime(r["dateHourMinute"], "%Y%m%d%H%M")
            except (ValueError, KeyError):
                continue
            k = (at, r["eventName"][:80], _clean(r.get("sessionSource"))[:120], _clean(r.get("sessionMedium"))[:120],
                 _clean(r.get("sessionCampaignName"))[:300], _clean(r.get("sessionGoogleAdsKeyword"))[:300])
            n = int(float(r.get("eventCount") or 0))
            e = have.get(k)
            if e is None:
                e = GaEvent(at=k[0], event=k[1], source=k[2], medium=k[3], campaign=k[4], keyword=k[5], count=n)
                db.add(e)
                have[k] = e
                rep["new"] += 1
            else:
                e.count = n
            rep["rows"] += 1
        db.commit()
        a = b + timedelta(days=1)
    c = cfg(db)
    c["last_sync"], c["last_error"] = datetime.now().isoformat(timespec="minutes"), None
    save_cfg(db, c)
    return rep


def match_calls(db: Session, d_from: date, d_to: date) -> dict:
    """Вхідні дзвінки без міток (або з мітками від GA4) ← найближчий клік по номеру перед дзвінком."""
    c = cfg(db)
    wb, wa = timedelta(minutes=float(c.get("window_before") or 4)), timedelta(minutes=float(c.get("window_after") or 1))
    t0, t1 = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    evs = db.scalars(select(GaEvent).where(GaEvent.at >= t0 - wb, GaEvent.at <= t1 + wa).order_by(GaEvent.at)).all()
    left = {e.id: e.count for e in evs}
    by_min: dict = defaultdict(list)
    for e in evs:
        by_min[e.at].append(e)
    rep = {"calls": 0, "matched": 0, "uncertain": 0, "cleared": 0}
    calls = db.scalars(select(Call).where(Call.at >= t0, Call.at <= t1, Call.direction == "in").order_by(Call.at)).all()
    for cl in calls:
        if cl.utm_source and cl.attr not in ("ga4", "ga4?"):          # мітки з колтрекінгу Ringostat — точніші, не чіпаємо
            continue
        rep["calls"] += 1
        m0 = cl.at.replace(second=0, microsecond=0)
        cand = []
        mm = m0 - wb
        while mm <= m0 + wa:
            for e in by_min.get(mm, []):
                if left.get(e.id, 0) > 0 and e.at - wa <= cl.at <= e.at + wb + timedelta(minutes=1):
                    cand.append(e)
            mm += timedelta(minutes=1)
        if not cand:
            if cl.attr in ("ga4", "ga4?"):
                cl.utm_source = cl.utm_medium = cl.utm_campaign = cl.utm_term = cl.attr = None
                rep["cleared"] += 1
            continue
        best = min(cand, key=lambda e: (abs((cl.at - (e.at + timedelta(seconds=30))).total_seconds()), -(e.medium in ("cpc", "ppc"))))
        left[best.id] -= 1
        kinds = {(e.source, e.medium, e.campaign) for e in cand}
        cl.utm_source, cl.utm_medium = best.source or "(direct)", best.medium or "(none)"
        cl.utm_campaign, cl.utm_term = best.campaign or None, best.keyword or None
        cl.attr = "ga4" if len(kinds) == 1 else "ga4?"
        rep["matched"] += 1
        rep["uncertain"] += cl.attr == "ga4?"
    db.commit()
    return rep


def refresh(db: Session, days: int = 2) -> dict | None:
    """Регулярне оновлення: останні дні подій + привʼязка дзвінків. Помилку зберігаємо в налаштуваннях, не падаємо."""
    c = cfg(db)
    if not (c.get("property") and c.get("events")):
        return None
    today = date.today()
    try:
        s = sync(db, today - timedelta(days=days), today)
        m = match_calls(db, today - timedelta(days=days), today)
        return {**s, **m}
    except Exception as e:      # noqa: BLE001
        c = cfg(db)
        c["last_error"] = str(e)[:300]
        save_cfg(db, c)
        return None


def summary(db: Session, d_from: date, d_to: date) -> dict:
    """Кліки по номеру за період: разом, з Google Ads, по кампаніях."""
    t0, t1 = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    tot, paid, camps = 0, 0, defaultdict(int)
    for e in db.scalars(select(GaEvent).where(GaEvent.at >= t0, GaEvent.at <= t1)):
        tot += e.count
        if "google" in e.source.lower() and e.medium.lower() in ("cpc", "ppc", "paid"):
            paid += e.count
            camps[e.campaign.strip().lower()] += e.count
    return {"total": tot, "paid": paid, "by_campaign": dict(camps)}
