"""Покинуті кошики: кошик покупця, що увійшов у кабінет, зберігається на сервері.

Не оформив за N годин → видно в центрі магазину (покупець, товари, сума, скільки висить), задача адміністраторам,
нагадування покупцю (бот турботи → SMS), дзвінок. Оформив замовлення — кошик закривається сам.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Patient, ShopAccount, ShopCart, Task

STATUS = {"open": "Не оформлено", "ordered": "Оформлено замовлення", "done": "Опрацьовано", "empty": "Очищено"}
SMS_TEXT = "Ваш кошик збережено. Оформити: {link}"   # 1 SMS (≤ 70 символів); MOLODIST — в альфа-імені
DEFAULT_TEXT = "{name}, ви залишили в кошику MOLODIST: {items}. Кошик збережено — оформити можна тут: {link}. Питання? Напишіть нам."


def save(db: Session, pid: int, items: list, data: dict) -> ShopCart:
    from . import shop_front as sf
    clean = []
    for i in items or []:
        try:
            q = max(1, min(99, int(i.get("qty") or 1)))
            clean.append({"id": int(i.get("id")), "qty": q})
        except (TypeError, ValueError, AttributeError):
            continue
    clean = clean[:40]
    cart = db.scalar(select(ShopCart).where(ShopCart.patient_id == pid))
    if not cart:
        cart = ShopCart(patient_id=pid, status="empty")
        db.add(cart)
    if [(x["id"], x["qty"]) for x in cart.items or []] == [(x["id"], x["qty"]) for x in clean] and cart.status != "empty":
        return cart
    total = 0.0
    if clean:
        try:
            total = float(sf.price_cart(db, clean).get("total") or 0)
        except Exception:  # noqa: BLE001
            total = 0.0
    if clean and cart.status != "open":
        cart.task_id, cart.reminded_at = None, None
    cart.items, cart.total, cart.updated_at = clean, total, datetime.utcnow()
    cart.status = "open" if clean else "empty"
    db.flush()
    return cart


def ordered(db: Session, pid: int | None) -> None:
    if not pid:
        return
    cart = db.scalar(select(ShopCart).where(ShopCart.patient_id == pid))
    if cart and cart.status == "open":
        cart.status, cart.items, cart.total = "ordered", [], 0
        _close_task(db, cart, "Покупець оформив замовлення")
        db.flush()


def _close_task(db: Session, cart: ShopCart, result: str) -> None:
    t = db.get(Task, cart.task_id) if cart.task_id else None
    if t and t.status == "open":
        t.status, t.result, t.done_at = "done", result, datetime.utcnow()


def age_text(dt: datetime | None) -> str:
    if not dt:
        return "—"
    m = int((datetime.utcnow() - dt).total_seconds() // 60)
    if m < 60:
        return f"{m} хв"
    if m < 48 * 60:
        return f"{m // 60} год"
    return f"{m // 1440} дн"


def rows(db: Session, data: dict, status: str = "open", min_hours: float = 0) -> list[dict]:
    q = select(ShopCart).order_by(ShopCart.updated_at.desc()).limit(300)
    if status in STATUS:
        q = q.where(ShopCart.status == status)
    carts = list(db.scalars(q))
    pts = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_([c.patient_id for c in carts] or [0])))}
    accs = {a.patient_id: a for a in db.scalars(select(ShopAccount).where(ShopAccount.patient_id.in_([c.patient_id for c in carts] or [0])))}
    by_id = {v["id"]: (it, v) for it in data["items"] for v in it["variants"]}
    out = []
    edge = datetime.utcnow() - timedelta(hours=min_hours)
    for c in carts:
        if status == "open" and c.updated_at and c.updated_at > edge:
            continue
        items = []
        for i in c.items or []:
            it, v = by_id.get(i["id"], (None, None))
            items.append({"name": (v or {}).get("name") or f"товар #{i['id']}", "qty": i["qty"], "price": (v or {}).get("price") or 0,
                          "img": it["imgs"][0] if it and it.get("imgs") else "", "slug": it["slug"] if it else None, "stock": bool(v and v.get("stock"))})
        p = pts.get(c.patient_id)
        a = accs.get(c.patient_id)
        nm = " ".join(x for x in ((a.last_name if a else ""), (a.first_name if a else "")) if x) or (p.full_name if p else "")
        out.append({"cart": c, "patient": p, "name": nm, "phone": getattr(p, "phone", "") if p else "", "lines": items, "age": age_text(c.updated_at),
                    "task": db.get(Task, c.task_id) if c.task_id else None})
    return out


def _items_text(c: ShopCart, data: dict) -> str:
    by_id = {v["id"]: v for it in data["items"] for v in it["variants"]}
    names = [by_id[i["id"]]["name"] for i in c.items or [] if i["id"] in by_id]
    if not names:
        return "товари"
    t = names[0][:50] + (f" та ще {len(names) - 1}" if len(names) > 1 else "")
    return t


def message(db: Session, cfg: dict, c: ShopCart, data: dict, root: str) -> str:
    a = db.scalar(select(ShopAccount).where(ShopAccount.patient_id == c.patient_id))
    nm = (a.first_name if a and a.first_name else "") or "Вітаємо"
    tpl = (cfg.get("cart_text") or "").strip() or DEFAULT_TEXT
    return tpl.replace("{name}", nm).replace("{items}", _items_text(c, data)).replace("{sum}", f"{int(c.total or 0)} ₴").replace("{link}", f"{root}/checkout/")[:480]


def email_body(db: Session, c: ShopCart | None, data: dict, root: str, name: str = "") -> tuple[str, str, str]:
    """Лист про покинутий кошик: (тема, текст, html). Без кошика — зразок з 2 товарів каталогу (тестовий лист)."""
    import html as _h
    from ..integrations import brevo
    from . import site_pages
    ec, sc_ = brevo.cfg(db), site_pages.cfg(db)
    by_id = {v["id"]: (it, v) for it in data.get("items") or [] for v in it["variants"]}
    if c:
        a = db.scalar(select(ShopAccount).where(ShopAccount.patient_id == c.patient_id))
        name = name or (a.first_name if a and a.first_name else "")
        pick = [(by_id[i["id"]], i["qty"]) for i in c.items or [] if i["id"] in by_id]
    else:
        pick = [((it, v), 1) for it in data.get("items") or [] for v in it["variants"][:1] if v.get("stock") and it.get("imgs")][:2]
    lines, rows, total = [], [], 0.0
    link_ = f"{root}/checkout/"
    for (it, v), q in pick[:8]:
        price, old = float(v.get("price") or 0), float(v.get("old") or 0)
        total += price * q
        lines.append(f"• {v['name']} × {q} — {int(price * q)} ₴")
        img = f"{root}/img/{it['imgs'][0]}?w=300&bg=ffffff" if it.get("imgs") else ""
        url = f"{root}/shop/{it['slug']}/" if it.get('slug') else link_
        pr = (f'<s style="color:#9a93a6;font-weight:400">{int(old)} ₴</s> ' if old > price else "") + f"{int(price)} ₴"
        rows.append(
            '<tr><td style="padding:12px 0;border-bottom:1px solid #EFEAE4;width:92px;vertical-align:top">'
            + (f'<a href="{url}"><img src="{img}" width="80" height="80" alt="" style="display:block;border-radius:12px;background:#fff;border:1px solid #EFEAE4;object-fit:contain"></a>' if img else "")
            + f'</td><td style="padding:12px 0 12px 14px;border-bottom:1px solid #EFEAE4;vertical-align:top">'
            f'<a href="{url}" style="color:#2A2430;text-decoration:none;font-weight:600;font-size:14px;line-height:1.4">{_h.escape(v["name"])}</a>'
            f'<div style="color:#8a8494;font-size:13px;margin-top:4px">Кількість: {q}</div></td>'
            f'<td style="padding:12px 0;border-bottom:1px solid #EFEAE4;text-align:right;vertical-align:top;white-space:nowrap;font-weight:700;font-size:14px">{pr}</td></tr>')
    if c and c.total:
        total = float(c.total)
    coins = int(total * 0.03) if total else 0
    hi = f"{name}, ваш кошик чекає на вас" if name else "Ваш кошик чекає на вас"
    link = f"{root}/checkout/"
    phone, phone_t = sc_.get("phone") or "+380933756437", sc_.get("phone_text") or "+38 (093) 375 64 37"
    tg, ig = sc_.get("telegram") or "", sc_.get("instagram") or ""
    text = (f"{hi}.\n\nМи зберегли товари, які ви обрали:\n" + "\n".join(lines) + f"\n\nРазом: {int(total)} ₴"
            + (f"\n+{coins} MOLODIST Coins на ваш рахунок після покупки." if coins else "")
            + f"\n\nОформити замовлення: {link}\n\nНе впевнені, що засіб вам підходить? Дайте відповідь на цей лист або зателефонуйте {phone_t} — "
              "лікар чи адміністратор MOLODIST підкаже.\n\nMOLODIST · медичний простір естетики та профілактики · Київ")
    benefit = ('<td style="width:33%;padding:0 6px;text-align:center;vertical-align:top;font-size:12px;color:#5f586a;line-height:1.45">'
               '<div style="font-size:20px;margin-bottom:6px">{0}</div>{1}</td>')
    body = (
        '<div style="background:#F7F4F0;padding:28px 10px;font-family:\'Open Sans\',Arial,sans-serif;color:#2A2430">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#fff;border-radius:18px;overflow:hidden">'
        '<tr><td style="padding:26px 26px 18px;text-align:center;border-bottom:1px solid #EFEAE4">'
        f'<a href="{root}"><img src="{root}/static/site/brand/wordmark-graphite-600.png" width="190" alt="MOLODIST" style="display:inline-block;width:190px;height:auto;border:0"></a>'
        '<div style="font-size:10px;letter-spacing:.26em;color:#8a8494;margin-top:8px">МЕДИЧНИЙ ПРОСТІР</div></td></tr>'
        '<tr><td style="padding:28px 26px 6px">'
        f'<div style="font:700 21px Montserrat,Arial,sans-serif;line-height:1.3;margin-bottom:8px">{_h.escape(hi)} 🤍</div>'
        '<div style="font-size:14px;line-height:1.6;color:#5f586a">Ми зберегли товари, які ви обрали. Щоб оформити замовлення, достатньо одного кліку — '
        'дані доставки підставляться самі.</div></td></tr>'
        '<tr><td style="padding:8px 26px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + "".join(rows) + '</table></td></tr>'
        '<tr><td style="padding:14px 26px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td style="font-size:15px;font-weight:600">Разом</td>'
        f'<td style="text-align:right;font:700 20px Montserrat,Arial,sans-serif">{int(total)} ₴</td></tr></table>'
        + (f'<div style="margin-top:10px;background:#F1EEF6;border-radius:10px;padding:10px 12px;font-size:13px;color:#3B3050">'
           f'✦ +{coins} MOLODIST Coins на ваш рахунок після покупки</div>' if coins else "")
        + '</td></tr>'
        f'<tr><td style="padding:22px 26px 6px;text-align:center"><a href="{link}" style="display:inline-block;background:#5C4B82;color:#fff;text-decoration:none;'
        'font-weight:700;font-size:15px;padding:14px 34px;border-radius:12px">Оформити замовлення</a></td></tr>'
        '<tr><td style="padding:22px 20px 6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        + benefit.format("✓", "Оригінальна професійна косметика від офіційних постачальників")
        + benefit.format("⚕", "Засоби, які призначають лікарі MOLODIST")
        + benefit.format("📦", "Доставка Новою поштою по всій Україні")
        + '</tr></table></td></tr>'
        '<tr><td style="padding:18px 26px 4px"><div style="border:1px solid #E9E5DF;border-radius:14px;padding:14px 16px;font-size:13.5px;line-height:1.6">'
        '<b>Не впевнені, що засіб вам підходить?</b><br>Дайте відповідь на цей лист або напишіть нам — лікар чи адміністратор MOLODIST підкаже, '
        'що обрати під ваш тип шкіри.<div style="margin-top:8px">'
        f'<a href="tel:{phone}" style="color:#5C4B82;font-weight:600;text-decoration:none">{_h.escape(phone_t)}</a>'
        + (f' &nbsp;·&nbsp; <a href="{tg}" style="color:#5C4B82;font-weight:600;text-decoration:none">Telegram</a>' if tg else "")
        + (f' &nbsp;·&nbsp; <a href="{ig}" style="color:#5C4B82;font-weight:600;text-decoration:none">Instagram</a>' if ig else "")
        + '</div></div></td></tr>'
        '<tr><td style="padding:20px 26px 24px;font-size:11.5px;line-height:1.6;color:#9a93a6;text-align:center">'
        f'MOLODIST · медичний простір естетики та профілактики<br>{_h.escape(sc_.get("address") or "м. Київ, вул. Князя Романа Мстиславича, 28А")}'
        f'<br><a href="{root}" style="color:#9a93a6">molodist.store</a><br>'
        'Ви отримали цей лист, бо залишили товари в кошику на molodist.store.</td></tr>'
        '</table></div>')
    return ec.get("cart_subject") or brevo.DEFAULT["cart_subject"], text, body


def remind(db: Session, cfg: dict, cart_id: int, data: dict, root: str, who: str = "") -> str:
    from . import shop_cabinet as sc
    c = db.get(ShopCart, cart_id)
    if not c or c.status != "open" or not c.items:
        raise ValueError("Кошик уже не активний.")
    p = db.get(Patient, c.patient_id)
    from ..integrations import brevo
    a = db.scalar(select(ShopAccount).where(ShopAccount.patient_id == c.patient_id))
    mail = (a.email if a and a.email else "") or (getattr(p, "email", "") or "")
    ch = ""
    if mail and brevo.ready(db) and brevo.cfg(db).get("cart_on"):          # email (Brevo) — основний канал для кошика
        subj, text, body = email_body(db, c, data, root)
        try:
            brevo.send(db, mail, subj, text, body)
            ch = "email"
        except brevo.EmailError:
            ch = ""
    if not ch:
        if not p or not getattr(p, "phone", None):
            raise ValueError("У картці немає телефону.")
        short = SMS_TEXT.replace("{link}", f"{root}/checkout/".replace("https://", "").replace("http://", ""))
        ch = sc._deliver(db, sc._phone(p.phone), message(db, cfg, c, data, root), "🛒 ", sms=short)
    c.reminded_at = datetime.utcnow()
    c.note = ((c.note or "") + f"\n{datetime.now():%d.%m %H:%M} нагадування ({ch}){' — ' + who if who else ''}").strip()[-2000:]
    db.flush()
    return ch


def mark_done(db: Session, cart_id: int, note: str, who: str = "") -> None:
    c = db.get(ShopCart, cart_id)
    if not c:
        return
    c.status = "done"
    c.note = ((c.note or "") + f"\n{datetime.now():%d.%m %H:%M} опрацьовано{' — ' + who if who else ''}: {note}".rstrip(": ")).strip()[-2000:]
    _close_task(db, c, note or "Опрацьовано")
    db.flush()


def job(db: Session, cfg: dict, data: dict, root: str) -> dict:
    """Щогодини: задача адміністраторам через N годин, авто-нагадування покупцю через M годин (якщо ввімкнено)."""
    rep = {"tasks": 0, "reminded": 0}
    hours = float(cfg.get("cart_task_hours") or 0)
    auto = float(cfg.get("cart_auto_hours") or 0)
    now = datetime.utcnow()
    carts = db.scalars(select(ShopCart).where(ShopCart.status == "open")).all()
    from ..integrations import messaging as m
    quiet = m.is_quiet_time()
    for c in carts:
        if not c.items or not c.updated_at:
            continue
        age = (now - c.updated_at).total_seconds() / 3600
        if hours and age >= hours and not c.task_id:
            p = db.get(Patient, c.patient_id)
            t = Task(title=f"Покинутий кошик: {int(c.total or 0)} ₴ — {(p.full_name if p else '')[:80]}",
                     description="Покупець поклав товари в кошик і не оформив. Звʼязатися: уточнити, чи є питання, допомогти з вибором / доставкою. "
                                 "Кошик — у «Магазин → Покинуті кошики».",
                     kind="shop_cart", source_key=f"shop_cart:{c.id}:{c.updated_at:%Y%m%d%H%M}", patient_id=c.patient_id, assignee_role="admin",
                     due_date=date.today(), priority="high" if (c.total or 0) >= 3000 else "normal", created_at=now)
            db.add(t)
            db.flush()
            c.task_id = t.id
            rep["tasks"] += 1
            if cfg.get("tg_cart"):
                try:
                    from . import shop_front as sf
                    for ch in sf.order_chats(db):
                        sf._tg_send(ch, f"🛒 Покинутий кошик: {int(c.total or 0)} ₴ — {(p.full_name if p else '')[:60]}. Задача адміністраторам створена.")
                except Exception:  # noqa: BLE001
                    pass
        if cfg.get("cart_auto_on") and auto and age >= auto and not c.reminded_at and not quiet and age < 24 * 7:
            try:
                remind(db, cfg, c.id, data, root, "авто")
                rep["reminded"] += 1
            except Exception:  # noqa: BLE001
                pass
        db.commit()
    return rep
