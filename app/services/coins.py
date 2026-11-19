"""MOLODIST Coins — окрема бонусна система магазину й медичного простору (журнал CoinTxn).

Це НЕ бонуси, перенесені з CleverBox (BonusTxn, services/loyalty.py) — ті ведуться окремо й окремо вмикаються власником.

Зараз: коїни лише нараховуються (реєстрація, запрошення друга, оплачені замовлення магазину). Використати їх не можна, доки власник
не запустить програму (coin_spend_on); строк дії відлічується з дня запуску — до того коїни без строку.
1 Coin = 1 грн знижки (після запуску). Журнал не редагується: виправлення — зворотним записом."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BonusTxn, CoinTxn, ShopCoin, SiteOrder

LABELS = {"welcome": "За реєстрацію", "ref_inviter": "За запрошеного друга", "ref_friend": "Бонус за запрошенням друга", "accrual": "За покупку",
          "spend": "Знижка MOLODIST Coins", "restore": "Повернено MOLODIST Coins", "reverse": "Скасовано нарахування", "expire": "Згоріли", "manual": "Нараховано вручну",
          "import": "Стартовий залишок", "birthday": "До дня народження", "review": "За відгук про товар"}
DEFAULT = {"coin_on": True, "coin_spend_on": False, "coin_start": "", "coin_earn_pct": 3.0, "coin_max_pct": 10.0, "coin_days": 180, "coin_wait_days": 3, "coin_review": 25}
CLOSED = ("Закрито", "Отримано")
LOST = ("Скасовано", "Повернення", "Фейкове")


def balance(db: Session, pid: int) -> float:
    from sqlalchemy import func
    return round(float(db.scalar(select(func.sum(CoinTxn.amount)).where(CoinTxn.patient_id == pid)) or 0), 2)


def rules(c: dict) -> dict:
    return {k: c.get(k, v) for k, v in DEFAULT.items()}


def add(db: Session, pid: int, amount: float, kind: str, ref: str, comment: str = "", days: int | None = None, live: bool = False) -> bool:
    """Запис у журнал; повтор з тим самим (пацієнт, вид, ref) нічого не створює. Строк дії ставиться лише коли програму запущено (live)."""
    if not amount or db.scalars(select(CoinTxn.id).where(CoinTxn.patient_id == pid, CoinTxn.kind == kind, CoinTxn.ref == ref)).first():
        return False
    if amount < 0:
        amount = -min(-amount, max(0.0, balance(db, pid)))          # залишок не може стати відʼємним
        if not amount:
            return False
    db.add(CoinTxn(patient_id=pid, amount=round(amount, 2), kind=kind[:12], comment=(comment or LABELS.get(kind) or "")[:300], ref=ref[:60],
                   expires_at=(date.today() + timedelta(days=days)) if amount > 0 and days and live else None, at=datetime.now()))
    db.flush()
    return True


def launch(db: Session, c: dict, today: date | None = None) -> int:
    """Запуск програми власником: з цього дня коїни можна використовувати, і в усіх уже нарахованих починається строк дії."""
    today = today or date.today()
    exp = today + timedelta(days=int(rules(c)["coin_days"]))
    n = 0
    for t in db.scalars(select(CoinTxn).where(CoinTxn.amount > 0, CoinTxn.expires_at.is_(None))):
        t.expires_at = exp
        n += 1
    db.commit()
    return n


def split_from_bonuses(db: Session) -> int:
    """16.11.2026: коїни, які 15 хвилин писалися в журнал бонусів CleverBox, — скопіювати у свій журнал (копія; з бонусів не видаляє)."""
    n = 0
    for t in db.scalars(select(BonusTxn).where(BonusTxn.ref.is_not(None))):
        if (t.ref or "").startswith(("cab:", "shopcoin:", "shoporder:", "shopearn:")) and \
                not db.scalars(select(CoinTxn.id).where(CoinTxn.patient_id == t.patient_id, CoinTxn.kind == t.kind, CoinTxn.ref == t.ref)).first():
            db.add(CoinTxn(patient_id=t.patient_id, amount=t.amount, kind=t.kind, comment=t.comment, ref=t.ref, at=t.at, expires_at=None))
            n += 1
    db.commit()
    return n


def migrate_shop_coins(db: Session, days: int = 180) -> int:
    """Коїни, нараховані в кабінеті магазину до обʼєднання, → у єдиний журнал (раз; повтор безпечний)."""
    n = 0
    for r in db.scalars(select(ShopCoin)):
        kind = "welcome" if r.kind == "signup" else r.kind
        n += add(db, r.patient_id, r.amount, kind, f"shopcoin:{r.id}", LABELS.get(kind, r.note or ""))
    db.commit()
    return n


def _lots(db: Session, pid: int) -> list[list]:
    """Залишки партій за FIFO: [[залишок, згорає]]."""
    lots: list[list] = []
    for t in db.scalars(select(CoinTxn).where(CoinTxn.patient_id == pid).order_by(CoinTxn.at, CoinTxn.id)):
        if t.amount > 0:
            lots.append([t.amount, t.expires_at])
        else:
            need = -t.amount
            for lot in sorted(lots, key=lambda x: (x[1] is None, x[1] or date.max)):
                take = min(lot[0], need)
                lot[0] -= take
                need -= take
                if need <= 0:
                    break
    return [l for l in lots if l[0] > 0.005]


def summary(db: Session, pid: int) -> dict:
    rows = db.scalars(select(CoinTxn).where(CoinTxn.patient_id == pid).order_by(CoinTxn.at.desc(), CoinTxn.id.desc()).limit(200)).all()
    lots = sorted([l for l in _lots(db, pid) if l[1]], key=lambda x: x[1])
    pend = [{"n": o.number, "amount": (o.raw or {})["coins_earn"]["amount"], "due": (o.raw or {})["coins_earn"].get("due")}
            for o in db.scalars(select(SiteOrder).where(SiteOrder.patient_id == pid, SiteOrder.source == "crm").order_by(SiteOrder.id.desc()).limit(60))
            if ((o.raw or {}).get("coins_earn") or {}).get("amount") and not (o.raw or {})["coins_earn"].get("posted") and o.status not in LOST]
    return {"total": int(math.floor(max(0.0, balance(db, pid)) + 1e-6)), "pending": sum(p["amount"] for p in pend), "pend": pend,
            "soon": {"amount": int(sum(l[0] for l in lots if l[1] == lots[0][1])), "date": lots[0][1]} if lots else None,
            "rows": [{"at": t.at, "amount": t.amount, "label": LABELS.get(t.kind, t.comment or t.kind), "note": t.comment if t.kind in ("accrual", "spend", "restore", "reverse") else ""}
                     for t in rows],
            "invited": sum(1 for t in rows if t.kind == "ref_inviter"), "ref": int(sum(t.amount for t in rows if t.kind == "ref_inviter"))}


def unfile_from_bonuses(db: Session) -> int:
    """16.11.2026, рішення власника: записи MOLODIST Coins, що помилково потрапили в журнал бонусів CleverBox, прибрати звідти.
    Видаляються лише записи з позначками Coins (cab: / shopcoin: / shoporder: / shopearn:) і лише якщо копія вже є в журналі Coins.
    Стартові залишки CleverBox та інші бонуси не зачіпаються."""
    n = 0
    for t in db.scalars(select(BonusTxn).where(BonusTxn.ref.is_not(None))).all():
        if (t.ref or "").startswith(("cab:", "shopcoin:", "shoporder:", "shopearn:")) and \
                db.scalars(select(CoinTxn.id).where(CoinTxn.patient_id == t.patient_id, CoinTxn.kind == t.kind, CoinTxn.ref == t.ref)).first():
            db.delete(t)
            n += 1
    db.commit()
    return n


# ---------------- магазин ----------------
def eligible(items: list[dict]) -> float:
    """Сума товарів, що беруть участь (акційні — ні)."""
    return round(sum(i["total"] for i in items if not i.get("sale")), 2)


def quote(db: Session, c: dict, items: list[dict], pid: int | None, want: int | None = None) -> dict:
    """Скільки коїнів можна списати на цей кошик і скільки буде нараховано. want=None — максимум."""
    r = rules(c)
    el = eligible(items)
    bal = int(math.floor(max(0.0, balance(db, pid)) + 1e-6)) if pid and r["coin_on"] else 0
    mx = min(bal, int(math.floor(el * float(r["coin_max_pct"]) / 100 + 1e-6))) if r["coin_on"] and r["coin_spend_on"] else 0
    use = max(0, min(mx, mx if want is None else int(want or 0)))
    return {"on": bool(r["coin_on"]), "spend": bool(r["coin_spend_on"]), "balance": bal, "max": mx, "use": use, "eligible": el,
            "earn": int(math.floor(max(0.0, el - use) * float(r["coin_earn_pct"]) / 100 + 1e-6)) if r["coin_on"] else 0}


def earn_for(c: dict, price: float, on_sale: bool = False) -> int:
    r = rules(c)
    return 0 if on_sale or not r["coin_on"] else int(math.floor(price * float(r["coin_earn_pct"]) / 100 + 1e-6))


def reserve(db: Session, o: SiteOrder, use: int) -> int:
    """Списати коїни під замовлення (це й резерв: якщо оплата не пройде — повернуться)."""
    if use <= 0 or not o.patient_id:
        return 0
    if not add(db, o.patient_id, -use, "spend", f"shoporder:{o.id}", f"замовлення {o.number}"):
        return 0
    o.raw = {**(o.raw or {}), "coins": {"used": use}}
    db.flush()
    return use


def release(db: Session, o: SiteOrder, c: dict) -> int:
    """Оплата не пройшла / замовлення скасовано → повернути списані коїни (раз)."""
    used = int(((o.raw or {}).get("coins") or {}).get("used") or 0)
    if not used or ((o.raw or {}).get("coins") or {}).get("restored") or not o.patient_id:
        return 0
    add(db, o.patient_id, used, "restore", f"shoporder:{o.id}", f"замовлення {o.number}", max(30, int(rules(c)["coin_days"])), live=bool(rules(c)["coin_spend_on"]))
    o.raw = {**(o.raw or {}), "coins": {**o.raw["coins"], "restored": True}}
    db.flush()
    return used


def plan_earn(db: Session, o: SiteOrder, c: dict) -> int:
    """Після оплати: скільки коїнів чекає на нарахування за це замовлення."""
    if not o.patient_id or (o.raw or {}).get("coins_earn"):
        return 0
    used = int(((o.raw or {}).get("coins") or {}).get("used") or 0)
    el = round(sum(float(i.get("total") or 0) for i in o.items or [] if not i.get("sale")), 2)
    n = int(math.floor(max(0.0, el - used) * float(rules(c)["coin_earn_pct"]) / 100 + 1e-6)) if rules(c)["coin_on"] else 0
    o.raw = {**(o.raw or {}), "coins_earn": {"amount": n}}
    db.flush()
    return n


def on_status(db: Session, o: SiteOrder, c: dict) -> None:
    raw = dict(o.raw or {})
    ce = dict(raw.get("coins_earn") or {})
    if o.status in CLOSED and o.paid:
        if not ce:
            plan_earn(db, o, c)
            raw = dict(o.raw or {})
            ce = dict(raw.get("coins_earn") or {})
        if ce.get("amount") and not ce.get("due"):
            ce["due"] = (date.today() + timedelta(days=int(rules(c)["coin_wait_days"]))).isoformat()
            o.raw = {**raw, "coins_earn": ce}
            db.flush()
    elif o.status in LOST:
        release(db, o, c)
        raw = dict(o.raw or {})
        if ce.get("posted") and not ce.get("reversed") and o.patient_id:
            add(db, o.patient_id, -ce["amount"], "reverse", f"shopearn:{o.id}", f"замовлення {o.number}")
            ce["reversed"] = True
            o.raw = {**raw, "coins_earn": ce}
            db.flush()


def post_due(db: Session, c: dict, today: date | None = None) -> int:
    """Щодня / раз на кілька хвилин: нарахувати коїни за закриті замовлення, у яких настав день нарахування."""
    today = today or date.today()
    n = 0
    for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.status.in_(CLOSED)).order_by(SiteOrder.id.desc()).limit(400)):
        ce = dict((o.raw or {}).get("coins_earn") or {})
        if ce.get("amount") and ce.get("due") and not ce.get("posted") and ce["due"] <= today.isoformat() and o.patient_id:
            add(db, o.patient_id, ce["amount"], "accrual", f"shopearn:{o.id}", f"замовлення {o.number}", int(rules(c)["coin_days"]), live=bool(rules(c)["coin_spend_on"]))
            ce["posted"] = True
            o.raw = {**(o.raw or {}), "coins_earn": ce}
            n += 1
    db.commit()
    return n
