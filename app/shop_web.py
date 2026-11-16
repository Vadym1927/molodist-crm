"""Маршрути власного інтернет-магазину (/_shop/…; на домені магазину — без префікса). Логіка — services/shop_front.py."""
from __future__ import annotations

import hmac
import json
import re
import time
from datetime import date
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .config import settings
from .models import ProductFile, User
from .security import db_dep

PER_PAGE = 24
_RATE: dict = {}


def _rate(request: Request, key: str, limit: int, window: int = 600) -> bool:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    now, k = time.time(), f"{key}:{ip}"
    hits = [t for t in _RATE.get(k, []) if now - t < window]
    _RATE[k] = hits + [now]
    if len(_RATE) > 5000:
        for kk in [x for x, v in _RATE.items() if not v or now - v[-1] > window]:
            _RATE.pop(kk, None)
    return len(hits) < limit


def register(app, templates, version: str):
    from .services import shop_front as sf, site_pages as sp

    def opened(request: Request, db: Session, c: dict) -> bool:
        if c.get("on"):
            return True
        k = str(c.get("preview_key") or "")
        got = request.query_params.get("p") or request.cookies.get("shop_prev") or ""
        if k and got and hmac.compare_digest(str(got).encode(), k.encode()):
            return True
        uid = request.session.get("uid") if "session" in request.scope else None
        u = db.get(User, uid) if uid else None
        return bool(u and u.active)

    def ctx(request: Request, db: Session, path: str = "/") -> dict:
        c = sf.cfg(db)
        if not opened(request, db, c):
            raise HTTPException(404)
        host = (request.headers.get("host") or "").split(":")[0].lower()
        on_site = host in sf.hosts()
        data = sf.catalog(db)
        site = sp.cfg(db)
        return {"request": request, "s": c, "c": site, "d": data, "base": "" if on_site else "/_shop", "noindex": not on_site,
                "canonical": c["canonical"].rstrip("/") + path, "ver": str(version)[:8], "year": date.today().year, "money": sp.money,
                "benefits": sf.lines(c.get("benefits")), "viber": sp.viber_link(db, site), "path": path, "q": "",
                "pkey": request.query_params.get("p") or ""}

    def resp(request: Request, name: str, cx: dict, status: int = 200):
        r = templates.TemplateResponse(request, name, cx, status_code=status)
        r.headers["Cache-Control"] = "public, max-age=120" if not cx["noindex"] else "no-store"
        if cx["noindex"]:
            r.headers["X-Robots-Tag"] = "noindex, nofollow"
        if cx.get("pkey"):
            r.set_cookie("shop_prev", cx["pkey"], max_age=30 * 86400, httponly=True, samesite="lax")
        return r

    def not_found(request: Request, cx: dict):
        cx.update(title="Сторінку не знайдено — MOLODIST", desc="", items=sf.home_blocks(cx["d"])["best"][:8])
        return resp(request, "shop/404.html", cx, 404)

    @app.get("/_shop/", response_class=HTMLResponse)
    @app.get("/_shop", response_class=HTMLResponse, include_in_schema=False)
    def shop_home(request: Request, db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/")
        h = cx["d"]["home"]
        cx.update(title=h.get("title") or "Професійна косметика для обличчя та тіла | MOLODIST",
                  desc=h.get("desc") or "Професійна косметика від лікарів MOLODIST: оригінальна продукція, підбір догляду, доставка по Україні.",
                  blocks=sf.home_blocks(cx["d"]), blog=[p for p in cx["d"]["pages"].values() if p["path"].startswith("/blog/") and p["path"] != "/blog/"][:3])
        return resp(request, "shop/home.html", cx)

    @app.get("/_shop/robots.txt")
    def shop_robots(db: Session = Depends(db_dep)):
        b = sf.cfg(db)["canonical"].rstrip("/")
        return Response(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /checkout/\nDisallow: /shop/cart/\nSitemap: {b}/sitemap.xml\n", media_type="text/plain")

    @app.get("/_shop/sitemap.xml")
    @app.get("/_shop/contentsitemap.xml", include_in_schema=False)
    def shop_sitemap(db: Session = Depends(db_dep)):
        b, d = sf.cfg(db)["canonical"].rstrip("/"), sf.catalog(db)
        urls = ["/", "/shop/"] + [c["path"] for c in d["cats"] if c["n"]] + [f"/shop/{i['slug']}/" for i in d["items"]] + list(d["pages"])
        body = "".join(f"<url><loc>{b}{u}</loc></url>" for u in dict.fromkeys(urls))
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>', media_type="application/xml")

    @app.get("/_shop/img/{fid:int}")
    def shop_img(fid: int, w: int = 600, db: Session = Depends(db_dep)):
        f = db.get(ProductFile, fid)
        if not f or f.kind != "photo" or not f.site or not Path(f.path).exists():
            raise HTTPException(404)
        w = min((300, 600, 1200), key=lambda x: abs(x - w))
        cache = Path(settings.files_dir) / "_thumbs" / f"{fid}_{w}.webp"
        if not cache.exists():
            try:
                from PIL import Image
                cache.parent.mkdir(parents=True, exist_ok=True)
                im = Image.open(f.path)
                im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                im.thumbnail((w, w * 2))
                im.save(cache, "WEBP", quality=82)
            except Exception:  # noqa: BLE001
                return FileResponse(f.path, headers={"Cache-Control": "public, max-age=604800"})
        return FileResponse(cache, media_type="image/webp", headers={"Cache-Control": "public, max-age=2592000, immutable"})

    # ---------- API: кошик, замовлення, Нова пошта ----------
    @app.post("/_shop/api/cart")
    async def shop_cart(request: Request, db: Session = Depends(db_dep)):
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        return JSONResponse(sf.price_cart(db, body.get("items") or []), headers={"Cache-Control": "no-store"})

    @app.post("/_shop/api/order")
    async def shop_order(request: Request, db: Session = Depends(db_dep)):
        c = sf.cfg(db)
        if not opened(request, db, c):
            raise HTTPException(404)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        if body.get("website") or not _rate(request, "shopord", 8):
            return JSONResponse({"ok": False, "msg": "Забагато спроб. Зателефонуйте нам, будь ласка."}, status_code=429)
        host = (request.headers.get("host") or "").split(":")[0].lower()
        try:
            r = sf.create_order(db, body, settings.public_url)
        except sf.ShopError as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        base = "" if host in sf.hosts() else "/_shop"
        r["done_url"] = f"{base}/checkout/done/?n={r['number']}"
        if r.get("parts"):
            r["done_url"] += "&parts=1"
        return JSONResponse(r, headers={"Cache-Control": "no-store"})

    @app.get("/_shop/api/np/cities")
    def shop_np_cities(request: Request, q: str = "", db: Session = Depends(db_dep)):
        if len(q.strip()) < 2 or not _rate(request, "np", 300):
            return JSONResponse({"rows": []})
        return JSONResponse({"rows": sf.np_cities(sf.cfg(db), q.strip())}, headers={"Cache-Control": "public, max-age=600"})

    @app.get("/_shop/api/np/wh")
    def shop_np_wh(request: Request, city: str = "", q: str = "", db: Session = Depends(db_dep)):
        if not city or not _rate(request, "np", 300):
            return JSONResponse({"rows": []})
        return JSONResponse({"rows": sf.np_warehouses(sf.cfg(db), city, q.strip())}, headers={"Cache-Control": "public, max-age=600"})

    @app.post("/api/public/monoparts")
    async def shop_monoparts(request: Request, db: Session = Depends(db_dep)):
        from .services import mono_parts as mp
        ok = mp.callback(db, await request.body(), request.headers.get("signature") or "")
        return JSONResponse({"ok": ok}, status_code=200 if ok else 400)

    @app.get("/_shop/checkout/", response_class=HTMLResponse)
    @app.get("/_shop/shop/cart/", response_class=HTMLResponse, include_in_schema=False)
    def shop_checkout(request: Request, db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/checkout/")
        from .services import mono_parts as mp
        cx.update(parts=mp.public(db))
        cx.update(title="Оформлення замовлення — MOLODIST", desc="", noindex_page=True, np=bool((cx["s"].get("np_key") or "").strip()),
                  gifts=[g[0] for g in sf.lines(cx["s"].get("gifts"))])
        return resp(request, "shop/checkout.html", cx)

    @app.get("/_shop/checkout/done/", response_class=HTMLResponse)
    def shop_done(request: Request, n: str = "", db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/checkout/done/")
        cx.update(title="Дякуємо за замовлення — MOLODIST", desc="", noindex_page=True, number=re.sub(r"[^\w-]", "", n)[:20])
        cx.update(parts=bool(request.query_params.get("parts")))
        return resp(request, "shop/done.html", cx)

    # ---------- каталог ----------
    def render_list(request, cx, items, page, sort, brand, **kw):
        brands = sorted({i["brand"] for i in items if i["brand"]})
        rows = sf.listing(items, sort, brand)
        pages = max(1, -(-len(rows) // PER_PAGE))
        page = min(max(1, page), pages)
        cx.update(items=rows[(page - 1) * PER_PAGE: page * PER_PAGE], total=len(rows), page=page, pages=pages, sort=sort, brand=brand, brands=brands, **kw)
        return resp(request, "shop/list.html", cx)

    @app.get("/_shop/shop/", response_class=HTMLResponse)
    def shop_all(request: Request, q: str = "", search: str = "", order: str = "", brand: str = "", page: int = 1, db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/shop/")
        q = (q or search).strip()[:80]
        if q:
            cx.update(title=f"Пошук: {q} — MOLODIST", desc="", q=q, noindex_page=True)
            return render_list(request, cx, sf.search(cx["d"], q), page, order, brand, h1=f"Пошук: «{q}»", cat=None, subs=[], crumbs=[], page_base="/shop/")
        cx.update(title="Магазин професійної косметики | MOLODIST", desc="Каталог професійної косметики MOLODIST: догляд за обличчям, тілом і волоссям. Доставка по Україні.")
        return render_list(request, cx, cx["d"]["items"], page, order, brand, h1="Магазин", cat=None, subs=cx["d"]["menu"], crumbs=[], page_base="/shop/")

    @app.get("/_shop/shop/{rest:path}", response_class=HTMLResponse)
    def shop_path(request: Request, rest: str, order: str = "", brand: str = "", page: int = 1, db: Session = Depends(db_dep)):
        segs = [x for x in rest.split("/") if x]
        cx = ctx(request, db, "/shop/" + "/".join(segs) + "/")
        d = cx["d"]
        if not segs:
            return RedirectResponse(cx["base"] + "/shop/", 301)
        if "filters" in segs:                                    # старі адреси фільтрів → категорія
            return RedirectResponse(cx["base"] + "/shop/" + "/".join(segs[:segs.index("filters")]) + "/", 301)
        if segs[-1].startswith("page-") and segs[-1][5:].isdigit():
            page, segs = int(segs[-1][5:]), segs[:-1]
        it = d["by_slug"].get(segs[0])
        if it and len(segs) <= 2:
            if len(segs) == 2:                                    # /shop/<slug>/<id>/ → канонічна адреса товару
                return RedirectResponse(f"{cx['base']}/shop/{it['slug']}/", 301)
            cats = [c for c in d["cats"] if it["slug"] in c["slugs"] and not c["path"].startswith("/shop/brendi/")]
            cat = max(cats, key=lambda c: c["depth"]) if cats else None
            rel = [x for x in (d["by_slug"][s] for s in (cat["slugs"] if cat else [])) if x["slug"] != it["slug"] and x["stock"]][:8]
            cx.update(canonical=cx["s"]["canonical"].rstrip("/") + f"/shop/{it['slug']}/", it=it, cat=cat, rel=rel,
                      title=it["seo_title"] or f"{it['name']} — купити в MOLODIST", desc=it["seo_desc"] or it["name"],
                      jsonld=json.dumps({"@context": "https://schema.org/", "@type": "Product", "name": it["name"], "sku": it["sku"],
                                         "brand": {"@type": "Brand", "name": it["brand"]} if it["brand"] else None,
                                         "image": [f"{cx['s']['canonical'].rstrip('/')}/img/{i}?w=1200" for i in it["imgs"][:4]],
                                         "description": it["seo_desc"] or it["name"],
                                         "offers": {"@type": "Offer", "priceCurrency": "UAH", "price": f"{it['price']:.0f}",
                                                    "url": cx["s"]["canonical"].rstrip("/") + f"/shop/{it['slug']}/",
                                                    "availability": "https://schema.org/" + ("InStock" if it["stock"] else "OutOfStock")}}, ensure_ascii=False))
            return resp(request, "shop/product.html", cx)
        path = "/shop/" + "/".join(segs) + "/"
        cat = d["by_path"].get(path)
        if not cat:
            return not_found(request, cx)
        if path == "/shop/brendi/":
            cx.update(title=cat.get("title") or "Бренди — MOLODIST", desc=cat.get("desc") or "", h1="Бренди")
            return resp(request, "shop/brands.html", cx)
        crumbs, p = [], "/shop/"
        for sg in segs[:-1]:
            p += sg + "/"
            if p in d["by_path"]:
                crumbs.append(d["by_path"][p])
        cx.update(canonical=cx["s"]["canonical"].rstrip("/") + path + (f"page-{page}/" if page > 1 else ""),
                  title=(cat.get("title") or cat["name"]) + (f" — сторінка {page}" if page > 1 else ""), desc=cat.get("desc") or "")
        return render_list(request, cx, [d["by_slug"][s] for s in cat["slugs"]], page, order, brand, h1=cat["name"], cat=cat,
                           subs=sf.children(cat, d), crumbs=crumbs, page_base=path)

    @app.get("/_shop/{rest:path}", response_class=HTMLResponse)
    def shop_page(request: Request, rest: str, db: Session = Depends(db_dep)):
        path = "/" + "/".join(x for x in rest.split("/") if x) + "/"
        cx = ctx(request, db, path)
        pg = cx["d"]["pages"].get(path)
        if not pg:
            return not_found(request, cx)
        cx.update(title=pg.get("title") or pg.get("h1") or "MOLODIST", desc=pg.get("desc") or "", pg=pg,
                  posts=[p for p in cx["d"]["pages"].values() if p["path"].startswith("/blog/") and p["path"] != "/blog/"] if path == "/blog/" else [])
        return resp(request, "shop/page.html", cx)
