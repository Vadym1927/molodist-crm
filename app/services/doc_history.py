"""Історія складського документа: хто, що і коли робив (журнал дій + оплати monobank)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, BankTxn, PaymentAlloc, StockDoc, User


def _uah(v) -> str:
    try:
        return f"{float(v):,.2f} ₴".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _text(a: AuditLog) -> str | None:
    d = a.details or {}
    act = a.action
    if act == "stock.create":
        return "Створено документ"
    if act == "stock.quick_doc":
        return f"Створено у вікні поставки · {d.get('lines', '')} поз." + (f" · на {len(d['docs'])} склади" if len(d.get("docs") or []) > 1 else "")
    if act == "stock.post":
        return "Проведено — залишки оновлено"
    if act == "stock.cancel":
        return "Скасовано — рухи по складу прибрано"
    if act == "stock.edit":
        ch = d.get("changes") or []
        return "Змінено: " + "; ".join(ch[:12]) + (f" … ще {len(ch) - 12}" if len(ch) > 12 else "") if ch else None
    if act == "stock.files":
        return f"Додано файли: {d.get('files', '')}"
    if act == "stock.file_delete":
        return f"Видалено файл «{d.get('name', '')}»"
    if act == "stock.pay":
        if d.get("op") == "clear":
            return "Знято ручні відмітки оплати"
        return "Відмічено оплату" + (f" {_uah(d['amount'])}" if d.get("amount") else "") + (f" · {d['fop']}" if d.get("fop") else "")
    if act == "stock.archive":
        return "Видалено в архів" + (f" · «{d['note']}»" if d.get("note") else "")
    if act == "stock.restore":
        return "Відновлено з архіву"
    if act == "delete.denied":
        return "Спроба видалити — " + ("пароль видалення не задано" if d.get("reason") == "no_pw" else "невірний пароль")
    if act == "stock.start":
        return "Стартові залишки — початок обліку"
    if act == "expiry.writeoff_draft":
        return f"Створено зі звіту термінів (прострочені партії: {d.get('lines', '')})"
    if act == "stock.receive":
        return "Створено з замовлення"
    if act == "stock.send":
        return "Позначено відправленим постачальнику"
    return act


def _local(v):
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from ..config import settings
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


def history(db: Session, doc: StockDoc) -> list[dict]:
    users = {u.id: u.name for u in db.scalars(select(User))}
    out = []
    logs = db.scalars(select(AuditLog).where(AuditLog.entity == "stock_doc", AuditLog.entity_id == doc.id)
                      .order_by(AuditLog.at)).all()
    if not any(a.action in ("stock.create", "stock.quick_doc", "stock.start", "expiry.writeoff_draft", "stock.receive") for a in logs):
        out.append({"at": _local(doc.created_at), "who": users.get(doc.created_by, "—") if doc.created_by else "Система",
                    "text": "Створено документ", "kind": "create"})
    for a in logs:
        t = _text(a)
        if t:
            out.append({"at": _local(a.at), "who": users.get(a.user_id) or ("API" if a.api_key_id else "Система"), "text": t,
                        "kind": a.action.split(".")[-1]})
    allocs = db.scalars(select(PaymentAlloc).where(PaymentAlloc.doc_id == doc.id, PaymentAlloc.txn_id.is_not(None))).all()
    by_txn: dict[str, float] = {}
    for x in allocs:
        by_txn[x.txn_id] = by_txn.get(x.txn_id, 0) + (x.amount or 0)
    for tid, amt in by_txn.items():
        t = db.get(BankTxn, tid)
        out.append({"at": (t.at if t else None) or _local(doc.created_at), "who": "monobank",
                    "text": f"Оплату {_uah(amt)} рознесено автоматично" + (f" · ФОП {t.fop}" if t and t.fop else "")
                            + (f" · «{t.description[:80]}»" if t and t.description else ""), "kind": "pay"})
    out.sort(key=lambda x: x["at"] or _local(doc.created_at))
    return out


def snapshot(doc: StockDoc) -> dict:
    return {"head": {k: getattr(doc, k) for k in ("doc_date", "supplier", "ext_no", "note", "reason", "wh_from", "wh_to")},
            "lines": {ln.id: (ln.product.name if ln.product else str(ln.product_id), ln.qty, ln.unit_cost, ln.expiry_date)
                      for ln in doc.lines}}


HEAD = {"doc_date": "дата", "supplier": "постачальник", "ext_no": "№ накладної", "note": "коментар", "reason": "причина",
        "wh_from": "склад-джерело", "wh_to": "склад"}


def diff(before: dict, after: dict) -> list[str]:
    from .catalog import WAREHOUSES
    ch = []
    for k, label in HEAD.items():
        a, b = before["head"].get(k), after["head"].get(k)
        if a != b:
            fmt = (lambda v: WAREHOUSES.get(v, v or "—")) if k.startswith("wh") else \
                  (lambda v: v.strftime("%d.%m.%Y") if hasattr(v, "strftime") else (v or "—"))
            ch.append(f"{label}: {fmt(a)} → {fmt(b)}")
    bl, al = before["lines"], after["lines"]
    for i, (n, q, c, e) in bl.items():
        if i not in al:
            ch.append(f"прибрано «{n[:40]}»")
            continue
        _, q2, c2, e2 = al[i]
        parts = []
        if (q or 0) != (q2 or 0):
            parts.append(f"к-сть {q or 0:g} → {q2 or 0:g}")
        if (c or 0) != (c2 or 0):
            parts.append(f"ціна {c or 0:g} → {c2 or 0:g}")
        if e != e2:
            parts.append(f"термін {e.strftime('%d.%m.%Y') if e else '—'} → {e2.strftime('%d.%m.%Y') if e2 else '—'}")
        if parts:
            ch.append(f"«{n[:40]}»: " + ", ".join(parts))
    for i, (n, q, c, e) in al.items():
        if i not in bl:
            ch.append(f"додано «{n[:40]}» × {q or 0:g}")
    return ch
