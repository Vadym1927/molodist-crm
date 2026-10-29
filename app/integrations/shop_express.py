"""Shop-Express (molodist.store) → картки товарів CRM: посилання на товар у магазині, фото, ціна на сайті.

Доступ: API-користувач магазину (адмінка → API → користувачі): логін і пароль. Схема Shop-Express:
POST {сайт}/api/auth/ {"login", "password"} → token; POST {сайт}/api/catalog/export/?token=… {"limit", "offset"}
→ товари (article, title, url, images…). Публічної документації немає, тому розбір гнучкий, а назви полів відповіді
(без значень) зберігаються в SyncState shop_express_probe для діагностики. Зіставлення: артикул → штрихкод → назва."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, SyncState

log = logging.getLogger("shop_express")
AUTH_PATHS = ("api/auth/", "api/auth")
EXPORT_PATHS = ("api/catalog/export/", "api/catalog/export")
ID_KEYS = ("id", "product_id", "productId")
SKU_KEYS = ("article", "sku", "vendor_code", "code", "artikul")
NAME_KEYS = ("title", "name", "caption", "product_name")
URL_KEYS = ("url", "link", "href", "product_url", "seo_url", "uri")
IMG_KEYS = ("images", "image", "photos", "photo", "picture", "pictures", "img", "main_image", "image_url")
PRICE_KEYS = ("price", "cost", "price_uah")
BARCODE_KEYS = ("barcode", "ean", "ean13", "gtin")


class ShopError(RuntimeError):
    pass


def _cipher():
    from .cleverbox import _token_cipher
    return _token_cipher()


def creds(db: Session) -> dict:
    st = db.get(SyncState, "shop_express_enc")
    if not st or not st.value:
        return {}
    try:
        return json.loads(_cipher().decrypt(st.value.encode()).decode())
    except Exception:  # noqa: BLE001
        return {}


def save_creds(db: Session, site: str, login: str, password: str) -> None:
    prev = creds(db)
    data = {"site": (site or prev.get("site") or "https://molodist.store/").strip(), "login": login.strip() or prev.get("login", ""),
            "password": password or prev.get("password", "")}
    if not data["site"].endswith("/"):
        data["site"] += "/"
    st = db.get(SyncState, "shop_express_enc") or SyncState(key="shop_express_enc")
    st.value, st.updated_at = _cipher().encrypt(json.dumps(data).encode()).decode(), datetime.utcnow()
    db.merge(st)
    db.commit()


def clear(db: Session) -> None:
    for k in ("shop_express_enc", "shop_express_probe"):
        st = db.get(SyncState, k)
        if st:
            db.delete(st)
    db.commit()


def _root(site: str) -> str:
    """https://molodist.store/eco/ukr/api/users → https://molodist.store/"""
    from urllib.parse import urlparse
    u = urlparse(site if "://" in site else "https://" + site)
    return f"{u.scheme}://{u.netloc}/"


def _resp(j) -> object:
    if isinstance(j, dict):
        if str(j.get("status", "")).upper() in ("ERROR", "FAIL") or j.get("error"):
            raise ShopError(str(j.get("message") or j.get("error") or j.get("response") or j)[:200])
        return j.get("response", j.get("data", j))
    return j


class ShopExpress:
    def __init__(self, site: str, login: str, password: str, http: httpx.Client | None = None):
        self.root = _root(site)
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.token = None
        last = None
        for p in AUTH_PATHS:
            try:
                r = self.http.post(urljoin(self.root, p), json={"login": login, "password": password})
                if r.status_code == 404:
                    continue
                j = _resp(r.json())
                tok = j.get("token") if isinstance(j, dict) else None
                if tok:
                    self.token = tok
                    return
                last = f"{r.status_code}: {str(j)[:150]}"
            except (ValueError, httpx.HTTPError, ShopError) as e:
                last = str(e)[:150]
        raise ShopError(f"вхід API-користувача не вдався ({last or 'немає відповіді'})")

    def products(self, page: int = 500):
        offset = 0
        path = None
        while True:
            for p in ([path] if path else EXPORT_PATHS):
                r = self.http.post(urljoin(self.root, p), params={"token": self.token},
                                   json={"limit": page, "offset": offset, "display_in_showcase": False})
                if r.status_code == 404 and not path:
                    continue
                path = p
                break
            if r.status_code >= 400:
                raise ShopError(f"експорт каталогу: {r.status_code} {r.text[:150]}")
            j = _resp(r.json())
            if isinstance(j, dict):
                rows = j.get("products") or j.get("items") or j.get("data") or j.get("catalog") or []
            else:
                rows = j
            if isinstance(rows, dict):
                rows = list(rows.values())
            rows = [x for x in rows or [] if isinstance(x, dict)]
            yield from rows
            offset += len(rows)
            if len(rows) < page:
                break


def _first(row: dict, keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _img(v, root: str) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("src") or v.get("original") or v.get("big") or next(iter(v.values()), None)
    if not v or not isinstance(v, str):
        return None
    return v if v.startswith("http") else urljoin(root, v.lstrip("/"))


def sync(db: Session, client: ShopExpress | None = None) -> dict:
    """Заповнює в товарах CRM: посилання на сайті, фото, ціну на сайті. Ручне посилання не затирає."""
    from ..parsers.observ import norm
    from ..services.catalog import match_product
    c = creds(db)
    if not client:
        if not c.get("login"):
            raise ShopError("Не задано API-користувача Shop-Express")
        client = ShopExpress(c["site"], c["login"], c["password"])
    products = db.scalars(select(Product)).all()
    by_sku = {p.sku.strip().lower(): p for p in products if p.sku}
    by_bar = {p.barcode.strip(): p for p in products if p.barcode}
    by_shop = {p.shop_id: p for p in products if p.shop_id}
    keys, n, matched, filled_url, filled_img = set(), 0, 0, 0, 0
    unmatched = []
    now = datetime.now()
    for row in client.products():
        n += 1
        keys.update(row.keys())
        sid = str(_first(row, ID_KEYS) or "")
        sku = str(_first(row, SKU_KEYS) or "").strip()
        bar = str(_first(row, BARCODE_KEYS) or "").strip()
        name = str(_first(row, NAME_KEYS) or "")
        if isinstance(_first(row, NAME_KEYS), dict):                     # мультимовні назви {ua: …, ru: …}
            nm = _first(row, NAME_KEYS)
            name = nm.get("ua") or nm.get("uk") or next(iter(nm.values()), "")
        p = by_shop.get(sid) or by_sku.get(sku.lower()) or by_bar.get(bar) or (match_product(db, name, products) if name else None)
        if not p:
            if len(unmatched) < 30:
                unmatched.append(f"{sku or '—'} · {name[:70]}")
            continue
        matched += 1
        p.shop_id = sid or p.shop_id
        url = _first(row, URL_KEYS)
        if isinstance(url, dict):
            url = url.get("ua") or url.get("uk") or next(iter(url.values()), None)
        if url and isinstance(url, str):
            full = url if url.startswith("http") else urljoin(client.root, url.lstrip("/"))
            if not p.url or "molodist.store" in (p.url or ""):
                if p.url != full:
                    filled_url += 1
                p.url = full[:500]
        img = _img(_first(row, IMG_KEYS), client.root)
        if img:
            if p.photo_url != img:
                filled_img += 1
            p.photo_url = img[:500]
        try:
            pr = float(str(_first(row, PRICE_KEYS) or "").replace(",", "."))
            p.shop_price = pr if pr > 0 else p.shop_price
        except ValueError:
            pass
        p.shop_synced_at = now
    db.commit()
    rep = {"at": now.isoformat(timespec="minutes"), "rows": n, "matched": matched, "urls": filled_url, "photos": filled_img,
           "unmatched": unmatched, "keys": sorted(keys)[:60]}
    st = db.get(SyncState, "shop_express_probe") or SyncState(key="shop_express_probe")
    st.value, st.updated_at = json.dumps(rep, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()
    return rep


def last_report(db: Session) -> dict | None:
    st = db.get(SyncState, "shop_express_probe")
    try:
        return json.loads(st.value) if st and st.value else None
    except ValueError:
        return None
