"""Магазин: онлайн-підбір догляду, нагадування «засіб закінчується», «Купити в 1 клік».

Підбір — правила за типом шкіри, головним запитом, віком і бюджетом: для кожного кроку догляду (очищення, тонізація,
сироватка, зволоження, SPF, вечірній активний засіб) обирається товар у наявності з потрібної категорії, який найкраще
відповідає запиту (ключові слова в назві й описі). Товари з порадою лікаря MOLODIST мають пріоритет.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Patient, SiteOrder

# ---------------- підбір догляду ----------------
QUIZ = [
    {"key": "skin", "q": "Який у вас тип шкіри?", "opts": [("dry", "Суха, буває стягнутість"), ("oily", "Жирна, блищить протягом дня"),
                                                         ("combo", "Комбінована: жирна Т-зона"), ("normal", "Нормальна, без особливих проблем"),
                                                         ("sens", "Чутлива, легко червоніє")]},
    {"key": "goal", "q": "Що турбує найбільше?", "opts": [("acne", "Висипання, акне"), ("pigment", "Пігментація, нерівний тон"),
                                                         ("age", "Зморшки, втрата пружності"), ("red", "Почервоніння, судинки"),
                                                         ("dehydr", "Сухість, зневоднення"), ("pores", "Розширені пори, текстура")]},
    {"key": "age", "q": "Ваш вік", "opts": [("20", "До 25"), ("30", "25–35"), ("40", "35–45"), ("50", "45+")]},
    {"key": "level", "q": "Який догляд вам ближчий?", "opts": [("min", "Мінімум кроків — 3 засоби"), ("full", "Повний ритуал ранок і вечір")]},
    {"key": "budget", "q": "Бюджет на один засіб", "opts": [("1500", "до 1 500 ₴"), ("3000", "до 3 000 ₴"), ("any", "Не важливо — головне результат")]},
]
KW = {
    "acne": ["акне", "acne", "blemish", "clear", "саліцил", "салицил", "salicyl", "bha", "азелаїн", "azel", "себо", "sebo", "проблемн", "висип"],
    "pigment": ["пігмент", "pigment", "вітамін c", "vitamin c", "вітаміном c", "c-tetra", "bright", "освітл", "tranex", "транекс", "азелаїн", "melan", "мелан", "тон"],
    "age": ["ретин", "retin", "пептид", "peptid", "anti-age", "антивік", "анти-ейдж", "ліфт", "lift", "колаген", "collagen", "age", "пружн", "зморш"],
    "red": ["розаце", "почервон", "redness", "купероз", "calm", "soothing", "заспок", "чутлив", "sensitive", "azelorosa", "судин", "rosa"],
    "dehydr": ["гіалур", "hyalur", "зволож", "hydra", "moist", "бар'єр", "барʼєр", "ceramid", "церамід", "живл", "nourish"],
    "pores": ["пор", "pore", "niacin", "ніацин", "bha", "кислот", "acid", "ексфол", "exfol"],
}
SKIN_KW = {"dry": ["живл", "крем", "cream", "rich", "бальзам", "олі"], "oily": ["гель", "gel", "флюїд", "fluid", "матув", "mat", "легк", "light"],
           "combo": ["гель", "gel", "флюїд", "fluid", "баланс", "balanc"], "sens": ["чутлив", "sensitive", "calm", "заспок", "soothing"], "normal": []}
# крок → підрядки адреси категорії (перший збіг) і ключові слова назви як запасний варіант
STEPS = [
    ("cleanse", "Очищення", "ранок і вечір", ["kosmetika-dlya-oblichchya", "ochyshchennya"], ["очищ", "умив", "гель для вмив", "пінка", "міцел", "cleans", "wash"]),
    ("tone", "Тонізація", "ранок і вечір", ["tonizatsiya"], ["тонік", "toner", "лосьйон для обличчя"]),
    ("serum", "Сироватка", "ранок", ["aktivni-sirovatki"], ["сироватк", "serum"]),
    ("cream", "Зволоження й живлення", "ранок і вечір", ["zvolozhennyazhivlennyaanti-eydzh"], ["крем", "cream", "емульс"]),
    ("spf", "Сонцезахист SPF", "ранок, щодня", ["/spf/"], ["spf", "сонцезах"]),
    ("night", "Вечірній активний засіб", "вечір, 2–4 рази на тиждень", ["aktivni-sirovatki", "sos-zasobi"], ["ретин", "retin", "кислот", "acid", "night", "нічн"]),
]
MIN_STEPS = ("cleanse", "serum", "spf")


def _text(it: dict) -> str:
    return (it["name"] + " " + " ".join(re.sub(r"<[^>]+>", " ", s.get("html") or "") for s in it.get("sections") or [])[:1500]).lower()


def pick(data: dict, ans: dict) -> dict:
    goal, skin = ans.get("goal") or "dehydr", ans.get("skin") or "normal"
    budget = {"1500": 1500, "3000": 3000}.get(str(ans.get("budget") or ""), 10 ** 9)
    steps = STEPS if ans.get("level") == "full" else [s for s in STEPS if s[0] in MIN_STEPS]
    if ans.get("age") in ("20",) and goal != "age":
        steps = [s for s in steps if s[0] != "night" or goal in ("acne", "pigment", "pores")]
    cats = data["cats"]
    used: set[str] = set()
    out = []
    for key, title, when, paths, words in steps:
        pool_slugs = set()
        for c in cats:
            if any(p in c["path"] for p in paths):
                pool_slugs |= set(c["slugs"])
        cands = [it for it in data["items"] if it["stock"] and it["slug"] not in used
                 and (it["slug"] in pool_slugs or any(w in it["name"].lower() for w in words))]
        if key == "spf":
            cands = [it for it in cands if "spf" in it["name"].lower() or "сонцезах" in it["name"].lower()] or cands
        elif key != "night":
            cands = [it for it in cands if "spf" not in it["name"].lower()] or cands
        best, best_s = None, -1e9
        for it in cands:
            t = _text(it)
            s = 3 * sum(1 for w in KW.get(goal, []) if w in t) + sum(1 for w in SKIN_KW.get(skin, []) if w in t)
            s += 2 * sum(1 for w in words if w in t)
            if it.get("tip"):
                s += 4
            if skin == "sens" and any(w in t for w in ("ретин", "retin", "кислот", "acid", "peel", "пілінг")):
                s -= 3
            if it["price"] > budget:
                s -= 100
            s += min(it.get("sold") or 0, 20) * 0.05
            if s > best_s:
                best, best_s = it, s
        if best and best_s > -50:
            used.add(best["slug"])
            out.append({"step": key, "title": title, "when": when, "item": best})
    total = sum(x["item"]["price"] for x in out)
    return {"steps": out, "total": total, "goal": dict(QUIZ[1]["opts"]).get(goal, ""), "skin": dict(QUIZ[0]["opts"]).get(skin, "")}


# ---------------- «Засіб закінчується» ----------------
DEFAULT_DAYS = {"spf": 45, "serum": 60, "cream": 60, "cleanse": 75, "tone": 75}
REORDER_TEXT = "{name}, ваш {product} скоро закінчиться. Повторити замовлення можна в один клік: {link} MOLODIST"


def days_for(it: dict, prod_days: int | None) -> int:
    if prod_days:
        return int(prod_days)
    n = it.get("name", "").lower()
    if "spf" in n or "сонцезах" in n:
        return DEFAULT_DAYS["spf"]
    if any(w in n for w in ("сироват", "serum")):
        return DEFAULT_DAYS["serum"]
    if any(w in n for w in ("очищ", "гель для вмив", "пінк", "міцел", "тонік", "toner", "cleans")):
        return DEFAULT_DAYS["cleanse"]
    if any(w in n for w in ("крем", "cream")):
        return DEFAULT_DAYS["cream"]
    return 0                                                             # маски, декоративна, аксесуари — не нагадуємо


def reorder_due(db: Session, data: dict, now: datetime | None = None, lead_days: int = 7) -> list[tuple[SiteOrder, list[dict]]]:
    from ..models import Product
    now = now or datetime.now()
    rows = db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.status.in_(("Відправлено", "Отримано", "Закрито")),
                                              SiteOrder.created_at >= now - timedelta(days=200))).all()
    pdays = {p.id: p.supply_days for p in db.scalars(select(Product).where(Product.supply_days.is_not(None)))}
    by_id = {v["id"]: v for it in data["items"] for v in it["variants"]}
    out = []
    for o in rows:
        sent = set((o.raw or {}).get("reorder_sent") or [])
        due = []
        for i in o.items or []:
            pid = i.get("product_id")
            if not pid or pid in sent or pid not in by_id or not by_id[pid]["stock"]:
                continue
            d = days_for(i, pdays.get(pid)) * max(1, int(i.get("qty") or 1))
            if d and o.created_at + timedelta(days=d - lead_days) <= now < o.created_at + timedelta(days=d + 30):
                due.append(i)
        if due:
            out.append((o, due))
    return out


def reorder_token(o: SiteOrder) -> str:
    from . import shop_reviews as rv
    return rv.token(o)


def send_reorders(db: Session, c: dict, data: dict, root: str) -> dict:
    rep = {"sent": 0}
    if not c.get("reorder_on"):
        return rep
    from ..integrations import messaging as m
    if m.is_quiet_time():
        return rep
    from . import shop_cabinet as sc, shop_reviews as rv
    seen_phone: set[str] = set()
    for o, items in reorder_due(db, data, lead_days=int(c.get("reorder_lead") or 7)):
        if not o.phone or o.phone in seen_phone:
            continue
        # не частіше раза на 14 днів одному покупцю
        recent = db.scalars(select(SiteOrder).where(SiteOrder.phone == o.phone, SiteOrder.source == "crm")).all()
        last = max((datetime.fromisoformat(x.raw["reorder_at"]) for x in recent if (x.raw or {}).get("reorder_at")), default=None)
        if last and datetime.now() - last < timedelta(days=14):
            continue
        name = rv.first_name(o, db)
        prod = items[0]["name"][:50] + (" та інші засоби" if len(items) > 1 else "")
        text = ((c.get("reorder_text") or "").strip() or REORDER_TEXT).replace("{name}", name).replace("{product}", prod) \
            .replace("{link}", f"{root}/reorder/{reorder_token(o)}/")[:480]
        try:
            ch = sc._deliver(db, sc._phone(o.phone), text, "🔁 ")
        except Exception:  # noqa: BLE001
            continue
        raw = dict(o.raw or {})
        raw["reorder_sent"] = list(set(raw.get("reorder_sent") or []) | {i["product_id"] for i in items})
        raw["reorder_at"] = datetime.now().isoformat(timespec="minutes")
        raw["log"] = (raw.get("log") or []) + [{"at": raw["reorder_at"], "by": "", "text": f"Нагадування «засіб закінчується» ({ch}): {prod}"}]
        o.raw = raw
        db.commit()
        seen_phone.add(o.phone)
        rep["sent"] += 1
        if rep["sent"] >= 40:
            break
    return rep


# ---------------- Купити в 1 клік ----------------
def quick_order(db: Session, product_id: int, qty: int, raw_phone: str, name: str, utm: dict | None = None, patient_id: int | None = None) -> dict:
    from ..integrations.messaging import normalize_phone
    from . import shop_front as sf, shop_orders as so
    phone = normalize_phone(raw_phone or "")
    if not phone:
        raise sf.ShopError("Перевірте номер телефону: +380 і 9 цифр.")
    cart = sf.price_cart(db, [{"id": product_id, "qty": max(1, min(20, int(qty or 1)))}])
    if not cart["items"]:
        raise sf.ShopError("Товар закінчився. Залиште номер у «Повідомити, коли зʼявиться».")
    name = " ".join(str(name or "").split())[:120] or "Покупець (1 клік)"
    no = f"M{datetime.now():%y%m%d}-{secrets.randbelow(9000) + 1000}"
    o = SiteOrder(source="crm", ext_id=no, number=no, created_at=datetime.now(), status="Нове", paid=False, total=cart["total"], total_full=cart["total"],
                  delivery_price=0, delivery="Уточнити з покупцем", payment="Уточнити з покупцем", name=name, phone=phone,
                  comment="Купівля в 1 клік: передзвонити, уточнити доставку й оплату.",
                  items=[{"product_id": i["id"], "name": i["name"], "sku": i["sku"], "qty": i["qty"], "price": i["price"], "total": i["total"], "sale": i.get("sale", False)}
                         for i in cart["items"]],
                  raw={"utm": {k: str(v)[:150] for k, v in (utm or {}).items() if k.startswith("utm_") or k in ("gclid", "fbclid", "landing")},
                       "one_click": True}, synced_at=datetime.now())
    db.add(o)
    pat = db.get(Patient, patient_id) if patient_id else so._patient(db, name, phone, None, {})
    if pat:
        o.patient_id = pat.id
    db.commit()
    try:
        sf.notify(db, o)
        db.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "number": no, "total": cart["total"], "id": o.id}
