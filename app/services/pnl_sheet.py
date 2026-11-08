"""P&L з Google-таблиці власника («PnL 2025», «PnL 2026 Гібрид») — імпорт і показ поруч із розрахунком CRM.

Формат: CSV-експорт аркуша (або текстовий експорт книги з рядками «## Sheet name: …»): рядок із місяцями
(Січень … Грудень), далі рядки «назва, 12 значень[, разом]». Рядки ВЕЛИКИМИ ЛІТЕРАМИ — розділи (ДОХІД, СОБІВАРТІСТЬ, …).
Зберігається в SyncState `pnl_sheet` як {рік: {"rows": [{"name","vals"[12],"kind"}], "imported_at", "sheet"}}.
Вперше заповнюється з pnl_sheet_seed.json (вивантаження власника 08.11.2026), далі — завантаженням файлу у звіті."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "pnl_sheet"
LAST_SHEET_YEAR = 2026        # рішення власника 08.11.2026: 2025–2026 доводимо гібридом (таблиця + CRM), з 2027 — лише розрахунок CRM
MONTHS_UA = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
STOP_RE = re.compile(r"^(відцоток по послугам|відсоток по послугам|прибуток по катег|валовий внесок)", re.I)
GROUP_NAMES = ("послуги загалом", "косметика", "абонементи", "поповнення балансу", "собівартість послуг", "оренда", "зарплата", "макетинг", "маркетинг",
               "реклама", "програми та сервіси", "банки та комісії", "господарські витрати", "податки фоп", "витрати на персонал", "інше")
KEY_ROWS = {"rev": r"^дохід \(revenue\)", "cogs": r"^собівартість \(cost", "gross": r"^валовий прибуток \(gross", "opex": r"^операційні витрати \(opex",
            "op": r"^операційний прибуток", "capex": r"^амортизація та капітальні", "oneoff": r"^разові витрати\s*$", "net": r"^чистий прибуток \(net"}


def _num(v) -> float | None:
    s = str(v or "").strip().replace(" ", "").replace(" ", "")
    if not s or s.startswith("#"):
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse(text: str) -> dict[int, dict]:
    """Текст файлу → {рік: {"rows": [...], "sheet": назва}} для аркушів P&L (назва містить PnL/P&L і рік)."""
    out: dict[int, dict] = {}
    chunks: list[tuple[str, str]] = []
    if "## Sheet name:" in text:
        parts = re.split(r"^## Sheet name:\s*(.*)$", text, flags=re.M)
        for i in range(1, len(parts) - 1, 2):
            chunks.append((parts[i].strip(), parts[i + 1]))
    else:
        chunks.append(("", text))
    for name, body in chunks:
        yr = re.search(r"20\d\d", name)
        if name and not (re.search(r"p\s*&?\s*n\s*l", name, re.I) and yr):
            continue
        rows = _parse_sheet(body)
        if not rows:
            continue
        year = int(yr.group(0)) if yr else _guess_year(body)
        if not year:
            continue
        # «нова модель» / «Вересень» / «Послуга» — не повний P&L: беремо аркуш з найбільшою кількістю рядків на рік
        if year in out and len(out[year]["rows"]) >= len(rows) and "гібрид" not in name.lower():
            continue
        out[year] = {"rows": rows, "sheet": name or "P&L"}
    return out


def _guess_year(body: str) -> int | None:
    m = re.search(r"(20\d\d)\s*рік", body)
    return int(m.group(1)) if m else None


def _parse_sheet(body: str) -> list[dict]:
    rdr = csv.reader(io.StringIO(body.strip("\n")))
    rows, started, idx = [], False, []
    for rec in rdr:
        if not rec:
            if started and rows and rows[-1]["kind"] != "blank":
                rows.append({"name": "", "vals": [0.0] * 12, "kind": "blank"})
            continue
        cells = [c.strip() for c in rec]
        low = [c.lower() for c in cells]
        if not started:
            if "січень" in low and "грудень" in low:
                started = True
                idx = [low.index(m) if m in low else None for m in MONTHS_UA]
                if any(i is None for i in idx):           # «Лютий » з пробілом тощо
                    idx = [next((j for j, c in enumerate(low) if c.strip() == m), None) for m in MONTHS_UA]
                mi = [i for i in idx if i is not None]
                if len(mi) < 12:
                    return []
            continue
        name = cells[0]
        if not name:
            continue
        if STOP_RE.search(name):
            break
        vals = [(_num(cells[i]) if i is not None and i < len(cells) else None) or 0.0 for i in idx]
        if not any(vals) and name.lower().strip() not in ("інше", "інші") and not name.isupper():
            if not any(k in name.lower() for k in GROUP_NAMES):
                continue
        base = re.sub(r"\(.*?\)", "", name).strip()
        is_key = any(re.search(rx, name.strip(), re.I) for rx in KEY_ROWS.values())
        kind = "head" if (is_key or (base.isupper() and re.search(r"[А-ЯІЇЄҐ]{4,}", base) and len(base.split()) >= 2 and len(base) >= 12
                                     and "reset" not in name.lower())) else (
            "group" if any(name.lower().strip().startswith(k) for k in GROUP_NAMES) else "line")
        rows.append({"name": name, "vals": [round(v, 2) for v in vals], "kind": kind})
    while rows and rows[-1]["kind"] == "blank":
        rows.pop()
    return rows


def load(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {int(k): v for k, v in d.items()}


def save(db: Session, data: dict[int, dict], source: str = "upload") -> None:
    cur = load(db)
    now = datetime.now().isoformat(timespec="seconds")
    for y, v in data.items():
        cur[int(y)] = {**v, "imported_at": now, "source": source}
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps({str(k): v for k, v in cur.items()}, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def import_text(db: Session, text: str, source: str = "upload") -> dict:
    data = parse(text)
    if not data:
        raise ValueError("У файлі не знайдено аркуша P&L з рядком місяців (Січень … Грудень)")
    save(db, data, source)
    return {y: len(v["rows"]) for y, v in data.items()}


SEED = Path(__file__).with_name("pnl_sheet_seed.json")


def ensure_seed(db: Session) -> bool:
    """Перший запуск: таблиця власника (вивантаження 08.11.2026) — якщо в БД ще нічого немає."""
    if load(db) or not SEED.exists():
        return False
    try:
        data = json.loads(SEED.read_text(encoding="utf-8"))
    except ValueError:
        return False
    save(db, {int(k): v for k, v in data.items()}, source="seed")
    return True


def year_view(db: Session, year: int) -> dict | None:
    d = load(db).get(year)
    if not d:
        return None
    rows = d["rows"]
    key = {}
    for k, rx in KEY_ROWS.items():
        for r in rows:
            if re.search(rx, r["name"].strip(), re.I):
                key[k] = r["vals"]
                break
    months = [i for i in range(12) if any(r["vals"][i] for r in rows)]
    return {"year": year, "rows": [dict(r, total=round(sum(r["vals"]), 2)) for r in rows], "key": key, "months": months,
            "sheet": d.get("sheet"), "imported_at": d.get("imported_at"), "source": d.get("source")}


# ---------------- гібрид: до старту обліку в CRM — таблиця власника, далі — розрахунок CRM ----------------
import unicodedata  # noqa: E402

ROW_MAP = [  # (regex назви рядка таблиці, поле CRM, підполе)
    (r"^дохід \(revenue\)", "rev", None), (r"^послуги загалом", "rev_s", None), (r"^косметика\s*$", "rev_p", None),
    (r"^собівартість \(cost", "cogs", None), (r"^собівартість послуг", "cogs_serv", None), (r"^1\.\s*витратні матеріали", "mat", None),
    (r"^2\.\s*оплата роботи", "labor", None), (r"^4\.\s*косметика", "cogs_p", None), (r"^5\.\s*від[цс]оток по косметиці", "cosm_pct", None),
    (r"^валовий прибуток \(gross", "gross", None), (r"^валовий прибуток по послугах", "gross_s", None),
    (r"^операційні витрати \(opex", "opex", None), (r"^оренда\s*$", "opex_g", "rent"), (r"^зарплата\s*$", "opex_g", "salary"),
    (r"^ма[кр]етинг\s*$", "opex_g", "marketing"), (r"^реклама\s*$", "opex_g", "ads"), (r"^програми та сервіси", "opex_g", "software"),
    (r"^банки та комісії", "opex_g", "bank"), (r"^господарські витрати\s*$", "opex_g", "household"), (r"^податки фоп", "opex_g", "tax"),
    (r"^витрати на персонал", "opex_g", "staff"), (r"^операційний прибуток", "op", None), (r"^амортизація та капітальні", "capex", None),
    (r"^разові витрати\s*$", "oneoff", None), (r"^чистий прибуток \(net", "net", None),
    (r"^нові пацієнти", "k_new", None), (r"^кількість пацієнтів", "k_pat", None), (r"^кількість візитів", "k_vis", None),
    (r"^середня кількість візитів", "k_vpp", None), (r"^середній чек послуги", "k_avg", None), (r"^ltv", "k_ltv", None),
    (r"^cogs\s*/\s*revenue", "cogs_pct", None), (r"^операційні витрати\s*/", "opex_pct", None), (r"^частка маркетингу", "k_mkt_pct", None),
    (r"^cpa\s+маркетинг", "k_cpa", None), (r"^маркетинг\s*/\s*gross", "k_mkt_gp", None), (r"^валова маржа", "gross_pct", None),
    (r"^точка беззбитковості", "k_breakeven", None), (r"^рентабельність бізнесу", "net_pct", None), (r"^маржинальність косметики", "gross_p_pct", None),
]
_ROW_RX = [(re.compile(rx, re.I), f, sub) for rx, f, sub in ROW_MAP]
PCT_FIELDS = {"cogs_pct", "opex_pct", "k_mkt_pct", "k_mkt_gp", "gross_pct", "net_pct", "gross_p_pct"}
SKIP_SECTIONS = {"opex_detail"}


def _norm_name(s: str, keep_paren: bool = False) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ʼ", "'").replace("’", "'")
    if not keep_paren:
        s = re.sub(r"\(.*?\)", " ", s)
    return re.sub(r"[^a-zа-яіїєґ0-9]+", " ", s).strip()


def _match_dict(name: str, d: dict) -> str | None:
    """Назва рядка таблиці → ключ словника CRM (категорія послуги / лікар): збіг за токенами."""
    n = _norm_name(name)
    if not n:
        return None
    toks = [t for t in n.split() if len(t) > 2]
    best, score = None, 0
    for k in d:
        kn = _norm_name(k)
        if not kn:
            continue
        if kn == n or kn.startswith(n) or n.startswith(kn):
            return k
        ktoks = set(kn.split())
        sc = sum(1 for t in toks if t in ktoks or any(x.startswith(t[:5]) for x in ktoks if len(t) >= 5))
        if sc > score:
            best, score = k, sc
    return best if score and score >= max(1, len(toks) // 2) else None


def _score(sheet_name: str, key: str) -> int:
    n, kn0, kn = _norm_name(sheet_name), _norm_name(key), _norm_name(key, keep_paren=True)
    if not n or not kn:
        return 0
    if kn0 == n:
        return 100
    if kn0.startswith(n) or n.startswith(kn0):
        return 50
    toks = [t for t in n.split() if len(t) > 2]
    ktoks = set(kn.split())
    sc = sum(1 for t in toks if t in ktoks or any(x.startswith(t[:5]) for x in ktoks if len(t) >= 5))
    return sc if sc >= max(1, len(toks) // 2) else 0


def assign(sheet_names: list[str], keys: list[str]) -> dict[str, list[str]]:
    """Кожен ключ CRM (категорія / лікар) — до одного рядка таблиці з найкращим збігом; кілька ключів можуть лягти в один рядок
    (напр. «Ботулінотерапія (Xeomin…)» і «Ботокс/Диспорт/Нейронокс» → «Ботулінотерапія»)."""
    out: dict[str, list[str]] = {n: [] for n in sheet_names}
    for k in keys:
        best, bs = None, 0
        for n in sheet_names:
            sc = _score(n, k)
            if sc > bs:
                best, bs = n, sc
        if best is not None and bs > 0:
            out[best].append(k)
    return out


def hybrid(db: Session, year: int, crm: dict | None) -> dict | None:
    """Рядки таблиці власника, де місяці з дати старту обліку в CRM замінені розрахунком CRM.
    crm — результат pnl_monthly.build(db, year) (або None). Повертає None, якщо таблиці за рік немає."""
    from .autonomy import since as _since
    sh = year_view(db, year)
    if not sh:
        return None
    s_ = _since(db)
    crm_from = None
    if crm and s_:
        crm_from = s_.month if s_.year == year else (1 if s_.year < year else None)
    crm_rows = {r["m"]: r for r in (crm or {}).get("rows", [])}
    months_src = []
    for i in range(12):
        mk = f"{year}-{i + 1:02d}"
        months_src.append("crm" if (crm_from and i + 1 >= crm_from and mk in crm_rows) else ("sheet" if i in sh["months"] else "none"))
    # зіставлення рядків таблиці (напрямки / лікарі) з ключами CRM — один раз на рік
    def _sec_names(start_rx: str, stop_rx: str) -> list[str]:
        names, on = [], False
        for rr in sh["rows"]:
            nm = rr["name"].strip()
            if re.search(start_rx, nm, re.I):
                on = True
                continue
            if on and (re.search(stop_rx, nm, re.I) or rr["kind"] == "head"):
                break
            if on and rr["kind"] == "line" and nm:
                names.append(rr["name"])
        return names
    keys_rev = sorted({k for x in crm_rows.values() for k in x.get("rev_cat", {})})
    keys_cogs = sorted({k for x in crm_rows.values() for k in x.get("cogs_cat", {})})
    keys_doc = sorted({k for x in crm_rows.values() for k in x.get("labor_doc", {})})
    amap = {"rev_cat": assign(_sec_names(r"^послуги загалом", r"^косметика\s*$"), keys_rev),
            "cogs_cat": assign(_sec_names(r"^1\.\s*витратні", r"^2\."), keys_cogs),
            "labor_doc": assign(_sec_names(r"^2\.\s*оплата", r"^4\."), keys_doc)}
    unmatched = {sec: [k for k in keys if not any(k in v for v in amap[sec].values())]
                 for sec, keys in (("rev_cat", keys_rev), ("cogs_cat", keys_cogs), ("labor_doc", keys_doc))}
    section = None
    out_rows = []
    for r in sh["rows"]:
        name = r["name"].strip()
        low = name.lower()
        field = sub = None
        for rx, f, sb in _ROW_RX:
            if rx.search(name):
                field, sub = f, sb
                break
        if r["kind"] == "head":
            if low.startswith("дохід"):
                section = "rev"
            elif low.startswith("собівартість"):
                section = "cogs"
            elif low.startswith("операційні витрати"):
                section = "opex"
            else:
                section = "other"
        if field is None and r["kind"] == "line" and section in ("rev", "cogs"):
            # категорія послуги / лікар — за збігом назви
            pass
        if field == "rev_s":
            section = "rev_cat"
        elif field == "mat":
            section = "cogs_cat"
        elif field == "labor":
            section = "labor_doc"
        elif field in ("rev_p", "cogs_p", "cosm_pct", "cogs_serv", "gross", "opex"):
            section = "other" if field in ("gross", "opex") else section
        vals = list(r["vals"])
        srcs = []
        for i, src in enumerate(months_src):
            mk = f"{year}-{i + 1:02d}"
            if src != "crm":
                srcs.append(src)
                continue
            x = crm_rows[mk]
            v = None
            if field:
                v = x.get(field)
                if sub is not None and isinstance(v, dict):
                    v = v.get(sub, 0.0)
            elif r["kind"] == "line" and section in amap:
                ks = amap[section].get(r["name"]) or []
                v = sum(x[section].get(k, 0.0) for k in ks) if ks else 0.0
            if v is None:
                vals[i] = 0.0
                srcs.append("na")
            else:
                vals[i] = round(float(v), 2)
                srcs.append("crm")
        tot = round(sum(vals), 2)
        if field in PCT_FIELDS or field in ("k_vpp", "k_avg", "k_ltv", "k_cpa", "k_breakeven"):
            nz = [v for v, s in zip(vals, srcs) if s in ("sheet", "crm") and v]
            tot = round(sum(nz) / len(nz), 2) if nz else 0.0
        out_rows.append({"name": r["name"], "kind": r["kind"], "vals": vals, "srcs": srcs, "total": tot, "field": field})
    key = {}
    for f in ("rev", "cogs", "gross", "opex", "op", "capex", "oneoff", "net"):
        for rr in out_rows:
            if rr["field"] == f:
                key[f] = rr["vals"]
                break
    return {"year": year, "rows": out_rows, "months_src": months_src, "crm_from": crm_from, "key": key, "sheet": sh["sheet"], "amap": amap,
            "unmatched": unmatched,
            "imported_at": sh["imported_at"], "source": sh["source"], "n_months": sum(1 for s in months_src if s != "none")}


# ---------------- напрямки: частка в послугах, прибуток по напрямках, валовий внесок ----------------
def directions(cats: list[dict], labor: list[float], cosm: list[tuple]) -> dict:
    """cats — 12 словників {напрямок: (дохід, матеріали)}; labor — оплата роботи лікарів по місяцях (розподіляється між напрямками
    пропорційно доходу); cosm — 12 кортежів (дохід косметики, закупівельна вартість, % лікарям).
    Повертає рядки по напрямках: дохід, частка в послугах, матеріали, оплата роботи (розподіл), прибуток, маржа, внесок у валовий."""
    names: list[str] = []
    for d in cats:
        for k in d:
            if k not in names:
                names.append(k)
    svc_rev = [sum(v[0] for v in d.values()) for d in cats]
    rows = []
    for n in names:
        rev = [float(cats[m].get(n, (0, 0))[0]) for m in range(12)]
        mat = [float(cats[m].get(n, (0, 0))[1]) for m in range(12)]
        lab = [round(labor[m] * rev[m] / svc_rev[m], 2) if svc_rev[m] else 0.0 for m in range(12)]
        prof = [round(rev[m] - mat[m] - lab[m], 2) for m in range(12)]
        rows.append({"name": n, "rev": rev, "mat": mat, "lab": lab, "profit": prof, "kind": "service"})
    c_rev = [float(c[0]) for c in cosm]
    c_cost = [float(c[1]) for c in cosm]
    c_pct = [float(c[2]) for c in cosm]
    rows.append({"name": "Косметика", "rev": c_rev, "mat": c_cost, "lab": c_pct, "profit": [round(c_rev[m] - c_cost[m] - c_pct[m], 2) for m in range(12)],
                 "kind": "product"})
    tot_rev = [svc_rev[m] + c_rev[m] for m in range(12)]
    tot_prof = [sum(r["profit"][m] for r in rows) for m in range(12)]
    for r in rows:
        r["share"] = [round(100 * r["rev"][m] / tot_rev[m], 2) if tot_rev[m] else None for m in range(12)]        # частка в доході
        r["margin"] = [round(100 * r["profit"][m] / r["rev"][m], 1) if r["rev"][m] else None for m in range(12)]  # маржа напрямку
        r["contrib"] = [round(100 * r["profit"][m] / tot_prof[m], 2) if tot_prof[m] else None for m in range(12)]  # внесок у валовий
        r["t_rev"], r["t_mat"], r["t_lab"], r["t_profit"] = (round(sum(r[k]), 2) for k in ("rev", "mat", "lab", "profit"))
    T_rev, T_prof = round(sum(tot_rev), 2), round(sum(tot_prof), 2)
    for r in rows:
        r["t_share"] = round(100 * r["t_rev"] / T_rev, 2) if T_rev else None
        r["t_margin"] = round(100 * r["t_profit"] / r["t_rev"], 1) if r["t_rev"] else None
        r["t_contrib"] = round(100 * r["t_profit"] / T_prof, 2) if T_prof else None
    rows.sort(key=lambda r: (r["kind"] == "product", -r["t_rev"]))
    return {"rows": rows, "tot_rev": tot_rev, "tot_prof": tot_prof, "svc_rev": svc_rev, "T_rev": T_rev, "T_prof": T_prof,
            "active": [m for m in range(12) if tot_rev[m]]}


def directions_from_hybrid(hy: dict) -> dict:
    """Напрямки з рядків гібриду: дохід — рядки під «Послуги загалом», матеріали — під «1. Витратні матеріали» (за назвою)."""
    rows = hy["rows"]

    def block(start_rx, stop_rx):
        out, on = {}, False
        for r in rows:
            nm = r["name"].strip()
            if re.search(start_rx, nm, re.I):
                on = True
                continue
            if on and (re.search(stop_rx, nm, re.I) or r["kind"] == "head"):
                break
            if on and r["kind"] == "line" and nm and not re.search(r"абонемент|сертифік|поповненн|баланс", nm, re.I):
                out[nm] = r["vals"]
        return out
    rev = block(r"^послуги загалом", r"^косметика\s*$")
    mat = block(r"^1\.\s*витратні", r"^2\.")
    mat_keys = list(mat)
    cats = []
    for m in range(12):
        d = {}
        for n, vals in rev.items():
            mk = n if n in mat else next((k for k in mat_keys if _score(n, k) >= 50 or _score(k, n) >= 50), None)
            d[n] = (vals[m], mat[mk][m] if mk else 0.0)
        cats.append(d)

    def vals(field):
        for r in rows:
            if r.get("field") == field:
                return r["vals"]
        return [0.0] * 12
    labor, c_rev, c_cost, c_pct = vals("labor"), vals("rev_p"), vals("cogs_p"), vals("cosm_pct")
    return directions(cats, labor, [(c_rev[m], c_cost[m], c_pct[m]) for m in range(12)])


def directions_from_crm(crm: dict) -> dict:
    """Напрямки з розрахунку CRM (pnl_monthly.build): rev_cat / cogs_cat по місяцях."""
    by = {int(r["m"][5:]) - 1: r for r in crm["rows"]}
    cats, labor, cosm = [], [], []
    for m in range(12):
        r = by.get(m)
        if not r:
            cats.append({}); labor.append(0.0); cosm.append((0.0, 0.0, 0.0))
            continue
        names = set(r["rev_cat"]) | set(r["cogs_cat"])
        cats.append({n: (r["rev_cat"].get(n, 0.0), r["cogs_cat"].get(n, 0.0)) for n in names})
        labor.append(float(r["labor_pay"] if "labor_pay" in r else r["labor"]))
        cosm.append((r["rev_p"], r["cogs_p"], r["cosm_pct"]))
    return directions(cats, labor, cosm)
