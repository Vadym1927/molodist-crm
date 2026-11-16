"""Перенос усього з Shop-Express (molodist.store) у картки товарів CRM — щоб відключити Shop-Express і нічого не втратити.

Що переноситься для кожного товару сайту (зіставлення — як у щоденній синхронізації: id сайту → артикул → штрихкод → назва):
• усі фото (images + gallery_common) → файли товару на нашому сервері (ProductFile, src_url — без дублів), позначка «на сайті»,
  порядок як на сайті; головне фото товару — наша копія, а не посилання на сайт;
• опис (short_description — саме він показується на сайті як «Опис»), SEO title / description / keywords, slug, посилання,
  бренд, категорії сайту, характеристики, ціна й стара ціна, наявність, модифікації → Product.site_info;
• показ на сайті → Product.site_on;
• повний сирий експорт каталогу — JSON-файлом на сервері (files/shop_express/export-…json) як резервна копія.
Повторний запуск оновлює текст і докачує лише нові фото. Нічого не видаляє."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Product, ProductFile, SyncState

KEY = "shop_migrate_report"
MIN_FREE = 2 * 1024 ** 3
TEXT_KEYS = ("short_description", "seo_title", "seo_description", "seo_keywords", "slug", "link", "brand", "groups", "characteristics",
             "price", "price_old", "discount", "presence", "quantity", "display_in_showcase", "condition", "unit", "modification",
             "parent_title", "parent_id", "external_id", "barcode", "sku", "title")
_BAD = re.compile(r"<\s*(script|style|iframe|object|embed)[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_ON = re.compile(r"\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)


def clean_html(h: str | None) -> str:
    """Опис із сайту для показу в CRM / на нашому сайті: без скриптів, стилів і обробників подій."""
    h = _BAD.sub("", h or "")
    h = _ON.sub("", h)
    return re.sub(r"javascript:", "", h, flags=re.I).strip()


def _urls(row: dict, root: str) -> list[str]:
    out = []

    def add(v):
        if isinstance(v, list):
            for x in v:
                add(x)
            return
        if isinstance(v, dict):
            v = v.get("original") or v.get("url") or v.get("src") or v.get("big") or v.get("large") or next(
                (x for x in v.values() if isinstance(x, str) and re.search(r"\.(jpe?g|png|webp|gif)", x, re.I)), None)
        if isinstance(v, str) and v.strip():
            u = v.strip() if v.startswith("http") else urljoin(root, v.strip().lstrip("/"))
            if u not in out:
                out.append(u)
    add(row.get("images"))
    add(row.get("gallery_common"))
    return out


def _free() -> int:
    try:
        return shutil.disk_usage(settings.files_dir).free
    except OSError:
        return MIN_FREE + 1


def _download(http: httpx.Client, url: str, root_host: str) -> tuple[bytes, str] | None:
    if not url.startswith("https://") or not urlparse(url).netloc:
        return None
    r = http.get(url, timeout=60)
    if r.status_code != 200 or not r.content or len(r.content) > 25 * 1024 * 1024:
        return None
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
    if not ctype.startswith("image/"):
        return None
    return r.content, ctype


def info_of(row: dict) -> dict:
    d = {k: row.get(k) for k in TEXT_KEYS if row.get(k) not in (None, "", [], {})}
    if isinstance(d.get("title"), dict):
        d["title"] = d["title"].get("ua") or d["title"].get("uk") or next(iter(d["title"].values()), "")
    for k in ("short_description", "seo_title", "seo_description", "seo_keywords", "link", "slug"):
        if isinstance(d.get(k), dict):
            d[k] = d[k].get("ua") or d[k].get("uk") or next(iter(d[k].values()), "")
    if d.get("short_description"):
        d["short_description"] = clean_html(str(d["short_description"]))
    return d


def migrate(db: Session, client=None, http: httpx.Client | None = None, job=None, download: bool = True) -> dict:
    from ..integrations import shop_express as sx
    from . import ingest
    log = (lambda m: job.log(m)) if job else (lambda m: None)
    c = sx.creds(db)
    if not client:
        if not c.get("login"):
            raise sx.ShopError("Не задано API-користувача Shop-Express")
        client = sx.ShopExpress(c["site"], c["login"], c["password"])
    log("Зіставляю товари сайту з CRM…")
    sx.sync(db, client)                                       # shop_id у картках — актуальні
    rows = list(client.products())
    log(f"Каталог сайту: {len(rows)} товарів")
    bdir = Path(settings.files_dir) / "shop_express"
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"export-{datetime.now():%Y%m%d-%H%M}.json"
    backup.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    by_shop: dict[str, list[Product]] = {}
    for p in db.scalars(select(Product).where(Product.shop_id.is_not(None))):
        by_shop.setdefault(str(p.shop_id), []).append(p)
    have = {(f.product_id, f.src_url) for f in db.scalars(select(ProductFile).where(ProductFile.src_url.is_not(None)))}
    by_src: dict[str, str] = {f.src_url: f.path for f in db.scalars(select(ProductFile).where(ProductFile.src_url.is_not(None)))}
    http = http or httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "MOLODIST-CRM/1.0"})
    root_host = urlparse(client.root).netloc
    rep = {"at": datetime.now().isoformat(timespec="minutes"), "rows": len(rows), "products": 0, "no_match": 0, "texts": 0,
           "photos_new": 0, "photos_skipped": 0, "photos_failed": 0, "backup": backup.name, "disk_stop": False, "no_match_list": []}
    now = datetime.now()
    for i, row in enumerate(rows, 1):
        sid = str(row.get("id") or "")
        targets = by_shop.get(sid) or []
        if not targets:
            rep["no_match"] += 1
            if len(rep["no_match_list"]) < 40:
                t = row.get("title")
                rep["no_match_list"].append(f"{row.get('sku') or '—'} · {(t.get('ua') if isinstance(t, dict) else t) or ''}"[:120])
            continue
        info = info_of(row)
        info["migrated_at"] = now.isoformat(timespec="minutes")
        urls = _urls(row, client.root)
        info["images"] = urls
        for p in targets:
            old = p.site_info or {}
            keep = {k: old[k] for k in ("short_description", "seo_title", "seo_description") if old.get("edited_at") and k in old}
            p.site_info = {**old, **info, **keep}                   # правки, зроблені вже в CRM, не затираємо
            if p.site_on is None:
                p.site_on = bool(row.get("display_in_showcase", True)) and str(row.get("presence") or "").lower() not in ("not_available", "0")
            rep["products"] += 1
            rep["texts"] += 1
        if download and urls:
            for n, u in enumerate(urls):
                for p in targets:
                    if (p.id, u) in have:
                        rep["photos_skipped"] += 1
                        continue
                    path = by_src.get(u)
                    if not path:
                        if _free() < MIN_FREE:
                            rep["disk_stop"] = True
                            break
                        try:
                            got = _download(http, u, root_host)
                        except httpx.HTTPError:
                            got = None
                        if not got:
                            rep["photos_failed"] += 1
                            continue
                        data, ctype = got
                        ext = mimetypes.guess_extension(ctype) or Path(urlparse(u).path).suffix or ".jpg"
                        name = (Path(urlparse(u).path).stem or hashlib.md5(u.encode()).hexdigest()[:10])[:150] + ext
                        path = ingest.store(data, f"product_{name}")
                        by_src[u] = path
                    f = ProductFile(product_id=p.id, path=path, name=Path(path).name.split("_", 1)[-1][:200], kind="photo",
                                    mime=mimetypes.guess_type(path)[0], src_url=u[:500], site=True, sort=n)
                    db.add(f)
                    db.flush()
                    have.add((p.id, u))
                    rep["photos_new"] += 1
                    if n == 0 or not (p.photo_url or "").startswith("/products/files/"):
                        p.photo_url = f"/products/files/{f.id}"            # головне фото — наша копія
            if rep["disk_stop"]:
                log("Мало місця на диску — зупинено завантаження фото")
                break
        if i % 20 == 0:
            db.commit()
            log(f"Оброблено {i} з {len(rows)} · нових фото {rep['photos_new']}")
    db.commit()
    db.merge(SyncState(key=KEY, value=json.dumps(rep, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    return rep


def last(db: Session) -> dict | None:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else None
    except ValueError:
        return None


def coverage(db: Session) -> dict:
    """Скільки товарів сайту вже мають усе своє: фото на сервері, опис."""
    rows = db.scalars(select(Product).where(Product.shop_id.is_not(None))).all()
    local = sum(1 for p in rows if (p.photo_url or "").startswith("/products/files/"))
    desc = sum(1 for p in rows if (p.site_info or {}).get("short_description"))
    remote = sum(1 for p in rows if (p.photo_url or "").startswith("http"))
    return {"site_products": len(rows), "local_photo": local, "remote_photo": remote, "with_desc": desc}
