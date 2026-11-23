"""Сайт Дарʼї Колган (molodist.com.ua/dr-kolgan) у CRM — замість окремих сервісів на портах 8095 / 8116.

Сторінки — статичні файли з app/static/dk (як були на сервері), API для них — /dr-kolgan/api/auth/… (ті самі адреси, що
використовують сторінки). Вхід за SMS-кодом через кабінет магазину (shop_cabinet, origin="dk"), профіль — DkProfile,
гайди в кабінеті й тести ІМТ / HADS — DkRecord, тест «Молодість 360» — M360Assessment. Платний гайд — посилання на оплату
monobank магазину (paylinks), оплата → доступ + 100% суми Дарʼї в зарплату (SalaryAdj). QR-коди /q/{slug} — QrHit."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Appointment, Check, Doctor, DkOrder, DkProfile, DkRecord, M360Assessment, Patient, PayLink, QrHit,
                      SalaryAdj, Sale, ShopAccount, SyncState, Visit)

KEY = "dr_kolgan"
COOKIE = "dk_s"
STATIC = Path(__file__).resolve().parents[1] / "static" / "dk"
SECURE = Path(__file__).resolve().parents[1] / "private" / "dk_guides"     # сторінки платних гайдів — лише через перевірку доступу
SALARY_ARTICLE = "Гайди (сайт)"

MATERIALS = {
    "full-checkup": {"id": "full-checkup", "title": "Щорічний чек-ап", "description": "Розширений гайд з аналізів і їх інтерпретації.",
                     "url": "/dr-kolgan/guides/full-checkup/", "protected": True, "price": 390, "pages": 47},
    "lifestyle-guide": {"id": "lifestyle-guide", "title": "Вплив способу життя на шкіру", "description": "Як сон, харчування, стрес і звички впливають на шкіру.",
                        "url": "/dr-kolgan/guides/lifestyle/", "folder": "lifestyle", "protected": True, "price": 390, "pages": 42},
    "healthy-plate": {"id": "healthy-plate", "title": "Як скласти свою тарілку", "description": "Простий гайд зі збалансованого харчування.",
                      "url": "/dr-kolgan/guides/healthy-plate/"},
    "breast-self-check": {"id": "breast-self-check", "title": "Самодіагностика молочних залоз",
                          "description": "Покрокова інструкція для регулярної самоперевірки.", "url": "/dr-kolgan/guides/breast-self-check/"},
    "mole-control": {"id": "mole-control", "title": "Контроль родимок", "description": "Простий алгоритм спостереження за змінами шкіри.",
                     "url": "/dr-kolgan/guides/mole-control/"},
}
QR = {"home": {"to": "https://molodist.store/", "campaign": "homepage_qr"},
      "cbox": {"to": "https://cbox.mobi/go/molodist", "campaign": "cbox_qr"}}

# HADS: порядок і бали як у боті турботи; браузер надсилає лише номер варіанта, бал рахує сервер
HADS = [("Т", [3, 2, 1, 0]), ("Д", [0, 1, 2, 3]), ("Т", [3, 2, 1, 0]), ("Д", [0, 1, 2, 3]), ("Т", [3, 2, 1, 0]), ("Д", [3, 2, 1, 0]),
        ("Т", [0, 1, 2, 3]), ("Д", [3, 2, 1, 0]), ("Т", [0, 1, 2, 3]), ("Д", [3, 2, 1, 0]), ("Т", [3, 2, 1, 0]), ("Д", [0, 1, 2, 3]),
        ("Т", [3, 2, 1, 0]), ("Д", [0, 1, 2, 3])]
GENDERS = ("Жінка", "Чоловік", "Інше")


class DkError(ValueError):
    pass


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        c = json.loads(st.value) if st and st.value else {}
    except ValueError:
        c = {}
    c.setdefault("doctor_id", None)
    c.setdefault("salary_pct", 100)
    return c


def doctor(db: Session) -> Doctor | None:
    """Кому нараховуються продажі гайдів: з налаштувань або лікар із прізвищем Колган."""
    did = cfg(db).get("doctor_id")
    if did:
        return db.get(Doctor, int(did))
    return next((d for d in db.scalars(select(Doctor)) if "колган" in (d.name or "").lower()), None)


# ---------------- вхід і профіль ----------------
def _shop_c(db: Session) -> dict:
    from . import shop_front as sf
    return {**sf.cfg(db), "cab_sms": "{code} — код входу в кабінет MOLODIST", "cab_on": True}


def send_code(db: Session, phone: str, ip: str = "") -> dict:
    from . import shop_cabinet as sc
    try:
        return sc.start(db, _shop_c(db), phone, ip, digits=6)
    except sc.CabError as e:
        raise DkError(str(e)) from None


def verify(db: Session, phone: str, code: str, ua: str = "", utm: str = "") -> tuple[str, int, Patient]:
    from . import shop_cabinet as sc
    try:
        token, days, acc = sc.verify(db, _shop_c(db), phone, code, ua=ua, origin="dk")
    except sc.CabError as e:
        raise DkError(str(e)) from None
    p = db.get(Patient, acc.patient_id)
    if not db.scalar(select(DkProfile).where(DkProfile.patient_id == p.id)):
        db.add(DkProfile(patient_id=p.id, first_name=acc.first_name, last_name=acc.last_name, email=acc.email,
                         registered_at=datetime.utcnow(), utm=(utm or "")[:300] or None))
        db.commit()
    return token, days, p


def session(db: Session, token: str | None) -> tuple[Patient, DkProfile] | None:
    from . import shop_cabinet as sc
    r = sc.me(db, token)
    if not r:
        return None
    acc, p = r
    prof = db.scalar(select(DkProfile).where(DkProfile.patient_id == p.id))
    if not prof:                                     # покупець магазину вперше на сайті Дарʼї
        prof = DkProfile(patient_id=p.id, first_name=acc.first_name, last_name=acc.last_name, email=acc.email, registered_at=datetime.utcnow())
        db.add(prof)
        db.commit()
    return p, prof


def logout(db: Session, token: str | None) -> None:
    from . import shop_cabinet as sc
    sc.logout(db, token)


def profile_json(p: Patient, prof: DkProfile) -> dict:
    return {"phone": p.phone, "firstName": prof.first_name or "", "lastName": prof.last_name or "",
            "birthDate": prof.birth_date.isoformat() if prof.birth_date else "", "gender": prof.gender or "", "email": prof.email or "",
            "instagram": "", "tiktok": "", "marketingConsent": bool(prof.marketing), "source": "dr-kolgan",
            "registeredAt": prof.registered_at.isoformat() + "Z" if prof.registered_at else ""}


def save_profile(db: Session, p: Patient, prof: DkProfile, d: dict) -> dict:
    fn, ln = " ".join(str(d.get("firstName") or "").split())[:80], " ".join(str(d.get("lastName") or "").split())[:80]
    if len(fn) < 2:
        raise DkError("Вкажіть імʼя.")
    bd = str(d.get("birthDate") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bd):
        raise DkError("Вкажіть коректну дату народження.")
    try:
        bdd = date.fromisoformat(bd)
    except ValueError:
        raise DkError("Вкажіть коректну дату народження.") from None
    g = str(d.get("gender") or "")
    if g not in GENDERS:
        raise DkError("Оберіть стать.")
    email = str(d.get("email") or "").strip()[:160]
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise DkError("Введіть коректний e-mail.")
    prof.first_name, prof.last_name, prof.birth_date, prof.gender = fn, ln or None, bdd, g
    prof.email, prof.marketing, prof.updated_at = email or None, bool(d.get("marketingConsent")), datetime.utcnow()
    if not p.full_name or p.full_name == p.phone:            # картку клініки не перейменовуємо — лише порожню
        p.full_name = f"{ln} {fn}".strip()
    if not p.birthday:
        p.birthday = bdd
    if email and not p.email:
        p.email = email
    acc = db.scalar(select(ShopAccount).where(ShopAccount.patient_id == p.id))
    if acc and not acc.first_name:
        acc.first_name, acc.last_name = fn, ln or None
    db.commit()
    return profile_json(p, prof)


def cabinet_ok(db: Session, token: str | None) -> bool:
    s = session(db, token)
    return bool(s and s[1].first_name and s[1].birth_date)


# ---------------- гайди ----------------
def materials(db: Session, pid: int) -> list[dict]:
    out = []
    for r in db.scalars(select(DkRecord).where(DkRecord.patient_id == pid, DkRecord.kind == "material").order_by(DkRecord.id.desc())):
        m = MATERIALS.get(r.ref or "")
        if m:
            out.append({k: v for k, v in m.items() if k not in ("price", "pages", "folder")} | {"grantedAt": r.created_at.isoformat() + "Z"})
    return out


def has_material(db: Session, pid: int, mid: str) -> bool:
    return bool(db.scalar(select(DkRecord.id).where(DkRecord.patient_id == pid, DkRecord.kind == "material", DkRecord.ref == mid).limit(1)))


def grant(db: Session, pid: int, mid: str, via: str = "free") -> dict:
    m = MATERIALS.get(mid)
    if not m:
        raise DkError("Матеріал не знайдено.")
    if m.get("protected") and via == "free":
        raise DkError("Цей гайд доступний після оплати.")
    if not has_material(db, pid, mid):
        db.add(DkRecord(patient_id=pid, kind="material", ref=mid, data={"via": via}))
        db.commit()
    return next(x for x in materials(db, pid) if x["id"] == mid)


def guide_page(db: Session, pid: int, mid: str, page: int) -> Path | None:
    m = MATERIALS.get(mid) or {}
    if not m.get("protected") or not has_material(db, pid, mid) or not 1 <= page <= int(m.get("pages") or 0):
        return None
    for base in (import_path().parent.parent / "dk_guides", SECURE):     # дані сервера (data/dk_guides) або в репозиторії
        f = base / mid / "pages" / f"{page:02d}.jpg"
        if f.is_file():
            return f
    return None


# ---------------- тести ----------------
def bmi(db: Session, pid: int, d: dict) -> dict:
    try:
        h, w = float(d.get("heightCm")), float(d.get("weightKg"))
    except (TypeError, ValueError):
        raise DkError("Введіть зріст і вагу числами.") from None
    if not 80 <= h <= 250:
        raise DkError("Введіть зріст від 80 до 250 см.")
    if not 20 <= w <= 400:
        raise DkError("Введіть вагу від 20 до 400 кг.")
    v = round(w / (h / 100) ** 2, 1)
    cat = "Недостатня маса тіла" if v < 18.5 else "Нормальна маса тіла" if v < 25 else "Надмірна маса тіла" if v < 30 else "Ожиріння"
    r = DkRecord(patient_id=pid, kind="bmi", data={"heightCm": h, "weightKg": w, "bmi": v, "category": cat})
    db.add(r)
    db.commit()
    return {"id": r.id, "calculatedAt": r.created_at.isoformat() + "Z", **r.data}


def _hads_level(score: int, dim: str) -> str:
    label = "тривога" if dim == "Т" else "депресія"
    return f"норма ({label})" if score <= 7 else f"виражена {label}" if score <= 10 else f"клінічно виражена {label}"


def hads(db: Session, pid: int, d: dict) -> dict:
    ans = d.get("answers")
    if not isinstance(ans, list) or len(ans) != len(HADS):
        raise DkError("Дайте відповідь на всі 14 питань.")
    a = dep = 0
    for i, (dim, pts) in enumerate(HADS):
        try:
            o = int(ans[i])
        except (TypeError, ValueError):
            raise DkError("Некоректна відповідь у тесті.") from None
        if not 0 <= o < len(pts):
            raise DkError("Некоректна відповідь у тесті.")
        if dim == "Т":
            a += pts[o]
        else:
            dep += pts[o]
    r = DkRecord(patient_id=pid, kind="hads", data={"anxiety": a, "depression": dep, "anxietyLevel": _hads_level(a, "Т"),
                                                    "depressionLevel": _hads_level(dep, "Д")})
    db.add(r)
    db.commit()
    return {"id": r.id, "completedAt": r.created_at.isoformat() + "Z", **r.data}


def results(db: Session, pid: int, kind: str) -> list[dict]:
    if kind == "molodist360":
        return [{"id": a.id, "assessmentId": a.token, "completedAt": (a.submitted_at or a.created_at).isoformat() + "Z",
                 "score": round(a.overall_score or 0), "status": a.overall_status or "", "summary": (a.result or {}).get("overall_summary") or "",
                 "resultUrl": f"/molodist-360/result/{a.token}"}
                for a in db.scalars(select(M360Assessment).where(M360Assessment.patient_id == pid, M360Assessment.status == "completed")
                                    .order_by(M360Assessment.id.desc()))]
    key = "calculatedAt" if kind == "bmi" else "completedAt"
    return [{"id": r.id, key: r.created_at.isoformat() + "Z", **(r.data or {})}
            for r in db.scalars(select(DkRecord).where(DkRecord.patient_id == pid, DkRecord.kind == kind).order_by(DkRecord.id.desc()))]


# ---------------- оплата платного гайду ----------------
def create_payment(db: Session, p: Patient, mid: str, base_url: str, site_root: str) -> dict:
    from . import paylinks as pl, shop_front as sf
    m = MATERIALS.get(mid) or {}
    if not m.get("protected"):
        raise DkError("Матеріал не знайдено.")
    if has_material(db, p.id, mid):
        return {"ok": True, "owned": True, "checkoutUrl": m["url"], "fields": {}}
    o = DkOrder(patient_id=p.id, product=mid, amount=float(m["price"]))
    db.add(o)
    db.flush()
    back = f"{site_root.rstrip('/')}/dr-kolgan/guides/{m.get('folder') or mid}/payment/?orderReference={o.id}"
    try:
        link = pl.create(db, o.amount, f"Гайд «{m['title']}» — Дарʼя Колган", base_url, patient_id=p.id,
                         account_id=int(sf.cfg(db).get("pay_account") or 0) or None, redirect_url=back,
                         basket=[{"name": f"Гайд «{m['title']}»", "qty": 1, "total": o.amount, "code": f"DK-{mid}"}])
    except pl.PayError as e:
        db.rollback()
        raise DkError(str(e)) from None
    link.info = {**(link.info or {}), "dk_order": o.id}
    o.paylink_id = link.id
    db.commit()
    # сторінки сайту відправляють форму POST на checkoutUrl з полями → наш редірект на сторінку оплати monobank
    return {"ok": True, "checkoutUrl": "/dr-kolgan/api/auth/payments/monobank/redirect", "fields": {"orderReference": str(o.id)}}


def pay_url(db: Session, pid: int, oid: str) -> str | None:
    o = db.get(DkOrder, int(oid)) if str(oid).isdigit() else None
    if not o or o.patient_id != pid or not o.paylink_id:
        return None
    link = db.get(PayLink, o.paylink_id)
    return link.url if link and link.status == "created" else None


def paid(db: Session, link: PayLink) -> None:
    """Викликається з paylinks при оплаті: доступ до гайду + нарахування Дарʼї (100%). Повторний виклик нічого не дублює."""
    oid = (link.info or {}).get("dk_order")
    o = db.get(DkOrder, int(oid)) if oid else None
    if not o or o.status == "paid":
        return
    o.status, o.paid_at = "paid", datetime.now()
    if not has_material(db, o.patient_id, o.product):
        db.add(DkRecord(patient_id=o.patient_id, kind="material", ref=o.product, data={"via": "paid", "order": o.id}))
    d = doctor(db)
    if d:
        amt = round(float(link.paid_amount or o.amount) * float(cfg(db).get("salary_pct") or 100) / 100, 2)
        title = (MATERIALS.get(o.product) or {}).get("title") or o.product
        adj = SalaryAdj(doctor_id=d.id, month=o.paid_at.strftime("%Y-%m"), amount=amt, article=SALARY_ARTICLE,
                        comment=f"Продаж гайду «{title}» на сайті, замовлення №{o.id}")
        db.add(adj)
        db.flush()
        o.salary_adj_id = adj.id


def order_status(db: Session, pid: int, oid: str) -> dict:
    from . import paylinks as pl
    o = db.get(DkOrder, int(oid)) if str(oid).isdigit() else None
    if not o or o.patient_id != pid:
        raise DkError("Замовлення не знайдено.")
    if o.status != "paid" and o.paylink_id:
        link = db.get(PayLink, o.paylink_id)
        if link and link.status != "paid":
            try:
                pl.check_status(db, link)
            except Exception:  # noqa: BLE001
                pass
        db.refresh(o)
    return {"ok": True, "status": "Approved" if o.status == "paid" else ("Created" if o.status == "created" else o.status),
            "granted": o.status == "paid" and has_material(db, pid, o.product)}


# ---------------- QR ----------------
def qr(db: Session, slug: str, q: dict, ua: str, lang: str, ref: str) -> str | None:
    c = QR.get((slug or "").lower())
    if not c:
        return None
    clean = lambda v, d="": (re.sub(r"[^a-zA-Z0-9_.-]", "", str(v or d))[:80] or d)       # noqa: E731
    src, med, camp, cont = clean(q.get("utm_source"), "qr"), clean(q.get("utm_medium"), "offline"), clean(q.get("utm_campaign"), c["campaign"]), clean(q.get("utm_content"))
    from . import site_leads as sl
    db.add(QrHit(slug=slug.lower(), utm_source=src, utm_medium=med, utm_campaign=camp, utm_content=cont or None,
                 device=sl.device(ua)[:30], lang=(lang or "").split(",")[0][:24] or None,
                 referrer=(re.sub(r"^https?://", "", ref or "").split("/")[0][:120] or None)))
    db.commit()
    from urllib.parse import urlencode
    return c["to"] + ("&" if "?" in c["to"] else "?") + urlencode({k: v for k, v in (("utm_source", src), ("utm_medium", med),
                                                                                      ("utm_campaign", camp), ("utm_content", cont)) if v})


# ---------------- звіт «Сайт Дарʼї» ----------------
def report(db: Session, d_from: date, d_to: date) -> dict:
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    profs = list(db.scalars(select(DkProfile).order_by(DkProfile.registered_at.desc())))
    m360 = list(db.scalars(select(M360Assessment)))
    pids = {p.patient_id for p in profs} | {x.patient_id for x in m360 if x.patient_id}
    recs = list(db.scalars(select(DkRecord).where(DkRecord.patient_id.in_(pids or [0]))))
    orders = list(db.scalars(select(DkOrder).where(DkOrder.patient_id.in_(pids or [0]))))
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(pids or [0])))}
    # перехід у пацієнти: запис / візит / оплата в клініці після реєстрації
    first: dict[int, datetime] = {}
    for p in profs:
        first[p.patient_id] = p.registered_at
    for x in m360:
        if x.patient_id and (x.patient_id not in first or x.created_at < first[x.patient_id]):
            first[x.patient_id] = x.created_at
    clinic = {}
    for pid in pids:
        t0 = first.get(pid) or datetime.min
        appts = [x for x in db.scalars(select(Appointment).where(Appointment.patient_id == pid, Appointment.created_at >= t0))
                 if x.status not in ("cancelled", "no_show")]
        came = [x for x in appts if x.status in ("done", "paid", "arrived")] + \
               list(db.scalars(select(Visit).where(Visit.patient_id == pid, Visit.status == "done", Visit.starts_at >= t0)))
        rev = sum(float(s.total or 0) for s in db.scalars(select(Sale).where(Sale.patient_id == pid, Sale.status == "paid", Sale.at >= t0)))
        rev += sum(float(c.amount or 0) for c in db.scalars(select(Check).where(Check.patient_id == pid, Check.deleted.is_(False), Check.date >= t0)))
        clinic[pid] = {"booked": bool(appts or came), "came": bool(came), "revenue": round(rev, 2)}
    rows = []
    for pid in sorted(pids, key=lambda i: first.get(i) or datetime.min, reverse=True):
        p, prof = pats.get(pid), next((x for x in profs if x.patient_id == pid), None)
        rr = [r for r in recs if r.patient_id == pid]
        oo = [o for o in orders if o.patient_id == pid and o.status == "paid"]
        mm = [x for x in m360 if x.patient_id == pid and x.status == "completed"]
        rows.append({"pid": pid, "name": (p.full_name if p else "") or "—", "phone": p.phone if p else "", "at": first.get(pid),
                     "registered": bool(prof), "profile": bool(prof and prof.birth_date), "utm": (prof.utm if prof else "") or "",
                     "free": sorted({r.ref for r in rr if r.kind == "material" and (r.data or {}).get("via") != "paid"}),
                     "bmi": sum(1 for r in rr if r.kind == "bmi"), "hads": sum(1 for r in rr if r.kind == "hads"), "m360": len(mm),
                     "m360_score": mm[-1].overall_score if mm else None,
                     "bought": sum(o.amount for o in oo), "n_bought": len(oo), **clinic.get(pid, {})})
    inp = [r for r in rows if r["at"] and a <= r["at"] <= b]
    paid_p = [o for o in orders if o.status == "paid" and o.paid_at and a <= o.paid_at <= b]
    n = len(inp)
    kpi = {"people": n, "registered": sum(1 for r in inp if r["registered"]), "profile": sum(1 for r in inp if r["profile"]),
           "m360": sum(1 for r in inp if r["m360"]), "bmi": sum(1 for r in inp if r["bmi"]), "hads": sum(1 for r in inp if r["hads"]),
           "free": sum(1 for r in inp if r["free"]), "buyers": len({o.patient_id for o in paid_p}), "sold": len(paid_p),
           "revenue": round(sum(o.amount for o in paid_p), 2), "booked": sum(1 for r in inp if r.get("booked")),
           "came": sum(1 for r in inp if r.get("came")), "clinic_revenue": round(sum(r.get("revenue") or 0 for r in inp), 2),
           "qr": db.scalar(select(func.count(QrHit.id)).where(QrHit.at >= a, QrHit.at <= b)) or 0}
    kpi["pct"] = {k: (round(100 * kpi[k] / n) if n else None) for k in ("profile", "m360", "free", "booked", "came")}
    kpi["conv_buy"] = round(100 * kpi["buyers"] / kpi["registered"]) if kpi["registered"] else None
    d = doctor(db)
    salary = sum(float(x.amount or 0) for x in db.scalars(select(SalaryAdj).where(SalaryAdj.article == SALARY_ARTICLE,
                                                                                    SalaryAdj.month >= d_from.strftime("%Y-%m"),
                                                                                    SalaryAdj.month <= d_to.strftime("%Y-%m"))))
    weeks: dict[str, dict] = {}
    for r in inp:
        k = (r["at"].date() - timedelta(days=r["at"].weekday())).strftime("%d.%m")
        w = weeks.setdefault(k, {"week": k, "people": 0, "m360": 0, "bought": 0, "came": 0})
        w["people"] += 1
        w["m360"] += 1 if r["m360"] else 0
        w["bought"] += 1 if r["n_bought"] else 0
        w["came"] += 1 if r.get("came") else 0
    return {"kpi": kpi, "rows": inp, "all_rows": rows, "weeks": sorted(weeks.values(), key=lambda x: datetime.strptime(x["week"], "%d.%m")),
            "orders": sorted(paid_p, key=lambda o: o.paid_at, reverse=True), "salary": round(salary, 2), "doctor": d,
            "qr": db.execute(select(QrHit.slug, func.count(QrHit.id)).where(QrHit.at >= a, QrHit.at <= b).group_by(QrHit.slug)).all()}


# ---------------- перенос зі старих сервісів (одноразово) ----------------
def import_path() -> Path:
    from ..config import settings
    return Path(settings.files_dir).resolve().parent / "import" / "dk_export.json"


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _patient(db: Session, phone: str, name: str, source: str) -> Patient | None:
    from .appeals import find_patient
    from .m360.core import _phone
    ph = _phone(phone or "")
    if not ph:
        return None
    p = find_patient(db, ph)
    if not p:
        p = Patient(full_name=name.strip() or ph, phone=ph, source=source, notes="Перенесено зі старого сервісу сайту Дарʼї Колган")
        db.add(p)
        db.flush()
    return p


def import_old(db: Session, data: dict) -> dict:
    """Перенос: тести «Молодість 360» → M360Assessment (+ мітка «Тест 360»), кабінети → DkProfile, гайди / ІМТ / HADS → DkRecord,
    оплачені замовлення → DkOrder (без нарахувань — історичні продажі). Повторний запуск нічого не дублює."""
    from .m360 import core as m3
    from .m360.questionnaire import QUESTION_LOOKUP
    from .m360.scoring import evaluate
    n = {"m360": 0, "m360_skip": 0, "profiles": 0, "records": 0, "orders": 0, "patients_new": 0, "no_phone": 0}
    before = db.scalar(select(func.count(Patient.id))) or 0
    for a in data.get("m360") or []:
        tok = str(a.get("id") or "")[:40]
        if not tok or db.scalar(select(M360Assessment.id).where(M360Assessment.token == tok)):
            n["m360_skip"] += 1
            continue
        pp = a.get("patient") or {}
        p = _patient(db, pp.get("phone") or "", f"{pp.get('last_name') or ''} {pp.get('first_name') or ''}", m3.SOURCE)
        if not p:
            n["no_phone"] += 1
            continue
        m3.tag(p)
        answers = {x["question_key"]: {k: x.get(k) for k in ("module_key", "factor_key", "answer_value", "answer_label", "answer_score")}
                   for x in a.get("answers") or [] if x.get("question_key")}
        result = None
        if a.get("status") == "completed":
            if answers and all(k in QUESTION_LOOKUP for k in answers):
                try:
                    result = evaluate([{"question_key": k, **v} for k, v in answers.items()])
                    result["overall_score"] = a.get("overall_score") if a.get("overall_score") is not None else result["overall_score"]
                except Exception:  # noqa: BLE001 — стара версія анкети: беремо збережені бали
                    result = None
            if result is None:
                result = {"overall_score": a.get("overall_score"), "overall_status": a.get("overall_status"),
                          "overall_summary": a.get("overall_summary"), "top_risks": a.get("top_risks") or [],
                          "modules": a.get("modules") or [], "factors": a.get("factors") or []}
        bd = None
        try:
            bd = date.fromisoformat(str(pp.get("birth_date"))[:10]) if pp.get("birth_date") else None
        except ValueError:
            pass
        db.add(M360Assessment(token=tok, patient_id=p.id, first_name=pp.get("first_name"), last_name=pp.get("last_name"), phone=p.phone,
                              email=pp.get("email") or None, birth_date=bd, gender=pp.get("gender"), city=pp.get("city"),
                              consent_results=bool(pp.get("consent_results")), answers=answers, result=result,
                              overall_score=(result or {}).get("overall_score"), overall_status=(result or {}).get("overall_status"),
                              status="completed" if a.get("status") == "completed" else "draft", source="перенесено зі старого сервісу",
                              created_at=_dt(a.get("created_at")) or datetime.utcnow(), submitted_at=_dt(a.get("submitted_at"))))
        n["m360"] += 1
    db.flush()
    for pr in data.get("profiles") or []:
        p = _patient(db, pr.get("phone") or "", f"{pr.get('lastName') or ''} {pr.get('firstName') or ''}", "Сайт Дарʼї Колган")
        if not p:
            n["no_phone"] += 1
            continue
        prof = db.scalar(select(DkProfile).where(DkProfile.patient_id == p.id))
        if not prof:
            bd = None
            try:
                bd = date.fromisoformat(str(pr.get("birthDate"))[:10]) if pr.get("birthDate") else None
            except ValueError:
                pass
            prof = DkProfile(patient_id=p.id, first_name=pr.get("firstName") or None, last_name=pr.get("lastName") or None,
                             email=pr.get("email") or None, birth_date=bd, gender=pr.get("gender") if pr.get("gender") in GENDERS else None,
                             marketing=bool(pr.get("marketingConsent")), registered_at=_dt(pr.get("registeredAt")) or datetime.utcnow(),
                             updated_at=_dt(pr.get("updatedAt")), imported=True, utm=f"старий сервіс · {pr.get('source') or ''}".strip(" ·"))
            db.add(prof)
            n["profiles"] += 1
        have = {(r.kind, r.ref, r.created_at) for r in db.scalars(select(DkRecord).where(DkRecord.patient_id == p.id))}
        for m in pr.get("materials") or []:
            if m.get("id") in MATERIALS and not has_material(db, p.id, m["id"]):
                db.add(DkRecord(patient_id=p.id, kind="material", ref=m["id"], created_at=_dt(m.get("grantedAt")) or datetime.utcnow(),
                                data={"via": "paid" if MATERIALS[m["id"]].get("protected") else "free", "imported": True}))
                db.flush()
                n["records"] += 1
        for kind in ("bmi", "hads"):
            for x in pr.get(kind) or []:
                at = _dt(x.get("calculatedAt") or x.get("completedAt")) or datetime.utcnow()
                if (kind, None, at) in have:
                    continue
                db.add(DkRecord(patient_id=p.id, kind=kind, created_at=at,
                                data={k: v for k, v in x.items() if k not in ("id", "calculatedAt", "completedAt")} | {"imported": True}))
                n["records"] += 1
    for o in data.get("orders") or []:
        if str(o.get("status")) != "Approved":
            continue
        p = _patient(db, o.get("phone") or "", "", "Сайт Дарʼї Колган")
        ref = f"old:{o.get('reference')}"
        if not p or db.scalar(select(DkOrder.id).where(DkOrder.product == str(o.get("productId") or "full-checkup"), DkOrder.patient_id == p.id,
                                                       DkOrder.paid_at == _dt(o.get("paidAt") or o.get("updatedAt") or o.get("createdAt")))):
            continue
        db.add(DkOrder(patient_id=p.id, product=str(o.get("productId") or "full-checkup"), amount=round(float(o.get("amount") or 0) / 100, 2),
                       status="paid", created_at=_dt(o.get("createdAt")) or datetime.utcnow(),
                       paid_at=_dt(o.get("paidAt") or o.get("updatedAt") or o.get("createdAt"))))
        n["orders"] += 1
        _ = ref
    db.commit()
    n["patients_new"] = (db.scalar(select(func.count(Patient.id))) or 0) - before
    return n


def m360_report(db: Session, d_from: date, d_to: date) -> dict:
    """Окремий звіт по тесту «Молодість 360»: усі анкети, середні бали за модулями, розподіл, хто прийшов у клініку."""
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    rows = list(db.scalars(select(M360Assessment).where(M360Assessment.created_at >= a, M360Assessment.created_at <= b)
                           .order_by(M360Assessment.created_at.desc())))
    done = [x for x in rows if x.status == "completed" and x.overall_score is not None]
    mods: dict[str, list] = {}
    for x in done:
        for m in (x.result or {}).get("modules") or []:
            mods.setdefault(m.get("module_name") or m.get("module_key"), []).append(float(m.get("module_score") or 0))
    risks: dict[str, int] = {}
    for x in done:
        for r in ((x.result or {}).get("top_risks") or [])[:3]:
            risks[r] = risks.get(r, 0) + 1
    bands = [("90–100", 90, 101), ("75–89", 75, 90), ("60–74", 60, 75), ("40–59", 40, 60), ("0–39", 0, 40)]
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_({x.patient_id for x in rows if x.patient_id} or {0})))}
    came = {}
    for pid in {x.patient_id for x in rows if x.patient_id}:
        t0 = min(x.created_at for x in rows if x.patient_id == pid)
        came[pid] = bool(db.scalar(select(Visit.id).where(Visit.patient_id == pid, Visit.status == "done", Visit.starts_at >= t0).limit(1))) or \
            any(ap.status in ("done", "paid", "arrived") for ap in db.scalars(select(Appointment).where(Appointment.patient_id == pid, Appointment.start >= t0)))
    return {"rows": rows, "pats": pats, "came": came, "started": len(rows), "done": len(done),
            "avg": round(sum(x.overall_score for x in done) / len(done), 1) if done else None,
            "mods": sorted(((k, round(sum(v) / len(v), 1)) for k, v in mods.items()), key=lambda t: t[1]),
            "risks": sorted(risks.items(), key=lambda t: -t[1])[:8],
            "bands": [(lbl, sum(1 for x in done if lo <= x.overall_score < hi)) for lbl, lo, hi in bands],
            "came_n": sum(1 for v in came.values() if v)}
