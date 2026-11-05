"""Видатки з каси з Telegram-групи бухгалтерії: адмін пише «155 грн / доставка НП / термінал Бондар» → витрата у «Фіноперації».

Розбір: сума (перше число), каса (найкраще збігання з активними касами / рахунками), стаття (найкраще збігання зі статтями
витрат), решта — коментар. Якщо касу чи статтю не впізнано — бот пропонує кнопки. Внесене можна скасувати кнопкою
(операція позначається видаленою, як «Видалити» у фіноопераціях)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..models import FinOp, SyncState, User
from ..parsers.observ import norm

KEY = "chat_exp_cfg"            # {"on": bool}
PENDING = "chat_exp_pending"    # {key: {...}} — чекають вибору каси / статті
AMOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(?:грн|₴|uah|гривень|гривні)?", re.I)
# синоніми, які часто пишуть адміни → слова зі статей
SYN = {"нп": "доставка нова пошта", "новапошта": "нова пошта", "укрпошта": "доставка", "таксі": "транспорт таксі",
       "бензин": "транспорт пальне", "канцелярія": "канцтовари", "вода": "вода господарські", "кава": "кава господарські"}


def _get(db: Session, key: str, default):
    st = db.get(SyncState, key)
    try:
        return json.loads(st.value) if st and st.value else default
    except ValueError:
        return default


def _put(db: Session, key: str, val) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = json.dumps(val, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def enabled(db: Session) -> bool:
    return bool(_get(db, KEY, {}).get("on", True))


def set_enabled(db: Session, on: bool) -> None:
    c = _get(db, KEY, {})
    c["on"] = bool(on)
    _put(db, KEY, c)


def _words(t: str) -> list[str]:
    return [w for w in norm(t or "").lower().split() if len(w) > 1]


def _stem(w: str) -> str:
    return w[:5] if len(w) > 5 else w


def _score(text_words: list[str], name: str) -> float:
    """Скільки слів назви (каси / статті) є в тексті — за основою слова (перші 5 літер)."""
    nw = {_stem(w) for w in _words(name)}
    if not nw:
        return 0.0
    tw = {_stem(w) for w in text_words}
    hit = len(nw & tw)
    return hit / len(nw) + 0.01 * hit


def _best(text_words: list[str], names: list[str]) -> tuple[str | None, float, list[str]]:
    sc = sorted(((_score(text_words, n), n) for n in names), key=lambda x: -x[0])
    top = [n for s, n in sc if s > 0][:6]
    return (sc[0][1] if sc and sc[0][0] > 0 else None), (sc[0][0] if sc else 0.0), top


def parse(db: Session, text: str) -> dict | None:
    """Текст повідомлення → {amount, cashbox, category, comment, cash_top, cat_top}; None — це не видаток (немає суми)."""
    from . import cashboxes, fin_articles
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return None
    m = AMOUNT_RE.search(t)
    if not m:
        return None
    amount = float(m.group(1).replace(" ", "").replace(" ", "") + ("." + m.group(2) if m.group(2) else ""))
    if amount <= 0 or amount > 10_000_000:
        return None
    rest = (t[:m.start()] + " " + t[m.end():]).strip()
    if len(_words(rest)) < 1:
        return None
    words = _words(rest)
    cash, cs, ctop = _best(words, cashboxes.names(db))
    cw = {_stem(w) for w in _words(cash)} if cash and cs >= 0.5 else set()
    exp = []
    for w in words:
        if _stem(w) not in cw:                       # слова каси («термінал Бондар») не плутаємо зі статтями («ЄСВ ФОП Бондар»)
            exp += _words(SYN.get(w, w))
    cat, ks, ktop = _best(exp, fin_articles.names(db, "expense"))
    lines = [x.strip(" .,-—") for x in re.split(r"[\n;]+", rest) if x.strip(" .,-—")]
    return {"amount": round(amount, 2), "cashbox": cash if cs >= 0.5 else None, "category": cat if ks >= 0.5 else None,
            "comment": " · ".join(lines)[:500] or None, "cash_top": ctop, "cat_top": ktop}


def user_by_tg(db: Session, tg_id) -> User | None:
    from sqlalchemy import select
    return db.scalars(select(User).where(User.telegram_chat_id == str(tg_id), User.active.is_(True))).first()


def create(db: Session, d: dict, author: str | None, user_id: int | None, ref: str | None = None) -> FinOp:
    x = FinOp(day=date.today(), kind="expense", category=d["category"][:60], amount=d["amount"], cashbox=(d.get("cashbox") or "")[:100] or None,
              comment=(" · ".join(filter(None, [d.get("comment"), f"з Telegram: {author}" if author else "з Telegram"])))[:500],
              created_by=user_id, ref=(ref or "")[:80] or None)
    db.add(x)
    db.commit()
    return x


def cancel(db: Session, op_id: int) -> FinOp | None:
    x = db.get(FinOp, op_id)
    if x and not x.deleted_at:
        x.deleted_at = datetime.utcnow()
        db.commit()
    return x


def pend_put(db: Session, key: str, d: dict) -> None:
    p = _get(db, PENDING, {})
    p[key] = d
    if len(p) > 50:
        p = dict(list(p.items())[-50:])
    _put(db, PENDING, p)


def pend_get(db: Session, key: str) -> dict | None:
    return _get(db, PENDING, {}).get(key)


def pend_del(db: Session, key: str) -> None:
    p = _get(db, PENDING, {})
    p.pop(key, None)
    _put(db, PENDING, p)


def text_done(x: FinOp) -> str:
    from html import escape
    return (f"✅ <b>Видаток внесено</b> · CRM-{x.id}\n<b>{x.amount:,.2f} ₴</b> · {escape(x.category)}".replace(",", " ")
            + (f"\nКаса: {escape(x.cashbox)}" if x.cashbox else "") + (f"\n{escape(x.comment)}" if x.comment else ""))
