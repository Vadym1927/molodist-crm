"""Оплати постачальникам: рознесення платежу на поставки (кілька складів однієї накладної) і товари.

Група оплати = одна накладна постачальника: StockDoc.pay_group (кілька документів — по складах) або сам документ.
Платіж ділиться між документами пропорційно неоплаченому залишку, а в документі — між рядками пропорційно сумі рядка.
Автоматичне зіставлення платежу monobank: постачальник за IBAN / ЄДРПОУ / назвою, далі № накладної в призначенні
платежу або точна сума неоплаченої групи. Невпевнені збіги не рознесуться — їх видно в списку «Платежі»."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import StockDoc, StockDocLine, PaymentAlloc, BankTxn, Supplier
from ..parsers.observ import norm

TOL = 1.0      # ₴ — допуск на копійки / комісію


def line_sum(ln: StockDocLine) -> float:
    return round((ln.qty or 0) * (ln.unit_cost or 0), 2)


def doc_total(doc: StockDoc) -> float:
    return round(sum(line_sum(ln) for ln in doc.lines), 2)


def paid_map(db: Session, doc_ids: list[int]) -> dict[int, float]:
    if not doc_ids:
        return {}
    return {d: float(s or 0) for d, s in db.execute(select(PaymentAlloc.doc_id, func.sum(PaymentAlloc.amount))
                                                   .where(PaymentAlloc.doc_id.in_(doc_ids)).group_by(PaymentAlloc.doc_id))}


def pay_state(total: float, paid: float) -> str:
    if total <= TOL:
        return "none"
    if paid >= total - TOL:
        return "paid"
    return "partial" if paid > TOL else "unpaid"


PAY_LABELS = {"paid": "оплачено", "partial": "частково", "unpaid": "не оплачено", "none": "—"}


@dataclass
class Group:
    key: str
    supplier: str | None
    ext_no: str | None
    date: object
    docs: list = field(default_factory=list)
    total: float = 0.0
    paid: float = 0.0

    @property
    def left(self) -> float:
        return round(max(self.total - self.paid, 0), 2)

    @property
    def state(self) -> str:
        return pay_state(self.total, self.paid)

    @property
    def label(self) -> str:
        whs = ", ".join(sorted({d.wh_to or "" for d in self.docs}))
        return f"{self.date:%d.%m.%Y} · {self.supplier or '—'} · №{self.ext_no or self.docs[0].number} · {whs}"


def group_key(doc: StockDoc) -> str:
    return doc.pay_group or f"doc:{doc.id}"


def groups(db: Session, supplier: str | None = None, only_open: bool = False) -> list[Group]:
    q = select(StockDoc).where(StockDoc.kind == "supply", StockDoc.status != "cancelled")
    docs = db.scalars(q.order_by(StockDoc.doc_date.desc(), StockDoc.id.desc())).all()
    if supplier:                       # у Python: SQLite lower() не знає кирилиці
        docs = [d for d in docs if (d.supplier or "").lower() == supplier.lower()]
    paid = paid_map(db, [d.id for d in docs])
    out: dict[str, Group] = {}
    for d in docs:
        g = out.setdefault(group_key(d), Group(group_key(d), d.supplier, d.ext_no, d.doc_date))
        g.docs.append(d)
        g.total = round(g.total + doc_total(d), 2)
        g.paid = round(g.paid + paid.get(d.id, 0), 2)
    res = list(out.values())
    return [g for g in res if g.left > TOL] if only_open else res


def find_group(db: Session, key: str) -> Group | None:
    return next((g for g in groups(db) if g.key == key), None)


MODE_LABELS = {"auto": "🤖 авто · monobank", "mono_manual": "🏦 monobank · вручну", "manual": "✋ вручну"}


def modes_map(db: Session, doc_ids: list[int]) -> dict[int, dict[str, float]]:
    """Документ → {режим: сума} (auto / mono_manual / manual)."""
    out: dict[int, dict[str, float]] = {}
    if not doc_ids:
        return out
    for a in db.scalars(select(PaymentAlloc).where(PaymentAlloc.doc_id.in_(doc_ids))):
        m = out.setdefault(a.doc_id, {})
        m[a.mode] = round(m.get(a.mode, 0) + (a.amount or 0), 2)
    return out


def allocate(db: Session, g: Group, amount: float, txn: BankTxn | None = None, fop: str | None = None,
             user_id: int | None = None) -> float:
    """Розносить amount (≤ залишку групи) на документи й рядки. Повертає рознесену суму."""
    amount = round(min(amount, g.left), 2)
    if amount <= 0:
        return 0.0
    paid = paid_map(db, [d.id for d in g.docs])
    lefts = {d.id: max(doc_total(d) - paid.get(d.id, 0), 0) for d in g.docs}
    base = sum(lefts.values()) or 1
    done = 0.0
    docs = [d for d in g.docs if lefts[d.id] > 0]
    for i, d in enumerate(docs):
        part = round(amount - done, 2) if i == len(docs) - 1 else round(amount * lefts[d.id] / base, 2)
        lines = [ln for ln in d.lines if line_sum(ln) > 0]
        tot = sum(line_sum(ln) for ln in lines) or 1
        acc = 0.0
        for j, ln in enumerate(lines):
            la = round(part - acc, 2) if j == len(lines) - 1 else round(part * line_sum(ln) / tot, 2)
            db.add(PaymentAlloc(txn_id=txn.id if txn else None, doc_id=d.id, line_id=ln.id, amount=la, fop=fop, by_user=user_id))
            acc += la
        if not lines:
            db.add(PaymentAlloc(txn_id=txn.id if txn else None, doc_id=d.id, amount=part, fop=fop, by_user=user_id))
        if fop and not d.paid_by:
            d.paid_by = fop
        done += part
    db.flush()
    return round(done, 2)


def unallocate_txn(db: Session, txn: BankTxn) -> None:
    docs = set()
    for a in db.scalars(select(PaymentAlloc).where(PaymentAlloc.txn_id == txn.id)):
        docs.add(a.doc_id)
        db.delete(a)
    txn.status, txn.note = "new", None
    db.flush()
    for did in docs:                                # «яким ФОП оплачено» — за тими оплатами, що лишились
        d = db.get(StockDoc, did)
        if d:
            left = db.scalars(select(PaymentAlloc.fop).where(PaymentAlloc.doc_id == did, PaymentAlloc.fop.is_not(None))).first()
            d.paid_by = left
    db.commit()


def clear_manual(db: Session, g: Group) -> None:
    for a in db.scalars(select(PaymentAlloc).where(PaymentAlloc.doc_id.in_([d.id for d in g.docs]),
                                                   PaymentAlloc.txn_id.is_(None))):
        db.delete(a)
    db.commit()


# ---------------- автоматичне зіставлення ----------------

def _iban(v: str | None) -> str:
    return re.sub(r"\s", "", (v or "")).upper()


# слова організаційно-правової форми — за ними постачальника не впізнаємо («ТОВАРИСТВО З ОБМЕЖЕНОЮ…» є в усіх ТОВ)
LEGAL_WORDS = {"тов", "фоп", "пп", "тзов", "ооо", "llc", "ltd", "ат", "пат", "прат", "кп", "дп", "товариство", "обмеженою",
               "відповідальністю", "відповідальн", "фізична", "особа", "підприємець", "приватне", "підприємство", "акціонерне",
               "публічне", "приватне", "компанія", "company", "група", "україна", "україни", "ukraine", "з", "та", "і", "и"}


def name_words(v: str | None) -> set[str]:
    """Значущі слова назви: без форми власності, регістр і латинська/кирилична «т» зведені."""
    words = norm(v or "").lower().replace("t", "т").split()
    return {w for w in words if len(w) >= 3 and w not in LEGAL_WORDS and not w.isdigit()}


def supplier_match(db: Session, txn: BankTxn) -> tuple[Supplier | None, str]:
    """(постачальник, як упізнано): 'req' — за IBAN / ЄДРПОУ (надійно), 'name' — лише за назвою отримувача."""
    from .suppliers import find
    from ..models import SupplierEntity
    hit = find(db, txn.counter_iban, txn.counter_edrpou)           # IBAN / ЄДРПОУ будь-якої юр. особи постачальника
    if hit:
        return hit[0], "req"
    words = name_words(txn.counter_name)
    if not words:
        return None, ""
    sups = db.scalars(select(Supplier)).all()
    ent_names: dict[int, set] = {}
    for e in db.scalars(select(SupplierEntity)):
        ent_names.setdefault(e.supplier_id, set()).update(name_words(e.legal_name))
    hits = [x for x in sups if words & (name_words(x.legal_name) | name_words(x.name) | ent_names.get(x.id, set()))]
    return (hits[0], "name") if len(hits) == 1 else (None, "")


def supplier_for(db: Session, txn: BankTxn) -> Supplier | None:
    return supplier_match(db, txn)[0]


def _digits_in(text: str | None) -> set[str]:
    return set(re.findall(r"\d{3,}", text or ""))


def match_txn(db: Session, txn: BankTxn) -> str:
    """Розносить платіж, якщо збіг однозначний. Повертає статус."""
    if txn.amount >= 0 or txn.status in ("matched", "ignored"):
        return txn.status
    sup, how = supplier_match(db, txn)
    if txn.supplier_id and (not sup or sup.id != txn.supplier_id):
        sup, how = db.get(Supplier, txn.supplier_id), "name"     # вибрано раніше — без реквізитів, лише обережно
    if not sup:
        return txn.status
    if how == "req":
        txn.supplier_id = sup.id
    amt = round(-txn.amount, 2)
    day = txn.at.date() if txn.at else None
    # накладна не пізніше ніж через 60 днів після платежу (передоплата) і не раніше ніж за рік до нього
    open_g = [g for g in groups(db, sup.name, only_open=True)
              if not day or not g.date or -365 <= (day - g.date).days <= 60]
    nums = _digits_in(txn.description)
    by_no = [g for g in open_g if g.ext_no and (set(re.findall(r"\d{3,}", g.ext_no)) & nums)]
    by_amt = [g for g in open_g if abs(g.left - amt) <= TOL]
    target = by_no[0] if len(by_no) == 1 else (by_amt[0] if len(by_amt) == 1 else None)
    if not target and how == "req" and len(open_g) == 1 and open_g[0].left + TOL >= amt:
        target = open_g[0]                         # єдина відкрита накладна — лише якщо постачальника впізнано за реквізитами
    if target and how == "name" and not by_no:
        target = None                              # лише за назвою + сума — не розносимо сами, чекає ручного рішення
    if not target:
        db.commit()
        return txn.status
    done = allocate(db, target, amt, txn=txn, fop=txn.fop)
    txn.status = "matched" if done >= amt - TOL else "partial"
    txn.note = f"→ {target.label}"
    db.commit()
    return txn.status


def weak_auto(db: Session) -> list[BankTxn]:
    """Автоматичні рознесення, де постачальника впізнано не за IBAN / ЄДРПОУ (могли промахнутись)."""
    tids = {a.txn_id for a in db.scalars(select(PaymentAlloc).where(PaymentAlloc.txn_id.is_not(None), PaymentAlloc.by_user.is_(None)))}
    out = []
    for tid in tids:
        t = db.get(BankTxn, tid)
        if t and supplier_match(db, t)[1] != "req":
            out.append(t)
    return out


def learn_entity(db: Session, txn: BankTxn, sup: Supplier) -> bool:
    """Платіж рознесли на постачальника вручну — запамʼятати реквізити отримувача як його юр. особу."""
    from .suppliers import find, save_entity
    if not (txn.counter_iban or txn.counter_edrpou):
        return False
    hit = find(db, txn.counter_iban, txn.counter_edrpou)
    if hit and hit[1]:
        return False
    try:
        save_entity(db, sup, {"legal_name": (txn.counter_name or "")[:300], "iban": txn.counter_iban or "",
                              "edrpou": txn.counter_edrpou or "", "note": "з платежу monobank"})
        return True
    except ValueError:
        db.rollback()
        return False


def rematch_all(db: Session) -> int:
    n = 0
    for t in db.scalars(select(BankTxn).where(BankTxn.status == "new", BankTxn.amount < 0)).all():
        if match_txn(db, t) in ("matched", "partial"):
            n += 1
    return n


def by_product(db: Session, doc_ids: list[int]) -> dict[int, float]:
    """Оплачено по рядках (line_id → ₴) — для показу «що саме оплачено»."""
    out = defaultdict(float)
    for lid, s in db.execute(select(PaymentAlloc.line_id, func.sum(PaymentAlloc.amount))
                             .where(PaymentAlloc.doc_id.in_(doc_ids or [0]), PaymentAlloc.line_id.is_not(None))
                             .group_by(PaymentAlloc.line_id)):
        out[lid] = float(s or 0)
    return out
