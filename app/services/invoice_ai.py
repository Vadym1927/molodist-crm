"""Розпізнавання накладної постачальника з файлу → чернетка поставки.

Excel / CSV — розбираються локально (шапка таблиці: найменування / кількість / ціна / сума / артикул).
Фото, скани й PDF — через Claude API (ключ у «Ключі й інтеграції»); без ключа PDF з текстовим шаром
розбирається таблицями pdfplumber. Далі CRM сама шукає постачальника (ЄДРПОУ / IBAN / назва) і товари
(артикул / штрихкод / назва), а склад бере з картки товару. Кількість і ціна — в одиницях приходу (як у накладній)."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Product, Supplier, SyncState
from ..parsers.observ import norm

log = logging.getLogger("invoice_ai")
API = "https://api.anthropic.com/v1/messages"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

PROMPT = """Це накладна / рахунок / видаткова накладна постачальника косметики для клініки в Україні.
Поверни ЛИШЕ JSON без пояснень, у такому вигляді:
{"supplier": {"name": "", "edrpou": "", "iban": ""}, "number": "", "date": "РРРР-ММ-ДД", "total": 0,
 "lines": [{"name": "", "sku": "", "barcode": "", "qty": 0, "unit": "", "price": 0, "sum": 0, "expiry": "РРРР-ММ-ДД або порожньо"}]}
Правила: постачальник — ПРОДАВЕЦЬ (не покупець); price — ціна за одиницю, яку фактично сплачує покупець (з ПДВ, якщо є);
qty — у тих одиницях, що в накладній; sku — артикул / код товару; expiry — термін придатності, якщо вказаний.
Числа — без пробілів, крапка як десятковий роздільник. Не вигадуй значень: невідоме — порожній рядок або 0."""


# ---------------- ключ Claude API ----------------

def api_key(db: Session) -> str:
    if getattr(settings, "anthropic_api_key", ""):
        return settings.anthropic_api_key
    st = db.get(SyncState, "anthropic_key_enc")
    if not st or not st.value:
        return ""
    from ..integrations.cleverbox import _token_cipher
    try:
        return _token_cipher().decrypt(st.value.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def save_key(db: Session, key: str) -> None:
    from ..integrations.cleverbox import _token_cipher
    st = db.get(SyncState, "anthropic_key_enc") or SyncState(key="anthropic_key_enc")
    st.value, st.updated_at = (_token_cipher().encrypt(key.strip().encode()).decode() if key.strip() else None), datetime.utcnow()
    db.merge(st)
    db.commit()


# ---------------- розбір ----------------

def _num(v) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d,.\-]", "", str(v).replace("\xa0", "").replace(" ", ""))
    if t.count(",") == 1 and t.count(".") == 0:
        t = t.replace(",", ".")
    elif t.count(",") and t.count("."):
        t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return 0.0


def _date(v) -> str:
    t = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(t[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


COLS = {"name": ("найменування", "назва", "товар", "найменування товару", "номенклатура", "product", "name"),
        "qty": ("кількість", "к-сть", "кіл-ть", "к-ть", "кол-во", "qty", "quantity"),
        "price": ("ціна", "ціна з пдв", "ціна без пдв", "price"),
        "sum": ("сума", "сума з пдв", "всього", "sum", "amount"),
        "sku": ("артикул", "код", "код товару", "sku", "art"),
        "barcode": ("штрихкод", "штрих-код", "barcode", "ean"),
        "unit": ("од.", "од", "одиниця", "од. вим.", "unit"),
        "expiry": ("термін придатності", "придатний до", "термін", "expiry")}


def _header(row) -> dict[str, int]:
    h = [str(x or "").strip().lower() for x in row]
    out = {}
    for k, names in COLS.items():
        for i, x in enumerate(h):
            if i in out.values() or not x:
                continue
            if x in names or any(x.startswith(n) for n in names if len(n) > 3):
                out[k] = i
                break
    return out


def parse_table(rows: list[list]) -> dict:
    """Таблиця з Excel / CSV / PDF: знайти рядок-шапку, далі — рядки товарів до підсумку."""
    res = {"supplier": {"name": "", "edrpou": "", "iban": ""}, "number": "", "date": "", "total": 0, "lines": []}
    text = " ".join(" ".join(str(c or "") for c in r) for r in rows[:15])
    m = re.search(r"(?:ЄДРПОУ|ЕДРПОУ|РНОКПП|ІПН)\D{0,10}(\d{8,10})", text)
    res["supplier"]["edrpou"] = m.group(1) if m else ""
    m = re.search(r"UA\d{27}", text.replace(" ", ""))
    res["supplier"]["iban"] = m.group(0) if m else ""
    m = re.search(r"(?:№|N|No)\s*([A-Za-zА-Яа-яІЇЄі\-]*\d[\w\-/]*)", text)
    res["number"] = m.group(1) if m else ""
    m = re.search(r"(?:Постачальник|Продавець|Від)\s*[:\-]?\s*([^\n,;]{3,80}?)(?:\s{2,}|ЄДРПОУ|,|$)", text)
    res["supplier"]["name"] = (m.group(1).strip() if m else "")
    hi, cols = None, {}
    for i, r in enumerate(rows):
        c = _header(r)
        if "name" in c and ("qty" in c or "sum" in c):
            hi, cols = i, c
            break
    if hi is None:
        return res
    for r in rows[hi + 1:]:
        name = str(r[cols["name"]] if cols["name"] < len(r) else "").strip()
        low = name.lower()
        if not name or low.startswith(("разом", "всього", "итого", "усього", "total")):
            if res["lines"]:
                break
            continue
        g = lambda k: r[cols[k]] if k in cols and cols[k] < len(r) else None  # noqa: E731
        qty, price, sm = _num(g("qty")), _num(g("price")), _num(g("sum"))
        if not qty and price and sm:
            qty = round(sm / price, 3)
        if not price and qty and sm:
            price = round(sm / qty, 2)
        if not qty:
            continue
        res["lines"].append({"name": name[:300], "sku": str(g("sku") or "").strip(), "barcode": str(g("barcode") or "").strip(),
                             "qty": qty, "unit": str(g("unit") or "").strip(), "price": price, "sum": sm or round(qty * price, 2),
                             "expiry": _date(g("expiry"))})
    res["total"] = round(sum(x["sum"] for x in res["lines"]), 2)
    return res


def _claude(key: str, data: bytes, mime: str, text: str | None = None, http: httpx.Client | None = None) -> dict:
    if text is not None:
        block = {"type": "text", "text": "Текст накладної:\n" + text[:60000]}
    elif mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(data).decode()}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
    c = http or httpx.Client(timeout=120)
    r = c.post(API, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
               json={"model": getattr(settings, "ai_model", "") or "claude-sonnet-5-5", "max_tokens": 8000,
                     "messages": [{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}]})
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
    out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("Claude API: відповідь без JSON")
    j = json.loads(m.group(0))
    for ln in j.get("lines") or []:
        for k in ("qty", "price", "sum"):
            ln[k] = _num(ln.get(k))
        ln["expiry"] = _date(ln.get("expiry"))
    j["date"] = _date(j.get("date"))
    j["total"] = _num(j.get("total"))
    return j


def extract(db: Session, data: bytes, filename: str, mime: str | None = None, claude=None) -> tuple[dict, str]:
    """→ (розібрані дані, джерело: excel | pdf | ai)."""
    from . import importer
    name = (filename or "").lower()
    mime = (mime or "").lower()
    if name.endswith((".xlsx", ".xls", ".xlsm", ".csv")):
        sheets = importer.read_sheets(data, filename)
        for _t, rows in sheets:
            res = parse_table(rows)
            if res["lines"]:
                return res, "excel"
        raise ValueError("У таблиці не знайдено рядків товарів (потрібні колонки «Найменування» і «Кількість» / «Сума»)")
    if name.endswith(".pdf"):
        mime = "application/pdf"
    elif name.endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif name.endswith(".png"):
        mime = "image/png"
    elif name.endswith(".webp"):
        mime = "image/webp"
    key = api_key(db)
    call = claude or _claude
    if mime == "application/pdf":
        if key:
            return call(key, data, mime), "ai"
        import pdfplumber
        rows, text = [], []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pg in pdf.pages[:10]:
                text.append(pg.extract_text() or "")
                for t in pg.extract_tables() or []:
                    rows.extend(t)
        res = parse_table([[" ".join(text)[:2000]]] + rows) if rows else None
        if res and res["lines"]:
            return res, "pdf"
        raise ValueError("Не вдалося прочитати таблицю з PDF. Для сканів і фото внесіть ключ Claude API в «Ключі й інтеграції».")
    if mime in IMAGE_TYPES:
        if not key:
            raise ValueError("Щоб розпізнавати фото, внесіть ключ Claude API в «Ще → Ключі й інтеграції».")
        return call(key, data, mime), "ai"
    raise ValueError("Підтримуються PDF, фото (JPG, PNG, WebP), Excel і CSV. Фото з iPhone (HEIC) збережіть як JPG.")


# ---------------- зіставлення з даними CRM ----------------

def _product_json(p: Product, r=None) -> dict:
    from .catalog import WAREHOUSES
    k = p.coef if p.coef and p.coef > 0 else 1
    return {"id": p.id, "name": p.name, "sku": p.sku or "", "brand": p.brand or "", "unit": "шт" if k == 1 else f"уп. ({k:g} {p.unit or 'од.'})",
            "coef": k, "wh_key": p.warehouse or "vitrina", "wh": WAREHOUSES.get(p.warehouse or "vitrina"),
            "stock": r.stock if r else None, "price": (r.cost_last or r.cost_avg) if r else (p.cost or 0)}


def match(db: Session, parsed: dict) -> dict:
    from .catalog import match_product, product_rows
    sups = db.scalars(select(Supplier)).all()
    sp = parsed.get("supplier") or {}
    ed, ib = re.sub(r"\D", "", sp.get("edrpou") or ""), re.sub(r"\s", "", sp.get("iban") or "").upper()
    sup = next((s for s in sups if ed and (s.edrpou or "") == ed), None) or \
        next((s for s in sups if ib and (s.iban or "").upper() == ib), None)
    if not sup and sp.get("name"):
        w = set(norm(sp["name"]).split()) - {"тов", "фоп", "пп", "тзов", "ооо"}
        hits = [s for s in sups if w and w & (set(norm(s.name).split()) | set(norm(s.legal_name or "").split()))]
        sup = hits[0] if len(hits) == 1 else None
    rows = {r.product.id: r for r in product_rows(db, [])}
    products = [r.product for r in rows.values()]
    by_sku = {p.sku.strip().lower(): p for p in products if p.sku}
    by_bar = {p.barcode.strip(): p for p in products if p.barcode}
    lines, matched = [], 0
    for ln in parsed.get("lines") or []:
        p = (by_bar.get(str(ln.get("barcode") or "").strip()) or by_sku.get(str(ln.get("sku") or "").strip().lower())
             or match_product(db, ln.get("name") or "", products))
        cands = []
        if not p:
            words = set(norm(ln.get("name") or "").split())
            scored = sorted(((len(words & set(norm(x.name).split())), x) for x in products), key=lambda t: -t[0])
            cands = [_product_json(x, rows.get(x.id)) for sc, x in scored[:4] if sc >= 2]
        else:
            matched += 1
        lines.append({"raw": ln.get("name"), "sku": ln.get("sku") or "", "qty": ln.get("qty") or 0, "price": ln.get("price") or 0,
                      "sum": ln.get("sum") or 0, "expiry": ln.get("expiry") or "",
                      "product": _product_json(p, rows.get(p.id)) if p else None, "candidates": cands})
    return {"supplier": sup.name if sup else (sp.get("name") or ""), "supplier_known": bool(sup),
            "supplier_raw": sp, "number": parsed.get("number") or "", "date": parsed.get("date") or date.today().isoformat(),
            "total": parsed.get("total") or round(sum(x["sum"] for x in lines), 2), "lines": lines,
            "matched": matched, "unmatched": len(lines) - matched}
