"""Маршрути власного інтернет-магазину (/_shop/…; на домені магазину — без префікса). Логіка — services/shop_front.py."""
from __future__ import annotations

import hmac
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import select
from .models import SiteOrder

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


def _same_origin(request: Request) -> bool:
    """POST з чужого сайту (Origin / Referer іншого домену) — відхиляємо; без заголовків (тести, старі браузери) — пропускаємо."""
    from urllib.parse import urlparse
    host = (request.headers.get("host") or "").split(":")[0].lower()
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    if not src or src == "null":
        return not src
    return (urlparse(src).hostname or "").lower() == host


def turnstile_ok(c: dict, token: str, ip: str = "") -> bool:
    """Cloudflare Turnstile (якщо ключі задано в «Інтеграції»): перевірка токена на сервері."""
    secret = str(c.get("ts_secret") or "").strip()
    if not secret or not str(c.get("ts_site") or "").strip():
        return True
    if not token:
        return False
    try:
        import httpx
        r = httpx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={"secret": secret, "response": token, "remoteip": ip}, timeout=8)
        return bool(r.json().get("success"))
    except Exception:  # noqa: BLE001 — сервіс недоступний: не блокуємо покупців
        return True


def human(request: Request, body: dict, c: dict) -> str:
    """'' — ок; інакше текст помилки. Пастка-поле, той самий сайт, Turnstile."""
    if body.get("website"):
        return "Заявку відхилено."
    if not _same_origin(request):
        return "Заявку відхилено."
    if not turnstile_ok(c, str(body.get("cft") or ""), request.headers.get("x-real-ip") or ""):
        return "Підтвердіть, будь ласка, що ви не робот (галочка над кнопкою)."
    return ""


def register(app, templates, version: str):
    from .services import shop_front as sf, site_pages as sp, shop_cabinet as sc

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

    def _book_url(db, site) -> str:
        from .services import online_booking as ob
        return ((site.get("canonical") or "").rstrip("/") if ob.cfg(db).get("on") else "/_site") + f"/{ob.SLUG}?g=" + "Консультації"

    def _cert_url(db, site) -> str:
        from .services import online_booking as ob
        return ((site.get("canonical") or "").rstrip("/") if ob.cfg(db).get("on") else "/_site") + f"/{ob.SLUG}#sertyfikaty"

    def ctx(request: Request, db: Session, path: str = "/") -> dict:
        c = sf.cfg(db)
        if not opened(request, db, c):
            raise HTTPException(404)
        host = (request.headers.get("host") or "").split(":")[0].lower()
        on_site = host in sf.hosts()
        data = sf.catalog(db)
        site = sp.cfg(db)
        from .services import mono_parts as _mp
        pp = _mp.public(db)
        got = sc.me(db, request.cookies.get(sc.COOKIE))
        return {"me": got[0] if got else None, "me_p": got[1] if got else None, "ref_in": (request.query_params.get("ref") or "")[:20],
                "pch": float(pp["min_sum"] or 0) if pp["ready"] else None, "pch_legal": sf.PARTS_LEGAL, "pch_show": bool(pp["ready"] or pp.get("on")), "request": request, "s": c, "c": site, "d": data, "base": "" if on_site else "/_shop", "noindex": not on_site,
                "canonical": c["canonical"].rstrip("/") + path, "ver": str(version)[:8], "year": date.today().year, "money": sp.money,
                "og_title": "", "og_desc": "", "og_img": (c["canonical"].rstrip("/") if on_site else settings.public_url.rstrip("/")) + "/static/shop/og/default.jpg", "abs": (c["canonical"].rstrip("/") if on_site else settings.public_url.rstrip("/")),
                "benefits": sf.lines(c.get("benefits")), "coin_earn": lambda price, sale=False: __import__("app.services.coins", fromlist=["x"]).earn_for(c, price, sale), "cat_icon": sf.cat_icon, "brand_text": __import__("app.services.shop_seo", fromlist=["x"]).brand_text, "cert_url": _cert_url(db, site), "book_url": _book_url(db, site), "viber": sp.viber_link(db, site), "path": path, "q": "",
                "pkey": request.query_params.get("p") or "", "grev": __import__("app.services.google_reviews", fromlist=["x"]).public(db, c)}

    def resp(request: Request, name: str, cx: dict, status: int = 200):
        from .services import shop_seo as _seo
        if cx.get("title"):
            cx["title"] = _seo.clean_title(cx["title"])
            if "desc" in cx and not cx.get("noindex_page"):
                cx["desc"] = _seo.clean_desc(cx.get("desc") or "", cx["title"])
        r = templates.TemplateResponse(request, name, cx, status_code=status)
        r.headers["Cache-Control"] = "public, max-age=120" if not (cx["noindex"] or cx.get("me") or cx.get("private")) else "no-store"
        if cx.get("me") or cx.get("private"):
            r.headers["Vary"] = "Cookie"
        if cx.get("ref_in") and not cx.get("me"):
            r.set_cookie(sc.REF_COOKIE, cx["ref_in"].upper(), max_age=30 * 86400, httponly=True, samesite="lax")
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
        cx.update(title="MOLODIST — професійна косметика, яку призначають лікарі",
                  desc="Medik8, AG SKIN, Image Skincare, Rejuran та інші бренди від лікарів MOLODIST. Оригінал, Покупка Частинами monobank, доставка по Україні — безкоштовно від 3 000 ₴.",
                  **({"og_title": f"Вам подарунок: {cx['s'].get('cab_coin_ref')} MOLODIST Coins на професійну косметику",
                      "og_desc": f"Зареєструйтесь у магазині MOLODIST за запрошенням друга й отримайте {cx['s'].get('cab_coin_ref')} MOLODIST Coins. Професійна косметика, яку призначають лікарі.",
                      "og_img": f"{cx['abs']}/static/shop/og/invite.jpg"} if cx["ref_in"] else {}),
                  insta=__import__("app.services.insta_feed", fromlist=["x"]).items(db),
                  insta_p=__import__("app.services.insta_feed", fromlist=["x"]).state(db).get("profile") or {},
                  blocks=sf.home_blocks(cx["d"]), blog=_blog(cx["d"], 4))
        _root = cx["s"]["canonical"].rstrip("/")
        cx["jsonld"] = json.dumps({"@context": "https://schema.org", "@graph": [
            {"@type": "OnlineStore", "@id": _root + "/#store", "name": "MOLODIST", "url": _root + "/",
             "logo": _root + "/static/site/brand/wordmark-graphite-600.png", "description": cx.get("desc") or "",
             "telephone": cx["c"].get("phone_text") or "", "email": cx["c"].get("email") or "",
             "address": {"@type": "PostalAddress", "streetAddress": "вул. Князя Романа Мстиславича, 28А", "addressLocality": "Київ", "addressCountry": "UA"},
             "sameAs": [x for x in (cx["c"].get("instagram"), cx["c"].get("telegram"), cx["c"].get("main_url")) if x]},
            {"@type": "WebSite", "url": _root + "/", "name": "MOLODIST",
             "potentialAction": {"@type": "SearchAction", "target": _root + "/shop/?q={search_term_string}", "query-input": "required name=search_term_string"}}]},
            ensure_ascii=False)
        return resp(request, "shop/home.html", cx)

    def _feed_root(db, c):
        return c["canonical"].rstrip("/")

    @app.get("/_shop/feeds/{name}.xml", include_in_schema=False)
    def shop_feed(name: str, db: Session = Depends(db_dep)):
        from .services import shop_feeds as fd
        c = sf.cfg(db)
        if name not in fd.FEED_KEYS or not c.get(f"feed_{name}", True):
            raise HTTPException(404)
        d = sf.catalog(db)
        nb = {x["name"] for x in d["cats"] if x.get("notice") and x["path"].startswith("/shop/brendi/")}
        if name == "hotline":
            if not str(c.get("hotline_id") or "").strip():
                raise HTTPException(404)
            body = fd.hotline(d, _feed_root(db, c), str(c.get("hotline_id")), "MOLODIST")
        else:
            body = fd.google(d, _feed_root(db, c), "MOLODIST", nb)
        return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=1800"})

    @app.get("/_shop/llms.txt", include_in_schema=False)
    @app.get("/_shop/llms-full.txt", include_in_schema=False)
    def shop_llms(request: Request, db: Session = Depends(db_dep)):
        from .services import shop_feeds as fd
        c = sf.cfg(db)
        if not c.get("llms_on", True):
            raise HTTPException(404)
        body = fd.llms(sf.catalog(db), _feed_root(db, c), c, sp.cfg(db), full=request.url.path.endswith("-full.txt"))
        return Response(body, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/_shop/robots.txt")
    def shop_robots(db: Session = Depends(db_dep)):
        b = sf.cfg(db)["canonical"].rstrip("/")
        return Response(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /checkout/\nDisallow: /shop/cart/\nDisallow: /cabinet/\nDisallow: /review/\nDisallow: /wish/\nDisallow: /*?*order=\nDisallow: /*?*brand=\nSitemap: {b}/sitemap.xml\n", media_type="text/plain")

    @app.get("/_shop/sitemap.xml")
    @app.get("/_shop/contentsitemap.xml", include_in_schema=False)
    @app.get("/_shop/productcatalogsitemap.xml", include_in_schema=False)
    def shop_sitemap(db: Session = Depends(db_dep)):
        b, d = sf.cfg(db)["canonical"].rstrip("/"), sf.catalog(db)
        urls = ["/", "/shop/"] + (["/pidbir/"] if sf.cfg(db).get("quiz_on", True) else []) + [c["path"] for c in d["cats"] if c["n"] or c.get("notice")] + [f"/shop/{i['slug']}/" for i in d["items"]] + [f"/shop/{s}/" for s in d.get("archive") or {}] + list(d["pages"])
        body = "".join(f"<url><loc>{b}{u}</loc></url>" for u in dict.fromkeys(urls))
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>', media_type="application/xml")

    @app.get("/_shop/insta/{name}")
    def shop_insta(name: str):
        from fastapi.responses import FileResponse as _FR
        from .services import insta_feed as _if
        f = _if.folder() / name
        if not re.fullmatch(r"\d+\.jpg", name) or not f.is_file():
            raise HTTPException(404)
        return _FR(f, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/_shop/brand/{name}")
    def shop_brand(name: str):
        import re as _re
        from fastapi.responses import FileResponse as _FR
        f = sf.brand_dir() / name
        if not _re.fullmatch(r"[a-z0-9-]+\.(png|svg|webp|jpg)", name) or not f.is_file():
            raise HTTPException(404)
        return _FR(f, headers={"Cache-Control": "public, max-age=2592000", "X-Content-Type-Options": "nosniff",
                               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    @app.get("/_shop/img/{fid:int}")
    def shop_img(fid: int, w: int = 600, bg: str = "", db: Session = Depends(db_dep)):
        f = db.get(ProductFile, fid)
        if not f or f.kind != "photo" or not f.site or not Path(f.path).exists():
            raise HTTPException(404)
        w = min((300, 600, 1200), key=lambda x: abs(x - w))
        if re.fullmatch(r"[0-9a-fA-F]{6}", bg or ""):             # для email: прозорий фон → суцільний колір, JPEG (Gmail робить прозоре чорним)
            cache = Path(settings.files_dir) / "_thumbs" / f"{fid}_{w}_{bg.lower()}.jpg"
            if not cache.exists():
                try:
                    from PIL import Image
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    im = Image.open(f.path).convert("RGBA")
                    im.thumbnail((w, w * 2))
                    out = Image.new("RGB", im.size, tuple(int(bg[i:i + 2], 16) for i in (0, 2, 4)))
                    out.paste(im, mask=im.split()[3])
                    out.save(cache, "JPEG", quality=85)
                except Exception:  # noqa: BLE001
                    return FileResponse(f.path)
            return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=2592000, immutable"})
        cache = Path(settings.files_dir) / "_thumbs" / f"{fid}_{w}{'m' if w >= 1200 else ''}.webp"
        if not cache.exists():
            try:
                from PIL import Image
                cache.parent.mkdir(parents=True, exist_ok=True)
                im = Image.open(f.path)
                im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                im.thumbnail((w, w * 2))
                if w >= 1200 and min(im.size) < 600:          # фід Google: фото ≥ 500×500 — маленькі збільшуємо до 600 по меншій стороні
                    k = 600 / min(im.size)
                    im = im.resize((round(im.size[0] * k), round(im.size[1] * k)), Image.LANCZOS)
                im.save(cache, "WEBP", quality=82)
            except Exception:  # noqa: BLE001
                return FileResponse(f.path, headers={"Cache-Control": "public, max-age=604800"})
        return FileResponse(cache, media_type="image/webp", headers={"Cache-Control": "public, max-age=2592000, immutable"})

    # ---------- API: кошик, замовлення, Нова пошта ----------
    @app.get("/_shop/api/search")
    def shop_search_api(request: Request, q: str = "", db: Session = Depends(db_dep)):
        c = sf.cfg(db)
        if not opened(request, db, c) or len(q.strip()) < 3:
            return JSONResponse({"rows": [], "n": 0})
        hits = sf.search(sf.catalog(db), q[:60])
        hits.sort(key=lambda i: (not i["stock"], -i["sold"]))
        return JSONResponse({"n": len(hits), "rows": [{"name": i["name"], "brand": i["brand"], "price": i["price"], "old": i["old"], "slug": i["slug"],
                                                       "img": i["imgs"][0] if i["imgs"] else None, "stock": i["stock"]} for i in hits[:8]]},
                            headers={"Cache-Control": "no-store"})

    @app.post("/_shop/api/cart")
    async def shop_cart(request: Request, db: Session = Depends(db_dep)):
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        cart = sf.price_cart(db, body.get("items") or [])
        from .services import coins as cn
        got = sc.me(db, request.cookies.get(sc.COOKIE))
        want = body.get("coins")
        cart["coins"] = cn.quote(db, sf.cfg(db), cart["items"], got[1].id if got else None, None if want is None else int(want or 0))
        cart["coins"]["me"] = bool(got)
        if body.get("promo"):
            from .services import shop_promo as _pr
            if not _rate(request, "promo", 40):
                cart["promo"] = {"ok": False, "msg": "Забагато спроб. Спробуйте пізніше.", "amount": 0}
            else:
                pr = _pr.check(db, body.get("promo"), cart["items"], got[1].phone if got else "")
                cart["promo"] = {k: pr.get(k) for k in ("ok", "code", "amount", "msg")}
        return JSONResponse(cart, headers={"Cache-Control": "no-store"})

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
        bad = human(request, body, c)
        if bad:
            return JSONResponse({"ok": False, "msg": bad}, status_code=400)
        host = (request.headers.get("host") or "").split(":")[0].lower()
        body = {k: v for k, v in body.items() if not str(k).startswith("_")}
        body["_done_base"] = (f"https://{host}" if host in sf.hosts() else settings.public_url.rstrip("/") + "/_shop") + "/checkout/done/?n="
        got = sc.me(db, request.cookies.get(sc.COOKIE))
        if not got and c.get("order_login", True) and c.get("cab_on", True):
            return JSONResponse({"ok": False, "msg": "Щоб оформити замовлення, увійдіть або зареєструйтесь.", "login": True}, status_code=401)
        if got:
            body["_patient_id"] = got[1].id
        try:
            r = sf.create_order(db, body, settings.public_url)
        except sf.ShopError as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        if got:
            try:
                from .services import shop_carts as _ct
                _ct.ordered(db, got[1].id)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
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

    @app.post("/api/public/shop-import")
    async def shop_import(request: Request, db: Session = Depends(db_dep)):
        """Структура старого сайту, знята в браузері власника: тіло — JSON {k: ключ перегляду, cats, pages, home}."""
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(400) from None
        k = str(sf.cfg(db).get("preview_key") or "")
        if not k or not hmac.compare_digest(str(body.get("k") or "").encode(), k.encode()) or not _rate(request, "shopimp", 10):
            raise HTTPException(403)
        try:
            return JSONResponse(sf.import_site(db, body))
        except sf.ShopError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    def _body(raw: bytes) -> dict:
        try:
            d = json.loads(raw or b"{}")
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def _ip(request: Request) -> str:
        return (request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for") or (request.client.host if request.client else "") or "").split(",")[0].strip()

    def _cab_guard(request: Request, db: Session) -> dict:
        c = sf.cfg(db)
        if not opened(request, db, c) or "json" not in (request.headers.get("content-type") or ""):
            raise HTTPException(404)
        return c

    @app.post("/_shop/api/cab/start")
    async def cab_start(request: Request, db: Session = Depends(db_dep)):
        c = _cab_guard(request, db)
        d = _body(await request.body())
        if d.get("website") or not _rate(request, "cabcode", 10):
            return JSONResponse({"ok": False, "msg": "Забагато спроб. Спробуйте пізніше."}, status_code=429)
        bad = human(request, d, c)
        if bad:
            return JSONResponse({"ok": False, "msg": bad}, status_code=400)
        try:
            return JSONResponse(sc.start(db, c, d.get("phone"), _ip(request)), headers={"Cache-Control": "no-store"})
        except sc.CabError as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)

    @app.post("/_shop/api/cab/verify")
    async def cab_verify(request: Request, db: Session = Depends(db_dep)):
        c = _cab_guard(request, db)
        d = _body(await request.body())
        if not _rate(request, "cabver", 30):
            return JSONResponse({"ok": False, "msg": "Забагато спроб. Спробуйте пізніше."}, status_code=429)
        try:
            token, days, acc = sc.verify(db, c, d.get("phone"), d.get("code"), d.get("name") or "", bool(d.get("remember", True)),
                                         str(d.get("ref") or "").strip() or request.cookies.get(sc.REF_COOKIE) or "", request.headers.get("user-agent") or "")
        except sc.CabError as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        if d.get("wish"):
            sc.set_wish(db, acc, d.get("wish"), merge=True)
        r = JSONResponse({"ok": True, "wish": acc.wishlist or []}, headers={"Cache-Control": "no-store"})
        r.set_cookie(sc.COOKIE, token, max_age=days * 86400 if d.get("remember", True) else None, httponly=True, samesite="lax",
                     secure=request.url.scheme == "https" or (request.headers.get("x-forwarded-proto") == "https"))
        r.delete_cookie(sc.REF_COOKIE)
        return r

    @app.post("/_shop/api/notify")
    async def shop_notify(request: Request, db: Session = Depends(db_dep)):
        _cab_guard(request, db)
        d = _body(await request.body())
        if d.get("website") or not _rate(request, "shopntf", 12):
            return JSONResponse({"ok": False, "msg": "Забагато спроб. Спробуйте пізніше."}, status_code=429)
        got = sc.me(db, request.cookies.get(sc.COOKIE))
        try:
            return JSONResponse(sc.notify_add(db, int(d.get("id") or 0), got[1].phone if got else d.get("phone"), sf.catalog(db)), headers={"Cache-Control": "no-store"})
        except (sc.CabError, ValueError, TypeError) as e:
            return JSONResponse({"ok": False, "msg": str(e) if isinstance(e, sc.CabError) else "Товар не знайдено."}, status_code=400)

    @app.post("/_shop/api/cab/{action}")
    async def cab_action(action: str, request: Request, db: Session = Depends(db_dep)):
        _cab_guard(request, db)
        d = _body(await request.body())
        tok = request.cookies.get(sc.COOKIE)
        if action == "logout":
            sc.logout(db, tok)
            r = JSONResponse({"ok": True})
            r.delete_cookie(sc.COOKIE)
            return r
        got = sc.me(db, tok)
        if not got:
            return JSONResponse({"ok": False, "msg": "Увійдіть у кабінет."}, status_code=401)
        acc, p = got
        try:
            if action == "profile":
                sc.save_profile(db, acc, p, d)
            elif action == "address":
                sc.save_address(db, acc, d)
            elif action == "cart":
                from .services import shop_carts as _ct
                _ct.save(db, p.id, d.get("items") or [], cx_data(db))
                db.commit()
            elif action == "wish":
                return JSONResponse({"ok": True, "wish": sc.set_wish(db, acc, d.get("ids") or [], merge=bool(d.get("merge")))}, headers={"Cache-Control": "no-store"})
            else:
                raise HTTPException(404)
        except sc.CabError as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    def cx_data(db):
        return sf.catalog(db)

    @app.get("/_shop/pidbir/", response_class=HTMLResponse)
    def shop_quiz(request: Request, db: Session = Depends(db_dep)):
        from .services import shop_extra as sx
        cx = ctx(request, db, "/pidbir/")
        if not cx["s"].get("quiz_on", True):
            raise HTTPException(404)
        qp = request.query_params
        ans = {q["key"]: qp.get(q["key"]) for q in sx.QUIZ if qp.get(q["key"]) in dict(q["opts"])}
        res = sx.pick(cx["d"], ans) if len(ans) == len(sx.QUIZ) else None
        cx.update(title="Підбір догляду онлайн — MOLODIST", desc="Дайте відповідь на 5 запитань — і отримайте домашній догляд під ваш тип шкіри й запит від магазину професійної косметики MOLODIST.",
                  quiz=sx.QUIZ, ans=ans, res=res, noindex_page=bool(ans))
        if res:
            cx["private"] = False
        return resp(request, "shop/quiz.html", cx)

    @app.get("/_shop/reorder/{tok}/", response_class=HTMLResponse)
    def shop_reorder(tok: str, request: Request, db: Session = Depends(db_dep)):
        from .services import shop_reviews as rv
        cx = ctx(request, db, "/reorder/")
        o = rv.by_token(db, tok)
        if not o:
            raise HTTPException(404)
        by_id = {v["id"]: (it, v) for it in cx["d"]["items"] for v in it["variants"]}
        rows = [{"id": i["product_id"], "qty": int(i.get("qty") or 1), "it": by_id[i["product_id"]][0], "v": by_id[i["product_id"]][1]}
                for i in o.items or [] if i.get("product_id") in by_id]
        cx.update(title="Повторити замовлення — MOLODIST", desc="", noindex_page=True, private=True, order=o, rows=rows)
        return resp(request, "shop/reorder.html", cx)

    @app.post("/_shop/api/oneclick")
    async def shop_oneclick(request: Request, db: Session = Depends(db_dep)):
        from .services import shop_extra as sx
        c = sf.cfg(db)
        if not opened(request, db, c) or not c.get("oneclick_on", True):
            raise HTTPException(404)
        d = _body(await request.body())
        if not _rate(request, "oneclick", 5, 3600):
            return JSONResponse({"ok": False, "msg": "Забагато спроб. Зателефонуйте нам, будь ласка."}, status_code=429)
        bad = human(request, d, c)
        if bad:
            return JSONResponse({"ok": False, "msg": bad}, status_code=400)
        got = sc.me(db, request.cookies.get(sc.COOKIE))
        try:
            r = sx.quick_order(db, int(d.get("id") or 0), int(d.get("qty") or 1), str(d.get("phone") or ""), str(d.get("name") or ""),
                               {k: v for k, v in d.items() if str(k).startswith("utm_") or k in ("gclid", "fbclid", "landing")}, got[1].id if got else None)
        except (sf.ShopError, ValueError) as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        return JSONResponse(r, headers={"Cache-Control": "no-store"})

    @app.post("/_shop/api/contact")
    async def shop_contact(request: Request, db: Session = Depends(db_dep)):
        c = sf.cfg(db)
        if not opened(request, db, c):
            raise HTTPException(404)
        d = _body(await request.body())
        if not _rate(request, "shopct", 5, 3600):
            return JSONResponse({"ok": False, "msg": "Забагато повідомлень. Зателефонуйте нам, будь ласка."}, status_code=429)
        bad = human(request, d, c)
        if bad:
            return JSONResponse({"ok": False, "msg": bad}, status_code=400)
        from .integrations.messaging import normalize_phone
        name, msg = " ".join(str(d.get("name") or "").split())[:100], str(d.get("message") or "").strip()[:1500]
        phone = normalize_phone(str(d.get("phone") or ""))
        if len(name) < 2 or not phone:
            return JSONResponse({"ok": False, "msg": "Вкажіть імʼя й телефон (+380…)."}, status_code=400)
        if len(re.findall(r"https?://", msg)) > 1:
            return JSONResponse({"ok": False, "msg": "Приберіть, будь ласка, посилання з повідомлення."}, status_code=400)
        from .services import site_leads as sl
        lead = {"name": name, "phone": phone, "message": msg, "formname": "Магазин · Контакти", "page": str(d.get("page") or "")[:300]}
        lead.update({k: str(v)[:200] for k, v in d.items() if str(k).startswith("utm_") or k in ("gclid", "fbclid")})
        r = sl.ingest(db, lead)
        try:
            sl.notify(db, lead, r, settings.public_url)
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"ok": bool(r.get("ok"))}, headers={"Cache-Control": "no-store"})

    @app.get("/_shop/p/{pid}/", include_in_schema=False)
    @app.get("/_shop/p/{pid}", include_in_schema=False)
    def shop_short_link(pid: int, request: Request, db: Session = Depends(db_dep)):
        """Коротке посилання на товар (для SMS) → 301 на сторінку товару; невідомий товар — на каталог."""
        cx = ctx(request, db, "/shop/")
        d = cx["d"]
        it = next((x for x in list(d["items"]) + list((d.get("archive") or {}).values()) if any(v["id"] == pid for v in x["variants"])), None)
        q = request.url.query
        return RedirectResponse(cx["base"] + (f"/shop/{it['slug']}/" if it else "/shop/") + (f"?{q}" if q else ""), 301)

    @app.get("/_shop/review/{tok}/", response_class=HTMLResponse)
    def shop_review_page(tok: str, request: Request, db: Session = Depends(db_dep)):
        from .services import shop_reviews as rv
        cx = ctx(request, db, "/review/")
        o = rv.by_token(db, tok)
        if not o:
            raise HTTPException(404)
        cx.update(title="Ваш відгук — MOLODIST", desc="", noindex_page=True, private=True, tok=tok, order=o,
                  ritems=rv.order_items(db, o, cx["d"]), rname=rv.first_name(o, db))
        return resp(request, "shop/review.html", cx)

    @app.post("/_shop/api/review")
    async def shop_review_save(request: Request, db: Session = Depends(db_dep)):
        from .services import shop_reviews as rv
        d = _body(await request.body())
        if d.get("website") or not _rate(request, "shoprv", 20):
            return JSONResponse({"ok": False, "msg": "Забагато спроб. Спробуйте пізніше."}, status_code=429)
        bad = human(request, d, sf.cfg(db))
        if bad:
            return JSONResponse({"ok": False, "msg": bad}, status_code=400)
        o = rv.by_token(db, str(d.get("token") or ""))
        if not o:
            return JSONResponse({"ok": False, "msg": "Посилання недійсне."}, status_code=404)
        try:
            rv.save(db, o, int(d.get("product_id") or 0), int(d.get("rating") or 0), str(d.get("text") or ""), str(d.get("name") or ""))
        except (ValueError, TypeError) as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        db.commit()
        try:
            if not sf.cfg(db).get("tg_review", True):
                raise RuntimeError("off")
            for ch in sf.order_chats(db):
                sf._tg_send(ch, f"⭐ Новий відгук на товар (замовлення №{o.number}). Перевірте й опублікуйте: «Магазин → Відгуки».")
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/_shop/cabinet/", response_class=HTMLResponse)
    def cab_page(request: Request, t: str = "profile", db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/cabinet/")
        cx.update(title="Особистий кабінет — MOLODIST", desc="", noindex_page=True, private=True, tabs=sc.TABS, np=bool((cx["s"].get("np_key") or "").strip()))
        if not cx["me"]:
            cx.update(ref_pre=(cx["ref_in"] or request.cookies.get(sc.REF_COOKIE) or "").upper()[:20])
            return resp(request, "shop/cab_login.html", cx)
        acc, p = cx["me"], cx["me_p"]
        t = t if t in dict(sc.TABS) else "profile"
        site_root = cx["s"]["canonical"].rstrip("/") if not cx["noindex"] else settings.public_url.rstrip("/") + "/_shop"
        wi = sc.wish_items(cx["d"], acc.wishlist or [])
        cx.update(care=sc.doctor_care(db, p, cx["d"]) if t == "care" else None)
        if t == "reviews":
            from .services import shop_reviews as _rv
            from .models import ShopReview as _SR
            mine = list(db.scalars(select(_SR).where(_SR.patient_id == p.id).order_by(_SR.created_at.desc()).limit(50)))
            names = {v["id"]: v["name"] for it in cx["d"]["items"] for v in it["variants"]}
            for r in mine:
                r.pname = names.get(r.product_id, "Товар")
            got_o = {r.order_id for r in mine}
            cx.update(my_reviews=mine, rv_tok=_rv.token, rv_orders=[o for o in sc.orders(db, p) if o.status in _rv.DONE and o.id not in got_o and o.source == "crm"][:5])
        cx.update(oimg={v["id"]: it["imgs"][0] for it in cx["d"]["items"] if it["imgs"] for v in it["variants"]})
        cx.update(t=t, acc=acc, p=p, coins=sc.coins(db, p.id), orders=sc.orders(db, p) if t == "orders" else [], wish=wi, wish_sum=sum(i["price"] for i in wi),
                  ref_url=f"{site_root}/?ref={acc.ref_code}", wish_url=f"{site_root}/wish/{acc.wish_token}/", STATUS_FAIL=sf.PAY_FAIL,
                  in_cat={v["id"] for it in cx["d"]["items"] for v in it["variants"] if v["stock"]})
        return resp(request, "shop/cabinet.html", cx)

    @app.get("/_shop/wish/{token}/", response_class=HTMLResponse)
    def wish_page(token: str, request: Request, db: Session = Depends(db_dep)):
        cx = ctx(request, db, f"/wish/{token}/")
        acc = sc.by_wish_token(db, token)
        if not acc:
            return not_found(request, cx)
        wi = sc.wish_items(cx["d"], acc.wishlist or [])
        cx.update(title="Вішліст — MOLODIST", desc="", noindex_page=True, private=True, owner=" ".join(x for x in (acc.last_name, acc.first_name) if x), items=wi,
                  og_title=f"Вішліст: {' '.join(x for x in (acc.first_name, acc.last_name) if x)} · MOLODIST".replace(":  ·", " ·"),
                  og_desc=(f"{len(wi)} бажань у магазині професійної косметики MOLODIST. Оберіть подарунок — доставимо Новою поштою." if wi else "Список бажань у магазині професійної косметики MOLODIST."),
                  og_img=f"{cx['abs']}/static/shop/og/wish.jpg",
                  wish_sum=sum(i["price"] for i in wi))
        return resp(request, "shop/wish.html", cx)

    @app.get("/_shop/checkout/", response_class=HTMLResponse)
    @app.get("/_shop/shop/cart/", response_class=HTMLResponse, include_in_schema=False)
    def shop_checkout(request: Request, db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/checkout/")
        if not cx["me"] and cx["s"].get("order_login", True) and cx["s"].get("cab_on", True):
            cx.update(title="Вхід — оформлення замовлення — MOLODIST", desc="", noindex_page=True, private=True, tabs=sc.TABS, for_order=True,
                      ref_pre=(cx["ref_in"] or request.cookies.get(sc.REF_COOKIE) or "").upper()[:20])
            return resp(request, "shop/cab_login.html", cx)
        from .services import mono_parts as mp
        cx.update(parts=mp.public(db))
        cx.update(title="Оформлення замовлення — MOLODIST", desc="", noindex_page=True, np=bool((cx["s"].get("np_key") or "").strip()),
                  gifts=[g[0] for g in sf.lines(cx["s"].get("gifts"))])
        cx.update(private=True, addr=sc.main_address(cx["me"]) if cx["me"] else None)
        return resp(request, "shop/checkout.html", cx)

    @app.get("/_shop/checkout/done/", response_class=HTMLResponse)
    def shop_done(request: Request, n: str = "", db: Session = Depends(db_dep)):
        cx = ctx(request, db, "/checkout/done/")
        cx.update(title="Дякуємо за замовлення — MOLODIST", desc="", noindex_page=True, number=re.sub(r"[^\w-]", "", n)[:20])
        cx.update(parts=bool(request.query_params.get("parts")))
        o = db.scalar(select(SiteOrder).where(SiteOrder.number == cx["number"], SiteOrder.source == "crm")) if cx["number"] else None
        if o and o.created_at and (datetime.now() - o.created_at).total_seconds() < 86400 and not (o.raw or {}).get("ga_done"):
            # подія purchase (GA4 / Google Ads / Meta) — один раз на замовлення: позначка на сервері, а не в sessionStorage вкладки
            # (після оплати в застосунку monobank покупець часто повертається в нову вкладку — тоді подія губилась)
            _raw = dict(o.raw or {})
            _raw["ga_done"] = datetime.now().isoformat(timespec="seconds")
            o.raw = _raw
            db.commit()
            cx["ga_purchase"] = {"transaction_id": o.number, "currency": "UAH", "value": float(o.total or 0), "shipping": float(o.delivery_price or 0),
                                 "items": [{"item_id": str(i.get("product_id") or i.get("sku") or ""), "item_name": i.get("name") or "",
                                            "price": float(i.get("price") or 0), "quantity": int(i.get("qty") or 1)} for i in o.items or []]}
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
        if "filters" in segs:                                    # старі адреси фільтрів → категорія / бренд
            i = segs.index("filters")
            if i + 2 < len(segs) and segs[i + 1] == "brand":
                b = segs[i + 2].lower()
                hit = next((c["path"] for c in d["cats"] if c["path"].startswith("/shop/brendi/") and (c["path"].rstrip("/").rsplit("/", 1)[-1] == b
                            or sf.slugify(c["name"]).startswith(b) or b.startswith(sf.slugify(c["name"].split(" (")[0])))), None)
                return RedirectResponse(cx["base"] + (hit or "/shop/brendi/"), 301)
            return RedirectResponse(cx["base"] + "/shop/" + "/".join(segs[:i]) + ("/" if i else ""), 301)
        _old = {"nabori": "/shop/", "rotova-porozhnina": "/shop/", "dietichni-ta-harchovi-dobavki": "/shop/brendi/jalupro/"}
        if len(segs) == 1 and segs[0] in _old and "/shop/" + segs[0] + "/" not in d["by_path"]:
            return RedirectResponse(cx["base"] + _old[segs[0]], 301)
        if len(segs) == 2 and segs[0] == "brendi" and "/shop/brendi/" + segs[1] + "/" not in d["by_path"]:
            return RedirectResponse(cx["base"] + "/shop/brendi/", 301)
        if segs[-1].startswith("page-") and segs[-1][5:].isdigit():
            page, segs = int(segs[-1][5:]), segs[:-1]
        it = d["by_slug"].get(segs[0]) or (d.get("archive") or {}).get(segs[0])
        if not it and len(segs) <= 2 and "/shop/" + "/".join(segs) + "/" not in d["by_path"]:
            gone = sf.hidden_target(db, d, segs[0])          # старе посилання на прихований товар → сторінка бренду, а не 404
            if gone:
                return RedirectResponse(cx["base"] + gone, 301)
        if it and len(segs) <= 2:
            if len(segs) == 2:                                    # /shop/<slug>/<id>/ → канонічна адреса товару
                return RedirectResponse(f"{cx['base']}/shop/{it['slug']}/", 301)
            cats = [c for c in d["cats"] if it["slug"] in c["slugs"] and not c["path"].startswith("/shop/brendi/")]
            cat = max(cats, key=lambda c: c["depth"]) if cats else None
            rel = [x for x in (d["by_slug"][s] for s in (cat["slugs"] if cat else []) if s in d["by_slug"]) if x["slug"] != it["slug"] and x["stock"]][:8]
            if it.get("archived") and len(rel) < 4:
                rel += [x for x in d["items"] if x["brand"] == it["brand"] and x["stock"] and x not in rel][:8 - len(rel)]
            from .services import shop_reviews as _rv
            rvs = _rv.published(db, [v["id"] for v in it["variants"]])
            tip_doc = None
            if it.get("tip") and it["tip"].get("doctor_id"):
                from .models import Doctor as _Dr
                tip_doc = db.get(_Dr, it["tip"]["doctor_id"])
            cx["tip_doc"] = tip_doc
            _bn = (it.get("brand") or "").strip().lower().split(" (")[0]
            cx["brand_path"] = next((c["path"] for c in d["cats"] if c["path"].startswith("/shop/brendi/") and c.get("n")
                                     and c["name"].strip().lower().split(" (")[0] == _bn), None) if _bn else None
            cx.update(canonical=cx["s"]["canonical"].rstrip("/") + f"/shop/{it['slug']}/", it=it, cat=cat, rel=rel, rvs=rvs,
                      title=it["seo_title"] or f"{it['name']} — купити в MOLODIST", desc=it["seo_desc"] or it["name"],
                      jsonld=json.dumps({"@context": "https://schema.org/", "@type": "Product", "name": it["name"], "sku": it["sku"],
                                         **({"gtin": it["variants"][0]["gtin"]} if it["variants"][0].get("gtin") else {}),
                                         "brand": {"@type": "Brand", "name": it["brand"]} if it["brand"] else None,
                                         "image": [f"{cx['s']['canonical'].rstrip('/')}/img/{i}?w=1200" for i in it["imgs"][:4]],
                                         "description": it["seo_desc"] or it["name"],
                                         **({} if it["price"] <= 0 else {"offers": {"@type": "Offer", "priceCurrency": "UAH", "price": f"{it['price']:.0f}",
                                                    "url": cx["s"]["canonical"].rstrip("/") + f"/shop/{it['slug']}/",
                                                    "availability": "https://schema.org/" + ("InStock" if it["stock"] else "OutOfStock"),
                                                    "itemCondition": "https://schema.org/NewCondition",
                                                    "priceValidUntil": f"{date.today().year + 1}-12-31",
                                                    "seller": {"@type": "Organization", "name": "MOLODIST"},
                                                    **({} if it["price"] < float(cx["s"].get("free_from") or 10 ** 9) else {"shippingDetails": {"@type": "OfferShippingDetails",
                                                                        "shippingRate": {"@type": "MonetaryAmount", "value": 0, "currency": "UAH"},
                                                                        "shippingDestination": {"@type": "DefinedRegion", "addressCountry": "UA"},
                                                                        "deliveryTime": {"@type": "ShippingDeliveryTime",
                                                                                         "handlingTime": {"@type": "QuantitativeValue", "minValue": 0, "maxValue": 1, "unitCode": "DAY"},
                                                                                         "transitTime": {"@type": "QuantitativeValue", "minValue": 1, "maxValue": 3, "unitCode": "DAY"}}}}),
                                                    "hasMerchantReturnPolicy": {"@type": "MerchantReturnPolicy", "applicableCountry": "UA",
                                                                                "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
                                                                                "merchantReturnDays": 7, "returnMethod": "https://schema.org/ReturnByMail",
                                                                                "returnFees": "https://schema.org/ReturnShippingFees"}}}),
                                         **({"aggregateRating": {"@type": "AggregateRating", "ratingValue": rvs["avg"], "reviewCount": rvs["n"]},
                                             "review": [{"@type": "Review", "author": {"@type": "Person", "name": r.name or "Покупець"},
                                                         "reviewRating": {"@type": "Rating", "ratingValue": r.rating}, "reviewBody": r.text or "",
                                                         "datePublished": r.created_at.strftime("%Y-%m-%d") if r.created_at else None} for r in rvs["rows"][:5]]}
                                            if rvs["n"] else {})}, ensure_ascii=False))
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
        subs, sub_all = sf.children(cat, d), cat
        if not subs and crumbs and crumbs[-1]["path"] != "/shop/brendi/":   # підкатегорія — показуємо сусідні, поточна активна
            subs, sub_all = sf.children(crumbs[-1], d), crumbs[-1]
        return render_list(request, cx, [d["by_slug"][s] for s in cat["slugs"]], page, order, brand, h1=cat["name"], cat=cat,
                           subs=subs, sub_all=sub_all if subs else None, crumbs=crumbs, page_base=path)

    def _resolve_links(html: str, d: dict, base: str, *db_req) -> str:
        from .services import shop_content as _sc
        book = ""
        if db_req:
            try:
                book = _book_url(db_req[0], sp.cfg(db_req[0]))
            except Exception:
                book = ""
        html = _sc.render(html, base, sf.cfg(db_req[0]).get("seller_req") if db_req else "", book)
        slugs = list(d["by_slug"])

        def fix(m):
            sl = m.group(2)
            if sl in d["by_slug"]:
                return m.group(0)
            hit = next((x for x in slugs if x.startswith(sl[:40])), None)
            return f'{m.group(1)}{hit}/"' if hit else m.group(0)
        return re.sub(r'(href="' + re.escape(base) + r'/shop/)([^"/]+)/"', fix, html)

    def _blog(d: dict, n: int | None = None) -> list[dict]:
        posts = [p for p in d["pages"].values() if p["path"].startswith("/blog/") and p["path"] != "/blog/" and not p.get("unlisted")]
        posts.sort(key=lambda p: p.get("date") or "", reverse=True)
        for p in posts:
            if p.get("cover") and not p.get("img"):
                it = d["by_slug"].get(p["cover"]) or next((v for k, v in d["by_slug"].items() if k.startswith(p["cover"][:40])), None)
                if it and it["imgs"]:
                    p["img_fid"] = it["imgs"][0]
        return posts[:n] if n else posts

    @app.get("/_shop/{rest:path}", response_class=HTMLResponse)
    def shop_page(request: Request, rest: str, db: Session = Depends(db_dep)):
        path = "/" + "/".join(x for x in rest.split("/") if x) + "/"
        from .services import shop_content as _scn
        if path in _scn.GONE:
            return Response("Сторінку видалено", status_code=410, media_type="text/plain; charset=utf-8")
        _to = _scn.redirect_for(path, sp.cfg(db).get("main_url") or "", sf.cfg(db).get("redirects") or "")
        if _to:
            return RedirectResponse(_to, 301)
        cx = ctx(request, db, path)
        pg = cx["d"]["pages"].get(path)
        if not pg:
            return not_found(request, cx)
        d = cx["d"]
        pg = {**pg, "html": _resolve_links(pg.get("html") or "", d, cx["base"], db)}
        prods = []
        for sl in pg.get("products") or []:
            it = d["by_slug"].get(sl) or next((v for k, v in d["by_slug"].items() if k.startswith(sl[:40])), None)
            if it and it not in prods:
                prods.append(it)
        if pg.get("post"):
            _blog(d)
            pg["img_fid"] = next((p.get("img_fid") for p in _blog(d) if p["path"] == path), None)
        cx.update(title=pg.get("title") or pg.get("h1") or "MOLODIST", desc=pg.get("desc") or "", pg=pg, prods=prods,
                  posts=_blog(d) if path == "/blog/" else [], more=[p for p in _blog(d) if p["path"] != path][:3] if pg.get("post") else [])
        if pg.get("post"):
            img = (f"{cx['abs']}{cx['base']}/img/{pg['img_fid']}?w=1200" if pg.get("img_fid") else f"{cx['abs']}{pg.get('cover_img') or '/static/shop/og/default.jpg'}")
            cx.update(og_img=img)
        return resp(request, "shop/page.html", cx)
