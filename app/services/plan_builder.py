"""Бюті-план, складений у CRM: шапка (дата консультації, лікар, місце, діагноз, анамнез, заключення, тип мімічної
активності, скарги), послуги й засоби догляду з прайсу/каталогу, порядок позицій (переставляти), дати процедур
(від дати консультації за інтервалами або вручну) і PDF у двох варіантах:
• short — коротко на друк (А4, одна сторінка): пацієнт, лікар, процедури з датами й цінами, домашній догляд, сума;
• full — повний: усе вище + діагноз, анамнез, заключення, тип мімічної активності, схема догляду.
Назва PDF — «ПІП телефон.pdf».
Медичні поля (діагноз, анамнез) редагують і бачать лише користувачі з правом «медичні дані»; у Telegram/SMS не йдуть."""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BeautyPlan, CbService, Patient, PlanItem, Product

MIMIC_TYPES = ["Гіпомімічний", "Нормомімічний", "Гіпермімічний", "Змішаний (асиметрична міміка)"]
PLACE_DEFAULT = "MOLODIST, Київ, вул. Князя Романа Мстиславича, 28А"
USAGE = {"morning": "ранок", "evening": "вечір"}


def ordered(plan: BeautyPlan) -> list[PlanItem]:
    return sorted(plan.items, key=lambda i: (i.pos if i.pos is not None else 10_000 + (i.id or 0), i.id or 0))


def _renumber(plan: BeautyPlan) -> None:
    for n, it in enumerate(ordered(plan)):
        it.pos = n


def create(db: Session, patient: Patient, doctor_id: int | None) -> BeautyPlan:
    pl = BeautyPlan(patient_id=patient.id, doctor_id=doctor_id, title="Бюті-план", session_at=datetime.now(),
                    status="active", place=PLACE_DEFAULT, built_in_crm=True, total_planned=0)
    db.add(pl)
    db.commit()
    return pl


def recalc_dates(plan: BeautyPlan, start: date | None = None) -> None:
    """Дати процедур по черзі: перша — дата консультації (або start), кожна наступна = попередня + інтервал попередньої."""
    d = start or (plan.session_at.date() if plan.session_at else date.today())
    prev = None
    for it in ordered(plan):
        if it.kind != "procedure":
            continue
        if prev is not None:            # курс попередньої процедури: к-сть × інтервал
            d = d + timedelta(days=int(prev.interval_days or 0) * max(1, prev.qty or 1))
        it.plan_date = d
        prev = it


def move(plan: BeautyPlan, item_id: int, delta: int) -> None:
    items = ordered(plan)
    idx = next((i for i, x in enumerate(items) if x.id == item_id), None)
    if idx is None:
        return
    j = max(0, min(len(items) - 1, idx + delta))
    items[idx], items[j] = items[j], items[idx]
    for n, it in enumerate(items):
        it.pos = n


def _num(v, cast=float):
    s = str(v or "").replace(",", ".").replace(" ", "").strip()
    if not s:
        return None
    try:
        return cast(float(s))
    except ValueError:
        return None


def _date(v) -> date | None:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def save(db: Session, plan: BeautyPlan, form, medical: bool, is_doctor_user: bool) -> dict:
    """Зберегти шапку й позиції з форми редактора. Повертає зміни для журналу дій."""
    ch: dict = {}

    def setf(field, val):
        if getattr(plan, field) != val:
            ch[field] = [str(getattr(plan, field))[:80] if getattr(plan, field) is not None else None, str(val)[:80] if val is not None else None]
            setattr(plan, field, val)

    sa = _date(form.get("session_at"))
    if sa:
        t = plan.session_at.time() if plan.session_at else datetime.now().time().replace(second=0, microsecond=0)
        setf("session_at", datetime.combine(sa, t))
    for f, lim in (("complaints", 2000), ("conclusion", 4000), ("place", 200), ("title", 200), ("routine_comment", 2000)):
        if f in form:
            setf(f, (str(form.get(f) or "").strip()[:lim] or None))
    if "mimic_type" in form:
        mt = str(form.get("mimic_type") or "").strip()[:60] or None
        setf("mimic_type", mt)
    if medical:
        for f in ("diagnosis", "anamnesis"):
            if f in form:
                setf(f, str(form.get(f) or "").strip()[:4000] or None)
    if not is_doctor_user and str(form.get("doctor_id") or "").isdigit():
        setf("doctor_id", int(form.get("doctor_id")))
    if form.get("status") in ("active", "completed", "cancelled"):
        setf("status", form.get("status"))

    for it in list(plan.items):
        if form.get(f"del_{it.id}"):
            ch[f"-{it.name[:40]}"] = ["позиція", "видалено"]
            plan.items.remove(it)
            continue
        nm = " ".join(str(form.get(f"name_{it.id}") or "").split())[:300]
        if nm and nm != it.name:
            it.name = nm
        for f, cast in (("unit_price", float), ("qty", int), ("interval_days", int), ("supply_days", int)):
            v = _num(form.get(f"{f}_{it.id}"), cast)
            if v is not None and v >= 0 and getattr(it, f) != v:
                setattr(it, f, max(1, v) if f == "qty" else v)
        if f"date_{it.id}" in form:
            it.plan_date = _date(form.get(f"date_{it.id}"))
        if f"note_{it.id}" in form:
            it.note = str(form.get(f"note_{it.id}") or "").strip()[:300] or None
        it.total = (it.qty or 1) * (it.unit_price or 0)

    # нові позиції
    pos = max([i.pos or 0 for i in plan.items] + [0]) + 1
    for k_, n_, p_, q_, iv_ in zip(form.getlist("new_kind"), form.getlist("new_name"), form.getlist("new_price"),
                                   form.getlist("new_qty"), form.getlist("new_interval")):
        n_ = " ".join(str(n_ or "").split())[:300]
        if not n_:
            continue
        kind = "product" if k_ == "product" else "procedure"
        q = max(1, _num(q_, int) or 1)
        pr = _num(p_)
        prod = svc = None
        if kind == "product":
            prod = db.scalar(select(Product).where(Product.name == n_).limit(1))
            if pr is None and prod:
                pr = prod.price or prod.price_manual
        else:
            svc = db.scalar(select(CbService).where(CbService.name == n_).limit(1))
            if pr is None and svc:
                pr = svc.price
        pr = float(pr or 0)
        iv = _num(iv_, int)
        it = PlanItem(kind=kind, name=n_, qty=q, unit_price=pr, total=q * pr, manual_done=0, pos=pos,
                      product_id=prod.id if prod else None,
                      interval_days=iv if kind == "procedure" else None, supply_days=iv if kind == "product" else None,
                      volume=(getattr(prod, "volume", None) if prod else None))
        plan.items.append(it)
        pos += 1
        ch[f"+{n_[:40]}"] = [None, f"{q} × {pr:g}"]

    _renumber(plan)
    act = str(form.get("action") or "")
    m = re.match(r"(up|down)_(\d+)$", act)
    if m:
        move(plan, int(m.group(2)), -1 if m.group(1) == "up" else 1)
    if act == "dates" or (m and form.get("auto_dates")):
        recalc_dates(plan)
    plan.total_planned = round(sum((i.qty or 1) * (i.unit_price or 0) for i in plan.items), 2)
    db.commit()
    return ch


# ---------------- PDF ----------------
def filename(plan: BeautyPlan) -> str:
    p = plan.patient
    name = re.sub(r"[\\/:*?\"<>|]+", " ", (p.full_name if p else "Пацієнт") or "Пацієнт").strip()
    phone = re.sub(r"\D+", "", (p.phone if p else "") or "")
    return f"{name} {phone}".strip() + ".pdf"


def _uah(v) -> str:
    return f"{(v or 0):,.0f} ₴".replace(",", " ")


def pdf(plan: BeautyPlan, variant: str = "short", show_medical: bool = True) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from .m360.pdf_report import _register_fonts
    _register_fonts()
    full = variant == "full"
    GRAPH, DEEP, MUT, LAV, CREAM = (colors.HexColor(x) for x in ("#2A2430", "#3B3050", "#8A8496", "#E4DEEF", "#F7F4F0"))
    st = {
        "brand": ParagraphStyle("b", fontName="MolodistBold", fontSize=18, textColor=DEEP, leading=22, spaceAfter=0, tracking=3),
        "sub": ParagraphStyle("s", fontName="MolodistSans", fontSize=8, textColor=MUT, leading=10),
        "h": ParagraphStyle("h", fontName="MolodistBold", fontSize=10.5, textColor=DEEP, leading=13, spaceBefore=8, spaceAfter=4),
        "t": ParagraphStyle("t", fontName="MolodistSans", fontSize=9.5, textColor=GRAPH, leading=12.5),
        "tb": ParagraphStyle("tb", fontName="MolodistBold", fontSize=9.5, textColor=GRAPH, leading=12.5),
        "small": ParagraphStyle("sm", fontName="MolodistSans", fontSize=8, textColor=MUT, leading=10),
    }
    P = lambda txt, s="t": Paragraph(str(txt or "").replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>"), st[s])  # noqa: E731
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=filename(plan)[:-4], author="MOLODIST")
    W = A4[0] - 32 * mm
    story = [Paragraph("M O L O D I S T", st["brand"]), P("медичний простір естетики та профілактики", "sub"), Spacer(1, 8)]
    p = plan.patient
    head = [[P("Пацієнт", "small"), P(p.full_name if p else "—", "tb"), P("Дата консультації", "small"),
             P(plan.session_at.strftime("%d.%m.%Y") if plan.session_at else "—", "tb")],
            [P("Телефон", "small"), P((p.phone if p else "") or "—"), P("Лікар", "small"), P(plan.doctor.name if plan.doctor else "—")]]
    if full:
        head.append([P("Місце", "small"), P(plan.place or PLACE_DEFAULT), P("Тип мімічної активності", "small"), P(plan.mimic_type or "—")])
    t = Table(head, colWidths=[24 * mm, W / 2 - 24 * mm, 34 * mm, W / 2 - 34 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), CREAM), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [Paragraph("БЮТІ-ПЛАН", st["h"]), t]
    if plan.complaints:
        story += [Paragraph("Скарги й побажання", st["h"]), P(plan.complaints)]
    if full and show_medical:
        if plan.anamnesis:
            story += [Paragraph("Анамнез", st["h"]), P(plan.anamnesis)]
        if plan.diagnosis:
            story += [Paragraph("Діагноз", st["h"]), P(plan.diagnosis)]
    if full and plan.conclusion:
        story += [Paragraph("Заключення лікаря", st["h"]), P(plan.conclusion)]

    items = ordered(plan)
    procs = [i for i in items if i.kind == "procedure"]
    prods = [i for i in items if i.kind == "product"]

    def table(rows, widths, hdr):
        tt = Table([hdr] + rows, colWidths=widths, repeatRows=1)
        tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), LAV), ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#E9E5DF")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                ("ALIGN", (-1, 0), (-1, -1), "RIGHT")]))
        return tt
    if procs:
        rows = []
        for n, i in enumerate(procs, 1):
            nm = P(i.name + (f"\n{i.note}" if full and i.note else ""))
            rows.append([P(str(n)), P(i.plan_date.strftime("%d.%m.%Y") if i.plan_date else "—"), nm,
                         P(f"кожні {i.interval_days} дн." if i.interval_days else "—"), P(str(i.qty or 1)), P(_uah((i.qty or 1) * (i.unit_price or 0)))])
        story += [Paragraph("Процедури", st["h"]),
                  table(rows, [8 * mm, 25 * mm, W - 8 * mm - 25 * mm - 27 * mm - 15 * mm - 26 * mm, 27 * mm, 15 * mm, 26 * mm],
                        [P("№", "small"), P("Дата", "small"), P("Процедура", "small"), P("Інтервал", "small"), P("К-сть", "small"), P("Сума", "small")])]
    if prods:
        rows = []
        for i in prods:
            use = " / ".join(USAGE.get(u, u) for u in (i.usage or []))
            nm = P(i.name + (f" · {i.volume}" if i.volume else "") + (f"\n{use}" if full and use else "") + (f"\n{i.note}" if full and i.note else ""))
            rows.append([nm, P(f"{i.supply_days} дн." if i.supply_days else "—"), P(str(i.qty or 1)), P(_uah((i.qty or 1) * (i.unit_price or 0)))])
        story += [Paragraph("Домашній догляд", st["h"]),
                  table(rows, [W - 27 * mm - 15 * mm - 26 * mm, 27 * mm, 15 * mm, 26 * mm],
                        [P("Засіб", "small"), P("Вистачає на", "small"), P("К-сть", "small"), P("Сума", "small")])]
    tot_p = sum((i.qty or 1) * (i.unit_price or 0) for i in procs)
    tot_c = sum((i.qty or 1) * (i.unit_price or 0) for i in prods)
    tt = Table([[P("Процедури"), P(_uah(tot_p))], [P("Домашній догляд"), P(_uah(tot_c))], [P("Разом за планом", "tb"), P(_uah(tot_p + tot_c), "tb")]],
               colWidths=[W - 40 * mm, 40 * mm])
    tt.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, 2), (-1, 2), 0.8, DEEP)]))
    story += [Spacer(1, 8), tt]
    if full and plan.routine_comment:
        story += [Paragraph("Коментар до догляду", st["h"]), P(plan.routine_comment)]
    story += [Spacer(1, 12), P("Ціни дійсні на дату складання плану. План складено лікарем MOLODIST за результатами консультації; "
                               "за потреби лікар коригує його на наступних візитах.", "small")]
    doc.build(story)
    return buf.getvalue()
