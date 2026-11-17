"""Фіди товарів і дані для ШІ: Google Merchant Center (Shopping + динамічний ремаркетинг), Meta (Facebook / Instagram Shopping),
Hotline.ua, а також llms.txt / llms-full.txt для пошукових ШІ-сервісів.

Ідентифікатор товару у фідах = id варіанта в CRM — той самий, що йде в події GA4 / Pixel (view_item, add_to_cart, purchase),
тож динамічний ремаркетинг Google Ads і Meta зіставляє перегляди з фідом.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from xml.sax.saxutils import escape as x


FEED_KEYS = ("google", "facebook", "hotline")


def _strip(h: str, n: int = 4900) -> str:
    t = re.sub(r"<[^>]+>", " ", h or "")
    t = html.unescape(re.sub(r"\s+", " ", t)).strip()
    return t[:n]


def _cats_of(data: dict) -> dict[str, list[dict]]:
    """slug → категорії товару (без брендів), найглибша першою."""
    out: dict[str, list[dict]] = {}
    for c in data["cats"]:
        if c["path"].startswith("/shop/brendi/") or c["path"] in ("/shop/sale/", "/shop/blog/"):
            continue
        for s in c["slugs"]:
            out.setdefault(s, []).append(c)
    for v in out.values():
        v.sort(key=lambda c: -c["depth"])
    return out


def _ptype(cats: list[dict], data: dict) -> str:
    if not cats:
        return ""
    by = {c["path"]: c for c in data["cats"]}
    chain, c = [], cats[0]
    while c:
        chain.append(c["name"])
        c = by.get(c.get("parent") or "")
    return " > ".join(reversed(chain))


_CYR = re.compile(r"[А-ЯЄІЇҐа-яєіїґʼ']")
_VOL = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ML|МЛ|G|Г|ГР|PCS|ШТ)\b", re.I)


def g_title(name: str, brand: str, label: str = "") -> str:
    """Назва для Google: «Бренд + назва + обʼєм», без КРИКУ капсом (кириличні слова — звичайними літерами,
    латинські назви ліній лишаються як є), обʼєм — «50 мл»."""
    t = " ".join((name or "").split())
    letters = [ch for ch in t if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:     # усе капсом
        words = []
        keep = {"SPF", "AHA", "BHA", "PHA", "NMF", "ZO", "PDRN", "PN", "HA", "UV", "LED", "MHC", "CBD"}
        for w in t.split(" "):
            if _CYR.search(w):
                words.append(w.lower())
            elif w.isupper() and len(w) >= 4 and w.strip("+-()\"'.,") not in keep:
                words.append("-".join(x.capitalize() for x in w.split("-")))
            else:
                words.append(w)
        t = " ".join(words)
        t = t[:1].upper() + t[1:]
    t = _VOL.sub(lambda m: f"{m.group(1)} " + {"ml": "мл", "мл": "мл", "g": "г", "г": "г", "гр": "г", "pcs": "шт", "шт": "шт"}[m.group(2).lower()], t)
    b = (brand or "").strip()
    if b and t.lower().startswith(b.lower()):
        t = b + t[len(b):]                                  # «DEMAX …» → «Demax …» (як у назві бренду)
    if b and b.lower() not in t.lower() and b.lower() != "molodist":
        t = f"{b} {t}"
    if label and not _VOL.search(t) and label.lower() not in t.lower():
        t = f"{t}, {label}"
    return t[:150]


def g_desc(desc: str, name: str, brand: str, ptype: str = "") -> str:
    """Опис для Google: без повтору назви капсом на початку; якщо опису майже немає — короткий текст з бренду й категорії."""
    d = (desc or "").strip()
    n = " ".join((name or "").split())
    if n and d[:len(n)].upper() == n.upper() and sum(ch.isupper() for ch in d[:len(n)] if ch.isalpha()) > 0.7 * max(1, sum(ch.isalpha() for ch in d[:len(n)])):
        d = d[len(n):].lstrip(" .,:;–—-")                      # «REJURAN … 19ГР Активно захищає…» → «Активно захищає…»
    if len(d) < 150:
        t = g_title(n, brand)
        kind = ptype.split(" > ")[-1].lower() if ptype else "професійна косметика для догляду за шкірою"
        extra = f"{t} — {kind}" + (f" бренду {brand}" if brand and brand.lower() != "molodist" else "") + "."
        d = " ".join(x for x in (extra, d if d.upper() != d or not d else "",
                                  "Оригінальна продукція від MOLODIST (Київ): лікар допоможе підібрати домашній догляд, "
                                  "доставка Новою поштою по Україні.") if x)
    return d[:4900]


_SKIN = (("всіх типів", "усіх типів шкіри"), ("сух", "суха"), ("жирн", "жирна"), ("комбінован", "комбінована"),
         ("чутлив", "чутлива"), ("проблемн", "проблемна"), ("нормальн", "нормальна"), ("зневоднен", "зневоднена"),
         ("зріл", "зріла"), ("купероз", "схильна до почервонінь"), ("розац", "схильна до почервонінь"))
_GOAL = (("зволож", "зволоження"), ("гідрат", "зволоження"), ("зморш", "anti-age"), ("старін", "anti-age"), ("ліфт", "ліфтинг"),
         ("пружн", "пружність"), ("пігмент", "вирівнювання тону"), ("сяйв", "сяйво"), ("бар", "відновлення бар'єру"),
         ("відновл", "відновлення"), ("заспок", "заспокоєння"), ("почервон", "зменшення почервонінь"), ("пор", "звуження пор"),
         ("себум", "контроль себуму"), ("блиск", "контроль себуму"), ("ультрафіолет", "захист від сонця"), ("spf", "захист від сонця"),
         ("очищ", "очищення"), ("живлен", "живлення"), ("антиоксид", "антиоксидантний захист"))
_ACT = (("гіалурон", "гіалуронова кислота"), ("ніацинамід", "ніацинамід"), ("ретинал", "ретинал"), ("ретинол", "ретинол"),
        ("вітамін c", "вітамін C"), ("вітаміном c", "вітамін C"), ("пантенол", "пантенол"), ("керамід", "кераміди"), ("пептид", "пептиди"),
        ("азелаїн", "азелаїнова кислота"), ("азелоглі", "азелогліцин"), ("саліцил", "саліцилова кислота"), ("мигдал", "мигдальна кислота"),
        ("екзосом", "екзосоми"), ("pdrn", "PDRN"), ("полінуклеотид", "полінуклеотиди"), ("пребіот", "пребіотики"), ("сквалан", "сквалан"),
        ("центел", "центела азіатська"), ("бакучіол", "бакучіол"))


def g_details(name: str, desc: str, ptype: str = "") -> tuple[list[tuple[str, str, str]], list[str]]:
    """Атрибути для Google (product_detail) і короткі переваги (product_highlight) — з назви, категорії й опису.
    Лише те, що прямо згадано в тексті картки; нічого не вигадуємо."""
    t = f"{name} {ptype} {desc}".lower().replace("’", "'").replace("ʼ", "'")
    def found(pairs, limit):
        out = []
        for k, v in pairs:
            if k in t and v not in out:
                out.append(v)
        return out[:limit]
    det = []
    for sec, attr, vals in (("Догляд", "Тип шкіри", found(_SKIN, 4)), ("Догляд", "Призначення", found(_GOAL, 4)),
                            ("Склад", "Активні компоненти", found(_ACT, 5))):
        if vals:
            det.append((sec, attr, ", ".join(vals)))
    if ptype:
        det.append(("Загальне", "Категорія", ptype.split(" > ")[-1]))
    hl = []
    for part in re.split(r"\s*[•●▪]\s*|\n", desc or ""):
        part = part.strip(" .;")
        if 15 <= len(part) <= 150 and part[:1].isupper() and part.upper() != part:
            hl.append(part)
        if len(hl) >= 6:
            break
    return det, hl


def items(data: dict, root: str, notice_brands: set[str] | None = None) -> list[dict]:
    """Плоский список варіантів для фідів (лише видимі на сайті)."""
    cats = _cats_of(data)
    out = []
    for it in data["items"]:
        ptype = _ptype(cats.get(it["slug"], []), data)
        desc = g_desc(_strip(" ".join(s["html"] for s in it.get("sections") or []) or it.get("desc") or ""), it["name"], it["brand"], ptype)
        imgs = [f"{root}/img/{f}?w=1200" for f in it["imgs"][:10]]
        for v in it["variants"]:
            title = v["name"] if len(it["variants"]) == 1 or not v.get("label") else f"{it['name']} {v['label']}"
            title = g_title(title, it["brand"], v.get("label") or "")
            out.append({"id": str(v["id"]), "group": str(it["id"]) if len(it["variants"]) > 1 else "", "title": title[:150], "desc": desc,
                        "link": f"{root}/shop/{it['slug']}/", "imgs": imgs, "brand": it["brand"] or "MOLODIST", "sku": v.get("sku") or "", "gtin": v.get("gtin") or "",
                        "price": float(v["old"] or v["price"]), "sale": float(v["price"]) if v.get("old") else 0.0, "stock": bool(v["stock"]),
                        "ptype": ptype, "det": g_details(it["name"], desc, ptype), "cat": cats.get(it["slug"], [None])[0], "consult": bool(notice_brands and it["brand"] in notice_brands)})
    return out


def google(data: dict, root: str, title: str = "MOLODIST", notice_brands: set[str] | None = None) -> str:
    """RSS 2.0 з простором імен g: — формат, який приймають Google Merchant Center і Meta Commerce Manager."""
    rows = []
    for i in items(data, root, notice_brands):
        if not i["imgs"]:
            continue                                                         # без фото Merchant Center товар відхиляє
        parts = [f"<g:id>{x(i['id'])}</g:id>", f"<title>{x(i['title'])}</title>", f"<description>{x(i['desc'])}</description>",
                 f"<link>{x(i['link'])}</link>", f"<g:image_link>{x(i['imgs'][0])}</g:image_link>"]
        parts += [f"<g:additional_image_link>{x(u)}</g:additional_image_link>" for u in i["imgs"][1:10]]
        parts += [f"<g:availability>{'in_stock' if i['stock'] else 'out_of_stock'}</g:availability>",
                  f"<g:price>{i['price']:.2f} UAH</g:price>", "<g:condition>new</g:condition>", f"<g:brand>{x(i['brand'])}</g:brand>",
                  "<g:google_product_category>567</g:google_product_category>"]   # Health & Beauty > Personal Care > Cosmetics > Skin Care
        if i["sale"]:
            parts.append(f"<g:sale_price>{i['sale']:.2f} UAH</g:sale_price>")
        if i["gtin"]:
            parts.append(f"<g:gtin>{i['gtin']}</g:gtin>")
        if i["sku"]:
            parts.append(f"<g:mpn>{x(i['sku'])}</g:mpn>")
        parts.append("<g:identifier_exists>no</g:identifier_exists>" if not (i["sku"] or i["gtin"]) else "")
        if i["group"]:
            parts.append(f"<g:item_group_id>{i['group']}</g:item_group_id>")
        if i["ptype"]:
            parts.append(f"<g:product_type>{x(i['ptype'])}</g:product_type>")
        det, hl = i.get("det") or ([], [])
        parts += [f"<g:product_detail><g:section_name>{x(a)}</g:section_name><g:attribute_name>{x(b)}</g:attribute_name>"
                  f"<g:attribute_value>{x(c)}</g:attribute_value></g:product_detail>" for a, b, c in det]
        parts += [f"<g:product_highlight>{x(h)}</g:product_highlight>" for h in hl]
        if i["consult"]:
            parts.append("<g:custom_label_0>consultation</g:custom_label_0>")
        if i["sale"]:
            parts.append("<g:custom_label_1>sale</g:custom_label_1>")
        rows.append("<item>" + "".join(p for p in parts if p) + "</item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>'
            f"<title>{x(title)}</title><link>{x(root)}/</link><description>Професійна косметика MOLODIST</description>"
            + "".join(rows) + "</channel></rss>")


def hotline(data: dict, root: str, firm_id: str, firm_name: str = "MOLODIST") -> str:
    """XML-прайс у форматі Hotline.ua (price → categories → items)."""
    cats = {}
    for c in data["cats"]:
        if c["path"].startswith("/shop/brendi/") or c["path"] in ("/shop/sale/", "/shop/blog/") or not c.get("n"):
            continue
        cats[c["path"]] = c
    ids = {p: n + 1 for n, p in enumerate(sorted(cats))}
    cx = []
    for p, c in sorted(cats.items()):
        par = ids.get(c.get("parent") or "")
        cx.append(f"<category><id>{ids[p]}</id>" + (f"<parentId>{par}</parentId>" if par else "") + f"<name>{x(c['name'])}</name></category>")
    ix = []
    for i in items(data, root):
        if not i["imgs"] or not i["cat"] or i["cat"]["path"] not in ids:
            continue
        ix.append("<item>" + f"<id>{i['id']}</id><categoryId>{ids[i['cat']['path']]}</categoryId>"
                  + (f"<code>{x(i['sku'])}</code>" if i["sku"] else "") + (f"<barcode>{i['gtin']}</barcode>" if i["gtin"] else "") + f"<vendor>{x(i['brand'])}</vendor><name>{x(i['title'])}</name>"
                  + f"<description>{x(i['desc'][:2500])}</description><url>{x(i['link'])}</url><image>{x(i['imgs'][0])}</image>"
                  + f"<priceRUAH>{(i['sale'] or i['price']):.0f}</priceRUAH>" + (f"<oldprice>{i['price']:.0f}</oldprice>" if i["sale"] else "")
                  + f"<stock>{'В наявності' if i['stock'] else 'Немає в наявності'}</stock><guarantee type=\"shop\">14</guarantee></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<price>' + f"<date>{datetime.now():%Y-%m-%d %H:%M}</date>"
            + f"<firmName>{x(firm_name)}</firmName><firmId>{x(firm_id or '')}</firmId><rate>1</rate>"
            + "<categories>" + "".join(cx) + "</categories><items>" + "".join(ix) + "</items></price>")


def llms(data: dict, root: str, c: dict, site: dict, full: bool = False) -> str:
    """llms.txt (https://llmstxt.org): коротко про магазин, ключові сторінки, бренди, категорії; повна версія — з усіма товарами й цінами."""
    brands = sorted({it["brand"] for it in data["items"] if it["brand"]})
    about = (c.get("ai_about") or "").strip() or (
        "MOLODIST — інтернет-магазин професійної косметики при медичному просторі естетики та профілактики MOLODIST у Києві. "
        "Продаємо оригінальну космецевтику від офіційних постачальників, яку лікарі MOLODIST призначають для домашнього догляду. "
        "Доставка Новою поштою по всій Україні, безкоштовно від 3 000 ₴; самовивіз у Києві.")
    L = ["# MOLODIST — магазин професійної косметики", "", "\n".join(f"> {ln}" if ln.strip() else ">" for ln in about.splitlines()), ""]
    L += ["## Основне", f"- Каталог: {root}/shop/", f"- Бренди: {root}/shop/brendi/", f"- Акції: {root}/shop/sale/",
          f"- Доставка й оплата: {root}/for-customers/dostavka-i-oplata/", f"- Повернення: {root}/for-customers/povernennya-tovaru/",
          f"- Про нас: {root}/for-customers/pro-nas/", f"- Контакти: {root}/contacts/", f"- Блог: {root}/blog/", ""]
    L += ["## Контакти", f"- Телефон: {site.get('phone_text') or ''}", f"- Адреса: {site.get('address') or ''}", f"- Графік: {site.get('hours') or ''}",
          f"- Telegram: {site.get('telegram') or ''}", f"- Instagram: {site.get('instagram') or ''}", ""]
    L += ["## Оплата й доставка", "- Онлайн карткою (Visa, Mastercard, Apple Pay, Google Pay) через monobank; Покупка Частинами monobank.",
          "- Нова пошта: відділення, поштомат, курʼєр. Відправка в день замовлення до 18:00. Накладеного платежу немає.",
          "- Програма лояльності MOLODIST Coins: бали за реєстрацію, запрошення друзів і покупки.", ""]
    L += ["## Бренди"]
    for b in brands:
        seg = next((c2["path"] for c2 in data["cats"] if c2["path"].startswith("/shop/brendi/") and c2["name"].lower() == b.lower()), None)
        L.append(f"- [{b}]({root}{seg})" if seg else f"- {b}")
    L += ["", "## Категорії"]
    for c2 in data["cats"]:
        if c2.get("n") and not c2["path"].startswith("/shop/brendi/") and c2["path"] not in ("/shop/blog/",) and c2["depth"] <= 2:
            L.append(f"- [{c2['name']}]({root}{c2['path']})")
    if full:
        L += ["", "## Товари (назва — бренд — ціна, грн — наявність)"]
        for it in sorted(data["items"], key=lambda i: (i["brand"] or "", i["name"])):
            pr = f"{it['price']:.0f}" + (f" (було {it['old']:.0f})" if it.get("old") else "")
            L.append(f"- [{it['name']}]({root}/shop/{it['slug']}/) — {it['brand'] or '—'} — {pr} — {'є' if it['stock'] else 'немає'}")
    else:
        L += ["", "## Optional", f"- [Повний перелік товарів із цінами]({root}/llms-full.txt)", f"- [Фід товарів (Google Merchant XML)]({root}/feeds/google.xml)"]
    return "\n".join(L) + "\n"
