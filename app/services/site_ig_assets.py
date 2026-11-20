"""Матеріали для сторінок болей з власного Instagram MOLODIST (той самий доступ, що й стрічка на сайті).

Опубліковане в Instagram уже має згоду пацієнтів, тож беремо звідти:
- рілс (відео + обкладинка) за словами в підписі — напр. «чотири фактори» для сторінки акне;
- фото «до / після» з публікацій про акне (каруселі розбираємо на окремі слайди).
Файли зберігаються на сервері (посилання Instagram живуть кілька годин), список — у SyncState «site_ig_assets»."""
from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState

KEY = "site_ig_assets"
FIELDS = "id,media_type,media_url,thumbnail_url,permalink,caption,timestamp,children{media_type,media_url,thumbnail_url}"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}\.(mp4|jpg)$")
TOPICS = {"akne": {"reel": r"чотири фактор|4 фактор", "posts": r"акне|постакне|висипан",
                   "ba": r"до\s*/?\s*(і\s*)?після|результат|кейс|динамік"},
          # сторінка «Пігментація й судини»: знайдене спершу приховане — власник вмикає рілс і фото сам (opt_in)
          "pigment": {"reel": r"\bipl\b|фотоомолодж|пігмент|судин|купероз|розаце", "posts": r"\bipl\b|пігмент|судин|купероз|розаце|фотоомолодж",
                      "ba": r"до\s*/?\s*(і\s*)?після|результат|кейс|динамік", "opt_in": True}}


def folder() -> Path:
    d = Path(settings.files_dir) / "site_ig"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def _save(db: Session, st: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(st, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def _list(db: Session, http, limit: int = 60) -> list[dict]:
    from . import meta_inbox as mi
    tok = mi._ig_token(db)
    if tok:
        js = http.get(f"{mi.IGRAPH}/me/media", params={"access_token": tok, "fields": FIELDS, "limit": limit}).json()
        if js.get("data"):
            return js["data"]
    tok = mi._page_token(db)
    if tok:
        me = http.get(f"{mi.GRAPH}/me", params={"access_token": tok, "fields": "instagram_business_account"}).json()
        ig = (me.get("instagram_business_account") or {}).get("id")
        if ig:
            js = http.get(f"{mi.GRAPH}/{ig}/media", params={"access_token": tok, "fields": FIELDS, "limit": limit}).json()
            if js.get("data"):
                return js["data"]
            raise RuntimeError((js.get("error") or {}).get("message") or "порожня відповідь Instagram")
    raise RuntimeError("немає доступу до Instagram (Канали → Instagram та Facebook)")


def _img(http, url: str, path: Path, max_side: int = 1080) -> bool:
    from PIL import Image
    r = http.get(url)
    if r.status_code != 200 or not (r.headers.get("content-type") or "").startswith("image/") or len(r.content) > 15 * 1024 * 1024:
        return False
    im = Image.open(BytesIO(r.content)).convert("RGB")
    im.thumbnail((max_side, max_side))
    im.save(path, "JPEG", quality=86)
    return True


def _video(http, url: str, path: Path) -> bool:
    with http.stream("GET", url) as r:
        if r.status_code != 200 or not (r.headers.get("content-type") or "").startswith("video/"):
            return False
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > 120 * 1024 * 1024:
                    raise RuntimeError("відео завелике")
                f.write(chunk)
    return path.stat().st_size > 10_000


def refresh(db: Session, topic: str = "akne", http=None) -> dict:
    """Знайти й зберегти рілс і фото «до / після» для сторінки (topic). Повертає новий стан."""
    t = TOPICS[topic]
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    st = state(db)
    cur = st.get(topic) or {}
    try:
        data = _list(db, http)
    except Exception as e:  # noqa: BLE001
        cur["error"] = re.sub(r"access_token=[^&\s]+", "access_token=…", str(e))[:300]
        st[topic] = cur
        _save(db, st)
        return cur
    d = folder()
    reel = None
    cases: list[dict] = []
    for m in data:
        cap = " ".join(str(m.get("caption") or "").split())
        mid = re.sub(r"\D", "", str(m.get("id") or ""))
        if not mid:
            continue
        if reel is None and m.get("media_type") == "VIDEO" and re.search(t["reel"], cap, re.I) and m.get("media_url"):
            v, p = d / f"{topic}-reel-{mid}.mp4", d / f"{topic}-reel-{mid}.jpg"
            try:
                ok = v.is_file() or _video(http, m["media_url"], v)
                if ok and m.get("thumbnail_url") and not p.is_file():
                    _img(http, m["thumbnail_url"], p)
                if ok:
                    reel = {"file": v.name, "poster": p.name if p.is_file() else "", "url": m.get("permalink"), "caption": cap[:300]}
            except Exception:  # noqa: BLE001
                v.unlink(missing_ok=True)
        if m.get("media_type") in ("IMAGE", "CAROUSEL_ALBUM") and re.search(t["posts"], cap, re.I) and re.search(t["ba"], cap, re.I):
            kids = ((m.get("children") or {}).get("data") or []) if m.get("media_type") == "CAROUSEL_ALBUM" else [m]
            for i, k in enumerate(kids):
                if k.get("media_type") != "IMAGE" or not k.get("media_url"):
                    continue
                f = d / f"{topic}-ig-{mid}-{i + 1}.jpg"
                try:
                    if f.is_file() or _img(http, k["media_url"], f):
                        cases.append({"file": f.name, "url": m.get("permalink"), "post": mid, "n": i + 1, "caption": cap[:200]})
                except Exception:  # noqa: BLE001
                    continue
    hide0 = set(cur.get("hide") or [])
    if t.get("opt_in"):                             # нові фото — приховані, доки власник не ввімкне
        hide0 |= {c["file"] for c in cases} - {c["file"] for c in cur.get("cases") or []}
    cur = {"reel_on": bool(cur.get("reel_on")), "reel": reel or cur.get("reel"), "cases": cases or cur.get("cases") or [], "checked_at": datetime.utcnow().isoformat(timespec="seconds"),
           "error": "" if (reel or cases) else "у останніх публікаціях не знайдено рілс / фото за ключовими словами", "hide": sorted(hide0)}
    st[topic] = cur
    _save(db, st)
    return cur


def assets(db: Session, topic: str, base: str) -> dict:
    """Для шаблону: посилання на рілс і фото, що є на диску (без прихованих)."""
    cur = state(db).get(topic) or {}
    d = folder()
    reel = cur.get("reel") if cur.get("reel") and (d / cur["reel"]["file"]).is_file() else None
    if (TOPICS.get(topic) or {}).get("opt_in") and not cur.get("reel_on"):
        reel = None
    if reel:
        reel = {**reel, "src": f"{base}/ig/{reel['file']}", "poster_src": f"{base}/ig/{reel['poster']}" if reel.get("poster") else ""}
    hide = set(cur.get("hide") or [])
    cases = [{**c, "src": f"{base}/ig/{c['file']}"} for c in cur.get("cases") or [] if c["file"] not in hide and (d / c["file"]).is_file()]
    return {"reel": reel, "ig_cases": cases}
