"""Рейтинг і відгуки MOLODIST з Google Карт (Places API, New) — блок довіри на сайті магазину.

Ключ API і Place ID власник вставляє в «Інтеграції». Дані оновлюються двічі на добу й зберігаються лише як тимчасовий кеш.
Розмітку schema.org для цих відгуків не додаємо: Google не дозволяє зірки в пошуку з відгуків про власний бізнес, узятих з Карт.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "shop_greviews"


def state(db: Session) -> dict:
    r = db.get(SyncState, KEY)
    try:
        return json.loads(r.value) if r and r.value else {}
    except ValueError:
        return {}


def _save(db: Session, st: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(st, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def due(db: Session, c: dict) -> bool:
    if not (c.get("gplaces_key") and c.get("gplace_id")):
        return False
    at = state(db).get("at")
    return not at or datetime.utcnow() - datetime.fromisoformat(at) > timedelta(hours=12)


def refresh(db: Session, c: dict, http=None) -> dict:
    import httpx
    key, pid = str(c.get("gplaces_key") or "").strip(), str(c.get("gplace_id") or "").strip()
    st = state(db)
    if not key or not pid:
        return {**st, "error": "Не задано ключ API або Place ID"}
    try:
        r = (http or httpx).get(f"https://places.googleapis.com/v1/places/{pid}", params={"languageCode": "uk"}, timeout=15,
                                headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "rating,userRatingCount,reviews,googleMapsUri,displayName"})
        d = r.json()
    except Exception as e:  # noqa: BLE001
        st.update(error=f"Google недоступний: {str(e)[:120]}", at=datetime.utcnow().isoformat(timespec="minutes"))
        _save(db, st)
        return st
    if r.status_code != 200:
        msg = ((d or {}).get("error") or {}).get("message") or f"HTTP {r.status_code}"
        st.update(error=msg[:200], at=datetime.utcnow().isoformat(timespec="minutes"))
        _save(db, st)
        return st
    rows = []
    for x in d.get("reviews") or []:
        txt = ((x.get("text") or x.get("originalText") or {}).get("text") or "").strip()
        a = x.get("authorAttribution") or {}
        if not txt:
            continue
        rows.append({"author": a.get("displayName") or "Користувач Google", "url": a.get("uri") or "", "photo": a.get("photoUri") or "",
                     "rating": int(x.get("rating") or 0), "text": txt[:700], "when": x.get("relativePublishTimeDescription") or "",
                     "time": (x.get("publishTime") or "")[:10]})
    st = {"rating": d.get("rating"), "count": d.get("userRatingCount"), "url": d.get("googleMapsUri") or "", "name": (d.get("displayName") or {}).get("text") or "",
          "reviews": [x for x in rows if x["rating"] >= int(c.get("greviews_min") or 4)][:6], "at": datetime.utcnow().isoformat(timespec="minutes"), "error": ""}
    _save(db, st)
    return st


def public(db: Session, c: dict) -> dict | None:
    if not c.get("greviews_on", True):
        return None
    st = state(db)
    return st if st.get("rating") else None
