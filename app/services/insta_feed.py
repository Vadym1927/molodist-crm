"""Стрічка Instagram на головній магазину: останні публікації власного акаунта MOLODIST.

Беремо тим самим доступом, що вже підключений у «Канали → Instagram та Facebook» (окремий токен не потрібен):
спершу токен Instagram (вхід через Instagram), якщо його немає — токен сторінки Facebook. Картинки зберігаємо в себе
(посилання Instagram живуть кілька годин), оновлюємо раз на годину."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState

KEY = "shop_insta"
FIELDS = "id,media_type,media_url,thumbnail_url,permalink,caption,timestamp"


def folder() -> Path:
    d = Path(settings.files_dir) / "shop_insta"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def items(db: Session) -> list[dict]:
    return [x for x in state(db).get("items") or [] if (folder() / x["file"]).is_file()]


def _media(db: Session, http) -> tuple[list[dict], str]:
    from . import meta_inbox as mi
    tok = mi._ig_token(db)
    if tok:
        js = http.get(f"{mi.IGRAPH}/me/media", params={"access_token": tok, "fields": FIELDS, "limit": 12}).json()
        if js.get("data"):
            return js["data"], "instagram"
        err = (js.get("error") or {}).get("message") or "порожня відповідь"
    else:
        err = "токен Instagram не задано"
    tok = mi._page_token(db)
    if tok:
        me = http.get(f"{mi.GRAPH}/me", params={"access_token": tok, "fields": "instagram_business_account"}).json()
        ig = (me.get("instagram_business_account") or {}).get("id")
        if ig:
            js = http.get(f"{mi.GRAPH}/{ig}/media", params={"access_token": tok, "fields": FIELDS, "limit": 12}).json()
            if js.get("data"):
                return js["data"], "facebook"
            err = (js.get("error") or {}).get("message") or err
        else:
            err = (me.get("error") or {}).get("message") or "до сторінки Facebook не привʼязано акаунт Instagram"
    raise RuntimeError(err)


def refresh(db: Session, http=None, n: int = 8) -> dict:
    http = http or httpx.Client(timeout=30, follow_redirects=True)
    old = state(db)
    try:
        data, via = _media(db, http)
    except Exception as e:  # noqa: BLE001 — показуємо причину в налаштуваннях, стару стрічку не чіпаємо
        st = {**old, "error": re.sub(r"access_token=[^&\s]+", "access_token=…", str(e))[:300], "checked_at": datetime.utcnow().isoformat(timespec="seconds")}
        db.merge(SyncState(key=KEY, value=json.dumps(st, ensure_ascii=False), updated_at=datetime.utcnow()))
        db.commit()
        return st
    out = []
    for m in data:
        if len(out) >= n:
            break
        src = m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")
        mid = re.sub(r"\D", "", str(m.get("id") or ""))
        if not src or not mid or not str(m.get("permalink") or "").startswith("https://www.instagram.com/"):
            continue
        f = folder() / f"{mid}.jpg"
        if not f.is_file():
            try:
                r = http.get(src)
                if r.status_code != 200 or not (r.headers.get("content-type") or "").startswith("image/") or len(r.content) > 8 * 1024 * 1024:
                    continue
                from io import BytesIO
                from PIL import Image
                im = Image.open(BytesIO(r.content)).convert("RGB")
                side = min(im.size)
                im = im.crop(((im.width - side) // 2, (im.height - side) // 2, (im.width + side) // 2, (im.height + side) // 2))
                im.thumbnail((640, 640))
                im.save(f, "JPEG", quality=84)
            except Exception:  # noqa: BLE001
                continue
        out.append({"id": mid, "file": f.name, "url": m["permalink"], "video": m.get("media_type") == "VIDEO",
                    "alt": " ".join(str(m.get("caption") or "").split())[:120]})
    prof = old.get("profile") or {}
    try:                                                              # шапка профілю: імʼя, аватар, лічильники
        from . import meta_inbox as mi
        if via == "instagram":
            pj = http.get(f"{mi.IGRAPH}/me", params={"access_token": mi._ig_token(db), "fields": "username,name,profile_picture_url,followers_count,media_count"}).json()
        else:
            me = http.get(f"{mi.GRAPH}/me", params={"access_token": mi._page_token(db), "fields": "instagram_business_account{username,name,profile_picture_url,followers_count,media_count}"}).json()
            pj = me.get("instagram_business_account") or {}
        if pj.get("username"):
            prof = {"username": str(pj["username"])[:60], "name": str(pj.get("name") or "")[:80], "followers": int(pj.get("followers_count") or 0), "posts": int(pj.get("media_count") or 0),
                    "avatar": prof.get("avatar") or ""}
            if pj.get("profile_picture_url"):
                r = http.get(pj["profile_picture_url"])
                if r.status_code == 200 and (r.headers.get("content-type") or "").startswith("image/"):
                    from io import BytesIO
                    from PIL import Image
                    im = Image.open(BytesIO(r.content)).convert("RGB")
                    im.thumbnail((240, 240))
                    im.save(folder() / "0.jpg", "JPEG", quality=86)
                    prof["avatar"] = "0.jpg"
    except Exception:  # noqa: BLE001 — шапка необовʼязкова
        pass
    st = {"items": out or old.get("items") or [], "via": via, "profile": prof, "at": datetime.utcnow().isoformat(timespec="seconds"), "checked_at": datetime.utcnow().isoformat(timespec="seconds"),
          "error": "" if out else "публікацій із фото не знайдено"}
    db.merge(SyncState(key=KEY, value=json.dumps(st, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    keep = {x["file"] for x in st["items"]}
    for f in folder().glob("*.jpg"):
        if f.name not in keep and f.name != "0.jpg":
            f.unlink(missing_ok=True)
    return st


def due(db: Session) -> bool:
    at = state(db).get("checked_at")
    try:
        return not at or datetime.utcnow() - datetime.fromisoformat(at) > timedelta(hours=1)
    except ValueError:
        return True
