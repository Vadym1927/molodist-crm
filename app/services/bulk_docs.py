"""Масове рознесення документів пацієнтів з архіву (медичні висновки, бюті-плани, інша документація).

Файли підписані пацієнтом у назві: телефон (10–12 цифр, напр. «380671234567__012__Прізвище Імʼя.pdf») або номер
(№ клієнта CleverBox, інакше № картки CRM) — у назві файлу чи папки. Кожен PDF пробуємо прочитати як бюті-план OBSERV:
вийшло → створюється план (пацієнт — за телефоном/номером, а не за ПІБ у PDF); ні → документ картки («Документи»;
анкети/згоди → «Згоди»); фото → «Обличчя». Повторне завантаження того ж файлу
(той самий пацієнт + назва + розмір) не дублює. Спершу — перегляд (dry run), запис лише після підтвердження."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BeautyPlan, Patient, PatientFile
from ..parsers.observ import ObservParseError, parse_observ_pdf

IMG = re.compile(r"\.(jpe?g|png|webp|heic|gif|bmp|tiff?)$", re.I)
DOC = re.compile(r"\.(pdf|docx?|xlsx?|txt|rtf)$", re.I)
SKIP = re.compile(r"(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|\._)", re.I)


def _phone(name: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{10,12})(?!\d)", name)
    return m.group(1)[-9:] if m else None


def _num(name: str) -> int | None:
    m = re.search(r"(?<!\d)(\d{1,6})(?!\d)", name)
    return int(m.group(1)) if m else None


def _patient_key(path: str) -> tuple[str | None, int | None, str]:
    """Ключ пацієнта: телефон або номер — з назви файлу, інакше з найближчої папки. (телефон, номер, звідки)."""
    p = PurePosixPath(path)
    for part, src in [(p.stem, "file")] + [(x, "folder") for x in reversed(p.parts[:-1])]:
        ph = _phone(part)
        if ph:
            return ph, None, src
        n = _num(part)
        if n is not None:
            return None, n, src
    return None, None, ""


def _kind(name: str, group: str) -> str:
    low = name.lower()
    if group == "plans" or "beautyplan" in low.replace(" ", "") or "бюті" in low or "beauty plan" in low:
        return "plan"
    return "doc" if DOC.search(low) or not IMG.search(low) else "photo"


def _category(name: str, group: str, kind: str) -> str:
    low = name.lower()
    if group == "medical" or re.search(r"висновок|заключ|аналіз|довідк|рецепт|узд|мрт", low):
        return "doc"
    if re.search(r"згод|consent|анкет|догов", low):
        return "consent"
    return "doc" if kind == "doc" else "face"


def scan(db: Session, archives: list[tuple[str, bytes]], group: str = "other") -> dict:
    """Перегляд: кожен файл → пацієнт, тип (план / документ / фото), категорія; без запису.
    group: medical | plans | other — підказка з якого архіву (впливає на категорію/тип)."""
    by_cb = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    by_id = {p.id: p for p in db.scalars(select(Patient))}
    by_phone: dict[str, list] = {}
    for p in by_id.values():
        digits = re.sub(r"\D", "", p.phone or "")
        if len(digits) >= 9:
            by_phone.setdefault(digits[-9:], []).append(p)
    have = {(f.patient_id, (f.caption or "").lower()) for f in db.scalars(select(PatientFile).where(PatientFile.patient_id.is_not(None)))}
    rows, no_patient, dup = [], 0, 0
    for arc_name, data in archives:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            rows.append({"archive": arc_name, "path": arc_name, "error": "не zip-архів"})
            continue
        for info in zf.infolist():
            if info.is_dir() or SKIP.search(info.filename) or info.file_size == 0:
                continue
            fname = PurePosixPath(info.filename).name
            try:
                fname_fixed = fname.encode("cp437").decode("cp866") if info.flag_bits & 0x800 == 0 else fname
            except (UnicodeEncodeError, UnicodeDecodeError):
                fname_fixed = fname
            phone, num, src = _patient_key(info.filename if info.flag_bits & 0x800 else fname_fixed)
            kind = _kind(fname_fixed, group)
            pat, via = None, ""
            if phone:
                hits = by_phone.get(phone) or []
                named = [h for h in hits if name_matches(fname_fixed, h.full_name)]
                if len(named) == 1:
                    pat, via = named[0], "phone"
                elif len(hits) == 1:
                    pat, via = hits[0], "phone"
                elif len(hits) > 1:
                    via = "ambiguous"
            elif num is not None:
                pat = by_cb.get(num)
                via = "cb" if pat else ""
                if pat is None and num in by_id:
                    pat, via = by_id[num], "crm"
            r = {"archive": arc_name, "path": info.filename, "name": fname_fixed, "size": info.file_size,
                 "num": phone or (num if num is not None else None), "num_src": src,
                 "kind": kind, "category": _category(fname_fixed, group, kind) if kind != "plan" else "plan",
                 "patient_id": pat.id if pat else None, "patient": pat.full_name if pat else None, "via": via}
            if pat is not None and via == "phone" and not name_matches(fname_fixed, pat.full_name):
                r["mismatch"] = True                      # телефон збігся, а ПІБ у назві файлу — ні: не записуємо без підтвердження
                r["cands"] = candidates(db, fname_fixed)
            if pat is None:
                no_patient += 1
            elif (pat.id, fname_fixed.lower()) in have:
                r["dup"] = True
                dup += 1
            rows.append(r)
    return {"rows": rows, "total": len(rows), "no_patient": no_patient, "dup": dup, "mismatch": sum(1 for r in rows if r.get("mismatch")),
            "plans": sum(1 for r in rows if r.get("kind") == "plan"), "docs": sum(1 for r in rows if r.get("kind") == "doc"),
            "photos": sum(1 for r in rows if r.get("kind") == "photo"),
            "patients": len({r["patient_id"] for r in rows if r.get("patient_id")})}


def apply(db: Session, archives: list[tuple[str, bytes]], group: str = "other", overrides: dict[str, int] | None = None,
          user_id: int | None = None) -> dict:
    """Запис: плани → import_plan з пацієнтом за номером; документи/фото → PatientFile. overrides: {path: patient_id}."""
    from . import ingest
    from .plans import import_plan
    overrides = overrides or {}
    plan_ = scan(db, archives, group)
    rep = {"plans": 0, "plans_failed": [], "files": 0, "skipped_dup": 0, "no_patient": 0, "patients": set()}
    zfs = {name: zipfile.ZipFile(io.BytesIO(data)) for name, data in archives}
    for r in plan_["rows"]:
        if r.get("error"):
            continue
        pid = overrides.get(r["path"]) or (None if r.get("mismatch") else r.get("patient_id"))
        if not pid:
            rep["no_patient"] += 1
            continue
        if r.get("dup") and r["path"] not in overrides:
            rep["skipped_dup"] += 1
            continue
        pat = db.get(Patient, pid)
        data = zfs[r["archive"]].read(r["path"])
        path = ingest.store(data, r["name"])
        if r["name"].lower().endswith(".pdf"):
            try:
                parsed = parse_observ_pdf(path)
            except (ObservParseError, Exception) as e:          # не OBSERV-план → документ картки
                parsed = None
                if r["kind"] == "plan":
                    rep["plans_failed"].append({"name": r["name"], "patient": pat.full_name, "error": str(e)[:160]})
            if parsed is not None and not parsed.session_at and db.scalar(select(BeautyPlan).where(
                    BeautyPlan.patient_id == pat.id, BeautyPlan.session_at.is_(None), BeautyPlan.total_planned == parsed.total)):
                rep["skipped_dup"] += 1                        # план без дати сесії вже є в картці — не дублюємо
                continue
            if parsed is not None:
                plan = import_plan(db, parsed, source_file=r["name"], patient=pat)
                plan.source_file = path
                db.add(PatientFile(patient_id=pat.id, kind="plan_pdf", path=path, caption=r["name"]))
                rep["plans"] += 1
            else:
                db.add(PatientFile(patient_id=pat.id, kind="doc", path=path, caption=r["name"], category=r["category"] if r["kind"] != "plan" else "doc"))
                rep["files"] += 1
        else:
            db.add(PatientFile(patient_id=pat.id, kind=("photo" if r["kind"] == "photo" else "doc"), path=path, caption=r["name"],
                               category=r["category"]))
            rep["files"] += 1
        rep["patients"].add(pat.id)
        db.commit()
    rep["patients"] = len(rep["patients"])
    return rep


NAME_RE = re.compile(r"^\d{10,12}__\d+__")


def find_dups(db: Session) -> dict:
    """Дублі після подвійного запису архіву: той самий файл (пацієнт + назва «телефон__№__…») двічі і той самий план двічі.
    Лишаємо перший запис, решта — зайві."""
    groups: dict = {}
    for f in db.scalars(select(PatientFile).where(PatientFile.patient_id.is_not(None)).order_by(PatientFile.id)):
        if f.caption and NAME_RE.match(f.caption):
            groups.setdefault((f.patient_id, f.caption, f.kind), []).append(f)
    files = [f for v in groups.values() for f in v[1:]]
    pg: dict = {}
    for pl in db.scalars(select(BeautyPlan).order_by(BeautyPlan.id)):
        src = PurePosixPath(pl.source_file or "").name
        if "__" in src:
            pg.setdefault((pl.patient_id, pl.session_at, pl.total_planned, len(pl.items)), []).append(pl)
    plans = [pl for v in pg.values() for pl in v[1:]]
    return {"files": files, "plans": plans}


def remove_dups(db: Session) -> dict:
    """Зайві копії: файли → «_trash» (відновлювані), зайві плани-дублі видаляються (перший план лишається)."""
    import shutil
    from datetime import datetime
    from pathlib import Path
    from ..config import settings
    d = find_dups(db)
    trash = Path(settings.files_dir) / "_trash" / datetime.now().strftime("%Y-%m")
    keep_paths = {f.path for f in db.scalars(select(PatientFile))} - {f.path for f in d["files"]}
    n = 0
    for f in d["files"]:
        try:
            src = Path(f.path)
            if src.exists() and f.path not in keep_paths:
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(trash / f"{f.id}_{src.name}"))
        except OSError:
            pass
        db.delete(f)
        n += 1
    np = 0
    for pl in d["plans"]:
        db.delete(pl)
        np += 1
    db.commit()
    return {"files": n, "plans": np}


def _words(text: str) -> set[str]:
    t = (text or "").lower().replace("ʼ", "").replace("'", "").replace("`", "").replace("’", "")
    t = t.replace("ё", "е").replace("і", "i").replace("ї", "i").replace("й", "и").replace("є", "е").replace("ґ", "г")
    return {w for w in re.findall(r"[a-zа-я]+", t) if len(w) >= 4}


_PATR = re.compile(r"(вна|вич|iч|ич|кизи|огли)$")
_NOISE = {"anti", "acne", "beauty", "beautyplan", "plan", "antiage", "antiacne", "консультативнии", "висновок", "лiкаря", "консультацiя",
          "дерматолога", "консультативний"}


def _name_words(text: str) -> set[str]:
    return {w[:5] for w in _words(text) if not _PATR.search(w) and w not in _NOISE}


def name_matches(filename: str, full_name: str | None) -> bool:
    """ПІБ у назві файлу збігається з пацієнтом: прізвище й імʼя (по-батькові не рахуємо — воно часто однакове в різних людей).
    Якщо в назві або в картці лише одне слово — досить його."""
    stem = re.sub(r"^\d{6,12}__\d+__", "", PurePosixPath(filename).stem)
    fw, pw = _name_words(stem.replace("_", " ")), _name_words(full_name or "")
    need = min(2, len(fw), len(pw))
    return need > 0 and len(fw & pw) >= need


def check(db: Session) -> dict:
    """Усі файли й плани, записані з архівів («телефон__№__ПІБ»): чи збігається ПІБ у назві з пацієнтом картки."""
    rows = []
    for f in db.scalars(select(PatientFile).where(PatientFile.patient_id.is_not(None)).order_by(PatientFile.id)):
        if f.caption and NAME_RE.match(f.caption):
            ok = name_matches(f.caption, f.patient.full_name if f.patient else "")
            rows.append({"id": f.id, "kind": f.kind, "name": f.caption, "patient_id": f.patient_id,
                         "patient": f.patient.full_name if f.patient else None, "ok": ok})
    return {"total": len(rows), "mismatch": [r for r in rows if not r["ok"]]}


def candidates(db: Session, filename: str) -> list[dict]:
    """Пацієнти, чиє ПІБ збігається з назвою файлу щонайменше двома словами (для підказки правильної картки)."""
    stem = re.sub(r"^\d{6,12}__\d+__", "", PurePosixPath(filename).stem)
    fw = _name_words(stem.replace("_", " "))
    out = []
    for p in db.scalars(select(Patient)):
        n = len(_name_words(p.full_name) & fw)
        if n >= min(2, len(fw)) and n > 0:
            out.append({"id": p.id, "name": p.full_name, "phone_tail": re.sub(r"\D", "", p.phone or "")[-4:], "n": n})
    return sorted(out, key=lambda x: -x["n"])[:5]


def fix_plan_dates(db: Session) -> list[dict]:
    """Плани без дати сесії: беремо дату створення PDF плану (без неї план рахується від дня завантаження → реалізація 0)."""
    from pathlib import Path
    from ..parsers.observ import pdf_created
    out = []
    for pl in db.scalars(select(BeautyPlan).where(BeautyPlan.session_at.is_(None))):
        if pl.source_file and Path(pl.source_file).exists():
            dt = pdf_created(pl.source_file)
            if dt:
                pl.session_at = dt
                out.append({"plan": pl.id, "patient": pl.patient.full_name, "session_at": dt.isoformat(timespec="minutes")})
    db.commit()
    return out


def plans_check(db: Session) -> list[dict]:
    """Усі плани: дата сесії, план / реалізовано / записано, к-сть візитів пацієнта після сесії — щоб побачити «нулі»."""
    from datetime import timedelta
    from ..models import Visit
    from . import kpi
    rows = []
    for st in kpi.all_statuses(db):
        pl = st.plan
        since = (pl.session_at or pl.created_at) - timedelta(days=1)
        vis = db.scalars(select(Visit).where(Visit.patient_id == pl.patient_id, Visit.starts_at >= since)).all()
        done = [v for v in vis if v.status == "done"]
        rows.append({"plan": pl.id, "patient_id": pl.patient_id, "patient": pl.patient.full_name,
                     "session_at": pl.session_at.isoformat(timespec="minutes") if pl.session_at else None,
                     "status": pl.status, "planned": round(st.planned_sum or 0), "realized": round(st.realized_sum or 0),
                     "booked": round(st.booked_sum or 0), "pct": round(st.realization_pct or 0, 1),
                     "proc": f"{st.proc_done}/{st.proc_planned}", "visits_after": len(vis), "visits_done": len(done),
                     "visits_sum": round(sum(float(v.amount or 0) for v in done)),
                     "from_archive": "__" in PurePosixPath(pl.source_file or "").name})
    return rows


def plans_unmatched(db: Session) -> list[dict]:
    """Для «Зіставлення»: по кожному плану — незакриті позиції та візити / записи / покупки пацієнта після сесії,
    які не зарахувались у жодну позицію (назви не збіглися)."""
    from datetime import timedelta
    from . import kpi
    crm = kpi.crm_rows(db)
    out = []
    for st in kpi.all_statuses(db):
        pl = st.plan
        since = (pl.session_at or pl.created_at) - timedelta(days=1)
        used = {(e[1], e[2]) for it in st.items for e in it.events}
        extra = crm.get(pl.patient_id) or {"visits": [], "sales": []}
        vis = [v for v in list(pl.patient.visits) + extra["visits"] if v.starts_at >= since and v.status in ("done", "booked")]
        sales = [x for x in list(pl.patient.sales) + extra["sales"] if x.sold_at >= since]
        free_v = [{"at": v.starts_at.isoformat(timespec="minutes"), "name": v.service_name, "sid": v.service_id, "status": v.status,
                   "amount": float(v.amount or 0)} for v in vis if (v.starts_at, v.service_name) not in used]
        free_s = [{"at": x.sold_at.isoformat(timespec="minutes"), "name": x.product_name, "pid": x.product_id, "amount": float(x.amount or 0)}
                  for x in sales if (x.sold_at, x.product_name) not in used]
        items = [{"id": it.item.id, "kind": it.item.kind, "name": it.item.name, "qty": it.item.qty, "done": it.done, "booked": it.booked,
                  "price": it.item.unit_price} for it in st.items if it.done + it.booked < (it.item.qty or 0)]
        if items and (free_v or free_s):
            out.append({"plan": pl.id, "patient": pl.patient.full_name, "patient_id": pl.patient_id, "items": items,
                        "visits": free_v, "sales": free_s})
    return out


def plans_audit(db: Session) -> dict:
    """Аналіз помилок: плани ↔ картки пацієнтів ↔ виконані / записані візити (лише читання)."""
    from datetime import datetime, timedelta
    from ..models import Appointment, Visit
    from ..parsers.observ import norm
    from . import kpi
    now = datetime.now()
    aliases = kpi._aliases(db)
    crm = kpi.crm_rows(db)
    adopted = crm.get("_adopted") or set()
    sts = kpi.all_statuses(db)
    pats = db.scalars(select(Patient)).all()
    by_words: dict = {}
    for p in pats:
        by_words.setdefault(frozenset(_name_words(p.full_name)), []).append(p)
    per_patient: dict = {}
    for st in sts:
        per_patient.setdefault(st.plan.patient_id, []).append(st.plan.id)

    def item_for(items, sid, name):
        """(позиція, причина): позиція, якій відповідає послуга, навіть якщо її кількість вичерпано."""
        for it in items:
            if it.item.kind != "procedure":
                continue
            al = aliases.get((it.item.kind, norm(it.item.name)))
            if al and al.cb_ids:
                ok = sid in al.cb_ids or (bool(name) and norm(name) in {norm(n) for n in (al.cb_names or [])})
            else:
                ok = bool(name) and kpi.name_match(it.item.name, name) >= 0.6
            if ok:
                return it
        return None
    out = []
    for st in sts:
        pl, pt = st.plan, st.plan.patient
        since = (pl.session_at or pl.created_at) - timedelta(days=1)
        issues = []
        src = PurePosixPath(pl.source_file or "").name
        cap = next((f.caption for f in pt.files if f.kind == "plan_pdf" and f.path == pl.source_file), None) or src
        if cap and NAME_RE.match(cap) and not name_matches(cap, pt.full_name):
            issues.append({"t": "name", "d": f"PDF «{cap}» у картці «{pt.full_name}»"})
        if len(per_patient[pt.id]) > 1:
            issues.append({"t": "two_plans", "d": f"у пацієнта планів: {len(per_patient[pt.id])} (№ {per_patient[pt.id]})"})
        twins = [p for p in pats if p.id != pt.id and len(_name_words(p.full_name) & _name_words(pt.full_name)) >= 2] if len(_name_words(pt.full_name)) >= 2 else \
                [p for p in pats if p.id != pt.id and _name_words(pt.full_name) and _name_words(pt.full_name) <= _name_words(p.full_name)]
        if twins:
            issues.append({"t": "twin_card", "d": "схожі картки: " + "; ".join(f"{p.full_name} №{p.id}" for p in twins[:4])})
        if not pt.cleverbox_id:
            issues.append({"t": "no_cb", "d": "картка не привʼязана до клієнта CleverBox"})
        isum = round(sum(float(it.item.total or 0) for it in st.items), 2)
        if abs(isum - float(pl.total_planned or 0)) > 1:
            issues.append({"t": "total", "d": f"сума позицій {isum:.0f} ≠ сума плану {float(pl.total_planned or 0):.0f}"})
        extra = crm.get(pt.id) or {"visits": [], "sales": []}
        cbv = [v for v in pt.visits if not (adopted and v.source == "cb_visit" and any(v.external_id.startswith(x) for x in adopted))]
        vis = [v for v in cbv + extra["visits"] if v.starts_at >= since]
        allv = list(pt.visits)
        cons = [v for v in allv if v.status == "done" and "beauty plan" in (v.service_name or "").lower()]
        if pl.session_at:
            near = min((abs((v.starts_at.date() - pl.session_at.date()).days) for v in cons), default=None)
            if near is None:
                issues.append({"t": "no_consult", "d": "у картці немає виконаної консультації Beauty plan"})
            elif near > 3:
                issues.append({"t": "consult_date", "d": f"найближча консультація Beauty plan за {near} дн. від дати сесії {pl.session_at:%d.%m.%Y}"})
        if not allv and not extra["visits"]:
            issues.append({"t": "no_visits", "d": "у картці немає жодного візиту"})
        used = {(e[1], e[2]) for it in st.items for e in it.events}
        for v in vis:
            nm = v.service_name
            if (v.starts_at, nm) in used or "консультац" in (nm or "").lower():
                continue
            if v.status == "booked" and v.starts_at < now - timedelta(hours=2):
                issues.append({"t": "stale_booked", "d": f"{v.starts_at:%d.%m.%Y} «{nm}» — минулий запис у статусі «записано»"})
                continue
            if v.status not in ("done", "booked"):
                continue
            it = item_for(st.items, v.service_id, nm)
            kind = "зроблено" if v.status == "done" else "записано"
            if it is not None:
                issues.append({"t": "over_qty", "d": f"{v.starts_at:%d.%m.%Y} «{nm}» ({kind}) — понад кількість у плані: «{it.item.name}» {it.done}+{it.booked}/{it.item.qty}"})
            else:
                issues.append({"t": "not_in_plan", "d": f"{v.starts_at:%d.%m.%Y} «{nm}» ({kind}, {float(v.amount or 0):.0f} ₴) — немає такої позиції в плані"})
        out.append({"plan": pl.id, "patient_id": pt.id, "patient": pt.full_name, "session": pl.session_at.strftime("%d.%m.%Y") if pl.session_at else None,
                    "planned": round(st.planned_sum or 0), "realized": round(st.realized_sum or 0), "booked": round(st.booked_sum or 0),
                    "proc": f"{st.proc_done}/{st.proc_planned}", "issues": issues})
    # консультація Beauty plan була, а плану в картці немає
    have = set(per_patient)
    missing: dict = {}
    for v in db.scalars(select(Visit).where(Visit.status == "done", Visit.service_name.ilike("%beauty plan%"))):
        if v.patient_id not in have:
            m = missing.setdefault(v.patient_id, {"patient_id": v.patient_id, "dates": []})
            m["dates"].append(v.starts_at.strftime("%d.%m.%Y"))
    names = {p.id: p.full_name for p in pats}
    files = {}
    for f in db.scalars(select(PatientFile).where(PatientFile.patient_id.in_(list(missing) or [0]))):
        low = (f.caption or "").lower()
        if "anti" in low or "beauty" in low or "plan" in low:
            files.setdefault(f.patient_id, []).append(f.caption)
    miss = [{**m, "patient": names.get(pid), "dates": sorted(set(m["dates"]), key=lambda d: d[6:] + d[3:5] + d[:2]), "pdfs": files.get(pid, [])} for pid, m in missing.items()]
    miss.sort(key=lambda m: m["dates"][-1][6:] + m["dates"][-1][3:5] + m["dates"][-1][:2], reverse=True)
    return {"plans": out, "consult_without_plan": miss}
