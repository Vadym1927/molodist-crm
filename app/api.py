"""REST API v1 для зовнішніх систем (n8n, сайт, інші CRM, BI).

Авторизація: заголовок `X-API-Key: mcrm_...` (або `Authorization: Bearer mcrm_...`).
Ключі створює власник у CRM → «Доступи → API». Дозволи ключа:
  read    — читання пацієнтів, планів, KPI, прогнозу, нагадувань
  write   — створення/оновлення пацієнтів і планів, завантаження PDF, запуск синку
  medical — у відповідях з'являються медичні нотатки лікаря
Документація (Swagger) — /api/docs (лише для власника, після входу в CRM).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Patient, Doctor, BeautyPlan, ApiKey
from .parsers.observ import ParsedPlan, PlanItem as ParsedItem, norm
from .security import db_dep, scope, audit
from .services import ingest, reminders
from .services.kpi import compute_plan, all_statuses, kpi_summary, forecast
from .services.plans import import_plan, get_or_create_doctor, find_patient
from .services.serialize import plan_dict, patient_brief, kpi_dict
from .integrations import webhooks

router = APIRouter(prefix="/api/v1", tags=["CRM API v1"])
READ, WRITE = scope("read"), scope("write")


def _medical(key: ApiKey) -> bool:
    return "medical" in (key.scopes or [])


# ---------------- схеми ----------------

class PatientIn(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    cleverbox_id: int | None = None
    doctor: str | None = Field(None, description="ПІБ лікаря; створюється, якщо немає")
    notes: str | None = None


class ItemIn(BaseModel):
    kind: str = Field(..., pattern="^(procedure|product)$")
    name: str
    qty: int = 1
    unit_price: float = 0
    volume: str | None = None
    usage: list[str] = []
    url: str | None = None
    interval_days: int | None = None
    supply_days: int | None = None


class PlanIn(BaseModel):
    patient: PatientIn
    doctor: str | None = None
    session_at: datetime | None = None
    title: str | None = None
    complaints: str | None = None
    medical_notes: str | None = None
    routine_comment: str | None = None
    items: list[ItemIn]


class ItemPatch(BaseModel):
    id: int
    qty: int | None = None
    manual_done: int | None = None
    interval_days: int | None = None
    supply_days: int | None = None


class PlanPatch(BaseModel):
    status: str | None = Field(None, pattern="^(active|completed|cancelled)$")
    items: list[ItemPatch] = []


# ---------------- пацієнти ----------------

@router.get("/patients", summary="Список пацієнтів зі статусом останнього плану")
def list_patients(q: str | None = None, flag: str | None = None, doctor_id: int | None = None,
                  limit: int = Query(100, le=500), offset: int = 0,
                  key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    words = set(norm(q).split()) if q else set()
    out = []
    for p in db.scalars(select(Patient).order_by(Patient.id)):
        if words and not (words <= set(norm(p.full_name).split()) or (p.phone and q in p.phone)):
            continue
        plan = p.plans[0] if p.plans else None
        if doctor_id and (not plan or plan.doctor_id != doctor_id):
            continue
        ps = compute_plan(db, plan) if plan else None
        if flag and (not ps or ps.flag != flag):
            continue
        out.append({**patient_brief(p), "last_plan": {
            "id": plan.id, "session_at": plan.session_at, "flag": ps.flag, "planned": ps.planned_sum,
            "realization_pct": ps.realization_pct, "next_booking": ps.next_booking} if plan else None})
    return {"total": len(out), "items": out[offset:offset + limit]}


@router.get("/patients/{pid}", summary="Пацієнт з усіма планами, візитами і покупками")
def get_patient(pid: int, key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    p = db.get(Patient, pid) or _404()
    return {**patient_brief(p), "notes": p.notes,
            "plans": [plan_dict(compute_plan(db, pl), _medical(key)) for pl in p.plans],
            "visits": [{"date": v.starts_at, "service": v.service_name, "master": v.master_name,
                        "status": v.status} for v in p.visits],
            "product_sales": [{"date": s.sold_at, "product": s.product_name, "qty": s.quantity,
                               "amount": s.amount} for s in p.sales]}


@router.post("/patients", summary="Створити або оновити пацієнта (пошук за cleverbox_id → телефоном → ПІБ)")
def upsert_patient(body: PatientIn, key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    p = None
    if body.cleverbox_id:
        p = db.scalar(select(Patient).where(Patient.cleverbox_id == body.cleverbox_id))
    if not p and (body.phone or body.full_name):
        p = find_patient(db, body.full_name or "", body.phone)
    created = p is None
    if created:
        if not body.full_name:
            raise HTTPException(422, "Для нового пацієнта потрібне full_name")
        p = Patient(full_name=body.full_name)
        db.add(p)
    _apply_patient(db, p, body)
    db.commit()
    audit(db, "api.patient." + ("create" if created else "update"), "patient", p.id, key=key)
    return {"created": created, **patient_brief(p)}


@router.patch("/patients/{pid}", summary="Оновити поля пацієнта")
def patch_patient(pid: int, body: PatientIn, key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    p = db.get(Patient, pid) or _404()
    _apply_patient(db, p, body)
    db.commit()
    audit(db, "api.patient.update", "patient", p.id, key=key)
    return patient_brief(p)


def _apply_patient(db, p: Patient, body: PatientIn):
    for f in ("full_name", "phone", "cleverbox_id", "notes"):
        v = getattr(body, f)
        if v is not None:
            setattr(p, f, v)
    if body.doctor:
        p.doctor_id = get_or_create_doctor(db, body.doctor).id


# ---------------- плани ----------------

@router.get("/plans", summary="Бюті-плани зі статусом реалізації")
def list_plans(status: str | None = None, doctor_id: int | None = None, date_from: date | None = None,
               date_to: date | None = None, key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    sts = all_statuses(db, date_from, date_to, doctor_id)
    if status:
        sts = [s for s in sts if s.plan.status == status]
    return {"total": len(sts), "items": [plan_dict(s, _medical(key)) for s in sts]}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    pl = db.get(BeautyPlan, plan_id) or _404()
    return plan_dict(compute_plan(db, pl), _medical(key))


@router.post("/plans", summary="Створити бюті-план з JSON (з іншої системи, форми, n8n)")
def create_plan(body: PlanIn, key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    patient = None
    if body.patient.cleverbox_id:
        patient = db.scalar(select(Patient).where(Patient.cleverbox_id == body.patient.cleverbox_id))
    if not body.patient.full_name and not patient:
        raise HTTPException(422, "patient.full_name обов'язкове")
    parsed = ParsedPlan(
        patient_name=body.patient.full_name or patient.full_name, doctor_name=body.doctor or body.patient.doctor,
        session_at=body.session_at or datetime.now(), complaints=body.complaints, comments=body.medical_notes,
        routine_comment=body.routine_comment, patient_phone=body.patient.phone, plan_title=body.title,
        items=[ParsedItem(kind=i.kind, name=i.name, qty=i.qty, unit_price=i.unit_price, total=i.qty * i.unit_price,
                          volume=i.volume, usage=i.usage, url=i.url) for i in body.items],
        total=sum(i.qty * i.unit_price for i in body.items))
    plan = import_plan(db, parsed, patient=patient)
    if body.patient.cleverbox_id and not plan.patient.cleverbox_id:
        plan.patient.cleverbox_id = body.patient.cleverbox_id
    for it, src in zip(plan.items, body.items):
        it.interval_days = src.interval_days or it.interval_days
        it.supply_days = src.supply_days or it.supply_days
    db.commit()
    data = plan_dict(compute_plan(db, plan), _medical(key))
    audit(db, "api.plan.create", "plan", plan.id, key=key)
    webhooks.fire("plan.created", plan_dict(compute_plan(db, plan)))
    return data


@router.post("/plans/upload", summary="Завантажити PDF бюті-плану Observ")
async def upload_plan(file: UploadFile = File(...), key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    plan, _, err = ingest.ingest_pdf(db, await file.read(), file.filename or "plan.pdf")
    if err:
        raise HTTPException(422, err)
    audit(db, "api.plan.upload", "plan", plan.id, key=key, file=file.filename)
    return plan_dict(compute_plan(db, plan), _medical(key))


@router.patch("/plans/{plan_id}", summary="Змінити статус плану або позиції (к-сть, ручна відмітка, інтервали)")
def patch_plan(plan_id: int, body: PlanPatch, key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    pl = db.get(BeautyPlan, plan_id) or _404()
    if body.status:
        pl.status = body.status
    items = {i.id: i for i in pl.items}
    for ch in body.items:
        it = items.get(ch.id) or _404(f"Позиції {ch.id} немає в плані {plan_id}")
        for f in ("qty", "manual_done", "interval_days", "supply_days"):
            v = getattr(ch, f)
            if v is not None:
                setattr(it, f, v)
        it.total = it.qty * it.unit_price
    pl.total_planned = sum(i.total for i in pl.items)
    db.commit()
    audit(db, "api.plan.update", "plan", pl.id, key=key, changes=body.model_dump(exclude_none=True))
    data = plan_dict(compute_plan(db, pl), _medical(key))
    webhooks.fire("plan.updated", plan_dict(compute_plan(db, pl)))
    return data


# ---------------- аналітика ----------------

@router.get("/kpi", summary="KPI: загальний і по лікарях")
def kpi(date_from: date | None = None, date_to: date | None = None,
        key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    total, by_doc = kpi_summary(all_statuses(db, date_from, date_to))
    return {"total": kpi_dict(total), "by_doctor": [kpi_dict(r) for r in by_doc]}


@router.get("/forecast", summary="Фінансовий прогноз по місяцях")
def get_forecast(months: int = Query(6, ge=1, le=24), key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    return {"months": forecast(all_statuses(db), months=months)}


@router.get("/reminders", summary="Актуальні нагадування (не записані, закінчується догляд)")
def get_reminders(key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    return {"items": [{"kind": r.kind, "patient_id": r.patient_id, "text_html": r.text}
                      for r in reminders.collect(db)]}


@router.get("/doctors")
def doctors(key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    return [{"id": d.id, "name": d.name, "cleverbox_id": d.cleverbox_id} for d in db.scalars(select(Doctor))]


@router.post("/sync", summary="Запустити синхронізацію з CleverBox зараз")
async def sync_now(key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    from .main import run_sync
    rep = await asyncio.to_thread(run_sync)
    audit(db, "api.sync", key=key)
    return {"ok": True, "report": rep}


def _404(msg: str = "Не знайдено"):
    raise HTTPException(404, msg)


# ---------------- експорт / імпорт ----------------

@router.get("/export/report.xlsx", summary="Звіт Excel: плани, позиції, KPI, прогноз")
def api_export_report(date_from: date | None = None, date_to: date | None = None,
                      key: ApiKey = Depends(READ), db: Session = Depends(db_dep)):
    from fastapi.responses import Response
    from .integrations.sheets import build_tables
    from .services.exporter import to_xlsx
    data = to_xlsx(build_tables(db, statuses=all_statuses(db, date_from, date_to)))
    audit(db, "api.export.report", key=key)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=molodist_report.xlsx"})


@router.post("/import", summary="Імпорт Excel/CSV (patients | plans | history; без kind — автоматично)")
async def api_import(file: UploadFile = File(...), kind: str | None = Query(None, pattern="^(patients|plans|history)$"),
                     key: ApiKey = Depends(WRITE), db: Session = Depends(db_dep)):
    from .services.importer import import_file
    try:
        reports = import_file(db, await file.read(), file.filename or "data.xlsx", kind)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit(db, "api.import", key=key, file=file.filename)
    return {"reports": reports}


# ---------------- бот для пацієнтів (OpenClaw) ----------------
# Ключ з дозволом «bot» (Доступи → API). Бот викликає ці методи; CRM шле повідомлення пацієнтам токеном бота.

BOT = scope("bot")


class BotLink(BaseModel):
    chat_id: str
    phone: str | None = Field(None, description="Номер з кнопки «Поділитися контактом»")
    username: str | None = None
    name: str | None = Field(None, description="Імʼя з Telegram — для нової картки, якщо пацієнта ще немає")
    create_if_missing: bool = False


class BotChat(BaseModel):
    chat_id: str


def _by_chat(db: Session, chat_id: str) -> Patient:
    p = db.scalar(select(Patient).where(Patient.telegram_chat_id == str(chat_id)))
    if not p:
        raise HTTPException(404, "Пацієнта з цим chat_id не привʼязано")
    return p


class AppealIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    source: str | None = None
    cause: str | None = None
    interest: str | None = None
    comment: str | None = None
    utm: str | None = None


@router.post("/appeals", summary="Нове звернення (бот, форма сайту, n8n): імʼя / телефон, джерело, що цікавить")
def api_appeal(body: AppealIn, key: ApiKey = Depends(BOT), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    if not (body.name or body.phone):
        raise HTTPException(400, "Потрібні імʼя або телефон")
    a = ap.create(db, body.model_dump())
    audit(db, "appeal.api", "appeal", a.id, key=key, source=a.source)
    return {"id": a.id, "status": a.status, "patient_id": a.patient_id}


@router.post("/bot/link", summary="Привʼязати Telegram пацієнта (за телефоном з контакту)")
def bot_link(body: BotLink, key: ApiKey = Depends(BOT), db: Session = Depends(db_dep)):
    from .integrations.messaging import normalize_phone
    phone = normalize_phone(body.phone)
    p = None
    if phone:
        tail = phone[-9:]
        for cand in db.scalars(select(Patient).where(Patient.phone.is_not(None))):
            if normalize_phone(cand.phone) and normalize_phone(cand.phone)[-9:] == tail:
                p = cand
                break
    created = False
    if not p and body.create_if_missing and phone and body.name:
        p = Patient(full_name=body.name.strip(), phone=body.phone, source="Telegram-бот")
        db.add(p)
        created = True
    if not p:
        return {"linked": False, "reason": "пацієнта з таким телефоном немає в CRM"}
    # той самий chat_id не може бути привʼязаний до двох карток
    for other in db.scalars(select(Patient).where(Patient.telegram_chat_id == str(body.chat_id), Patient.id != (p.id or 0))):
        other.telegram_chat_id = None
    p.telegram_chat_id, p.telegram_username = str(body.chat_id), body.username
    p.telegram_linked_at = datetime.utcnow()
    db.commit()
    audit(db, "bot.link", "patient", p.id, key=key, created=created)
    return {"linked": True, "created": created, "patient_id": p.id, "name": p.full_name}


@router.post("/bot/unsubscribe", summary="Пацієнт відписався від розсилок (сервісні нагадування лишаються)")
def bot_unsubscribe(body: BotChat, key: ApiKey = Depends(BOT), db: Session = Depends(db_dep)):
    p = _by_chat(db, body.chat_id)
    p.marketing_opt_out, p.opt_out_at = True, datetime.now()
    db.commit()
    audit(db, "bot.unsubscribe", "patient", p.id, key=key)
    return {"ok": True}


@router.post("/bot/subscribe", summary="Пацієнт знову погодився на розсилки")
def bot_subscribe(body: BotChat, key: ApiKey = Depends(BOT), db: Session = Depends(db_dep)):
    p = _by_chat(db, body.chat_id)
    p.marketing_opt_out, p.opt_out_at = False, None
    db.commit()
    audit(db, "bot.subscribe", "patient", p.id, key=key)
    return {"ok": True}


@router.post("/bot/unlink", summary="Відвʼязати Telegram (пацієнт вийшов з бота)")
def bot_unlink(body: BotChat, key: ApiKey = Depends(BOT), db: Session = Depends(db_dep)):
    p = _by_chat(db, body.chat_id)
    p.telegram_chat_id = None
    db.commit()
    audit(db, "bot.unlink", "patient", p.id, key=key)
    return {"ok": True}


@router.get("/bot/patient/{chat_id}", summary="«Мій бюті-план» для пацієнта: прогрес, наступна процедура, догляд")
def bot_patient(chat_id: str, key: ApiKey = Depends(BOT), db: Session = Depends(db_dep)):
    from datetime import timedelta
    from .services.scripts import first_name
    p = _by_chat(db, chat_id)
    out = {"name": first_name(p.full_name), "subscribed": not p.marketing_opt_out, "plan": None}
    nxt_visit = min((v.starts_at for v in p.visits if v.status == "booked" and v.starts_at > datetime.now()), default=None)
    out["next_visit"] = nxt_visit.isoformat(timespec="minutes") if nxt_visit else None
    if p.plans:
        ps = compute_plan(db, p.plans[0])
        nxt = ps.next_procedure
        care = []
        for st in ps.items:
            if st.item.kind != "product":
                continue
            ends = (st.last_done + timedelta(days=st.item.supply_days)) if st.last_done and st.item.supply_days else None
            care.append({"name": st.item.name, "url": st.item.url, "usage": st.item.usage or [],
                         "bought": st.done >= st.item.qty, "ends_about": ends.date().isoformat() if ends else None})
        out["plan"] = {
            "title": ps.plan.title, "date": ps.plan.session_at.date().isoformat() if ps.plan.session_at else None,
            "doctor": ps.plan.doctor.name if ps.plan.doctor else None,
            "procedures": [{"name": st.item.name, "done": min(st.done, st.item.qty), "total": st.item.qty,
                            "booked": st.booked} for st in ps.items if st.item.kind == "procedure"],
            "progress_pct": round(100 * ps.proc_done / ps.proc_planned) if ps.proc_planned else 0,
            "next_procedure": nxt.item.name if nxt else None,
            "next_due": ps.due_date.date().isoformat() if ps.due_date and nxt else None,
            "home_care": care, "routine_comment": ps.plan.routine_comment,
        }
    return out
