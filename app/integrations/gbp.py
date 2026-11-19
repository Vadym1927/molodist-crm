"""Google Business Profile (профіль MOLODIST у Google): відгуки й відповіді на них з CRM.

OAuth-клієнт (Web application) власник створює сам у Google Cloud; Client ID і секрет вставляє в CRM.
Секрет і refresh-токен зберігаються лише зашифрованими (SECRET_KEY), у відповідях і логах не показуються.
Відповідь на відгук — публічна: публікується лише кнопкою людини в CRM, автоматичних відповідей немає.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "gbp"
SCOPE = "https://www.googleapis.com/auth/business.manage"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
ACC = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
V4 = "https://mybusiness.googleapis.com/v4"
STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


class GbpError(RuntimeError):
    pass


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def save(db: Session, c: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def _enc(v: str) -> str:
    from .cleverbox import _token_cipher
    return _token_cipher().encrypt(v.encode()).decode() if v else ""


def _dec(v: str | None) -> str:
    from .cleverbox import _token_cipher
    try:
        return _token_cipher().decrypt(v.encode()).decode() if v else ""
    except Exception:  # noqa: BLE001
        return ""


def set_client(db: Session, client_id: str, secret: str) -> None:
    c = cfg(db)
    if client_id.strip():
        c["client_id"] = client_id.strip()[:200]
    if secret.strip():
        c["secret_enc"] = _enc(secret.strip())
    save(db, c)


def status(c: dict) -> dict:
    return {"client": bool(c.get("client_id") and c.get("secret_enc")), "connected": bool(c.get("refresh_enc")),
            "location": c.get("location") or "", "title": c.get("title") or "", "email": c.get("email") or "", "at": c.get("at") or ""}


def auth_url(db: Session, redirect: str) -> str:
    c = cfg(db)
    if not (c.get("client_id") and c.get("secret_enc")):
        raise GbpError("Спершу вставте Client ID і секрет.")
    c["state"] = secrets.token_urlsafe(16)
    save(db, c)
    return AUTH + "?" + urlencode({"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
                                   "access_type": "offline", "prompt": "consent", "state": c["state"], "include_granted_scopes": "true"})


def callback(db: Session, code: str, state: str, redirect: str, http: httpx.Client | None = None) -> dict:
    c = cfg(db)
    if not state or state != c.get("state"):
        raise GbpError("Посилання застаріло — натисніть «Підключити Google» ще раз.")
    http = http or httpx.Client(timeout=20)
    r = http.post(TOKEN, data={"code": code, "client_id": c["client_id"], "client_secret": _dec(c.get("secret_enc")),
                               "redirect_uri": redirect, "grant_type": "authorization_code"})
    j = r.json() if r.content else {}
    if r.status_code != 200 or not j.get("refresh_token"):
        raise GbpError(f"Google не видав доступ: {j.get('error_description') or j.get('error') or r.status_code}")
    c.pop("state", None)
    c["refresh_enc"] = _enc(j["refresh_token"])
    c["_at"], c["_exp"] = _enc(j.get("access_token") or ""), time.time() + int(j.get("expires_in") or 0) - 60
    save(db, c)
    pick_location(db, http)
    return status(cfg(db))


def _token(db: Session, http: httpx.Client) -> str:
    c = cfg(db)
    if c.get("_at") and float(c.get("_exp") or 0) > time.time():
        return _dec(c["_at"])
    rt = _dec(c.get("refresh_enc"))
    if not rt:
        raise GbpError("Профіль Google не підключено.")
    r = http.post(TOKEN, data={"refresh_token": rt, "client_id": c.get("client_id"), "client_secret": _dec(c.get("secret_enc")),
                               "grant_type": "refresh_token"})
    j = r.json() if r.content else {}
    if r.status_code != 200 or not j.get("access_token"):
        raise GbpError(f"Доступ до Google закінчився — підключіть ще раз ({j.get('error') or r.status_code}).")
    c["_at"], c["_exp"] = _enc(j["access_token"]), time.time() + int(j.get("expires_in") or 3600) - 60
    save(db, c)
    return j["access_token"]


def _call(db: Session, method: str, url: str, http: httpx.Client | None = None, **kw) -> dict:
    http = http or httpx.Client(timeout=25)
    r = http.request(method, url, headers={"Authorization": f"Bearer {_token(db, http)}"}, **kw)
    j = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = ((j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")) or r.text[:200]
        raise GbpError(f"Google: {msg}")
    return j


def pick_location(db: Session, http: httpx.Client | None = None) -> dict:
    """Перший акаунт і перша точка (MOLODIST має одну точку). Зберігає accounts/…/locations/… і назву."""
    accs = _call(db, "GET", ACC, http).get("accounts") or []
    for a in accs:
        locs = _call(db, "GET", f"{INFO}/{a['name']}/locations", http, params={"readMask": "name,title,storefrontAddress", "pageSize": 20}).get("locations") or []
        if locs:
            c = cfg(db)
            c["account"], c["location"], c["title"] = a["name"], locs[0]["name"], locs[0].get("title") or ""
            c["all"] = [{"name": x["name"], "title": x.get("title") or ""} for x in locs]
            save(db, c)
            return c
    raise GbpError("У цьому акаунті Google немає профілю компанії.")


def _path(c: dict) -> str:
    if not (c.get("account") and c.get("location")):
        raise GbpError("Не вибрано точку профілю.")
    return f"{V4}/{c['account']}/{c['location']}"


def reviews(db: Session, http: httpx.Client | None = None, pages: int = 5) -> list[dict]:
    c = cfg(db)
    out, tok = [], ""
    for _ in range(pages):
        j = _call(db, "GET", _path(c) + "/reviews", http, params={"pageSize": 50, **({"pageToken": tok} if tok else {})})
        for r in j.get("reviews") or []:
            out.append({"id": r.get("reviewId") or r.get("name", "").split("/")[-1], "name": (r.get("reviewer") or {}).get("displayName") or "Користувач Google",
                        "stars": STARS.get(r.get("starRating") or "", 0), "text": r.get("comment") or "",
                        "at": (r.get("createTime") or "")[:10], "reply": (r.get("reviewReply") or {}).get("comment") or "",
                        "reply_at": ((r.get("reviewReply") or {}).get("updateTime") or "")[:10]})
        tok = j.get("nextPageToken") or ""
        if not tok:
            break
    c = cfg(db)
    c["at"] = datetime.now().isoformat(timespec="minutes")
    c["avg"], c["total"] = j.get("averageRating"), j.get("totalReviewCount")
    save(db, c)
    return out


def reply(db: Session, review_id: str, text: str, http: httpx.Client | None = None) -> None:
    text = (text or "").strip()
    if not text:
        raise GbpError("Порожня відповідь.")
    if len(text) > 4000:
        raise GbpError("Задовга відповідь (до 4000 символів).")
    if not review_id.replace("-", "").replace("_", "").isalnum():
        raise GbpError("Невірний відгук.")
    _call(db, "PUT", f"{_path(cfg(db))}/reviews/{review_id}/reply", http, json={"comment": text})


def disconnect(db: Session) -> None:
    c = cfg(db)
    for k in ("refresh_enc", "_at", "_exp", "state"):
        c.pop(k, None)
    save(db, c)


def set_description(db: Session, text: str, http: httpx.Client | None = None) -> None:
    """Опис профілю компанії (до 750 символів, без посилань) — публікується в Google одразу."""
    text = (text or "").strip()
    if not text:
        raise GbpError("Порожній опис.")
    if len(text) > 750:
        raise GbpError(f"Задовгий опис: {len(text)} символів, Google дозволяє до 750.")
    c = cfg(db)
    if not c.get("location"):
        raise GbpError("Не вибрано точку профілю.")
    _call(db, "PATCH", f"{INFO}/{c['location']}", http, params={"updateMask": "profile.description"},
          json={"profile": {"description": text}})
    c = cfg(db)
    c["description"] = text
    save(db, c)
