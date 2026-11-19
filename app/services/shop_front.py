"""Власний інтернет-магазин косметики на CRM (заміна Shop-Express на домені molodist.store).

• Товари — картки CRM із блоком «На сайті» (site_on + site_info.slug): фото на нашому сервері, опис, SEO.
• Структура й SEO старого сайту (категорії з адресами, заголовками й описами, склад категорій, сторінки, блог) — один раз
  знімається з molodist.store (crawl) у SyncState «shop_site»; нові сторінки відкриваються за ТИМИ САМИМИ адресами.
• Ціна — з CRM (ціна продажу); наявність — фактичний залишок CRM на складах SHOP_WHS (журнал складу), показ і ручне «немає» — у картці.
• Замовлення → SiteOrder(source="crm") + картка пацієнта за телефоном + сповіщення адміністраторам; оплата — посилання monobank.
Поки магазин не опубліковано — сторінки бачать лише співробітники CRM або за посиланням перегляду."""
from __future__ import annotations

import json
import re
import secrets
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Patient, Product, ProductFile, SiteOrder, SyncState, User

KEY, SITE_KEY = "shop_front", "shop_site"
OLD = "https://molodist.store/"
DEFAULT = {
    "on": False, "hosts": [], "preview_key": "", "canonical": "https://molodist.store",
    "strip": "Безкоштовна доставка від 3 000 ₴ по всій Україні",
    "free_from": 3000, "gift_from": 0, "gifts": "", "pay_account": 0, "cod": True, "pickup": True, "np_key": "",
    "benefits": "Оригінальна продукція|Лише від офіційних дистрибʼюторів брендів\nПідбір догляду лікарем|Безкоштовна консультація лікаря MOLODIST щодо домашнього догляду\n"
                "Швидка відправка|Замовлення до 18:00 відправляємо того ж дня\nПокупка Частинами monobank|Безкоштовна розстрочка: до 4 платежів без комісій",
    "about": "MOLODIST — медичний простір естетики та профілактики в Києві. В інтернет-магазині зібрана професійна косметика для домашнього догляду, з якою щодня працюють наші лікарі-дерматологи: ZO Skin Health, Medik8, Medicare, Demax, AG SKIN, Tebiskin, Image Skincare, Rejuran та інші бренди.\nМи продаємо лише оригінальну продукцію від офіційних дистрибʼюторів і зберігаємо її з дотриманням умов виробника. У каталозі — засоби для очищення й тонізації, активні сироватки з ретинолом, вітаміном C і кислотами, зволожувальні та анти-ейдж креми, сонцезахист SPF, догляд за шкірою навколо очей, тілом і волоссям.\nНе впевнені, що підійде саме вам? Напишіть нам — лікар MOLODIST безкоштовно підбере домашній догляд під тип і стан вашої шкіри: акне, пігментація, чутливість, вікові зміни, відновлення після процедур. Так домашній догляд підтримує результат лікування, а не працює проти нього.\nДоставляємо Новою поштою по всій Україні, у Києві замовлення можна забрати в MOLODIST. Оплата карткою онлайн, при отриманні або Покупка Частинами monobank.",
    "instagram": "https://www.instagram.com/_molodist/",
    "hero": [],                  # [{img, title, text, url}]
    "coin_on": True, "coin_spend_on": False, "coin_start": "", "order_login": True, "coin_earn_pct": 3.0, "coin_max_pct": 10.0, "coin_days": 180, "coin_wait_days": 3, "coin_review": 25,   # MOLODIST Coins у магазині
    "seller_req": "ФОП Бондар Вадим Миколайович\nАдреса: м. Київ, вул. Князя Романа Мстиславича, 28А\nТелефон: +38 (093) 375 64 37\nE-mail: info@molodist.com.ua",
    "ticker": "Безкоштовна доставка від 3 000 ₴ по всій Україні\nВідправляємо в день замовлення до 18:00\nZO Skin Health і ClearChoice — після консультації лікаря, звертайтеся до адміністратора\nMOLODIST Coins за реєстрацію та кожну покупку",
    "gtm_id": "", "ga4_id": "", "ads_id": "", "ads_label": "", "remarketing_on": True, "fb_pixel": "",
    "feed_google": True, "feed_facebook": True, "feed_hotline": True, "hotline_id": "", "llms_on": True, "ai_about": "",
    "cookie_on": True, "ts_site": "", "ts_secret": "", "tg_order": True, "tg_pay": True, "tg_review": True, "tg_cart": False,
    "redirects": "", "gsc_verify": "",
    "gplaces_key": "", "gplace_id": "", "greviews_on": True, "greviews_min": 4,
    "reorder_on": False, "reorder_lead": 7, "reorder_text": "", "quiz_on": True, "oneclick_on": True,
    "review_on": False, "review_days": 5, "review_text": "", "review_test_phone": "",
    "cart_task_hours": 2, "cart_auto_on": False, "cart_auto_hours": 3, "cart_text": "",
    "order_msg_on": False,       # повідомлення покупцю: оплачено / відправлено (з ТТН) / скасовано
    "notify_on": False, "notify_text": "{name} знову в наявності в магазині MOLODIST: {url}",
    "cab_on": True, "cab_sms": "{code} — код входу в магазин MOLODIST", "cab_sms_day": 150, "cab_coin_signup": 100, "cab_coin_ref": 200,
    "parts_file": "",            # локальна копія шильдика для низу сайту (files/shop_brand/…)
    "parts_paw": "", "parts_paw_file": "",   # офіційна лапка (з цифрою) — у куті фото товару
    "parts_icon": "",            # офіційний значок «Покупка Частинами» (лапка) — файл із матеріалів monobank
}
STATUSES = ["Нове", "Опрацьовується", "Очікує оплати", "Оплачено", "Відправлено", "Отримано", "Закрито", "Скасовано", "Повернення", "Фейкове"]
LEGACY_STATUS = {"В роботі": "Опрацьовується", "Виконано": "Закрито"}
PAY_FAIL = {"failed": "оплата не пройшла", "expired": "час на оплату минув (прострочено)", "cancelled": "рахунок скасовано"}
_cache: dict = {"at": 0.0, "hosts": set(), "cat_at": 0.0, "cat": None}


class ShopError(ValueError):
    pass


# ---------------- налаштування ----------------


SHOP_WHS = ("vitrina",)                                  # склади, з яких продає інтернет-магазин

def gtin(code: str) -> str:
    """Штрихкод → GTIN (8/12/13/14 цифр із правильною контрольною цифрою), інакше порожньо."""
    c = re.sub(r"\D", "", str(code or ""))
    if len(c) not in (8, 12, 13, 14) or set(c) == {"0"}:
        return ""
    body, chk = c[:-1], int(c[-1])
    tot = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return c if (10 - tot % 10) % 10 == chk else ""

def cfg(db: Session) -> dict:
    c = json.loads(json.dumps(DEFAULT))
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            c.update(json.loads(st.value))
        except ValueError:
            pass
    c["cod"] = False                                  # післяплати немає (рішення власника 16.11.2026)
    _t15 = "15" + ":00"
    if _t15 in str(c.get("benefits") or ""):
        c["benefits"] = c["benefits"].replace(_t15, "18" + ":00")
    if "molodist.clinic" in str(c.get("instagram") or ""):
        c["instagram"] = DEFAULT["instagram"]
    return c


# Обовʼязковий текст банку для сайтів, де є «Покупка Частинами» — дослівно, не редагувати.
PARTS_LEGAL = {
    "title": "Покупка частинами monobank | Universal Bank",
    "provider": "АТ «УНІВЕРСАЛ БАНК» ліцензія НБУ No92 від 20.01.1994, номер у держреєстрі банків No 226.",
    "items": ["Мінімальна сума розстрочки: 1 грн", "Максимальна сума розстрочки: 400 000 грн", "Реальна річна процентна ставка: 0,000001%",
              "Доступний строк, платежів: 3 — 25", "Порядок погашення: щомісячні платежі рівними частинами", "Перший платіж у момент оформлення покупки"],
    "note": "Інформація про істотні характеристики продукту та попередження розміщені на сайті продукту",
    "url": "https://chast.monobank.ua/", "url_text": "chast.monobank.ua",
}
_IMG_EXT = {"image/png": ".png", "image/svg+xml": ".svg", "image/webp": ".webp", "image/jpeg": ".jpg"}


def brand_dir():
    from pathlib import Path
    from ..config import settings
    d = Path(settings.files_dir) / "shop_brand"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_parts_label(c: dict, data: bytes, ctype: str, key: str = "parts_file") -> None:
    """Офіційний шильдик / лапка monobank (файл із їхніх матеріалів) — зберігаємо в себе як є, без змін."""
    ext = _IMG_EXT.get((ctype or "").split(";")[0].strip().lower())
    if not ext and data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    if not ext or not data or len(data) > 3 * 1024 * 1024:
        raise ShopError("Шильдик: потрібен файл png / svg / webp до 3 МБ")
    name = f"{'parts-paw' if key == 'parts_paw_file' else 'parts-label'}-{int(time.time())}{ext}"
    (brand_dir() / name).write_bytes(data)
    c[key] = name


def fetch_parts_label(c: dict, url: str, http=None, key: str = "parts_file") -> None:
    import httpx
    if not url.startswith("https://"):
        raise ShopError("Шильдик: посилання має починатися з https://")
    try:
        r = (http or httpx).get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        raise ShopError(f"Шильдик не завантажився: {e}") from e
    if r.status_code != 200:
        raise ShopError(f"Шильдик не завантажився: HTTP {r.status_code}")
    set_parts_label(c, r.content, r.headers.get("content-type") or "", key)


def save_cfg(db: Session, c: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps({k: c[k] for k in DEFAULT if k in c}, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    _cache["at"] = 0.0


def hosts() -> set[str]:
    if time.time() - _cache["at"] > 60:
        try:
            from ..models import SessionLocal
            with SessionLocal() as db:
                c = cfg(db)
                _cache["hosts"] = {h.strip().lower() for h in c["hosts"] if h.strip()} if c.get("on") else set()
        except Exception:  # noqa: BLE001
            pass
        _cache["at"] = time.time()
    return _cache["hosts"]


def preview_key(db: Session) -> str:
    c = cfg(db)
    if not c.get("preview_key"):
        c["preview_key"] = secrets.token_urlsafe(9)
        save_cfg(db, c)
    return c["preview_key"]


def lines(t: str | None) -> list[list[str]]:
    return [[x.strip() for x in ln.split("|", 1)] + ([""] if "|" not in ln else []) for ln in (t or "").splitlines() if ln.strip()]


# ---------------- структура старого сайту ----------------
def site(db: Session) -> dict:
    st = db.get(SyncState, SITE_KEY)
    try:
        return json.loads(st.value) if st and st.value else {"cats": [], "pages": [], "at": None}
    except ValueError:
        return {"cats": [], "pages": [], "at": None}


def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _meta(s) -> dict:
    d = s.find("meta", attrs={"name": "description"})
    h1 = s.find("h1")
    return {"title": (s.title.string or "").strip() if s.title and s.title.string else "", "desc": (d.get("content") or "").strip() if d else "",
            "h1": h1.get_text(" ", strip=True) if h1 else ""}


def _content_html(s) -> str:
    from .shop_migrate import clean_html
    for t in s.find_all(["script", "style", "header", "footer", "nav", "form", "noscript"]):
        t.decompose()
    art = s.find("article")
    if not art:
        h1 = s.find("h1")
        art = h1
        while art is not None and art.parent is not None and len(art.get_text(" ", strip=True)) < len(h1.get_text()) + 200 and art.parent.name != "body":
            art = art.parent
    if art is None:
        return ""
    for h in art.find_all("h1"):
        h.decompose()
    for img in art.find_all("img"):
        if img.get("src"):
            img["src"] = urljoin(OLD, img["src"])
    for a in art.find_all("a"):
        if a.get("href", "").startswith(OLD):
            a["href"] = a["href"][len(OLD) - 1:]
    return clean_html(str(art))[:60000]


def crawl(db: Session, http: httpx.Client | None = None, job=None) -> dict:
    """Зняти з molodist.store структуру й SEO: категорії (адреса, заголовки, опис, товари), сторінки, блог."""
    log = (lambda m: job.log(m)) if job else (lambda m: None)
    http = http or httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": "MOLODIST-CRM/1.0 (site migration)"})
    slugs = {(p.site_info or {}).get("slug") for p in db.scalars(select(Product).where(Product.site_info.is_not(None)))} - {None, ""}

    pause = 0.0 if job is None and http is not None and not isinstance(http, httpx.Client) else 1.5

    def get(path: str) -> str:
        if pause:
            time.sleep(pause)                               # повільно, як звичайний відвідувач: сайт обмежує часті запити
        r = http.get(urljoin(OLD, path.lstrip("/")))
        t = r.text if r.status_code == 200 else ""
        if "<title>Please wait" in t[:3000] or (r.status_code in (403, 429, 503)):
            raise ShopError("molodist.store тимчасово обмежив запити з сервера (сторінка «Please wait»). Нічого не змінено — повторіть за 15–30 хвилин.")
        return t

    skip = ("cart", "filters", "order", "search", "compare", "wishlist")
    cat_re = re.compile(r"^/shop/((?:[^/]+/){1,3})$")

    def is_cat(path: str) -> bool:
        m = cat_re.match(path)
        if not m:
            return False
        segs = m.group(1).strip("/").split("/")
        return not (segs[-1].isdigit() or segs[-1].startswith("page-") or any(x in skip for x in segs) or (len(segs) == 1 and segs[0] in slugs)
                    or segs[0] in slugs)

    def links(html: str) -> list[str]:
        """Посилання зі сторінки — регулярним виразом по сирому HTML (розбір деревом губить хвіст великих сторінок)."""
        out = []
        for h in re.findall(r"""href\s*=\s*["']([^"'#?]+)""", html or ""):
            h = re.sub(r"^https?://(www\.)?molodist\.store", "", h.strip())
            if h.startswith("/") and not h.startswith("//"):
                h = h if h.endswith("/") else h + "/"
                if h not in out:
                    out.append(h)
        return out

    home_html = get("/")
    home = _soup(home_html)
    menu = [l for l in links(home_html) if is_cat(l)]
    queue, cats, seen = list(dict.fromkeys(["/shop/brendi/"] + menu)), {}, set()
    while queue and len(cats) < 300:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        html = get(path)
        if not html:
            continue
        s = _soup(html)
        m = _meta(s)
        prod, pages = [], 1
        ls = links(html)
        for l in ls:
            pm = re.match(rf"^{re.escape(path)}page-(\d+)/$", l)
            if pm:
                pages = max(pages, int(pm.group(1)))
            elif is_cat(l) and l.startswith(path) and l not in seen:
                queue.append(l)

        def take(ll):
            for l in ll:
                pm2 = re.match(r"^/shop/([^/]+)/(?:\d+/)?$", l)
                if pm2 and pm2.group(1) in slugs and pm2.group(1) not in prod:
                    prod.append(pm2.group(1))
        take(ls)
        n = 2
        while n <= min(pages, 60):
            more = links(get(f"{path}page-{n}/"))
            take(more)
            for l in more:
                pm = re.match(rf"^{re.escape(path)}page-(\d+)/$", l)
                if pm:
                    pages = max(pages, int(pm.group(1)))
            n += 1
        img = s.find("meta", attrs={"property": "og:image"})
        cats[path] = {"path": path, "name": m["h1"] or m["title"], "title": m["title"], "desc": m["desc"], "slugs": prod,
                      "in_menu": path in menu, "img": (img.get("content") if img else "") or ""}
        log(f"{path}: {len(prod)} товарів")
    # сторінки й блог
    pages = []
    try:
        sm = http.get(urljoin(OLD, "contentsitemap.xml")).text
        urls = [u[len(OLD) - 1:] for u in re.findall(r"<loc>([^<]+)</loc>", sm) if u.startswith(OLD)]
    except httpx.HTTPError:
        urls = []
    for u in urls:
        if u in ("/", "/shop/") or u.startswith("/shop/"):
            continue
        html = get(u)
        if not html:
            continue
        s = _soup(html)
        m = _meta(s)
        if "мотор" in (m["h1"] + m["title"]).lower():          # демо-стаття шаблону Shop-Express
            continue
        og = s.find("meta", attrs={"property": "og:image"})
        pages.append({"path": u if u.endswith("/") else u + "/", **m, "img": (og.get("content") if og else "") or "", "html": _content_html(s)})
    hm = _meta(home)
    if not any(c["slugs"] for c in cats.values()) and site(db).get("cats"):
        raise ShopError("У категоріях не знайдено жодного товару — схоже, сайт віддав не ті сторінки. Збережені дані не змінено.")
    data = {"at": datetime.now().isoformat(timespec="minutes"), "cats": list(cats.values()), "pages": pages, "home": hm}
    db.merge(SyncState(key=SITE_KEY, value=json.dumps(data, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    _cache["cat_at"] = 0.0
    return {"cats": len(cats), "pages": len(pages), "with_products": sum(1 for c in cats.values() if c["slugs"]),
            "products_in_cats": len({x for c in cats.values() for x in c["slugs"]}), "site_products": len(slugs)}


# ---------------- каталог ----------------
def _price(p: Product) -> float:
    si = p.site_info or {}
    return float(p.price_manual or p.price or si.get("price") or 0)


def catalog(db: Session, fresh: bool = False) -> dict:
    """Товари магазину (кеш 60 с): {items: [...], by_slug, brands, cats, pages}."""
    if not fresh and _cache["cat"] and time.time() - _cache["cat_at"] < 60:
        return _cache["cat"]
    files = defaultdict(list)
    for f in db.scalars(select(ProductFile).where(ProductFile.kind == "photo", ProductFile.site.is_(True)).order_by(ProductFile.sort, ProductFile.id)):
        files[f.product_id].append(f.id)
    sold = defaultdict(float)
    since = datetime.now() - timedelta(days=365)
    for o in db.scalars(select(SiteOrder).where(SiteOrder.created_at >= since)):
        for it in o.items or []:
            if it.get("product_id"):
                sold[it["product_id"]] += it.get("qty") or 0
    by_slug: dict[str, dict] = {}
    arch: dict[str, dict] = {}
    today = datetime.now().date().isoformat()
    from . import short_expiry as _sx
    sxc = _sx.cfg(db)
    try:
        short = _sx.short_stock(db, sxc)
    except Exception:  # noqa: BLE001
        short = {}
    onhand: dict[int, float] | None = None
    try:                                                    # наявність — за фактичним залишком CRM на вітрині (журнал складу)
        from .stock import balances, ledger_active
        if ledger_active(db):
            onhand = defaultdict(float)
            for (pid_, wh_), q_ in balances(db).items():
                if wh_ in SHOP_WHS:
                    onhand[pid_] += q_
    except Exception:  # noqa: BLE001
        onhand = None
    for p in db.scalars(select(Product).where(Product.site_info.is_not(None)).order_by(Product.id)):
        si = p.site_info or {}
        slug = si.get("slug")
        if not slug:
            continue
        hidden = p.site_on is False or p.active is False         # прихований товар: сторінка лишається (архів), без каталогу й кошика
        target = arch if hidden else by_slug
        price = _price(p)
        if price <= 0 and not hidden:
            continue
        old = float(si.get("price_old") or 0)
        sale = si.get("sale") or {}
        if sale.get("pct") and (not sale.get("until") or str(sale["until"]) >= today):       # знижка на сайті, задана в CRM
            old, price = price, float(round(price * (1 - float(sale["pct"]) / 100)))
        full_ = old if old > price else price
        sx_ = short.get(p.id) if not hidden else None
        if sx_ and _sx.price(sxc, full_) >= price:           # інша знижка вже більша — коротку не застосовуємо
            sx_ = None
        rest_ = price                                       # ціна решти (не коротких) одиниць — з іншою знижкою, якщо є
        if sx_:                                             # короткий термін придатності: −N% від повної ціни, лише на к-сть короткої партії
            old, price = full_, _sx.price(sxc, full_)
        if onhand is not None:
            left_ = int(max(0.0, onhand.get(p.id, 0.0)) + 1e-6)
            stock = not hidden and str(si.get("presence", 1)).lower() not in ("0", "false", "not_available") and left_ >= 1
        else:
            left_ = None
            stock = not hidden and str(si.get("presence", 1)).lower() not in ("0", "false", "not_available") and (si.get("quantity") is None or float(si.get("quantity") or 0) > 0)
        wait_ = False
        if si.get("backorder") and not stock and not hidden:   # позначка власника сильніша за стару «presence» з імпорту
            stock, left_, wait_ = True, None, True          # «під замовлення»: продаємо без залишку на вітрині, відправка за 1–3 дні
        v = {"id": p.id, "left": left_, "name": si.get("title") or p.name, "price": price, "old": old if old > price else 0, "stock": stock, "wait": wait_,
             "sku": p.sku or si.get("sku") or "", "label": p.volume or "", "gtin": gtin(p.barcode or si.get("barcode") or "")}
        if sx_:
            v.update(short=sx_["qty"], short_exp=sx_["expiry"].isoformat(), full=rest_)
        it = target.get(slug)
        if it:
            it["variants"].append(v)
            it["sold"] += sold.get(p.id, 0)
            if not it["imgs"] and files.get(p.id):
                it["imgs"] = files[p.id]
            it["stock"] = it["stock"] or stock
            it["wait"] = it["wait"] and (wait_ or not stock)
            continue
        brand = si.get("brand_name") or p.brand or ""
        target[slug] = {"slug": slug, "id": p.id, "name": si.get("title") or p.name, "brand": brand, "price": price, "old": v["old"],
                         "stock": stock, "wait": wait_, "imgs": files.get(p.id, []), "desc": si.get("short_description") or "",
                         "sections": sections(si.get("short_description") or ""),
                         "seo_title": si.get("seo_title") or "", "seo_desc": si.get("seo_description") or "", "sold": sold.get(p.id, 0),
                         "new": p.id, "variants": [v], "sku": v["sku"], "sticker": si.get("sticker") or "",
                         "tip": si.get("tip") if (si.get("tip") or {}).get("text") else None,
                         "short": v.get("short"), "short_exp": v.get("short_exp")}
    for it in by_slug.values():
        it["archived"] = False
    for it in arch.values():
        it["archived"] = True
    for it in list(by_slug.values()) + list(arch.values()):
        it["pct"] = round(100 * (1 - it["price"] / it["old"])) if it["old"] else 0
    st = site(db)
    cats = []
    for c in st["cats"]:
        if c.get("hidden"):
            continue
        sl = [s for s in c["slugs"] if s in by_slug]
        if c["path"] == "/shop/sale/":                  # SALE — лише товари, що зараз справді зі знижкою (ручною чи «короткий термін»)
            sl = [k for k, it in sorted(by_slug.items(), key=lambda kv: -kv[1]["pct"]) if it["old"] and it["stock"]]
        cats.append({**c, "slugs": sl, "n": len(sl), "depth": c["path"].strip("/").count("/"), "parent": "/".join(c["path"].rstrip("/").split("/")[:-1]) + "/"})
    brands_root = "/shop/brendi/"
    data = {"items": list(by_slug.values()), "by_slug": by_slug, "archive": {k: v for k, v in arch.items() if k not in by_slug}, "cats": cats, "by_path": {c["path"]: c for c in cats},
            "menu": sorted((c for c in cats if c["depth"] == 1 and c["path"] != brands_root and c["n"] and c.get("in_menu", True) is not False),
                           key=lambda c: (c.get("sort") or 100)),
            "brands": sorted((c for c in cats if c["path"].startswith(brands_root) and c["path"] != brands_root and (c["n"] or c.get("notice"))), key=lambda c: c["name"].lower()),
            "pages": __import__("app.services.shop_content", fromlist=["x"]).pages({p["path"]: p for p in st["pages"]}), "home": st.get("home") or {}, "at": st.get("at")}
    _cache["cat"], _cache["cat_at"] = data, time.time()
    return data


# ---------------- опис товару: чистий HTML, розбитий на розділи ----------------
_SEC = re.compile(r"(спосіб|метод|як)\s+(застосуван|використан|нанесен)|застосування|використання|склад|інгредієнт|ingredients|показання|результат|"
                  r"переваги|дія|ефект|особливості|для кого|тип шкіри|увага|застереження|протипоказання|обʼєм|об'єм|зберігання|рекомендації", re.I)


def sections(html: str) -> list[dict]:
    """Опис із Shop-Express → розділи [{title, html}]: без стилів і порожніх блоків; заголовки розділів — справжні заголовки або
    короткі рядки на кшталт «Спосіб застосування», «Склад», «Активні інгредієнти»."""
    if not (html or "").strip():
        return []
    try:
        s = _soup(html)
    except Exception:  # noqa: BLE001
        return [{"title": "Опис", "html": html}]
    for t in s.find_all(["script", "style", "iframe", "form", "input", "button"]):
        t.decompose()
    for t in s.find_all(True):
        t.attrs = {k: v for k, v in t.attrs.items() if k in ("href", "src", "alt")}
        if t.name in ("span", "font", "section", "article"):
            t.unwrap()
    for t in s.find_all("div"):
        if t.find(["p", "ul", "ol", "div", "h1", "h2", "h3", "h4", "table"]):
            t.unwrap()
        else:
            t.name = "p"
    for t in s.find_all(["p", "li", "h1", "h2", "h3", "h4", "strong", "b", "em"]):
        if not t.get_text(strip=True) and not t.find("img"):
            t.decompose()
    out, cur = [], {"title": "Опис", "parts": []}

    def head_of(t) -> str:
        if getattr(t, "name", None) in ("h1", "h2", "h3", "h4"):
            return t.get_text(" ", strip=True)
        if getattr(t, "name", None) == "p":
            txt = t.get_text(" ", strip=True)
            bold = t.find(["strong", "b"])
            only_bold = bool(bold) and bold.get_text(" ", strip=True) == txt
            if txt and len(txt) <= 60 and (only_bold or (_SEC.search(txt) and len(txt.split()) <= 5 and not txt.endswith("."))):
                return txt
        return ""
    for t in list(s.children):
        txt = str(t).strip()
        if not txt:
            continue
        h = head_of(t)
        if h and h.lower().strip(" :") not in ("опис", "default"):
            if cur["parts"]:
                out.append(cur)
            cur = {"title": h.strip(" :")[:80].capitalize() if h.isupper() else h.strip(" :")[:80], "parts": []}
            continue
        if h:
            continue
        cur["parts"].append(txt if getattr(t, "name", None) else f"<p>{txt}</p>")
    if cur["parts"]:
        out.append(cur)
    return [{"title": x["title"], "html": "\n".join(x["parts"])} for x in out]


# ---------------- структура: редагування в CRM ----------------
def save_site(db: Session, st: dict) -> None:
    db.merge(SyncState(key=SITE_KEY, value=json.dumps(st, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    _cache["cat_at"] = 0.0


def import_site(db: Session, payload: dict) -> dict:
    """Структура старого сайту, знята в браузері власника (коли сайт не віддає сторінки серверу): категорії й сторінки."""
    from .shop_migrate import clean_html
    slugs = {(p.site_info or {}).get("slug") for p in db.scalars(select(Product).where(Product.site_info.is_not(None)))} - {None, ""}
    old = {c["path"]: c for c in site(db).get("cats") or []}
    cats = []
    for c in payload.get("cats") or []:
        path = str(c.get("path") or "")
        segs = path.strip("/").split("/")
        if not re.match(r"^/shop/([^/]+/){1,3}$", path) or segs[1] in slugs or "Please wait" in str(c.get("title")):
            continue
        prev = old.get(path) or {}
        cats.append({"path": path, "name": str(c.get("h1") or c.get("title") or segs[-1])[:200], "title": str(c.get("title") or "")[:300],
                     "desc": str(c.get("desc") or "")[:1000], "img": str(c.get("img") or "")[:500], "in_menu": bool(c.get("in_menu")),
                     "slugs": list(dict.fromkeys(x for x in c.get("slugs") or [] if x in slugs)),
                     **{k: prev[k] for k in ("sort", "hidden") if k in prev}})
    if not any(c["slugs"] for c in cats):
        raise ShopError("У знятих категоріях немає товарів — дані не збережено")
    pages = [{"path": str(p.get("path"))[:300], "title": str(p.get("title") or "")[:300], "desc": str(p.get("desc") or "")[:1000],
              "h1": str(p.get("h1") or "")[:300], "img": str(p.get("img") or "")[:500], "html": clean_html(str(p.get("html") or ""))[:60000]}
             for p in payload.get("pages") or [] if str(p.get("path") or "").startswith("/") and "мотор" not in str(p.get("h1") or "").lower()
             and "Please wait" not in str(p.get("title"))]
    st = {"at": datetime.now().isoformat(timespec="minutes"), "cats": cats, "pages": pages or site(db).get("pages") or [],
          "home": payload.get("home") or site(db).get("home") or {}}
    save_site(db, st)
    return {"cats": len(cats), "with_products": sum(1 for c in cats if c["slugs"]), "products_in_cats": len({x for c in cats for x in c["slugs"]}),
            "pages": len(st["pages"])}


def edit_cat(db: Session, path: str, data: dict) -> None:
    st = site(db)
    c = next((x for x in st["cats"] if x["path"] == path), None)
    if not c:
        if not re.match(r"^/shop/([a-z0-9-]+/){1,3}$", path):
            raise ShopError("Адреса категорії: /shop/nazva-latynytseiu/ (малі латинські літери, цифри, дефіс)")
        c = {"path": path, "slugs": [], "img": "", "in_menu": True}
        st["cats"].append(c)
    if not str(data.get("name") or "").strip():
        raise ShopError("Вкажіть назву категорії")
    c.update(name=str(data["name"]).strip()[:200], title=str(data.get("title") or "").strip()[:300], desc=str(data.get("desc") or "").strip()[:1000],
             in_menu=bool(data.get("in_menu")), hidden=bool(data.get("hidden")))
    if "notice" in data:
        c["notice"] = str(data.get("notice") or "").strip()[:1500]
    try:
        c["sort"] = int(data.get("sort") or 100)
    except ValueError:
        c["sort"] = 100
    save_site(db, st)


def set_product_cats(db: Session, slug: str, paths: set[str], only: set[str] | None = None) -> None:
    """Категорії товару: paths — де він має бути; only — які категорії взагалі чіпаємо (для масових дій)."""
    st = site(db)
    for c in st["cats"]:
        if only is not None and c["path"] not in only:
            continue
        has = slug in c["slugs"]
        if c["path"] in paths and not has:
            c["slugs"].append(slug)
        elif c["path"] not in paths and has:
            c["slugs"].remove(slug)
    save_site(db, st)


def admin_rows(db: Session, q: str = "", cat: str = "", flt: str = "") -> list[dict]:
    """Товари для «Налаштування → Інтернет-магазин → Товари»: усі картки з даними сайту (і приховані)."""
    st = site(db)
    in_cats = defaultdict(list)
    for c in st["cats"]:
        for sl in c["slugs"]:
            in_cats[sl].append(c)
    rows, today = [], datetime.now().date().isoformat()
    files = defaultdict(list)
    for f in db.scalars(select(ProductFile).where(ProductFile.kind == "photo", ProductFile.site.is_(True)).order_by(ProductFile.sort, ProductFile.id)):
        files[f.product_id].append(f.id)
    for p in db.scalars(select(Product).where(Product.site_info.is_not(None)).order_by(Product.brand, Product.name)):
        si = p.site_info or {}
        if not si.get("slug"):
            continue
        sale = si.get("sale") or {}
        live = bool(sale.get("pct")) and (not sale.get("until") or str(sale["until"]) >= today)
        base = _price(p)
        r = {"p": p, "slug": si["slug"], "name": si.get("title") or p.name, "brand": si.get("brand_name") or p.brand or "", "base": base,
             "price": round(base * (1 - float(sale["pct"]) / 100)) if live else base, "sale": sale if sale.get("pct") else None, "live": live,
             "on": p.site_on is not False and p.active is not False, "cats": in_cats.get(si["slug"], []), "img": (files.get(p.id) or [None])[0],
             "sticker": si.get("sticker") or "",
             "stock": str(si.get("presence", 1)).lower() not in ("0", "false", "not_available")}
        if q and q.lower() not in f"{r['name']} {r['brand']} {p.sku or ''}".lower():
            continue
        if cat and not any(c["path"] == cat for c in r["cats"]):
            continue
        if (flt == "on" and not r["on"]) or (flt == "off" and r["on"]) or (flt == "sale" and not r["sale"]) or (flt == "nocat" and r["cats"]) \
                or (flt == "nophoto" and r["img"]) or (flt == "sticker" and not r["sticker"]):
            continue
        rows.append(r)
    return rows


def bulk(db: Session, ids: list[int], action: str, data: dict) -> int:
    n = 0
    prods = db.scalars(select(Product).where(Product.id.in_(ids or [0]))).all()
    if action in ("cat_add", "cat_del"):
        path = str(data.get("cat") or "")
        st = site(db)
        c = next((x for x in st["cats"] if x["path"] == path), None)
        if not c:
            raise ShopError("Оберіть категорію")
        for p in prods:
            sl = (p.site_info or {}).get("slug")
            if sl and action == "cat_add" and sl not in c["slugs"]:
                c["slugs"].append(sl); n += 1
            elif sl and action == "cat_del" and sl in c["slugs"]:
                c["slugs"].remove(sl); n += 1
        save_site(db, st)
        return n
    for p in prods:
        si = dict(p.site_info or {})
        if action == "sale":
            try:
                pct = float(str(data.get("pct") or 0).replace(",", "."))
            except ValueError:
                raise ShopError("Знижка — число від 1 до 90") from None
            if not 1 <= pct <= 90:
                raise ShopError("Знижка — число від 1 до 90")
            si["sale"] = {"pct": pct, "until": str(data.get("until") or "")[:10]}
            si.pop("price_old", None)
        elif action == "sale_off":
            si.pop("sale", None)
            si.pop("price_old", None)
        elif action == "sticker":
            t = " ".join(str(data.get("sticker") or "").split())[:24]
            if not t:
                raise ShopError("Вкажіть текст стікера")
            si["sticker"] = t
        elif action == "sticker_off":
            si.pop("sticker", None)
        elif action == "show":
            p.site_on = True
        elif action == "hide":
            p.site_on = False
        else:
            raise ShopError("Невідома дія")
        p.site_info = si
        n += 1
    db.commit()
    _cache["cat_at"] = 0.0
    return n


def make_sale(db: Session, o: SiteOrder, pl) -> int | None:
    """Оплачене онлайн замовлення → чек у CRM (продаж товарів, списання зі складу); фіскальний чек — від monobank."""
    from . import sales as sl, paylinks as pls
    if (o.raw or {}).get("sale_id") or not o.patient_id:
        return (o.raw or {}).get("sale_id")
    acc = pls._acc_of(pls._raw(db), pl) or {}
    items = []
    for i in o.items or []:
        if not i.get("product_id"):
            continue
        sq = int(i.get("short") or 0)
        if sq:                                          # короткий термін: окремий рядок зі знижкою — списується саме коротка партія
            items.append({"kind": "product", "ref_id": i["product_id"], "name": i["name"], "qty": sq, "price": float(i["short_price"]),
                          "expiry": date.fromisoformat(i["short_exp"]) if i.get("short_exp") else None})
            if i["qty"] - sq > 0:
                items.append({"kind": "product", "ref_id": i["product_id"], "name": i["name"], "qty": i["qty"] - sq, "price": float(i["full"])})
        else:
            items.append({"kind": "product", "ref_id": i["product_id"], "name": i["name"], "qty": i["qty"], "price": i["price"]})
    if not items:
        return None
    used = float(((o.raw or {}).get("coins") or {}).get("used") or 0)
    promo = (o.raw or {}).get("promo") or {}
    pamt = float(promo.get("amount") or 0)
    label = " + ".join(x for x in (("MOLODIST Coins" if used else ""), (f"промокод {promo.get('code')}" if pamt else "")) if x) or "MOLODIST Coins"
    used += pamt
    paid_sum = round(sum(i["qty"] * i["price"] for i in items) - used, 2)
    sale = sl.create(db, {"patient_id": o.patient_id, "no_discounts": True, "no_accrual": True, "manual_discount": used, "discount_label": label, "items": items,
                          "payments": [{"method": sl.paylink_method(acc.get("name") or "monobank"), "amount": paid_sum}],
                          "comment": f"Інтернет-магазин, замовлення {o.number}"})
    pl.sale_id = sale.id
    o.raw = {**(o.raw or {}), "sale_id": sale.id}
    db.flush()
    return sale.id


def children(cat: dict, data: dict) -> list[dict]:
    return [c for c in data["cats"] if c["parent"] == cat["path"] and c["n"]]


def listing(items: list[dict], sort: str = "", brand: str = "") -> list[dict]:
    if brand:
        items = [i for i in items if i["brand"].lower() == brand.lower()]
    key = {"cheap": lambda i: i["price"], "expensive": lambda i: -i["price"], "new": lambda i: -i["new"], "sale": lambda i: -i["pct"]}.get(sort)
    items = sorted(items, key=key) if key else sorted(items, key=lambda i: (-i["sold"], -i["new"]))
    return sorted(items, key=lambda i: not i["stock"])


def search(data: dict, q: str) -> list[dict]:
    words = [w for w in re.split(r"\s+", (q or "").lower().strip()) if len(w) > 1]
    if not words:
        return []
    return [i for i in data["items"] if all(w in f"{i['name']} {i['brand']} {i['sku']}".lower() for w in words)]


CAT_ICONS = (("sale", "sale"), ("volos", "hair"), ("aksesuar", "hair"), ("oblich", "face"), ("til", "body"), ("ochi", "eye"), ("spf", "sun"))


def cat_icon(path: str) -> str:
    seg = path.strip("/").split("/")[-1]
    return next((ic for key, ic in CAT_ICONS if key in seg), "jar")


def seo_coverage(db: Session) -> dict:
    """Чи все SEO з Shop-Express на місці: скільки товарів сайту мають адресу, title, description, опис, фото."""
    rows = [p for p in db.scalars(select(Product).where(Product.site_info.is_not(None))) if (p.site_info or {}).get("slug")]
    g = lambda k: sum(1 for p in rows if (p.site_info or {}).get(k))
    st = site(db)
    cats = st.get("cats") or []
    return {"products": len(rows), "on": sum(1 for p in rows if p.site_on), "title": g("seo_title"), "desc": g("seo_description"), "text": g("short_description"),
            "photo": sum(1 for p in rows if (p.photo_url or "").startswith("/products/files/")),
            "no_title": [p.name for p in rows if p.site_on and not (p.site_info or {}).get("seo_title")][:15],
            "cats": len(cats), "cats_title": sum(1 for c in cats if c.get("title")), "cats_desc": sum(1 for c in cats if c.get("desc")),
            "pages": len(st.get("pages") or []), "in_cats": len({x for c in cats for x in c.get("slugs") or []})}


def home_blocks(data: dict) -> dict:
    live = [i for i in data["items"] if i["stock"]]
    return {"sale": sorted([i for i in live if i["pct"]], key=lambda i: -i["pct"])[:12],
            "new": sorted(live, key=lambda i: -i["new"])[:12],
            "best": sorted([i for i in live if i["sold"]], key=lambda i: -i["sold"])[:12] or live[:12]}


# ---------------- кошик і замовлення ----------------
def price_cart(db: Session, rows: list[dict], c: dict | None = None) -> dict:
    """rows: [{id (product id), qty}] → позиції з актуальними цінами CRM, сума, доставка, подарунок."""
    c = c or cfg(db)
    data = catalog(db)
    by_id = {v["id"]: (it, v) for it in data["items"] for v in it["variants"]}
    out, total = [], 0.0
    for r in rows or []:
        try:
            pid, qty = int(r.get("id")), max(1, min(50, int(r.get("qty") or 1)))
        except (TypeError, ValueError):
            continue
        if pid not in by_id:
            continue
        it, v = by_id[pid]
        if not v["stock"]:
            continue
        if v.get("left") is not None:                   # не більше, ніж є на вітрині
            qty = min(qty, int(v["left"]))
            if qty < 1:
                continue
        sq = min(qty, int(v.get("short") or 0))
        if sq:                                          # знижка «короткий термін» — лише на одиниці короткої партії
            line = round(v["price"] * sq + float(v["full"]) * (qty - sq), 2)
        else:
            line = round(v["price"] * qty, 2)
        total += line
        out.append({"id": pid, "slug": it["slug"], "name": v["name"], "brand": it["brand"], "qty": qty, "price": round(line / qty, 2), "total": line,
                    "sale": bool(v.get("old")),
                    "img": it["imgs"][0] if it["imgs"] else None, "sku": v["sku"],
                    **({"short": sq, "short_price": v["price"], "short_exp": v["short_exp"], "full": v["full"]} if sq else {})})
    free_from = float(c.get("free_from") or 0)
    gifts = [g[0] for g in lines(c.get("gifts"))] if c.get("gift_from") and total >= float(c["gift_from"]) else []
    return {"items": out, "total": round(total, 2), "free_delivery": bool(free_from and total >= free_from),
            "to_free": max(0, round(free_from - total, 2)) if free_from else 0, "gifts": gifts}


def create_order(db: Session, data: dict, base_url: str) -> dict:
    from ..integrations.messaging import normalize_phone
    from . import shop_orders as so
    c = cfg(db)
    cart = price_cart(db, data.get("items") or [], c)
    if not cart["items"]:
        raise ShopError("Кошик порожній або товари закінчились.")
    name = " ".join(str(data.get("name") or "").split())[:120]
    phone = normalize_phone(str(data.get("phone") or ""))
    if len(name) < 2:
        raise ShopError("Вкажіть імʼя та прізвище.")
    if not phone:
        raise ShopError("Перевірте номер телефону: +380 і 9 цифр.")
    delivery = data.get("delivery") if data.get("delivery") in ("np", "np_courier") else ("pickup" if data.get("delivery") == "pickup" and c.get("pickup") else "np")
    city, wh = str(data.get("city") or "").strip()[:120], str(data.get("warehouse") or "").strip()[:300]
    if delivery == "np_courier":
        wh = str(data.get("addr") or "").strip()[:300]
        if len(city) < 2 or len(wh) < 5:
            raise ShopError("Вкажіть місто й адресу доставки курʼєром: вулиця, будинок, квартира.")
    elif delivery == "np" and (len(city) < 2 or len(wh) < 1):
        raise ShopError("Вкажіть місто й відділення Нової пошти.")
    pay = data.get("pay") if data.get("pay") in ("online", "cod", "parts") else "online"
    if pay == "parts":
        from . import mono_parts as _mp
        _pp = _mp.public(db)
        if not _pp["ready"] or cart["total"] < float(_pp["min_sum"] or 0):
            pay = "online"
    if pay == "cod" and not c.get("cod"):
        pay = "online"
    gift = str(data.get("gift") or "")[:200] if str(data.get("gift") or "") in cart["gifts"] else ""
    from . import coins as cn
    acc_pid = data.get("_patient_id")                       # покупець увійшов у кабінет — можна списати MOLODIST Coins
    cq = cn.quote(db, c, cart["items"], acc_pid, int(data.get("coins") or 0)) if acc_pid and pay != "parts" else {"use": 0}
    use = cq["use"]
    full = cart["total"]
    from . import shop_promo as _pr
    pr = _pr.check(db, data.get("promo"), cart["items"], phone, use) if _pr.norm(data.get("promo")) else None
    if pr and not pr["ok"]:
        raise ShopError(pr["msg"])
    pamt = pr["amount"] if pr else 0
    if use or pamt:
        cart = {**cart, "total": round(full - use - pamt, 2)}
    fr = float(c.get("free_from") or 0)
    cart["free_delivery"] = bool(fr and cart["total"] >= fr)          # безкоштовна доставка — від кінцевої суми після знижок і Coins
    no = f"M{datetime.now():%y%m%d}-{secrets.randbelow(9000) + 1000}"
    o = SiteOrder(source="crm", ext_id=no, number=no, created_at=datetime.now(), status="Очікує оплати" if pay != "cod" else "Нове", paid=False,
                  total=cart["total"], total_full=full, delivery_price=0,
                  delivery={"pickup": "Самовивіз з MOLODIST", "np_courier": "Нова пошта — курʼєр"}.get(delivery, "Нова пошта"),
                  payment={"online": "Онлайн карткою (monobank)", "cod": "При отриманні", "parts": "Покупка Частинами monobank"}[pay],
                  name=name, phone=phone, email=str(data.get("email") or "").strip()[:200] or None, city=city or None, address=wh or None,
                  comment=(str(data.get("comment") or "").strip()[:1000] + (f"\nПодарунок: {gift}" if gift else "")).strip() or None,
                  items=[{"product_id": i["id"], "name": i["name"], "sku": i["sku"], "qty": i["qty"], "price": i["price"], "total": i["total"], "sale": i.get("sale", False),
                          **({k: i[k] for k in ("short", "short_price", "short_exp", "full")} if i.get("short") else {})} for i in cart["items"]],
                  extra=[{"name": f"Подарунок: {gift}", "price": 0, "on": True}] if gift else None,
                  raw={"utm": {k: str(v)[:150] for k, v in data.items() if k.startswith("utm_") or k in ("gclid", "fbclid", "gbraid", "wbraid", "landing")},
                       "free_delivery": cart["free_delivery"]},
                  synced_at=datetime.now())
    db.add(o)
    pat = so._patient(db, name, phone, o.email, {})
    if pat:
        o.patient_id = pat.id
    if acc_pid:
        o.patient_id = acc_pid                               # замовлення з кабінету — завжди в картку власника кабінету
    db.flush()
    if use and cn.reserve(db, o, use) != use:
        db.rollback()
        raise ShopError("Не вдалося застосувати MOLODIST Coins — оновіть сторінку й спробуйте ще раз.")
    if use:
        o.extra = (o.extra or []) + [{"name": f"Знижка MOLODIST Coins: {use}", "price": -use, "on": True}]
    if pamt:
        o.raw = {**(o.raw or {}), "promo": {"code": pr["code"], "amount": pamt, "lines": pr["lines"]}}
        o.extra = (o.extra or []) + [{"name": f"Промокод {pr['code']}", "price": -pamt, "on": True}]
        _pr.used(db, pr["id"])
    db.commit()
    out = {"ok": True, "number": no, "total": cart["total"], "id": o.id}
    if pay == "parts":
        from . import mono_parts as mp
        from ..config import settings as _st
        try:
            mp.create(db, db.get(SiteOrder, out["id"]), int(data.get("parts") or 0), _st.public_url.rstrip("/") + "/api/public/monoparts")
            out["parts"] = True
        except mp.PartsError as e:
            db.rollback()
            o = db.get(SiteOrder, out["id"])
            o.status = "Нове"
            o.comment = ((o.comment or "") + f"\nПокупка Частинами не оформилась: {str(e)[:150]}").strip()
            db.commit()
            out["parts_error"] = "Не вдалося оформити Покупку Частинами: " + str(e)[:120] + ". Замовлення збережено — адміністратор запропонує іншу оплату."
    if pay == "online":
        try:
            from . import paylinks as pl
            link = pl.create(db, cart["total"], f"Замовлення {no}", base_url, patient_id=o.patient_id, account_id=int(c.get("pay_account") or 0) or None,
                             basket=_basket(cart["items"], use, (pamt, pr["lines"]) if pamt else None))
            link.info = {**(link.info or {}), "shop_order": o.id}
            db.commit()
            out["pay_url"] = link.url
        except Exception as e:  # noqa: BLE001 — оплата недоступна: замовлення лишається, адміністратор звʼяжеться
            db.rollback()
            o = db.get(SiteOrder, out["id"])
            o.status = "Нове"
            o.comment = ((o.comment or "") + f"\nОнлайн-оплата не створилась: {str(e)[:150]}").strip()
            db.commit()
    notify(db, db.get(SiteOrder, out["id"]))
    return out


def split_discount(items: list[dict], disc: float) -> list[float]:
    """Знижку коїнами — пропорційно між неакційними рядками, залишок копійок — методом найбільших залишків. Сума точно = disc."""
    base = [float(i["total"]) if not i.get("sale") else 0.0 for i in items]
    tot = sum(base)
    if disc <= 0 or tot <= 0:
        return [0.0] * len(items)
    kop = int(round(disc * 100))
    raw = [b * kop / tot for b in base]
    out = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - out[i]))[:kop - sum(out)]:
        out[i] += 1
    return [x / 100 for x in out]


def _basket(items: list[dict], use: int, promo: tuple | None = None) -> list[dict]:
    d = split_discount(items, use)
    if promo:
        from . import shop_promo as _pr
        d = [round(a + b, 2) for a, b in zip(d, _pr.split(items, promo[0], promo[1]))]
    return [{"name": i["name"][:128], "qty": i["qty"], "total": round(i["total"] - x, 2), "code": str(i["sku"] or i["id"])} for i, x in zip(items, d)]


def paid(db: Session, pl) -> None:
    """Оплачене посилання → замовлення «Оплачено»."""
    oid = (pl.info or {}).get("shop_order")
    o = db.get(SiteOrder, oid) if oid else None
    if o and not o.paid:
        o.paid, o.status = True, "Оплачено"
        o.raw = {**(o.raw or {}), "pay_state": "paid", "paid_at": datetime.utcnow().isoformat(timespec="seconds")}
        log(o, "Оплачено онлайн")
        db.flush()
        try:
            from . import coins as cn
            cn.plan_earn(db, o, cfg(db))
        except Exception:  # noqa: BLE001
            pass
        try:
            make_sale(db, o, pl)
        except Exception as e:  # noqa: BLE001 — гроші прийшли: замовлення оплачене, чек адміністратор проведе вручну
            db.rollback()
            o = db.get(SiteOrder, oid)
            o.paid, o.status = True, "Оплачено"
            o.comment = ((o.comment or "") + f"\nЧек у CRM не створився автоматично: {str(e)[:150]}").strip()
            db.flush()
        notify(db, o, paid_now=True)
        _cust(db, o, "paid")


def _cust(db: Session, o: SiteOrder, event: str) -> None:
    try:
        from . import shop_cabinet as sc
        if sc.order_notice(db, cfg(db), o, event):
            log(o, {"paid": "Покупцю надіслано: оплачено", "sent": "Покупцю надіслано: відправлено", "cancel": "Покупцю надіслано: скасовано",
                  "arrived": "Покупцю надіслано: прибуло у відділення"}[event])
    except Exception:  # noqa: BLE001 — повідомлення покупцю не має ламати обробку замовлення
        pass


def _tg_send(chat: str, text: str, reply_to: int | None = None) -> tuple[bool, int | None]:
    """Повідомлення в Telegram; повертає id повідомлення — щоб статус оплати приходив відповіддю на нього."""
    from . import tg_notify
    from ..config import settings
    if not settings.telegram_bot_token:
        return bool(tg_notify.send_text(chat, text)), None
    data = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if reply_to:
        data.update(reply_to_message_id=str(reply_to), allow_sending_without_reply="true")
    try:
        r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", data=data, timeout=20)
        return r.status_code == 200, ((r.json().get("result") or {}).get("message_id") if r.status_code == 200 else None)
    except (httpx.HTTPError, ValueError):
        return False, None


def order_chats(db: Session) -> list[str]:
    """Кому йдуть замовлення магазину: чат заявок (/zayavky) + адміністратори й власник."""
    from . import tg_notify, site_leads
    out = []
    lead = str(site_leads.cfg(db).get("tg_chat") or "").strip()
    if lead:
        out.append(lead)
    for role in ("admin", "owner"):
        for u in tg_notify.recipients(db, role):
            if u.telegram_chat_id and str(u.telegram_chat_id) not in out:
                out.append(str(u.telegram_chat_id))
    return out


def log(o: SiteOrder, text: str, user: str = "") -> None:
    raw = dict(o.raw or {})
    raw["log"] = ((raw.get("log") or []) + [{"at": datetime.now().isoformat(timespec="minutes"), "by": user[:80], "text": text[:300]}])[-60:]
    o.raw = raw


def _order_lines(o: SiteOrder) -> list[str]:
    from html import escape as e
    from ..config import settings
    return [f"{e(o.name or '')} · {e(o.phone or '')}", "; ".join(f"{e(i['name'][:60])} × {i['qty']:g}" for i in (o.items or [])[:8]),
            f"Сума: {o.total:,.0f} ₴ · {e(o.payment or '')} · {e(o.delivery or '')}".replace(",", " "),
            f"{settings.public_url.rstrip('/')}/shop-orders/{o.id}"]


def notify(db: Session, o: SiteOrder, paid_now: bool = False, fail: str = "") -> int:
    """Нове замовлення → повідомлення в чат заявок і адміністраторам. Оплата / несплата — відповіддю на те саме повідомлення."""
    from html import escape as e
    raw = dict(o.raw or {})
    sent = raw.get("tg") or []
    n = 0
    c_ = cfg(db)
    if (paid_now or fail) and not c_.get("tg_pay", True):
        return 0
    if not (paid_now or fail) and not c_.get("tg_order", True):
        return 0
    if paid_now or fail:
        head = f"✅ <b>Оплачено</b> — замовлення №{e(o.number or '')}, {o.total:,.0f} ₴".replace(",", " ") if paid_now else \
            f"⚠️ <b>Не оплачено</b> — замовлення №{e(o.number or '')}: {PAY_FAIL.get(fail, fail)}. Звʼяжіться з покупцем."
        if sent:
            for ch, mid in sent:
                n += _tg_send(str(ch), head, mid)[0]
            return n
        text = "\n".join([head] + _order_lines(o))
        for ch in order_chats(db):
            n += _tg_send(ch, text)[0]
        return n
    wait = "" if (o.payment or "").lower().startswith("при отриманні") or o.paid else "\n⏳ <i>Оплата ще не підтверджена — статус прийде відповіддю на це повідомлення.</i>"
    text = "\n".join([f"<b>🛍 Нове замовлення в інтернет-магазині</b> №{e(o.number or '')}"] + _order_lines(o)) + wait
    for ch in order_chats(db):
        ok, mid = _tg_send(ch, text)
        n += ok
        if mid:
            sent.append([ch, mid])
    if sent:
        raw["tg"] = sent
        o.raw = raw
        db.flush()
    return n


def pay_event(db: Session, pl, status: str) -> None:
    """Посилання на оплату не оплачене (помилка / прострочено / скасовано) → позначка в замовленні й відповідь у чат."""
    oid = (pl.info or {}).get("shop_order")
    o = db.get(SiteOrder, oid) if oid else None
    if not o or o.paid or status not in PAY_FAIL or (o.raw or {}).get("pay_state") == status:
        return
    o.raw = {**(o.raw or {}), "pay_state": status, "pay_state_at": datetime.utcnow().isoformat(timespec="seconds")}
    log(o, f"Оплата: {PAY_FAIL[status]}")
    from . import coins as cn
    if cn.release(db, o, cfg(db)):
        log(o, "MOLODIST Coins повернено покупцю")
    db.flush()
    notify(db, o, fail=status)


def set_status(db: Session, o: SiteOrder, status: str, user: User | None = None, ttn: str | None = None, paid: bool | None = None,
               resp: int | None = None) -> None:
    who = (user.name or user.login) if user else ""
    raw = dict(o.raw or {})
    if status in STATUSES and status != o.status:
        log(o, f"Статус: {o.status or '—'} → {status}", who)
        raw = dict(o.raw or {})
        o.status = status
        if status == "Отримано":
            raw["recv_at"] = datetime.now().isoformat(timespec="minutes")
    if ttn is not None and ttn.strip()[:40] != (raw.get("ttn") or ""):
        raw["ttn"] = ttn.strip()[:40]
        raw["log"] = (raw.get("log") or []) + [{"at": datetime.now().isoformat(timespec="minutes"), "by": who, "text": f"ТТН: {raw['ttn'] or 'прибрано'}"}]
    if resp is not None and resp != (raw.get("resp") or 0):
        raw["resp"] = resp or None
        u = db.get(User, resp) if resp else None
        raw["log"] = (raw.get("log") or []) + [{"at": datetime.now().isoformat(timespec="minutes"), "by": who, "text": f"Відповідальний: {(u.name or u.login) if u else 'не призначено'}"}]
    if paid is not None and bool(o.paid) != paid:
        o.paid = paid
        raw["log"] = (raw.get("log") or []) + [{"at": datetime.now().isoformat(timespec="minutes"), "by": who, "text": "Оплату позначено вручну: " + ("оплачено" if paid else "не оплачено")}]
    o.raw = raw
    try:
        from . import shop_reviews as _rv
        _rv.mark_due(o, cfg(db))
    except Exception:  # noqa: BLE001
        pass
    db.flush()
    try:
        from . import coins as cn
        cn.on_status(db, o, cfg(db))
    except Exception:  # noqa: BLE001
        pass
    if o.status == "Відправлено":
        _cust(db, o, "sent")
    elif o.status == "Скасовано":
        _cust(db, o, "cancel")
    elif o.paid and o.status == "Оплачено":
        _cust(db, o, "paid")


def funnel(rows: list) -> list[dict]:
    cnt = defaultdict(int)
    for o in rows:
        cnt[LEGACY_STATUS.get(o.status or "", o.status or "")] += 1
    return [{"name": x, "n": cnt.get(x, 0)} for x in STATUSES]


def order_view(db: Session, o: SiteOrder) -> dict:
    ids = [i.get("product_id") for i in o.items or [] if i.get("product_id")]
    photo = {p.id: p.photo_url for p in db.scalars(select(Product).where(Product.id.in_(ids or [0])))}
    raw = o.raw or {}
    return {"photo": photo, "ttn": raw.get("ttn") or "", "resp": raw.get("resp"), "log": list(reversed(raw.get("log") or [])), "pay_state": raw.get("pay_state"),
            "pay_fail": PAY_FAIL.get(raw.get("pay_state") or ""), "sale_id": raw.get("sale_id")}


# ---------------- Нова пошта ----------------
def np_call(c: dict, model: str, method: str, props: dict, http: httpx.Client | None = None) -> list:
    key = (c.get("np_key") or "").strip()
    if not key:
        return []
    http = http or httpx.Client(timeout=15)
    try:
        j = http.post("https://api.novaposhta.ua/v2.0/json/", json={"apiKey": key, "modelName": model, "calledMethod": method,
                                                                    "methodProperties": props}).json()
    except (httpx.HTTPError, ValueError):
        return []
    return j.get("data") or [] if j.get("success") else []


def np_cities(c: dict, q: str) -> list[dict]:
    d = np_call(c, "Address", "searchSettlements", {"CityName": q[:60], "Limit": "12", "Page": "1"})
    rows = (d[0].get("Addresses") if d else []) or []
    return [{"ref": r.get("DeliveryCity"), "name": r.get("Present")} for r in rows if r.get("DeliveryCity")]


def np_warehouses(c: dict, city_ref: str, q: str = "") -> list[dict]:
    d = np_call(c, "Address", "getWarehouses", {"CityRef": city_ref, "FindByString": q[:60], "Limit": "40", "Page": "1"})
    return [{"ref": r.get("Ref"), "name": r.get("Description")} for r in d]


def path_of_old(url: str) -> str:
    u = urlparse(url)
    return u.path if u.netloc.endswith("molodist.store") else url


_TR = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюяыэё", ["a", "b", "v", "h", "g", "d", "e", "ie", "zh", "z", "y", "i", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                                                       "u", "f", "kh", "ts", "ch", "sh", "shch", "", "iu", "ia", "y", "e", "e"]))


def slugify(s: str) -> str:
    s = "".join(_TR.get(ch, ch) for ch in str(s or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "brand"


def brands_sync(db: Session) -> dict:
    """Бренди → сторінки /shop/brendi/<бренд>/: новий бренд на сайті отримує сторінку автоматично,
    нові товари бренду додаються до його сторінки. Ручні налаштування сторінки (назва, SEO, приховано) не чіпаємо."""
    st = site(db)
    by_brand: dict[str, list[str]] = defaultdict(list)
    shown: dict[str, str] = {}
    for p in db.scalars(select(Product).where(Product.site_info.is_not(None))):
        si = p.site_info or {}
        b = (si.get("brand_name") or p.brand or "").strip()
        if not b or not si.get("slug") or p.site_on is False or p.active is False:
            continue
        by_brand[b.lower()].append(si["slug"])
        shown.setdefault(b.lower(), b)
    cats = {c["path"]: c for c in st["cats"]}
    bcats = {c["name"].strip().lower(): c for c in st["cats"] if c["path"].startswith("/shop/brendi/") and c["path"] != "/shop/brendi/"}
    try:
        from .shop_seo import BRANDS
        alias = {v[0].lower(): k for k, v in BRANDS.items()}
    except Exception:  # noqa: BLE001
        alias = {}
    rep = {"new": [], "added": 0}
    for key, slugs in by_brand.items():
        c = bcats.get(key) or next((x for n, x in bcats.items() if n.split(" (")[0] == key or key.split(" (")[0] == n.split(" (")[0]), None)
        if not c:
            seg = alias.get(key) or slugify(shown[key])
            path = f"/shop/brendi/{seg}/"
            if path in cats:
                c = cats[path]
            else:
                from .shop_seo import brand_seo
                t_, d_ = brand_seo(seg, shown[key])
                c = {"path": path, "name": shown[key], "title": t_, "desc": d_, "img": "", "in_menu": False, "slugs": [], "auto": True}
                st["cats"].append(c)
                cats[path] = c
                rep["new"].append(shown[key])
        for sl in slugs:
            if sl not in c["slugs"]:
                c["slugs"].append(sl)
                rep["added"] += 1
    if rep["new"] or rep["added"]:
        save_site(db, st)
    return rep


def hidden_target(db: Session, data: dict, slug: str) -> str | None:
    """Товар є в CRM, але прихований із сайту: куди вести старе посилання (бренд → магазин)."""
    if not re.fullmatch(r"[a-z0-9-]{3,200}", slug or ""):
        return None
    p = next((x for x in db.scalars(select(Product).where(Product.site_info.is_not(None))) if (x.site_info or {}).get("slug") == slug), None)
    if not p:
        return None
    brand = ((p.site_info or {}).get("brand_name") or p.brand or "").strip().lower()
    for c in data["cats"]:
        if c["path"].startswith("/shop/brendi/") and c["name"].strip().lower().split(" (")[0] == brand.split(" (")[0] and c.get("n"):
            return c["path"]
    return "/shop/"


def old_links_report(db: Session) -> dict:
    """Перевірка старих посилань molodist.store (з карток товарів CRM): чи відкривається товар на новому сайті."""
    from urllib.parse import urlparse
    data = catalog(db)
    rows = {"ok": [], "redirect": [], "missing": []}
    for p in db.scalars(select(Product).where(Product.site_info.is_not(None)).order_by(Product.name)):
        si = p.site_info or {}
        links = {x for x in (si.get("link"), p.url) if x and ("molodist.store" in x or x.startswith("/"))}
        for ln in links:
            path = urlparse(ln if ln.startswith("http") else "https://molodist.store" + ln).path
            segs = [x for x in path.split("/") if x]
            row = {"id": p.id, "name": si.get("title") or p.name, "link": "https://molodist.store" + (path if path.endswith("/") else path + "/")}
            if len(segs) >= 2 and segs[0] == "shop" and segs[1] in data["by_slug"]:
                rows["ok"].append(row)
            elif len(segs) >= 2 and segs[0] == "shop" and segs[1] in (data.get("archive") or {}):
                rows["redirect"].append({**row, "to": "сторінка товару «зараз немає в продажу онлайн»"})
            elif len(segs) >= 2 and segs[0] == "shop" and hidden_target(db, data, segs[1]):
                rows["redirect"].append({**row, "to": hidden_target(db, data, segs[1])})
            else:
                rows["missing"].append(row)
    return rows
