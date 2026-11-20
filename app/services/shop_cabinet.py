"""Кабінет покупця інтернет-магазину MOLODIST: вхід кодом на телефон (Telegram-бот турботи → SMS), «запамʼятати мене»,
контактні дані, мої замовлення, адреси доставки (підставляються в оформлення), вішліст із посиланням «поділитися»,
запроси друга (реферальний код), MOLODIST коїни (у розробці: нараховуються, витрачати ще не можна).

Кабінет привʼязаний до картки пацієнта за телефоном. Медичних даних у кабінеті магазину немає."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CoinTxn, Patient, Product, ShopAccount, ShopCode, ShopCoin, ShopNotify, ShopSession, SiteOrder

COOKIE = "mshop_s"
REF_COOKIE = "mshop_ref"
CODE_TTL = 5 * 60
DAYS_REMEMBER, DAYS_SHORT = 180, 1
COIN_KINDS = {"signup": "За реєстрацію", "ref_inviter": "За запрошеного друга", "ref_friend": "Бонус за запрошенням друга"}
TABS = [("profile", "Контактні дані"), ("orders", "Мої замовлення"), ("care", "Рекомендації лікаря"), ("loyalty", "MOLODIST Coins"), ("reviews", "Мої відгуки"),
        ("invite", "Запроси друга"), ("wish", "Вішліст"), ("gifts", "Подарунки від друзів і партнерів"), ("addr", "Адреси доставки")]


class CabError(ValueError):
    pass


def _h(*parts: str) -> str:
    return hmac.new(settings.secret_key.encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()


def mask(phone: str) -> str:
    return f"+{phone[:5]}•••{phone[-3:]}" if len(phone) >= 10 else phone


def _phone(raw: str) -> str:
    from ..integrations.messaging import normalize_phone
    p = normalize_phone(str(raw or ""))
    if not p:
        raise CabError("Перевірте номер телефону: +380 і 9 цифр.")
    return p


def sms_text(c: dict, code: str) -> str:
    t = (c.get("cab_sms") or "").strip() or "{code} — код входу в магазин MOLODIST"
    return (t if "{code}" in t else t + " {code}").replace("{code}", code)[:300]


def back_sms(site_root: str, slug: str) -> str:
    """SMS «знову в наявності» — 1 сегмент (≤70)."""
    url = f"{site_root}/shop/{slug}/".split("://", 1)[-1]
    return f"Знову в наявності: {url}" if len(url) <= 51 else url


def _deliver(db: Session, phone: str, text: str, icon: str = "🔐 ", sms: str = "", sms_ok: bool = True) -> str:
    """Код: спершу в Telegram (якщо пацієнт підключив бот турботи), інакше SMS. Повертає канал або кидає CabError."""
    from ..integrations import messaging as m
    from . import appeals as ap
    p = ap.find_patient(db, phone)
    if p:                                               # особистий Telegram акаунта клініки (пацієнт уже писав туди) — першим
        if m.send_tg_business(p.id, text, internal=True).ok:
            return "telegram"
    if p and p.telegram_chat_id and m.telegram_available():
        try:
            if m.send_telegram(p.telegram_chat_id, icon + text).ok:
                return "telegram"
        except Exception:  # noqa: BLE001 — не вийшло в Telegram → SMS
            pass
    if not sms_ok:                                      # SMS заборонено (платне) — лише Telegram
        return "none"
    r = m.send_turbosms(phone, sms or text, ["sms"], internal=True)
    if not r.ok:
        raise CabError("Не вдалося надіслати код. Спробуйте за хвилину або зателефонуйте нам.")
    return "sms"


def start(db: Session, c: dict, raw_phone: str, ip: str = "") -> dict:
    """Надіслати код входу. Обмеження: 1 код на хвилину й 4 на годину на номер, 8 на годину з однієї адреси, денний ліміт SMS."""
    if not c.get("cab_on", True):
        raise CabError("Вхід у кабінет тимчасово вимкнено.")
    phone = _phone(raw_phone)
    now = datetime.utcnow()
    hour = now - timedelta(hours=1)
    last = db.scalars(select(ShopCode).where(ShopCode.phone == phone).order_by(ShopCode.id.desc()).limit(1)).first()
    if last and last.created_at and (now - last.created_at).total_seconds() < 60:
        raise CabError("Код уже надіслано. Новий можна отримати за хвилину.")
    if (db.scalar(select(func.count(ShopCode.id)).where(ShopCode.phone == phone, ShopCode.created_at >= hour)) or 0) >= 4 or \
            (ip and (db.scalar(select(func.count(ShopCode.id)).where(ShopCode.ip == ip, ShopCode.created_at >= hour)) or 0) >= 8):
        raise CabError("Забагато спроб. Спробуйте за годину або зателефонуйте нам.")
    day = db.scalar(select(func.count(ShopCode.id)).where(ShopCode.channel == "sms", ShopCode.created_at >= now - timedelta(days=1))) or 0
    if day >= int(c.get("cab_sms_day") or 150):
        raise CabError("Вхід за кодом тимчасово недоступний. Зателефонуйте нам, будь ласка.")
    code = f"{secrets.randbelow(100000):05d}"
    ch = _deliver(db, phone, sms_text(c, code))
    db.add(ShopCode(phone=phone, code_hash=_h("code", phone, code), created_at=now, expires_at=now + timedelta(seconds=CODE_TTL), tries=0, used=False,
                    ip=ip[:64] or None, channel=ch))
    db.commit()
    return {"ok": True, "to": mask(phone), "channel": ch, "new": not _account_by_phone(db, phone)}


def _account_by_phone(db: Session, phone: str) -> ShopAccount | None:
    from . import appeals as ap
    p = ap.find_patient(db, phone)
    return db.scalars(select(ShopAccount).where(ShopAccount.patient_id == p.id)).first() if p else None


def _ref_code(db: Session) -> str:
    while True:
        code = "M" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if not db.scalars(select(ShopAccount.id).where(ShopAccount.ref_code == code)).first():
            return code


def coin(db: Session, pid: int, amount: int, kind: str, ref: str = "", days: int = 180, live: bool = False) -> None:
    """Нарахування в журнал MOLODIST Coins (services/coins.py)."""
    from . import coins as cn
    cn.add(db, pid, int(amount or 0), "welcome" if kind == "signup" else kind, f"cab:{ref or kind}", days=days, live=live)


def verify(db: Session, c: dict, raw_phone: str, code: str, name: str = "", remember: bool = True, ref: str = "", ua: str = "") -> tuple[str, int, ShopAccount]:
    """Перевірити код → (токен сесії, строк у днях, кабінет). Нового покупця реєструє: картка пацієнта + коїни."""
    phone = _phone(raw_phone)
    now = datetime.utcnow()
    row = db.scalars(select(ShopCode).where(ShopCode.phone == phone, ShopCode.used.is_(False)).order_by(ShopCode.id.desc()).limit(1)).first()
    if not row or not row.expires_at or row.expires_at < now:
        raise CabError("Код застарів. Отримайте новий.")
    if (row.tries or 0) >= 5:
        raise CabError("Забагато невдалих спроб. Отримайте новий код.")
    if not hmac.compare_digest(row.code_hash, _h("code", phone, re.sub(r"\D", "", str(code or "")))):
        row.tries = (row.tries or 0) + 1
        db.commit()
        raise CabError("Невірний код.")
    row.used = True
    from . import appeals as ap
    p = ap.find_patient(db, phone)
    name = " ".join(str(name or "").split())[:120]
    if not p:
        p = Patient(full_name=name or phone, phone=phone, source="Інтернет-магазин", notes="Картку створено при реєстрації в кабінеті інтернет-магазину")
        from .shop_orders import mark_shop
        mark_shop(p)
        db.add(p)
        db.flush()
    acc = db.scalars(select(ShopAccount).where(ShopAccount.patient_id == p.id)).first()
    if not acc:
        parts = (name or (p.full_name if p.full_name != phone else "")).split()
        acc = ShopAccount(patient_id=p.id, created_at=now, ref_code=_ref_code(db), wish_token=secrets.token_urlsafe(12), wishlist=[], addresses=[],
                          first_name=(parts[1] if len(parts) > 1 else (parts[0] if parts else ""))[:100] or None,
                          last_name=(parts[0] if len(parts) > 1 else "")[:100] or None, email=getattr(p, "email", None))
        db.add(acc)
        db.flush()
        coin(db, p.id, int(c.get("cab_coin_signup") or 0), "signup", days=int(c.get("coin_days") or 180), live=bool(c.get("coin_spend_on")))
        inv = db.scalars(select(ShopAccount).where(ShopAccount.ref_code == str(ref or "").strip().upper()[:20])).first() if ref else None
        if inv and inv.patient_id != p.id:
            acc.referred_by = inv.patient_id
            coin(db, p.id, int(c.get("cab_coin_ref") or 0), "ref_friend", f"from:{inv.patient_id}", int(c.get("coin_days") or 180), bool(c.get("coin_spend_on")))
            coin(db, inv.patient_id, int(c.get("cab_coin_ref") or 0), "ref_inviter", f"friend:{p.id}", int(c.get("coin_days") or 180), bool(c.get("coin_spend_on")))
    acc.last_login = now
    token = secrets.token_urlsafe(32)
    days = DAYS_REMEMBER if remember else DAYS_SHORT
    db.add(ShopSession(token_hash=_h("sess", token), patient_id=p.id, created_at=now, expires_at=now + timedelta(days=days), ua=(ua or "")[:200]))
    db.commit()
    return token, days, acc


def me(db: Session, token: str | None) -> tuple[ShopAccount, Patient] | None:
    if not token:
        return None
    s = db.scalars(select(ShopSession).where(ShopSession.token_hash == _h("sess", token))).first()
    if not s or not s.expires_at or s.expires_at < datetime.utcnow():
        return None
    acc = db.scalars(select(ShopAccount).where(ShopAccount.patient_id == s.patient_id)).first()
    p = db.get(Patient, s.patient_id)
    return (acc, p) if acc and p else None


def logout(db: Session, token: str | None) -> None:
    if token:
        s = db.scalars(select(ShopSession).where(ShopSession.token_hash == _h("sess", token))).first()
        if s:
            db.delete(s)
            db.commit()


def save_profile(db: Session, acc: ShopAccount, p: Patient, d: dict) -> None:
    fn, ln = " ".join(str(d.get("first_name") or "").split())[:100], " ".join(str(d.get("last_name") or "").split())[:100]
    if len(fn) < 2:
        raise CabError("Вкажіть імʼя.")
    email = str(d.get("email") or "").strip()[:200]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise CabError("Перевірте email.")
    acc.first_name, acc.last_name, acc.email = fn, ln or None, email or None
    if not p.full_name or p.full_name == p.phone:                 # картку клініки не перейменовуємо — лише порожню
        p.full_name = f"{ln} {fn}".strip()
    db.commit()


def save_address(db: Session, acc: ShopAccount, d: dict) -> None:
    rows = [dict(a) for a in acc.addresses or []]
    if d.get("delete"):
        rows = [a for a in rows if a.get("id") != d["delete"]]
    elif d.get("main"):
        for a in rows:
            a["main"] = a.get("id") == d["main"]
    else:
        city, wh = str(d.get("city") or "").strip()[:120], str(d.get("warehouse") or "").strip()[:300]
        name = " ".join(str(d.get("name") or "").split())[:120]
        if len(city) < 2 or not wh:
            raise CabError("Вкажіть місто й відділення або поштомат Нової пошти.")
        if len(name) < 2:
            raise CabError("Вкажіть отримувача (імʼя та прізвище).")
        if len(rows) >= 8:
            raise CabError("Можна зберегти до 8 адрес — видаліть зайву.")
        phone = _phone(d.get("phone")) if str(d.get("phone") or "").strip() else ""
        rows.append({"id": secrets.token_hex(4), "name": name, "phone": phone, "city": city, "city_ref": str(d.get("city_ref") or "")[:60], "warehouse": wh,
                     "main": not rows or bool(d.get("as_main"))})
        if rows[-1]["main"]:
            for a in rows[:-1]:
                a["main"] = False
    if rows and not any(a.get("main") for a in rows):
        rows[0]["main"] = True
    acc.addresses = rows
    db.commit()


def main_address(acc: ShopAccount) -> dict | None:
    rows = acc.addresses or []
    return next((a for a in rows if a.get("main")), rows[0] if rows else None)


def set_wish(db: Session, acc: ShopAccount, ids: list, merge: bool = False) -> list[int]:
    new = [int(x) for x in ids or [] if str(x).isdigit()][:60]
    if merge:
        new = list(dict.fromkeys((acc.wishlist or []) + new))[:60]
    acc.wishlist = list(dict.fromkeys(new))
    db.commit()
    return acc.wishlist


def wish_items(data: dict, ids: list) -> list[dict]:
    """Картки каталогу для вішліста (за id товару CRM — будь-якого варіанта)."""
    by_id = {v["id"]: it for it in data["items"] for v in it["variants"]}
    out = []
    for i in ids or []:
        it = by_id.get(i)
        if it and it not in out:
            out.append(it)
    return out


def orders(db: Session, p: Patient) -> list[SiteOrder]:
    return db.scalars(select(SiteOrder).where(SiteOrder.patient_id == p.id).order_by(SiteOrder.created_at.desc().nullslast()).limit(100)).all()


def coins(db: Session, pid: int) -> dict:
    from . import coins as cn
    return cn.summary(db, pid)


def by_wish_token(db: Session, token: str) -> ShopAccount | None:
    return db.scalars(select(ShopAccount).where(ShopAccount.wish_token == token[:40])).first() if token else None


def stats(db: Session) -> dict:
    return {"accounts": db.scalar(select(func.count(ShopAccount.id))) or 0,
            "sms_day": db.scalar(select(func.count(ShopCode.id)).where(ShopCode.channel == "sms", ShopCode.created_at >= datetime.utcnow() - timedelta(days=1))) or 0,
            "coins": int(db.scalar(select(func.coalesce(func.sum(CoinTxn.amount), 0))) or 0)}


# ---------------- «Повідомити, коли зʼявиться» ----------------
def notify_add(db: Session, product_id: int, raw_phone: str, data: dict) -> dict:
    """Підписка на появу товару. Один номер — один запис на товар; уже в наявності — підписка не потрібна."""
    phone = _phone(raw_phone)
    by_id = {v["id"]: (it, v) for it in list(data["items"]) + list((data.get("archive") or {}).values()) for v in it["variants"]}
    if product_id not in by_id:
        raise CabError("Товар не знайдено.")
    if by_id[product_id][1]["stock"]:
        raise CabError("Цей товар уже є в наявності — його можна замовити.")
    if not db.scalars(select(ShopNotify.id).where(ShopNotify.product_id == product_id, ShopNotify.phone == phone, ShopNotify.sent_at.is_(None))).first():
        from . import appeals as ap
        p = ap.find_patient(db, phone)
        db.add(ShopNotify(product_id=product_id, phone=phone, patient_id=p.id if p else None, created_at=datetime.utcnow()))
        db.commit()
    return {"ok": True, "to": mask(phone)}


def notify_waiting(db: Session) -> list[dict]:
    """Для CRM: на які товари чекають і скільки людей."""
    rows = db.execute(select(ShopNotify.product_id, func.count(ShopNotify.id), func.min(ShopNotify.created_at)).where(ShopNotify.sent_at.is_(None))
                      .group_by(ShopNotify.product_id)).all()
    names = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_([r[0] for r in rows] or [0])))}
    out = [{"id": r[0], "name": ((names[r[0]].site_info or {}).get("title") or names[r[0]].name) if r[0] in names else f"Товар {r[0]}", "n": r[1], "since": r[2]} for r in rows]
    return sorted(out, key=lambda x: -x["n"])


def notify_send(db: Session, c: dict, data: dict, site_root: str) -> dict:
    """Товар знову в наявності → повідомлення тим, хто чекав (Telegram-бот → SMS). Працює лише коли ввімкнено в налаштуваннях."""
    rep = {"sent": 0, "failed": 0, "ready": 0}
    by_id = {v["id"]: (it, v) for it in data["items"] for v in it["variants"]}
    rows = db.scalars(select(ShopNotify).where(ShopNotify.sent_at.is_(None)).order_by(ShopNotify.id).limit(300)).all()
    ready = [r for r in rows if r.product_id in by_id and by_id[r.product_id][1]["stock"]]
    rep["ready"] = len(ready)
    if not ready or not c.get("notify_on"):
        return rep
    from ..integrations import messaging as m
    if m.is_quiet_time():
        return rep
    tpl = (c.get("notify_text") or "").strip() or "{name} знову в наявності в магазині MOLODIST: {url}"
    for r in ready[:40]:
        it = by_id[r.product_id][0]
        text = tpl.replace("{name}", it["name"][:60]).replace("{url}", f"{site_root}/shop/{it['slug']}/")[:300]
        try:
            ch = _deliver(db, r.phone, text, "🔔 ", sms=back_sms(site_root, it["slug"]))
        except CabError:
            rep["failed"] += 1
            continue
        r.sent_at, r.channel = datetime.utcnow(), ch
        rep["sent"] += 1
        db.commit()
    return rep


# ---------------- рекомендації лікаря (домашній догляд із бюті-плану) ----------------
def doctor_care(db: Session, p: Patient, data: dict) -> dict:
    """Засоби домашнього догляду з останнього бюті-плану пацієнта: що є в магазині — картками, решта — назвами.
    Лише назви засобів: без діагнозів, нотаток лікаря й процедур."""
    from ..models import BeautyPlan, PlanItem
    plan = db.scalars(select(BeautyPlan).where(BeautyPlan.patient_id == p.id).order_by(BeautyPlan.session_at.desc().nullslast(), BeautyPlan.id.desc()).limit(1)).first()
    if not plan:
        return {"items": [], "other": [], "date": None}
    by_id = {v["id"]: it for it in data["items"] for v in it["variants"]}
    items, other = [], []
    for it in db.scalars(select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.kind == "product").order_by(PlanItem.id)):
        card = by_id.get(it.product_id) if it.product_id else None
        if card and card not in items:
            items.append(card)
        elif not card and it.name not in other:
            other.append(it.name)
    return {"items": items, "other": other[:30], "date": plan.session_at, "sum": sum(i["price"] for i in items if i["stock"])}


# ---------------- сповіщення покупцю про замовлення ----------------
ORDER_MSG = {"paid": "Замовлення {n} оплачено. Дякуємо!",   # ≤ 70 символів — 1 SMS; MOLODIST — в альфа-імені
             "sent": "Замовлення {n} відправлено НП{ttn}",
             "cancel": "Замовлення {n} скасовано. Питання: 0933756437",
             "arrived": "Замовлення {n} чекає у відділенні НП{wh}"}


def order_notice(db: Session, c: dict, o: SiteOrder, event: str) -> bool:
    """Повідомлення покупцю про оплату / відправлення / скасування (бот турботи → SMS). Раз на подію; лише коли ввімкнено."""
    raw = dict(o.raw or {})
    done = raw.get("cust_msgs") or []
    if not c.get("order_msg_on") or event not in ORDER_MSG or event in done or not o.phone or o.source != "crm":
        return False
    ttn = raw.get("ttn") or ""
    wh = str(raw.get("np_wh") or "")
    text = ORDER_MSG[event].replace("{n}", o.number or "").replace("{ttn}", f", ТТН {ttn}" if ttn else "") \
        .replace("{wh}", f" №{wh}" if wh.isdigit() else "")
    try:
        _deliver(db, _phone(o.phone), text, "📦 ")
    except CabError:
        return False
    raw["cust_msgs"] = done + [event]
    o.raw = raw
    db.flush()
    return True
