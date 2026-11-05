"""Історія планування цін: знімки полів товарів до / після зміни, збережені версії, відкат."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PriceSnapshot, Product

FIELDS = ("cost_currency", "cost_fx", "rrp_fx", "planned_price", "price")
KINDS = {"version": "Збережена версія", "price": "Прайс постачальника", "save": "Зміни в таблиці", "sale": "Ціна продажу",
         "restore": "Повернення", "undo": "Скасування прайсу"}


def state(p: Product) -> dict:
    return {f: getattr(p, f) for f in FIELDS}


def record(db: Session, kind: str, before: dict[int, dict], products: list[Product], user_id: int | None, brand: str | None = None,
           supplier_id: int | None = None, price_id: int | None = None, note: str | None = None, only_changed: bool = True) -> PriceSnapshot | None:
    """Знімок: before — стан до (pid → поля), products — уже змінені товари (стан після)."""
    items = []
    for p in products:
        b, a = before.get(p.id) or state(p), state(p)
        if only_changed and b == a:
            continue
        items.append({"pid": p.id, "name": p.name, "sku": p.sku, "brand": p.brand, "supplier": p.supplier, "before": b, "after": a})
    if not items:
        return None
    if not brand:
        bs = {p.brand for p in products if p.brand}
        brand = bs.pop() if len(bs) == 1 else None
    sn = PriceSnapshot(created_at=datetime.now(), user_id=user_id, kind=kind, brand=brand, supplier_id=supplier_id, price_id=price_id,
                       note=(note or "")[:300] or None, items=items, n=len(items), rates=_rates(db))
    db.add(sn)
    db.commit()
    return sn


def _rates(db: Session) -> dict:
    from .pricing import rates
    r = rates(db)
    return {"USD": r["USD"], "EUR": r["EUR"], "round": r["round"]}


def xlsx(db: Session, sn: PriceSnapshot) -> bytes:
    """Версія / зміна планування в Excel: значення після (і до — для змін), закупівля в грн за курсом на момент збереження."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from .pricing import margin, markup
    from ..models import Supplier
    rt = sn.rates or _rates(db)
    sfx = {s.name.strip().lower(): s for s in db.scalars(select(Supplier))}
    wb = Workbook()
    ws = wb.active
    ws.title = "Планування"
    ws.append([f"{KINDS.get(sn.kind, sn.kind)} №{sn.id} · {sn.created_at:%d.%m.%Y %H:%M}" + (f" · {sn.brand}" if sn.brand else "")
               + (f" · {sn.note}" if sn.note else "") + f"  ·  курс USD {rt.get('USD')} · EUR {rt.get('EUR')}"])
    chg = sn.kind != "version"
    head = ["Бренд", "Товар", "Артикул", "Валюта", "Закупівля (вал.)", "Курс", "Закупівля, грн", "РРЦ (вал.)", "РРЦ, грн",
            "Рекомендована ціна", "Націнка, %", "Маржа від рекомендованої, %", "Ціна продажу", "Маржа від ціни продажу, %"]
    if chg:
        head += ["Було: закупівля (вал.)", "Було: РРЦ", "Було: рекомендована", "Було: ціна продажу"]
    ws.append(head)
    for c in ws[2]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="3B3050")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for it in sn.items or []:
        a, b = it.get("after") or {}, it.get("before") or {}
        cur = a.get("cost_currency") or "UAH"
        s_ = sfx.get((it.get("supplier") or "").strip().lower())
        r = 1.0 if cur == "UAH" else ({"USD": s_.fx_usd, "EUR": s_.fx_eur}.get(cur) if s_ else None) or rt.get(cur) or 1.0
        cost = round(a["cost_fx"] * r, 2) if a.get("cost_fx") else None
        rrp = round(a["rrp_fx"] * r, 2) if a.get("rrp_fx") else None
        plan, price = a.get("planned_price"), a.get("price")
        row = [it.get("brand"), it.get("name"), it.get("sku"), cur, a.get("cost_fx"), r if cur != "UAH" else None, cost, a.get("rrp_fx"), rrp,
               plan, markup(plan, cost), margin(plan, cost), price, margin(price, cost)]
        if chg:
            row += [b.get("cost_fx"), b.get("rrp_fx"), b.get("planned_price"), b.get("price")]
        ws.append(row)
    for i, w in enumerate([14, 46, 12, 8] + [13] * 14):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.freeze_panes = "C3"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def version(db: Session, products: list[Product], user_id: int | None, brand: str | None, supplier_id: int | None, note: str | None) -> PriceSnapshot | None:
    """Зберегти поточне планування (без змін) як версію, до якої можна повернутись."""
    return record(db, "version", {}, products, user_id, brand, supplier_id, note=note, only_changed=False)


def restore(db: Session, sn: PriceSnapshot, which: str, user_id: int | None) -> int:
    """which = before (відкотити зміну) | after (повернути стан цієї версії). Повернення теж пишеться в історію."""
    ids = [x["pid"] for x in sn.items or []]
    prods = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))} if ids else {}
    before = {pid: state(p) for pid, p in prods.items()}
    changed = []
    for x in sn.items or []:
        p = prods.get(x["pid"])
        v = x.get("before" if which == "before" else "after") or {}
        if not p:
            continue
        for f in FIELDS:
            if f in v:
                if f == "price" and v[f] != p.price:
                    p.price_set_at = datetime.now() if v[f] else None
                setattr(p, f, v[f])
        changed.append(p)
    db.commit()
    record(db, "restore", before, changed, user_id, sn.brand, sn.supplier_id,
           note=f"{'Відкат' if which == 'before' else 'Повернуто версію'} №{sn.id} ({KINDS.get(sn.kind, sn.kind)})")
    return len(changed)


def for_brand(db: Session, brand: str | None, limit: int = 60) -> list[PriceSnapshot]:
    q = select(PriceSnapshot).order_by(PriceSnapshot.id.desc()).limit(limit)
    if brand:
        q = q.where(PriceSnapshot.brand == brand)
    return db.scalars(q).all()
