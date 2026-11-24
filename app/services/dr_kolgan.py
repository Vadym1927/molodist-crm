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
PAY_NAME = "Послуги краси"              # назва в оплаті monobank для гайдів

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


# ---------------- вхід і профіль (окремо від магазину: свої коди й сесії, без акаунта магазину) ----------------
CODE_TTL, DAYS = 5 * 60, 180
SMS_SIGN = {"dk": "{code} — код входу. MOLODIST · лікар Дарʼя Колган", "m360": "{code} — код підтвердження. MOLODIST 360"}
_NAME = re.compile(r"[А-ЩЬЮЯЄІЇҐа-щьюяєіїґʼ'’ -]{3,80}")


def check_name(v: str, what: str) -> str:
    """Імʼя / прізвище: лише кирилиця, не менше 3 літер."""
    v = " ".join(str(v or "").split())
    if not _NAME.fullmatch(v) or len(re.sub(r"[^А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]", "", v)) < 3:
        raise DkError(f"{what}: лише українськими літерами, не менше 3 літер.")
    return v[:1].upper() + v[1:]


def _h(*parts: str) -> str:
    import hashlib
    import hmac
    from ..config import settings
    return hmac.new(settings.secret_key.encode(), ("dk|" + "|".join(parts)).encode(), hashlib.sha256).hexdigest()


def _ph(raw: str) -> str:
    from .m360.core import _phone
    p = _phone(raw)
    if not p:
        raise DkError("Введіть номер у форматі +380 XX XXX XX XX.")
    return p


def send_code(db: Session, phone: str, ip: str = "", src: str = "dk") -> dict:
    """Код входу: 1 на хвилину й 4 на годину на номер, 8 на годину з однієї адреси, 100 SMS на добу."""
    import secrets
    from .shop_cabinet import _deliver, mask, CabError
    from ..models import DkCode
    ph = _ph(phone)
    now = datetime.utcnow()
    last = db.scalars(select(DkCode).where(DkCode.phone == ph).order_by(DkCode.id.desc()).limit(1)).first()
    if last and (now - last.created_at).total_seconds() < 60:
        raise DkError("Код уже надіслано. Новий можна отримати за хвилину.")
    hour = now - timedelta(hours=1)
    if (db.scalar(select(func.count(DkCode.id)).where(DkCode.phone == ph, DkCode.created_at >= hour)) or 0) >= 4 or \
            (ip and (db.scalar(select(func.count(DkCode.id)).where(DkCode.ip == ip, DkCode.created_at >= hour)) or 0) >= 8):
        raise DkError("Забагато спроб. Спробуйте за годину або зателефонуйте нам.")
    if (db.scalar(select(func.count(DkCode.id)).where(DkCode.channel == "sms", DkCode.created_at >= now - timedelta(days=1))) or 0) >= 100:
        raise DkError("Вхід за кодом тимчасово недоступний. Зателефонуйте нам, будь ласка.")
    code = str(secrets.randbelow(10 ** 6)).zfill(6)
    try:
        ch = _deliver(db, ph, SMS_SIGN.get(src, SMS_SIGN["dk"]).replace("{code}", code))
    except CabError as e:
        raise DkError(str(e)) from None
    db.add(DkCode(phone=ph, code_hash=_h("code", ph, code), created_at=now, expires_at=now + timedelta(seconds=CODE_TTL), tries=0,
                  used=False, ip=(ip or "")[:64] or None, channel=ch))
    db.commit()
    return {"ok": True, "to": mask(ph), "channel": ch}


def verify(db: Session, phone: str, code: str, ua: str = "", utm: str = "", origin: str = "dk") -> tuple[str, int, Patient]:
    import hmac
    import secrets
    from .appeals import find_patient
    from ..models import DkCode, DkSession
    ph = _ph(phone)
    now = datetime.utcnow()
    row = db.scalars(select(DkCode).where(DkCode.phone == ph, DkCode.used.is_(False)).order_by(DkCode.id.desc()).limit(1)).first()
    if not row or row.expires_at < now:
        raise DkError("Код застарів. Отримайте новий.")
    if (row.tries or 0) >= 5:
        raise DkError("Забагато невдалих спроб. Отримайте новий код.")
    if not hmac.compare_digest(row.code_hash, _h("code", ph, re.sub(r"\D", "", str(code or "")))):
        row.tries = (row.tries or 0) + 1
        db.commit()
        raise DkError("Невірний код.")
    row.used = True
    p = find_patient(db, ph)
    if not p:
        p = Patient(full_name=ph, phone=ph, source="Сайт / Молодість 360" if origin == "m360" else "Сайт Дарʼї Колган",
                    notes="Картку створено при вході на " + ("тест «Молодість 360»" if origin == "m360" else "сайт Дарʼї Колган"))
        db.add(p)
        db.flush()
    if not db.scalar(select(DkProfile).where(DkProfile.patient_id == p.id)):
        db.add(DkProfile(patient_id=p.id, registered_at=now, utm=(utm or "")[:300] or None, origin=origin))
    token = secrets.token_urlsafe(32)
    db.add(DkSession(token_hash=_h("sess", token), patient_id=p.id, created_at=now, expires_at=now + timedelta(days=DAYS), ua=(ua or "")[:200]))
    db.commit()
    return token, DAYS, p


def session(db: Session, token: str | None) -> tuple[Patient, DkProfile] | None:
    from ..models import DkSession
    if not token:
        return None
    s = db.scalar(select(DkSession).where(DkSession.token_hash == _h("sess", token)))
    if not s or s.expires_at < datetime.utcnow():
        return None
    p = db.get(Patient, s.patient_id)
    prof = db.scalar(select(DkProfile).where(DkProfile.patient_id == s.patient_id))
    if p and not prof:
        prof = DkProfile(patient_id=p.id, registered_at=datetime.utcnow())
        db.add(prof)
        db.commit()
    return (p, prof) if p else None


def logout(db: Session, token: str | None) -> None:
    from ..models import DkSession
    if token:
        s = db.scalar(select(DkSession).where(DkSession.token_hash == _h("sess", token)))
        if s:
            db.delete(s)
            db.commit()


def detach_shop(db: Session) -> int:
    """Одноразово: прибрати акаунти й сесії магазину, створені входом на сайт Дарʼї (до розділення), де в магазині нічого немає."""
    from ..models import CoinTxn, ShopCart, ShopSession, SiteOrder
    n = 0
    for acc in db.scalars(select(ShopAccount).where(ShopAccount.patient_id.in_(select(DkProfile.patient_id)))):
        pid = acc.patient_id
        if db.scalar(select(SiteOrder.id).where(SiteOrder.patient_id == pid).limit(1)) or \
                db.scalar(select(ShopCart.id).where(ShopCart.patient_id == pid).limit(1)) or \
                db.scalar(select(CoinTxn.id).where(CoinTxn.patient_id == pid).limit(1)) or acc.wishlist or acc.addresses or acc.referred_by:
            continue
        for s_ in db.scalars(select(ShopSession).where(ShopSession.patient_id == pid)):
            db.delete(s_)
        db.delete(acc)
        n += 1
    db.commit()
    return n


def profile_json(p: Patient, prof: DkProfile) -> dict:
    return {"phone": p.phone, "firstName": prof.first_name or "", "lastName": prof.last_name or "",
            "birthDate": prof.birth_date.isoformat() if prof.birth_date else "", "gender": prof.gender or "", "email": prof.email or "",
            "instagram": "", "tiktok": "", "marketingConsent": bool(prof.marketing), "source": "dr-kolgan",
            "registeredAt": prof.registered_at.isoformat() + "Z" if prof.registered_at else ""}


def save_profile(db: Session, p: Patient, prof: DkProfile, d: dict) -> dict:
    fn, ln = check_name(d.get("firstName"), "Імʼя"), check_name(d.get("lastName"), "Прізвище")
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
        # у monobank (сторінка оплати, кошик, фіскальний чек) — «Послуги краси»; який саме гайд — у замовленні CRM (DkOrder)
        link = pl.create(db, o.amount, PAY_NAME, base_url, patient_id=p.id,
                         account_id=int(sf.cfg(db).get("pay_account") or 0) or None, redirect_url=back,
                         basket=[{"name": PAY_NAME, "qty": 1, "total": o.amount, "code": f"DK-{mid}"}])
        link.info = {**(link.info or {}), "dk_title": m["title"]}
    except pl.PayError as e:
        db.rollback()
        raise DkError(str(e)) from None
    link.info = {**(link.info or {}), "dk_order": o.id}
    link.purpose = f"{PAY_NAME} (гайд «{m['title']}», сайт Дарʼї)"[:300]          # у CRM видно, за що
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
def was_patient(db: Session, pt: Patient | None, t0: datetime) -> bool:
    """Картка існувала до реєстрації на сайті / тесту (CleverBox, CRM, візити чи чеки раніше) — у конверсію сайту в клініку не рахується."""
    if not pt or not t0 or t0 <= datetime.min:
        return False
    return bool((pt.cb_registered_at and pt.cb_registered_at < t0 - timedelta(days=1))
                or (pt.created_at and pt.created_at < t0 - timedelta(hours=1))
                or db.scalar(select(Visit.id).where(Visit.patient_id == pt.id, Visit.status == "done", Visit.starts_at < t0).limit(1))
                or db.scalar(select(Check.id).where(Check.patient_id == pt.id, Check.deleted.is_(False), Check.date < t0).limit(1)))


def report(db: Session, d_from: date, d_to: date) -> dict:
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    profs = list(db.scalars(select(DkProfile).order_by(DkProfile.registered_at.desc())))
    act = set(db.scalars(select(DkRecord.patient_id))) | set(db.scalars(select(DkOrder.patient_id)))
    profs = [p for p in profs if p.origin != "m360" or p.patient_id in act]   # вхід лише заради тесту 360 — не сайт Дарʼї
    pids = {p.patient_id for p in profs}                     # лише сайт Дарʼї; «Молодість 360» — окрема аналітика
    recs = list(db.scalars(select(DkRecord).where(DkRecord.patient_id.in_(pids or [0]))))
    orders = list(db.scalars(select(DkOrder).where(DkOrder.patient_id.in_(pids or [0]))))
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(pids or [0])))}
    first: dict[int, datetime] = {p.patient_id: p.registered_at for p in profs}
    clinic = {}
    for pid in pids:
        t0 = first.get(pid) or datetime.min
        appts = [x for x in db.scalars(select(Appointment).where(Appointment.patient_id == pid, Appointment.created_at >= t0))
                 if x.status not in ("cancelled", "no_show")]
        came = [x for x in appts if x.status in ("done", "paid", "arrived")] + \
               list(db.scalars(select(Visit).where(Visit.patient_id == pid, Visit.status == "done", Visit.starts_at >= t0)))
        rev = sum(float(s.total or 0) for s in db.scalars(select(Sale).where(Sale.patient_id == pid, Sale.status == "paid", Sale.at >= t0)))
        rev += sum(float(c.amount or 0) for c in db.scalars(select(Check).where(Check.patient_id == pid, Check.deleted.is_(False), Check.date >= t0)))
        # конверсію в клініку рахуємо лише для НОВИХ: картки не було до реєстрації на сайті (ні в CleverBox, ні в CRM, ні візитів/чеків раніше)
        pt = pats.get(pid)
        old_pt = was_patient(db, pt, t0)
        clinic[pid] = {"existing": old_pt, "booked": bool(appts or came) and not old_pt, "came": bool(came) and not old_pt,
                       "revenue": 0.0 if old_pt else round(rev, 2), "old_came": bool(came) and old_pt}
    rows = []
    for pid in sorted(pids, key=lambda i: first.get(i) or datetime.min, reverse=True):
        p, prof = pats.get(pid), next((x for x in profs if x.patient_id == pid), None)
        rr = [r for r in recs if r.patient_id == pid]
        oo = [o for o in orders if o.patient_id == pid and o.status == "paid"]
        rows.append({"pid": pid, "name": (p.full_name if p else "") or "—", "phone": p.phone if p else "", "at": first.get(pid),
                     "registered": bool(prof), "profile": bool(prof and prof.birth_date), "utm": (prof.utm if prof else "") or "",
                     "free": sorted({r.ref for r in rr if r.kind == "material" and (r.data or {}).get("via") != "paid"}),
                     "bmi": sum(1 for r in rr if r.kind == "bmi"), "hads": sum(1 for r in rr if r.kind == "hads"),                      "bought": sum(o.amount for o in oo), "n_bought": len(oo), **clinic.get(pid, {})})
    inp = [r for r in rows if r["at"] and a <= r["at"] <= b]
    paid_p = [o for o in orders if o.status == "paid" and o.paid_at and a <= o.paid_at <= b]
    n = len(inp)
    kpi = {"people": n, "registered": sum(1 for r in inp if r["registered"]), "profile": sum(1 for r in inp if r["profile"]),
           "bmi": sum(1 for r in inp if r["bmi"]), "hads": sum(1 for r in inp if r["hads"]),
           "free": sum(1 for r in inp if r["free"]), "buyers": len({o.patient_id for o in paid_p}), "sold": len(paid_p),
           "revenue": round(sum(o.amount for o in paid_p), 2), "booked": sum(1 for r in inp if r.get("booked")),
           "came": sum(1 for r in inp if r.get("came")), "existing": sum(1 for r in inp if r.get("existing")), "clinic_revenue": round(sum(r.get("revenue") or 0 for r in inp), 2),
           "qr": db.scalar(select(func.count(QrHit.id)).where(QrHit.at >= a, QrHit.at <= b)) or 0}
    kpi["new"] = n - kpi["existing"]
    kpi["pct"] = {k: (round(100 * kpi[k] / n) if n else None) for k in ("profile", "free")}
    kpi["pct"].update({k: (round(100 * kpi[k] / kpi["new"]) if kpi["new"] else None) for k in ("booked", "came")})
    kpi["conv_buy"] = round(100 * kpi["buyers"] / kpi["registered"]) if kpi["registered"] else None
    d = doctor(db)
    salary = sum(float(x.amount or 0) for x in db.scalars(select(SalaryAdj).where(SalaryAdj.article == SALARY_ARTICLE,
                                                                                    SalaryAdj.month >= d_from.strftime("%Y-%m"),
                                                                                    SalaryAdj.month <= d_to.strftime("%Y-%m"))))
    weeks: dict[str, dict] = {}
    for r in inp:
        k = (r["at"].date() - timedelta(days=r["at"].weekday())).strftime("%d.%m")
        w = weeks.setdefault(k, {"week": k, "people": 0, "bought": 0, "came": 0})
        w["people"] += 1
        w["bought"] += 1 if r["n_bought"] else 0
        w["came"] += 1 if r.get("came") else 0
    return {"kpi": kpi, "rows": inp, "all_rows": rows, "weeks": sorted(weeks.values(), key=lambda x: datetime.strptime(x["week"], "%d.%m")),
            "orders": sorted(paid_p, key=lambda o: o.paid_at, reverse=True), "salary": round(salary, 2), "doctor": d,
            "qr": db.execute(select(QrHit.slug, func.count(QrHit.id)).where(QrHit.at >= a, QrHit.at <= b).group_by(QrHit.slug)).all()}


# ---------------- перенос зі старих сервісів (одноразово) ----------------
def import_path() -> Path:
    from ..config import settings
    return Path(settings.files_dir).resolve().parent / "import" / "dk_export.json"


def qr_import_path() -> Path:
    return import_path().parent / "qr_hits.jsonl"


def import_qr(db: Session, lines: list[str]) -> dict:
    """Історія сканувань QR зі старого сервісу (jsonl: at ISO UTC, slug, utm*, device, language, referrer) → QrHit (київський час).
    Повторний імпорт не дублює: пропускаємо, якщо такий slug і час уже є."""
    from zoneinfo import ZoneInfo
    kyiv = ZoneInfo("Europe/Kyiv")
    n = {"added": 0, "skipped": 0}
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            n["skipped"] += 1
            continue
        slug = re.sub(r"[^a-z0-9_-]", "", str(e.get("slug") or "").lower())[:40]
        try:
            at = datetime.fromisoformat(str(e.get("at") or "").replace("Z", "+00:00"))
        except ValueError:
            at = None
        if not slug or not at:
            n["skipped"] += 1
            continue
        at = (at.astimezone(kyiv) if at.tzinfo else at).replace(tzinfo=None)
        if db.scalar(select(QrHit.id).where(QrHit.slug == slug, QrHit.at == at).limit(1)):
            n["skipped"] += 1
            continue
        g = lambda *ks: next((str(e[k])[:80] for k in ks if e.get(k)), None)  # noqa: E731
        db.add(QrHit(slug=slug, at=at, utm_source=g("utmSource", "utm_source"), utm_medium=g("utmMedium", "utm_medium"),
                     utm_campaign=g("utmCampaign", "utm_campaign"), utm_content=g("utmContent", "utm_content"),
                     device=(g("device") or "")[:30] or None, lang=(g("language", "lang") or "")[:24] or None,
                     referrer=(g("referrer") or "")[:120] or None))
        n["added"] += 1
    db.commit()
    return n


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
        if was_patient(db, pats.get(pid), t0):
            came[pid] = "old"
            continue
        came[pid] = bool(db.scalar(select(Visit.id).where(Visit.patient_id == pid, Visit.status == "done", Visit.starts_at >= t0).limit(1))) or \
            any(ap.status in ("done", "paid", "arrived") for ap in db.scalars(select(Appointment).where(Appointment.patient_id == pid, Appointment.start >= t0)))
    return {"rows": rows, "pats": pats, "came": came, "started": len(rows), "done": len(done),
            "avg": round(sum(x.overall_score for x in done) / len(done), 1) if done else None,
            "mods": sorted(((k, round(sum(v) / len(v), 1)) for k, v in mods.items()), key=lambda t: t[1]),
            "risks": sorted(risks.items(), key=lambda t: -t[1])[:8],
            "bands": [(lbl, sum(1 for x in done if lo <= x.overall_score < hi)) for lbl, lo, hi in bands],
            "came_n": sum(1 for v in came.values() if v is True), "old_n": sum(1 for v in came.values() if v == "old")}


def revoke(db: Session, pid: int, mid: str) -> int:
    """Забрати доступ до гайду в кабінеті пацієнта (запис доступу видаляється; оплата й нарахування лишаються)."""
    n = 0
    for r in db.scalars(select(DkRecord).where(DkRecord.patient_id == pid, DkRecord.kind == "material", DkRecord.ref == mid)):
        db.delete(r)
        n += 1
    db.commit()
    return n


def refund(db: Session, oid: int) -> bool:
    """Оплату гайду повернуто: замовлення «refunded» (не рахується в продажах), нарахування Дарʼї прибирається. Доступ не чіпаємо."""
    o = db.get(DkOrder, oid)
    if not o or o.status != "paid":
        return False
    o.status = "refunded"
    if o.salary_adj_id:
        adj = db.get(SalaryAdj, o.salary_adj_id)
        if adj:
            db.delete(adj)
        o.salary_adj_id = None
    db.commit()
    return True


def card(db: Session, pid: int) -> dict | None:
    """Блок «Сайт Дарʼї» в картці пацієнта: профіль, гайди (куплені / безкоштовні), ІМТ і HADS (з сайту й бота), оплати."""
    prof = db.scalar(select(DkProfile).where(DkProfile.patient_id == pid))
    recs = list(db.scalars(select(DkRecord).where(DkRecord.patient_id == pid).order_by(DkRecord.created_at.desc())))
    orders = list(db.scalars(select(DkOrder).where(DkOrder.patient_id == pid).order_by(DkOrder.id.desc())))
    if not (prof or recs or orders):
        return None
    mats = [{"title": (MATERIALS.get(r.ref) or {}).get("title", r.ref), "paid": bool((MATERIALS.get(r.ref) or {}).get("protected")), "at": r.created_at}
            for r in recs if r.kind == "material"]
    return {"profile": prof, "materials": mats, "bmi": [r for r in recs if r.kind == "bmi"], "hads": [r for r in recs if r.kind == "hads"],
            "orders": orders}


def add_bot_test(db: Session, pid: int, kind: str, d: dict) -> DkRecord:
    """Результат тесту з бота турботи (ІМТ / HADS) → картка пацієнта (той самий формат, що з сайту)."""
    if kind == "bmi":
        try:
            h, w = float(d.get("heightCm")), float(d.get("weightKg"))
        except (TypeError, ValueError):
            raise DkError("heightCm і weightKg — числа") from None
        v = round(w / (h / 100) ** 2, 1)
        cat = "Недостатня маса тіла" if v < 18.5 else "Нормальна маса тіла" if v < 25 else "Надмірна маса тіла" if v < 30 else "Ожиріння"
        data = {"heightCm": h, "weightKg": w, "bmi": v, "category": cat}
    elif kind == "hads":
        try:
            a, dep = int(d.get("anxiety")), int(d.get("depression"))
        except (TypeError, ValueError):
            raise DkError("anxiety і depression — цілі числа 0–21") from None
        if not (0 <= a <= 21 and 0 <= dep <= 21):
            raise DkError("anxiety і depression — 0–21")
        data = {"anxiety": a, "depression": dep, "anxietyLevel": _hads_level(a, "Т"), "depressionLevel": _hads_level(dep, "Д")}
    else:
        raise DkError("kind: bmi | hads")
    at = _dt(d.get("at")) or datetime.utcnow()
    ext = str(d.get("id") or "")[:60] or None
    if ext:
        old = db.scalar(select(DkRecord).where(DkRecord.patient_id == pid, DkRecord.kind == kind, DkRecord.ref == f"bot:{ext}"))
        if old:
            return old
    r = DkRecord(patient_id=pid, kind=kind, ref=f"bot:{ext}" if ext else "bot", created_at=at, data={**data, "source": "bot"})
    db.add(r)
    db.commit()
    return r


def cleanup(db: Session, full: list[int], tests_only: list[int]) -> dict:
    """Прибрати тестові анкети «Молодість 360» і тестові картки. Картку видаляємо лише якщо її створив сайт / перенос і в ній немає
    записів, візитів, продажів, чеків, звернень, оплат. Інакше — лише анкети й дані сайту."""
    from ..models import Appeal, DkSession
    out = {"assessments": 0, "patients": 0, "kept": []}
    for pid in set(full) | set(tests_only):
        for a in db.scalars(select(M360Assessment).where(M360Assessment.patient_id == pid)):
            db.delete(a)
            out["assessments"] += 1
        p = db.get(Patient, pid)
        if p:
            p.client_groups = [g for g in (p.client_groups or []) if (g.get("name") if isinstance(g, dict) else g) != "Тест 360"] or None
        if pid not in full or not p:
            continue
        site = (p.source or "") in ("Сайт / Молодість 360", "Сайт Дарʼї Колган") or "старого сервісу" in (p.notes or "")
        busy = any(db.scalar(select(m.id).where(m.patient_id == pid).limit(1)) for m in (Appointment, Visit, Sale, Check, Appeal, PayLink))
        if not site or busy:
            out["kept"].append(pid)
            continue
        for m in (DkRecord, DkOrder, DkProfile, DkSession):
            for x in db.scalars(select(m).where(m.patient_id == pid)):
                db.delete(x)
        db.flush()
        db.delete(p)
        out["patients"] += 1
    db.commit()
    return out
