"""Прайс постачальника по бренду → закупівельні ціни в «Плануванні ціни реалізації».

Завантаження (Excel .xlsx / .xls / CSV): шукаємо рядок заголовків, колонки «артикул / назва / штрихкод / ціна закупівлі / РРЦ»,
валюту (з заголовків або вибрану). Рядки зіставляються з товарами бренду: артикул → штрихкод → назва.
Прайс зберігається (файл, дата прайсу, дата й час завантаження) і видно в картці постачальника.
«Застосувати» ставить Product.cost_currency / cost_fx (закупівля у валюті) і, за бажанням, rrp_fx (РРЦ)."""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, Supplier, SupplierPrice
from ..parsers.observ import norm

COST_KEYS = ("закуп", "вхідн", "вхідна", "опт", "дилер", "дистриб", "салон", "професійн", "собіварт", "wholesale", "purchase",
             "net price", "dealer", "cost", "закупочн", "оптов")
RRP_KEYS = ("ррц", "роздр", "рекоменд", "retail", "rrp", "msrp", "рц ")
PRICE_KEYS = ("ціна", "цена", "price", "вартість", "стоимость", "prijs", "preis")
SKU_KEYS = ("артикул", "арт.", "код товар", "sku", "article", "art.", "ref", "код")
BAR_KEYS = ("штрих", "barcode", "ean", "штрихкод")
NAME_KEYS = ("назва", "найменування", "наименование", "название", "товар", "продукц", "name", "product", "description", "опис")


def _s(v) -> str:
    return " ".join(str(v if v is not None else "").split())


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    raw = str(v).replace("\xa0", " ").strip()
    if re.search(r"[a-zа-яіїєґ]", re.sub(r"(?i)грн|uah|usd|eur|€|\$|₴", "", raw), re.I):
        return None                                   # «200мл», «20 mL», «Арт. 15» — не ціна
    t = re.sub(r"[^\d,.\-]", "", raw.replace(" ", ""))
    if t.count(",") and t.count("."):
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        x = float(t)
    except ValueError:
        return None
    return x if x > 0 else None


def _cur(text: str) -> str | None:
    t = (text or "").lower()
    if "$" in t or "usd" in t or "дол" in t:
        return "USD"
    if "€" in t or "eur" in t or "євро" in t or "евро" in t:
        return "EUR"
    if "грн" in t or "uah" in t or "₴" in t:
        return "UAH"
    return None


def read_table(data: bytes, filename: str) -> list[list]:
    """Файл → таблиця (усі аркуші підряд). xlsx / xls / csv."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            out += [list(r) for r in ws.iter_rows(values_only=True)]
            out.append([])
        return out
    if name.endswith(".xls"):
        import xlrd
        bk = xlrd.open_workbook(file_contents=data, ignore_workbook_corruption=True)
        out = []
        for sh in bk.sheets():
            out += [sh.row_values(i) for i in range(sh.nrows)]
            out.append([])
        return out
    if name.endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", "ignore")
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        return [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    raise ValueError("Підтримуються прайси Excel (.xlsx, .xls) і CSV")


def _has(cell: str, keys) -> bool:
    return any(k in cell for k in keys)


def _header(row: list) -> dict | None:
    cells = [_s(c).lower() for c in row]
    if sum(1 for c in cells if c) < 2:
        return None
    cols: dict = {}
    for i, c in enumerate(cells):
        if not c:
            continue
        if "bar" not in cols and _has(c, BAR_KEYS):
            cols["bar"] = i
        elif "sku" not in cols and _has(c, SKU_KEYS) and not _has(c, PRICE_KEYS):
            cols["sku"] = i
        elif "name" not in cols and _has(c, NAME_KEYS) and not _has(c, PRICE_KEYS):
            cols["name"] = i
        elif _has(c, RRP_KEYS):
            cols.setdefault("rrp", i)
        elif _has(c, COST_KEYS):
            cols.setdefault("cost", i)
        elif _has(c, PRICE_KEYS):
            cols.setdefault("price", i)
    if "cost" not in cols and "price" in cols:
        cols["cost"] = cols["price"]
    if "name" in cols and "cost" in cols:
        cols["_cur"] = {k: _cur(cells[cols[k]]) for k in ("cost", "rrp") if k in cols}
        cols["_idx"] = {v for k, v in cols.items() if not k.startswith("_")}
        return cols
    return None


def pdf_table(data: bytes) -> list[list]:
    """PDF з текстом (не скан) → таблиця з усіх сторінок; рядки тексту — як запасний варіант. Порожньо — скан, потрібне розпізнавання."""
    import pdfplumber
    out: list = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pg in pdf.pages:
            tbs = pg.extract_tables() or []
            for t in tbs:
                out += [[c for c in r] for r in t if r]
            if not tbs:                                  # таблиця без ліній — колонки з тексту за великими пробілами
                for ln in (pg.extract_text(layout=True) or "").splitlines():
                    cells = [c for c in re.split(r"\s{2,}", ln.strip()) if c]
                    if len(cells) >= 2:
                        out.append(cells)
    return out


def parse(data: bytes, filename: str, table: list | None = None) -> dict:
    """→ {rows: [{name, sku, barcode, cost, rrp}], currency (з заголовків або None), header: {...}}."""
    tab = table if table is not None else read_table(data, filename)
    rows, hdr, cur, rcur = [], None, None, None
    prev = None
    for r in tab:
        txt = r and not any(isinstance(c, (int, float)) and c for c in r) and not any(_num(c) and len(_s(c)) < 12 and re.fullmatch(r"[\d\s.,]+", _s(c)) for c in r)
        h = _header(r) if txt else None
        if not h and txt and prev:                    # заголовок у два рядки (обʼєднані клітинки: «Ціна» / «опт | РРЦ»)
            w = max(len(prev), len(r))
            h = _header([" ".join(filter(None, [_s(prev[i]) if i < len(prev) else "", _s(r[i]) if i < len(r) else ""])) for i in range(w)])
        prev = r if txt else None
        if h:
            hdr = h
            cur = cur or h["_cur"].get("cost")
            rcur = rcur or h["_cur"].get("rrp")
            continue
        if not hdr or not r:
            continue
        taken: set = set()

        def g(k, _r=r, _t=taken):
            """Клітинка колонки k; у PDF (Word) текст часто зсунутий на 1–3 клітинки від заголовка — беремо найближчу непорожню,
            яку ще не взяла інша колонка (назва не може стати ціною)."""
            if k not in hdr:
                return None
            i = hdr[k]
            for d in (0, 1, -1, 2, -2, 3, -3):
                j = i + d
                if 0 <= j < len(_r) and j not in _t and _s(_r[j]) and (d == 0 or j not in hdr["_idx"]):
                    if k in ("cost", "rrp") and _num(_r[j]) is None:
                        continue
                    _t.add(j)
                    return _r[j]
            return None
        name, cost = _s(g("name")), _num(g("cost"))
        if name and not cost and rows and not any(_num(c) for c in r if c is not None and re.search(r"\d[\d\s]*[.,]\d{2}$", _s(c))):
            if not rows[-1].get("name2") and len(name) > 3:      # продовження назви з наступного рядка (укр. назва під англ.)
                rows[-1]["name2"] = name[:300]
            continue
        if not name or not cost:
            continue
        sku = _s(g("sku"))
        if isinstance(g("sku"), float) and g("sku").is_integer():
            sku = str(int(g("sku")))
        bar = g("bar")
        bar = str(int(bar)) if isinstance(bar, float) and bar.is_integer() else _s(bar)
        rows.append({"name": name[:300], "sku": sku[:100], "barcode": bar[:64], "cost": round(cost, 4), "rrp": _num(g("rrp"))})
    if not hdr:
        raise ValueError("Не знайшов у файлі колонки «Назва» і «Ціна». Перевірте, що в прайсі є рядок заголовків.")
    if not rows:
        raise ValueError("У прайсі немає рядків з назвою і ціною")
    return {"rows": rows, "currency": cur, "rrp_currency": rcur}


def _k(v: str | None) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]", "", (v or "").lower())


_UNIT = {"мл": "ml", "ml": "ml", "г": "g", "гр": "g", "g": "g", "шт": "pcs", "pcs": "pcs"}


def _tok(v: str | None) -> set:
    t = re.sub(r"(\d)([a-zа-яії]+)", r"\1 \2", (v or "").lower().replace("®", " ").replace("™", " "))
    out = set()
    for w in norm(t).lower().replace("%", " ").split():
        w = _UNIT.get(w, w)
        if len(w) > 1 or w.isdigit():
            out.add(w)
    return out


def _sim(a: set, b: set) -> float:
    """Схожість назв: Жаккар або «одна назва повністю в іншій» (≥ 3 спільні слова); обʼєм / числа мають збігатися."""
    da, dbb = {w for w in a if w.isdigit()}, {w for w in b if w.isdigit()}
    if da and dbb and da != dbb:
        return 0.0
    common = len(a & b)
    jac = common / len(a | b) if a | b else 0.0
    cont = common / min(len(a), len(b)) if a and b and common >= 3 else 0.0
    return max(jac, 0.85 * cont)


def match(db: Session, brand: str | None, rows: list[dict]) -> list[dict]:
    """Зіставити рядки прайсу з товарами бренду (активні): артикул → штрихкод → назва (схожість ≥ 75 %)."""
    q = select(Product).where(Product.active.is_not(False))
    if brand:
        q = q.where(Product.brand == brand)
    prods = db.scalars(q).all()
    by_sku: dict = {}
    by_bar: dict = {}
    for p in prods:
        if p.sku and _k(p.sku):
            by_sku.setdefault(_k(p.sku), p)
        if p.barcode and _k(p.barcode):
            by_bar.setdefault(_k(p.barcode), p)
    bw = (norm(brand or "").lower().split() or [""])[0]
    bt = {bw} if bw else set()                                   # перше слово бренду (ZO, AG…) не впливає на схожість
    toks = [(p, _tok(p.name) - bt) for p in prods]
    used: set = set()
    out = []
    for r in rows:
        p, how = None, None
        if r.get("sku") and _k(r["sku"]) in by_sku:
            p, how = by_sku[_k(r["sku"])], "артикул"
        elif r.get("barcode") and _k(r["barcode"]) in by_bar:
            p, how = by_bar[_k(r["barcode"])], "штрихкод"
        else:
            ts = [t for t in (_tok(r["name"]) - bt, _tok(r.get("name2")) - bt) if t]
            best, sc = None, 0.0
            for pp, pt in toks:
                if not ts or not pt or pp.id in used:
                    continue
                j = max(_sim(t, pt) for t in ts)
                if j > sc:
                    best, sc = pp, j
            if best and sc >= 0.75:
                p, how = best, f"назва {round(sc * 100)}%"
        if p:
            used.add(p.id)
        out.append({**r, "pid": p.id if p else None, "how": how})
    return out


def default_supplier(db: Session, brand: str | None) -> Supplier | None:
    """Основний постачальник бренду — найчастіший Product.supplier серед товарів бренду."""
    if not brand:
        return None
    c = Counter(s for (s,) in db.execute(select(Product.supplier).where(Product.brand == brand, Product.supplier.is_not(None))) if s)
    if not c:
        return None
    name = c.most_common(1)[0][0].strip().lower()
    return next((s for s in db.scalars(select(Supplier)) if s.name.strip().lower() == name), None)


PROMPT = """Це прайс-лист постачальника косметики (PDF або фото), можливо кілька сторінок. Перепиши ВСІ товарні рядки.
Поверни ЛИШЕ JSON без пояснень:
{"currency": "EUR | USD | UAH", "rrp_currency": "EUR | USD | UAH або порожньо",
 "rows": [{"name": "", "sku": "", "barcode": "", "cost": 0, "rrp": 0}]}
Правила: name — назва товару з обʼємом; sku — артикул / код, якщо є; barcode — штрихкод, якщо є;
cost — ціна ЗАКУПІВЛІ для салону / опт / дилер / «ціна» (якщо колонка одна); rrp — рекомендована роздрібна ціна (РРЦ), якщо є, інакше 0.
currency — валюта цін закупівлі ($ / USD, € / EUR, грн / UAH); rrp_currency — якщо РРЦ в іншій валюті.
Пропускай заголовки розділів, порожні й підсумкові рядки. Числа — числами, крапка як роздільник. Не вигадуй: невідоме — порожньо або 0."""
DOC_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".webp")


def _doc_mime(filename: str) -> str | None:
    n = (filename or "").lower()
    return {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
        next((e for e in DOC_EXT if n.endswith(e)), ""), None)


def _fill(db: Session, sp: SupplierPrice, res: dict) -> None:
    """Розпізнані рядки → валюта, РРЦ у валюті закупівлі, зіставлення з товарами бренду."""
    cur = sp.currency if sp.cur_forced and sp.currency in ("USD", "EUR", "UAH") else (res.get("currency") or sp.currency or "UAH")
    rows = match(db, sp.brand, res["rows"])
    rc = res.get("rrp_currency")
    if rc and rc != cur:                                       # РРЦ в іншій валюті (напр. закупівля в EUR, РРЦ у грн) — у валюту закупівлі
        from .pricing import rates
        rt = rates(db)
        for r in rows:
            if r.get("rrp"):
                r["rrp"] = round(r["rrp"] * rt.get(rc, 1.0) / (rt.get(cur, 1.0) or 1.0), 2)
                r["rrp_src"] = f"{rc}"
    sp.currency, sp.rows, sp.n_rows, sp.n_matched = cur, rows, len(rows), sum(1 for r in rows if r["pid"])


def _norm_ai(j: dict) -> dict:
    """Відповідь агента / Claude → {rows, currency, rrp_currency} (як у parse)."""
    j = j if isinstance(j, dict) else {}
    cu = lambda v: v if v in ("USD", "EUR", "UAH") else _cur(str(v or ""))    # noqa: E731
    rows = []
    for r in j.get("rows") or j.get("lines") or []:
        if not isinstance(r, dict):
            continue
        name, cost = _s(r.get("name")), _num(r.get("cost") if r.get("cost") is not None else r.get("price"))
        if name and cost:
            rows.append({"name": name[:300], "sku": _s(r.get("sku"))[:100], "barcode": _s(r.get("barcode"))[:64],
                         "cost": round(cost, 4), "rrp": _num(r.get("rrp"))})
    if not rows:
        raise ValueError("У прайсі не знайдено рядків з назвою і ціною")
    return {"rows": rows, "currency": cu(j.get("currency")), "rrp_currency": cu(j.get("rrp_currency"))}


def _claude_read(db: Session, data: bytes, mime: str, claude=None) -> dict:
    import base64
    import json as _json
    import httpx
    from . import invoice_ai
    if claude:
        return claude(data, mime, PROMPT)
    key = invoice_ai.api_key(db)
    if not key:
        raise ValueError("Для PDF-прайсу потрібен агент на VPS або ключ Claude API («Налаштування → Розпізнавання накладних»)")
    block = {"type": "document" if mime == "application/pdf" else "image",
             "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
    r = httpx.post(invoice_ai.API, timeout=300, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   json={"model": invoice_ai.model(), "max_tokens": 16000,
                         "messages": [{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}]})
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
    out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("Claude API: відповідь без JSON")
    return _json.loads(m.group(0))


def create(db: Session, data: bytes, filename: str, brand: str | None, supplier_id: int | None, currency: str | None,
           price_date: date | None, user_id: int | None, claude=None) -> SupplierPrice:
    """Excel / CSV — розбір одразу; PDF / фото — через агента на VPS (черга /api/v1/invoices, kind=price) або Claude API, як накладні."""
    from . import ingest, invoice_ai
    mime = _doc_mime(filename)
    res = None if mime else parse(data, filename)               # помилка формату — одразу, без запису
    if mime == "application/pdf":                                # PDF з текстом — розбираємо самі, без агента
        try:
            res = parse(data, filename, pdf_table(data))
        except Exception:  # noqa: BLE001 — скан або без заголовків колонок → агент / Claude
            res = None
    if not supplier_id:
        s = default_supplier(db, brand)
        supplier_id = s.id if s else None
    forced = currency in ("USD", "EUR", "UAH")
    sp = SupplierPrice(supplier_id=supplier_id, brand=brand or None, file_path=ingest.store(data, filename or "prais"),
                       file_name=(filename or "прайс")[:300], price_date=price_date or date.today(), currency=currency if forced else None,
                       cur_forced=forced, uploaded_at=datetime.now(), user_id=user_id, rows=[], n_rows=0, n_matched=0)
    db.add(sp)
    if res is not None:
        _fill(db, sp, res)
    elif not claude and invoice_ai.engine(db) == "agent":
        sp.status = "recognizing"
        db.flush()
        j = invoice_ai.queue(db, data, filename, mime, user_id)
        j.kind, j.ref_id = "price", sp.id
    else:
        try:
            _fill(db, sp, _norm_ai(_claude_read(db, data, mime, claude)))
        except Exception as e:  # noqa: BLE001
            sp.status, sp.error = "error", f"Не розпізнано: {str(e)[:300]}"
    db.commit()
    return sp


def retry(db: Session, sp: SupplierPrice) -> SupplierPrice:
    """Ще раз розібрати збережений файл: PDF з текстом — самі; інакше Claude API, якщо є ключ. Без результату — лишається в агента."""
    from pathlib import Path
    from . import invoice_ai
    data = Path(sp.file_path).read_bytes() if sp.file_path and Path(sp.file_path).exists() else b""
    if not data:
        sp.status, sp.error = "error", "Файл прайсу недоступний"
        db.commit()
        return sp
    res, why = None, None
    mime = _doc_mime(sp.file_name or "")
    try:
        if mime == "application/pdf":
            tab = pdf_table(data)
            if not tab:
                why = "у PDF немає тексту — це скан / картинка, потрібне розпізнавання (агент або Claude API)"
            else:
                try:
                    res = parse(data, sp.file_name, tab)
                except ValueError as e:
                    sample = " | ".join(" ; ".join(_s(c) for c in r if _s(c))[:120] for r in tab[:6] if any(_s(c) for c in r))
                    why = f"{e} Текст у PDF є ({len(tab)} рядків), початок: {sample[:400]}"
        elif mime:
            why = "фото прайсу — потрібне розпізнавання (агент або Claude API)"
        else:
            res = parse(data, sp.file_name)
    except Exception as e:  # noqa: BLE001
        why = f"не вдалося прочитати файл: {str(e)[:200]}"
    if res is None and mime and invoice_ai.api_key(db):
        try:
            res = _norm_ai(_claude_read(db, data, mime))
        except Exception as e:  # noqa: BLE001
            why = f"Claude API: {str(e)[:200]}"
    if res is not None:
        _fill(db, sp, res)
        sp.status, sp.error = None, None
        from ..models import InvoiceJob
        for j in db.scalars(select(InvoiceJob).where(InvoiceJob.kind == "price", InvoiceJob.ref_id == sp.id,
                                                     InvoiceJob.status.in_(("pending", "taken")))):
            j.status, j.error, j.done_at = "error", "розібрано в CRM без агента", datetime.now()   # агенту більше не віддаємо
    else:
        sp.error = ("Без агента не розібрано: " + (why or "невідома причина"))[:500]
    db.commit()
    return sp


def apply_agent(db: Session, sp: SupplierPrice, result: dict | None, error: str | None = None) -> SupplierPrice:
    """Відповідь агента по PDF / фото прайсу → рядки й зіставлення."""
    if sp.status != "recognizing":
        return sp
    try:
        if error and not result:
            raise ValueError(error)
        _fill(db, sp, _norm_ai(result or {}))
        sp.status, sp.error = None, None
    except Exception as e:  # noqa: BLE001
        sp.status, sp.error = "error", f"Не розпізнано агентом: {str(e)[:300]}"
    db.commit()
    return sp


def preview(db: Session, sp: SupplierPrice) -> list[dict]:
    """Рядки прайсу з товаром CRM і тим, що зміниться."""
    ids = [r["pid"] for r in sp.rows or [] if r.get("pid")]
    prods = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))} if ids else {}
    out = []
    for i, r in enumerate(sp.rows or []):
        p = prods.get(r.get("pid"))
        same = bool(p and p.cost_currency == sp.currency and p.cost_fx and abs(p.cost_fx - r["cost"]) < 0.005
                    and (not r.get("rrp") or (p.rrp_fx and abs(p.rrp_fx - r["rrp"]) < 0.005)))
        out.append({"i": i, **r, "p": p, "old_cur": p.cost_currency if p else None, "old": p.cost_fx if p else None,
                    "old_rrp": p.rrp_fx if p else None, "same": same})
    return out


def apply(db: Session, sp: SupplierPrice, idx: list[int], with_rrp: bool = True, user_id: int | None = None) -> int:
    """Підставити закупівлю у валюті (і РРЦ) вибраним товарам; стан «до» — в історію (можна відкотити)."""
    from . import price_history as ph
    rows = sp.rows or []
    before, changed = {}, []
    for i in idx:
        if not (0 <= i < len(rows)) or not rows[i].get("pid"):
            continue
        p = db.get(Product, rows[i]["pid"])
        if not p:
            continue
        before.setdefault(p.id, ph.state(p))
        p.cost_currency, p.cost_fx = sp.currency or "UAH", rows[i]["cost"]
        if with_rrp and rows[i].get("rrp"):
            p.rrp_fx = rows[i]["rrp"]
        changed.append(p)
    sp.applied_at, sp.n_applied = datetime.now(), len(changed)
    db.commit()
    ph.record(db, "price", before, changed, user_id, sp.brand, sp.supplier_id, sp.id,
              note=f"Прайс {sp.file_name or ''} від {sp.price_date:%d.%m.%Y}" if sp.price_date else f"Прайс {sp.file_name or ''}")
    return len(changed)


def undo(db: Session, sp: SupplierPrice, user_id: int | None = None) -> int:
    """Скасувати підстановку з цього прайсу: з історії (стан «до»), а для старих підстановок без історії — прибрати
    закупівлю у валюті й РРЦ, якщо в товарі досі значення з цього прайсу."""
    from ..models import PriceSnapshot
    from . import price_history as ph
    sn = db.scalars(select(PriceSnapshot).where(PriceSnapshot.kind == "price", PriceSnapshot.price_id == sp.id)
                    .order_by(PriceSnapshot.id.desc())).first()
    if sn:
        n = ph.restore(db, sn, "before", user_id)
    else:
        before, changed = {}, []
        for r in sp.rows or []:
            p = db.get(Product, r["pid"]) if r.get("pid") else None
            if not p or p.cost_fx is None or abs((p.cost_fx or 0) - r["cost"]) > 0.005 or (p.cost_currency or "UAH") != (sp.currency or "UAH"):
                continue
            before[p.id] = ph.state(p)
            p.cost_fx = None
            if r.get("rrp") and p.rrp_fx and abs(p.rrp_fx - r["rrp"]) < 0.005:
                p.rrp_fx = None
            changed.append(p)
        db.commit()
        ph.record(db, "undo", before, changed, user_id, sp.brand, sp.supplier_id, sp.id, note=f"Скасовано прайс {sp.file_name or ''}")
        n = len(changed)
    sp.applied_at, sp.n_applied = None, None
    db.commit()
    return n


def set_match(db: Session, sp: SupplierPrice, i: int, pid: int | None) -> None:
    rows = [dict(r) for r in sp.rows or []]
    if 0 <= i < len(rows):
        rows[i]["pid"], rows[i]["how"] = pid, ("вручну" if pid else None)
        sp.rows = rows
        sp.n_matched = sum(1 for r in rows if r.get("pid"))
        db.commit()


def for_supplier(db: Session, sup: Supplier) -> list[SupplierPrice]:
    return db.scalars(select(SupplierPrice).where(SupplierPrice.supplier_id == sup.id).order_by(SupplierPrice.uploaded_at.desc()).limit(20)).all()


def latest_by_brand(db: Session) -> dict[str, SupplierPrice]:
    out: dict = {}
    for sp in db.scalars(select(SupplierPrice).order_by(SupplierPrice.uploaded_at.desc()).limit(300)):
        if sp.brand and sp.brand not in out:
            out[sp.brand] = sp
    return out
