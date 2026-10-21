"""MOLODIST Beauty Plan CRM — веб-інтерфейс (з ролями), REST API, планувальник синхронізації та Telegram-бот."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .models import (init_db, SessionLocal, Patient, Doctor, BeautyPlan, PlanItem, PatientFile, Alias,
                     CbService, Visit, SyncState, User, ApiKey, Webhook, AuditLog, ROLES)
from .parsers.observ import norm
from .security import (can, hash_password, verify_password, ensure_owner, db_dep, current_user, require,
                       patient_visible, plan_visible, new_api_key, audit)
from .services import ingest, reminders
from .services.kpi import compute_plan, all_statuses, kpi_summary, forecast, name_match
from .services.serialize import plan_dict
from .integrations import webhooks
from . import api as api_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("crm")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["uah"] = lambda v: f"{(v or 0):,.0f}".replace(",", " ") + " ₴"
templates.env.filters["dt"] = lambda v, f="%d.%m.%Y": v.strftime(f) if v else "—"
templates.env.globals.update(
    can=can, ROLES=ROLES, EVENTS=webhooks.EVENTS,
    FLAG={"green": ("записана", "ok"), "yellow": ("без запису", "warn"),
          "red": ("прострочено", "bad"), "done": ("завершено", "done")})

state = {"bot": None, "last_sync": None, "last_error": None}


def _set_sync_state(key: str, value: str | None) -> None:
    with SessionLocal() as db:
        item = db.get(SyncState, key) or SyncState(key=key)
        item.value = value
        db.merge(item)
        db.commit()


def auto_sync_enabled() -> bool:
    """Auto-sync stays enabled unless an owner explicitly turns it off."""
    with SessionLocal() as db:
        item = db.get(SyncState, "cleverbox_auto_sync")
        return item is None or item.value != "0"


def ensure_sync_schedule() -> None:
    """Keep one automatic CleverBox job, including after a token is set in the UI."""
    sched = state.get("scheduler")
    if not sched:
        return
    if settings.cleverbox_token and auto_sync_enabled():
        sched.add_job(job_sync, "cron", hour="*/2", minute=5,
                      id="cleverbox_sync", replace_existing=True)
    elif sched.get_job("cleverbox_sync"):
        sched.remove_job("cleverbox_sync")


# ---------------- фонові задачі ----------------

def run_sync() -> dict:
    from .integrations.cleverbox import full_sync
    from .integrations.cb_webhooks import SYNC_LOCK
    with SYNC_LOCK, SessionLocal() as db:
        rep = full_sync(db)
    state["last_sync"] = (datetime.now(), rep)
    from .services.tasks import generate_auto
    with SessionLocal() as db:
        rep["tasks"] = generate_auto(db)
    webhooks.fire("sync.done", {"report": {k: v for k, v in rep.items() if k != "patients"},
                                "patients_linked": rep.get("patients", {}).get("linked")})
    if settings.google_sheet_id and settings.google_service_account_file:
        try:
            run_sheets()
        except Exception as e:      # noqa: BLE001
            log.warning("Sheets export failed: %s", e)
    return rep


def run_sheets() -> str:
    from .integrations.sheets import export
    with SessionLocal() as db:
        return export(db)


async def job_sync():
    from .services import alerts
    try:
        rep = await asyncio.to_thread(run_sync)
        state["last_error"] = None
        _set_sync_state("cleverbox_last_success", datetime.now().isoformat(timespec="seconds"))
        _set_sync_state("cleverbox_last_error", "")
        partial = [f"{k}: {v}" for k, v in rep.items() if isinstance(v, str) and v.startswith("помилка")]
        if partial:      # основний синк пройшов, але окремий модуль (склад/чеки/календар) — ні
            await asyncio.to_thread(alerts.failure, "sync_part", "; ".join(partial),
                                    "Можливо, модуль вимкнений у тарифі CleverBox або немає прав у токена.")
        else:
            await asyncio.to_thread(alerts.success, "sync_part")
        await asyncio.to_thread(alerts.success, "sync")
    except Exception as e:          # noqa: BLE001
        state["last_error"] = f"{datetime.now():%d.%m %H:%M} {e}"
        _set_sync_state("cleverbox_last_error", state["last_error"])
        log.exception("sync failed")
        await asyncio.to_thread(alerts.failure, "sync", str(e), alerts.hint_for(str(e)))


async def job_campaigns():
    """Запуск запланованих розсилок і продовження перерваних (після рестарту / тихих годин)."""
    from .services import campaigns as camp, jobs
    from .models import Campaign
    try:
        with SessionLocal() as db:
            camp.run_due(db)
            ids = [c.id for c in db.scalars(select(Campaign).where(Campaign.status == "sending"))]
        for cid in ids:
            jobs.start(f"Розсилка #{cid}", camp.deliver, campaign_id=cid)
    except Exception:              # noqa: BLE001
        log.exception("campaigns job failed")


async def job_message_statuses():
    from .services import campaigns as camp
    def _refresh():
        with SessionLocal() as db:
            return camp.refresh_statuses(db)
    try:
        await asyncio.to_thread(_refresh)
    except Exception:              # noqa: BLE001
        log.exception("statuses refresh failed")


async def job_backup():
    from .services import backups
    try:
        await asyncio.to_thread(backups.run_backup, "auto")
    except Exception:              # noqa: BLE001
        pass                        # сповіщення вже надіслано в run_backup


async def job_digest():
    from .services.tasks import generate_auto
    from .services import alerts
    try:
        with SessionLocal() as db:
            generate_auto(db)
            rems = reminders.collect(db)
        webhooks.fire("reminders.daily", {"items": [{"kind": r.kind, "patient_id": r.patient_id, "text_html": r.text}
                                                    for r in rems]})
        if state["bot"]:
            from .integrations.telegram_bot import send_digest
            await send_digest(state["bot"])
        await asyncio.to_thread(alerts.success, "digest")
    except Exception as e:          # noqa: BLE001
        log.exception("digest failed")
        await asyncio.to_thread(alerts.failure, "digest", str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_owner(db)
        from .integrations.cleverbox import load_saved_token
        load_saved_token(db)
        from .services.scripts import seed_defaults
        seed_defaults(db)
    tasks, sched = [], None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        sched = AsyncIOScheduler(timezone=settings.timezone)
        state["scheduler"] = sched
        ensure_sync_schedule()
        sched.add_job(job_digest, "cron", day_of_week="mon-sat", hour=settings.remind_hour, minute=0)
        sched.add_job(job_backup, "cron", hour=settings.backup_hour, minute=30)
        sched.add_job(job_campaigns, "interval", minutes=5)
        sched.add_job(job_message_statuses, "interval", minutes=30)
        sched.start()
    except Exception as e:          # noqa: BLE001
        log.warning("scheduler not started: %s", e)
    if settings.telegram_bot_token:
        from .integrations.telegram_bot import make_bot
        bot, dp = make_bot()
        state["bot"] = bot
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
    yield
    for t in tasks:
        t.cancel()
    if sched:
        sched.shutdown(wait=False)
    state["scheduler"] = None


app = FastAPI(title="MOLODIST Beauty Plan CRM API", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None,
              description=api_module.__doc__)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 14,
                   https_only=settings.public_url.startswith("https"))
app.include_router(api_module.router)


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(request, "error.html", {"request": request, "user": user, "nav": "",
                                      "code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)


def render(request: Request, name: str, **ctx):
    user = getattr(request.state, "user", None)
    counts = None
    if user:
        from .services.tasks import counts_for
        with SessionLocal() as db:
            counts = counts_for(db, user)
    return templates.TemplateResponse(request, name, {"request": request, "nav": name.split(".")[0],
                                                      "user": user, "task_counts": counts, **ctx})


def doctor_scope(user: User) -> int | None:
    """Лікар бачить лише свої плани; власник/адмін — усі."""
    return user.doctor_id if user.role == "doctor" else None


# ---------------- вхід ----------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), db: Session = Depends(db_dep)):
    from .security import login_limiter, ip_limiter, client_ip
    ip, lg = client_ip(request), login.strip().lower()
    left = max(login_limiter.blocked_for(f"login:{lg}"), ip_limiter.blocked_for(f"ip:{ip}"))
    if left:
        return render(request, "login.html", login=login,
                      error=f"Забагато невдалих спроб. Вхід тимчасово заблоковано — спробуйте через {left} хв.")
    u = db.scalar(select(User).where(func.lower(User.login) == lg))
    if not u or not u.active or not verify_password(password, u.password_hash):
        just_blocked = login_limiter.fail(f"login:{lg}") | ip_limiter.fail(f"ip:{ip}")
        audit(db, "login.failed", user=u if u else None, login=lg[:64], ip=ip)
        if just_blocked:
            audit(db, "login.blocked", login=lg[:64], ip=ip, minutes=settings.login_block_minutes)
            import threading
            from .services import alerts
            threading.Thread(target=alerts.send, daemon=True, args=(
                f"🔐 <b>Безпека: блокування входу</b>\n{settings.login_max_attempts} невдалих спроб — логін «{lg[:40]}», "
                f"IP {ip}. Вхід заблоковано на {settings.login_block_minutes} хв.",)).start()
            return render(request, "login.html", login=login,
                          error=f"Забагато невдалих спроб. Вхід заблоковано на {settings.login_block_minutes} хв.")
        return render(request, "login.html", error="Невірний логін або пароль", login=login)
    login_limiter.reset(f"login:{lg}")
    request.session.clear()
    request.session["uid"] = u.id
    u.last_login = datetime.utcnow()
    db.commit()
    audit(db, "login", user=u)
    return RedirectResponse("/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/me", response_class=HTMLResponse)
def me(request: Request, user: User = Depends(current_user)):
    return render(request, "me.html")


@app.post("/me")
def me_save(request: Request, current: str = Form(""), new: str = Form(""), telegram_chat_id: str = Form(""),
            user: User = Depends(current_user), db: Session = Depends(db_dep)):
    u = db.get(User, user.id)
    u.telegram_chat_id = telegram_chat_id.strip() or None
    msg = "Збережено"
    if new:
        if len(new) < 8:
            msg = "Новий пароль — щонайменше 8 символів"
        elif not verify_password(current, u.password_hash):
            msg = "Поточний пароль невірний"
        else:
            u.password_hash = hash_password(new)
            msg = "Пароль змінено"
            audit(db, "user.password_change", "user", u.id, user=u)
    db.commit()
    request.state.user = u
    return render(request, "me.html", msg=msg)


# ---------------- дашборд ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, df: str | None = None, dt_: str | None = None,
              user: User = Depends(current_user), db: Session = Depends(db_dep)):
    d_from = date.fromisoformat(df) if df else None
    d_to = date.fromisoformat(dt_) if dt_ else None
    doc = doctor_scope(user)
    if user.role == "doctor" and not doc:
        return render(request, "error.html", code=403,
                      detail="Ваш обліковий запис ще не привʼязаний до лікаря. Зверніться до власника.")
    sts = all_statuses(db, d_from, d_to, doctor_id=doc)
    total, by_doc = kpi_summary(sts)
    ctx = dict(total=total, by_doc=by_doc, df=df, dt_=dt_)
    if can(user, "finance"):
        fc = forecast(all_statuses(db))
        ctx.update(fc=fc, fc_max=max([r["total"] for r in fc] + [1]))
    if can(user, "reminders") or user.role == "doctor":
        allowed_patients = {ps.plan.patient_id for ps in sts} if doc else None
        rems = [r for r in reminders.collect(db) if allowed_patients is None or r.patient_id in allowed_patients]
        ctx.update(not_booked=[r for r in rems if r.kind == "not_booked"],
                   care=[r for r in rems if r.kind == "care_ending"])
    last = db.get(SyncState, "last_sales_sync")
    ctx.update(last_sync=last.value if last else None, last_error=state["last_error"],
               sheets=bool(settings.google_sheet_id), cb=bool(settings.cleverbox_token),
               unassigned=db.scalar(select(func.count()).select_from(PatientFile)
                                    .where(PatientFile.patient_id.is_(None))) if can(user, "files_assign") else 0)
    return render(request, "dashboard.html", **ctx)


# ---------------- пацієнти ----------------

def _patient_or_403(db, user, pid) -> Patient:
    p = db.get(Patient, pid)
    if not p:
        raise HTTPException(404, "Пацієнта не знайдено")
    if not patient_visible(user, p):
        raise HTTPException(403, "Це не ваш пацієнт")
    return p


@app.get("/patients/{pid}", response_class=HTMLResponse)
def patient_card(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    statuses = [compute_plan(db, pl) for pl in p.plans if plan_visible(user, pl)]
    completed_visits = [v for v in p.visits if v.status == "done"]
    planned_visits = [v for v in p.visits if v.status == "booked" and v.starts_at >= datetime.now()]
    dated_events = [v.starts_at for v in p.visits if v.starts_at]
    service_revenue = sum(v.amount or 0 for v in completed_visits)
    product_revenue = sum(s.amount or 0 for s in p.sales)
    from .models import Task as _Task
    from .services.tasks import visible_query as _vq
    ptasks = db.scalars(_vq(user).where(_Task.patient_id == p.id).order_by(_Task.status, _Task.due_date)).all()
    from .services import scripts as _sc
    from .integrations import messaging as _msg
    from .models import MessageLog as _ML
    msgs = db.scalars(select(_ML).where(_ML.patient_id == p.id).order_by(_ML.at.desc()).limit(30)).all()
    kind = "not_booked" if statuses and statuses[0].next_procedure else "follow_up"
    return render(request, "patient.html", p=p, statuses=statuses, ptasks=ptasks, msgs=msgs,
                  pscripts=_sc.for_patient(db, p, kind, user.name) if can(user, "message") else [],
                  avail=_msg.available_channels(p),
                  users=db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all(),
                  doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all(),
                  photos=[f for f in p.files if f.kind == "photo"],
                  docs=[f for f in p.files if f.kind != "photo"], now=datetime.now(),
                  patient_kpi=dict(done=len(completed_visits), planned=len(planned_visits),
                                   first=min(dated_events) if dated_events else None,
                                   next=min((v.starts_at for v in planned_visits), default=None),
                                   service_revenue=service_revenue, product_revenue=product_revenue,
                                   revenue=service_revenue + product_revenue))


@app.post("/patients/{pid:int}")
def patient_update(pid: int, full_name: str = Form(...), phone: str = Form(""), cleverbox_id: str = Form(""),
                   doctor_id: str = Form(""), notes: str = Form(""), email: str = Form(""),
                   gender: str = Form(""), address: str = Form(""), source: str = Form(""),
                   communication_channel: str = Form(""), client_groups: str = Form(""),
                   cb_balance: str = Form(""), cb_bonus: str = Form(""),
                   user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    p.full_name, p.phone, p.notes = full_name.strip(), phone.strip() or None, notes.strip() or None
    p.cleverbox_id = int(cleverbox_id) if cleverbox_id.strip().isdigit() else None
    p.email, p.gender, p.address = email.strip() or None, gender.strip() or None, address.strip() or None
    p.source, p.communication_channel = source.strip() or None, communication_channel.strip() or None
    p.client_groups = [x.strip() for x in client_groups.split(",") if x.strip()] or None
    def money(value: str):
        try:
            return float(value.strip().replace(" ", "").replace(",", ".")) if value.strip() else None
        except ValueError:
            return None
    p.cb_balance, p.cb_bonus = money(cb_balance), money(cb_bonus)
    if user.role != "doctor":
        p.doctor_id = int(doctor_id) if doctor_id.isdigit() else None
    db.commit()
    audit(db, "patient.update", "patient", p.id, user=user)
    return RedirectResponse(f"/patients/{pid}", 303)


@app.post("/patients")
def patient_create(full_name: str = Form(...), phone: str = Form(""), doctor_id: str = Form(""),
                   notes: str = Form(""), user: User = Depends(require("patient_edit")),
                   db: Session = Depends(db_dep)):
    """Ручне створення картки без прив'язки до CleverBox."""
    name = full_name.strip()
    if not name:
        raise HTTPException(400, "Вкажіть ПІБ пацієнта")
    assigned_doctor = user.doctor_id if user.role == "doctor" else (int(doctor_id) if doctor_id.isdigit() else None)
    p = Patient(full_name=name, phone=phone.strip() or None, doctor_id=assigned_doctor, notes=notes.strip() or None,
                source="manual")
    db.add(p)
    db.commit()
    audit(db, "patient.create_manual", "patient", p.id, user=user)
    return RedirectResponse(f"/patients/{p.id}", 303)


@app.post("/patients/test")
def patient_create_test(user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    """Одна демонстраційна картка для перевірки інтерфейсу, без персональних даних."""
    p = db.scalar(select(Patient).where(Patient.source == "manual_test"))
    if not p:
        doctor_id = user.doctor_id if user.role == "doctor" else None
        p = Patient(full_name="Тестова картка пацієнта", phone=None, doctor_id=doctor_id,
                    notes="Демонстраційна картка. Не є даними реального пацієнта.", source="manual_test")
        db.add(p)
        db.commit()
        audit(db, "patient.create_test", "patient", p.id, user=user)
    return RedirectResponse(f"/patients/{p.id}", 303)


@app.post("/plans/{plan_id}/items")
async def plan_items_update(request: Request, plan_id: int, user: User = Depends(require("plan_edit")),
                            db: Session = Depends(db_dep)):
    form = await request.form()
    plan = db.get(BeautyPlan, plan_id)
    if not plan or not plan_visible(user, plan):
        raise HTTPException(403, "Немає доступу до цього плану")
    changes = {}
    for it in plan.items:
        for f in ("interval_days", "supply_days", "manual_done", "qty"):
            v = form.get(f"{f}_{it.id}")
            if v is not None and str(v).strip().isdigit() and getattr(it, f) != int(v):
                changes[f"{it.name[:40]}.{f}"] = [getattr(it, f), int(v)]
                setattr(it, f, int(v))
        it.total = it.qty * it.unit_price
    plan.total_planned = sum(i.total for i in plan.items)
    if form.get("status") in ("active", "completed", "cancelled") and form["status"] != plan.status:
        changes["status"] = [plan.status, form["status"]]
        plan.status = form["status"]
    if user.role != "doctor" and str(form.get("doctor_id", "")).isdigit():
        plan.doctor_id = int(form["doctor_id"])
    db.commit()
    if changes:
        audit(db, "plan.update", "plan", plan.id, user=user, changes=changes)
        webhooks.fire("plan.updated", plan_dict(compute_plan(db, plan)))
    return RedirectResponse(f"/patients/{plan.patient_id}#plan{plan.id}", 303)


@app.post("/plans/{plan_id}/delete")
def plan_delete(plan_id: int, user: User = Depends(require("plan_delete")), db: Session = Depends(db_dep)):
    plan = db.get(BeautyPlan, plan_id)
    pid = plan.patient_id
    audit(db, "plan.delete", "plan", plan.id, user=user, patient=plan.patient.full_name, total=plan.total_planned)
    db.delete(plan)
    db.commit()
    return RedirectResponse(f"/patients/{pid}", 303)


# ---------------- файли ----------------

@app.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), user: User = Depends(require("upload")),
                 db: Session = Depends(db_dep)):
    results = []
    for f in files:
        data = await f.read()
        if (f.filename or "").lower().endswith(".pdf"):
            plan, _, err = ingest.ingest_pdf(db, data, f.filename)
            results.append((f.filename, plan, err))
            if plan:
                audit(db, "plan.upload", "plan", plan.id, user=user, file=f.filename)
        else:
            ff = ingest.ingest_photo(db, data, f.filename or "photo.jpg", None)
            results.append((f.filename, None, None if ff.patient_id else "фото без пацієнта → «Нерозібрані»"))
    if len(results) == 1 and results[0][1]:
        return RedirectResponse(f"/patients/{results[0][1].patient_id}", 303)
    return render(request, "upload_result.html", results=results)


@app.post("/patients/{pid}/photos")
async def upload_photos(pid: int, files: list[UploadFile] = File(...), caption: str = Form(""),
                        user: User = Depends(require("upload")), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    for f in files:
        ingest.ingest_photo(db, await f.read(), f.filename or "photo.jpg", caption or None, patient=p)
    return RedirectResponse(f"/patients/{pid}#files", 303)


@app.get("/files/{fid}")
def file_get(fid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    f = db.get(PatientFile, fid)
    if not f or not Path(f.path).exists():
        raise HTTPException(404, "Файл не знайдено")
    if f.patient_id:
        _patient_or_403(db, user, f.patient_id)
    elif not can(user, "files_assign"):
        raise HTTPException(403, "Немає доступу")
    return FileResponse(f.path, media_type=mimetypes.guess_type(f.path)[0] or "application/octet-stream")


@app.get("/unassigned", response_class=HTMLResponse)
def unassigned(request: Request, user: User = Depends(require("files_assign")), db: Session = Depends(db_dep)):
    files = db.scalars(select(PatientFile).where(PatientFile.patient_id.is_(None))
                       .order_by(PatientFile.created_at.desc())).all()
    return render(request, "unassigned.html", files=files,
                  patients=db.scalars(select(Patient).order_by(Patient.full_name)).all())


@app.post("/files/{fid}/assign")
def file_assign(fid: int, patient_id: int = Form(...), user: User = Depends(require("files_assign")),
                db: Session = Depends(db_dep)):
    f = db.get(PatientFile, fid)
    f.patient_id = patient_id
    db.commit()
    audit(db, "file.assign", "patient", patient_id, user=user, file_id=fid)
    return RedirectResponse("/unassigned", 303)


# ---------------- зіставлення з CleverBox ----------------

@app.get("/mapping", response_class=HTMLResponse)
def mapping(request: Request, user: User = Depends(require("mapping")), db: Session = Depends(db_dep)):
    names: dict[tuple[str, str], str] = {}
    for it in db.scalars(select(PlanItem).where(PlanItem.kind == "procedure")):
        names.setdefault((it.kind, norm(it.name)), it.name)
    services = db.scalars(select(CbService).order_by(CbService.category, CbService.name)).all()
    aliases = {(a.kind, a.plan_name_norm): a for a in db.scalars(select(Alias))}
    seen_names = sorted({v.service_name for v in db.scalars(select(Visit)) if v.service_name})
    rows = [(key, name, aliases.get(key), [s for s in seen_names if name_match(name, s) >= 0.6])
            for key, name in sorted(names.items(), key=lambda kv: kv[1])]
    return render(request, "mapping.html", rows=rows, services=services)


@app.post("/mapping")
async def mapping_save(request: Request, user: User = Depends(require("mapping")), db: Session = Depends(db_dep)):
    form = await request.form()
    kind, key = form["kind"], form["key"]
    ids = [int(x) for x in form.getlist("cb_ids") if str(x).isdigit()]
    al = db.scalar(select(Alias).where(Alias.kind == kind, Alias.plan_name_norm == key))
    if not ids:
        if al:
            db.delete(al)
    else:
        al = al or Alias(kind=kind, plan_name_norm=key)
        al.cb_ids = ids
        al.cb_names = [db.get(CbService, i).name for i in ids if db.get(CbService, i)]
        db.add(al)
    db.commit()
    audit(db, "mapping.update", user=user, key=key, cb_ids=ids)
    return RedirectResponse("/mapping", 303)


# ---------------- доступи (лише власник) ----------------

@app.get("/access", response_class=HTMLResponse)
def access(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    return render(request, "access.html", tab="users",
                  users=db.scalars(select(User).order_by(User.role.desc(), User.name)).all(),
                  doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all())


@app.post("/access/users")
def user_save(request: Request, uid: str = Form(""), login: str = Form(...), name: str = Form(...),
              role: str = Form(...), doctor_id: str = Form(""), password: str = Form(""), active: str = Form(""),
              user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    if role not in ROLES:
        raise HTTPException(400, "Невідома роль")
    u = db.get(User, int(uid)) if uid.isdigit() else None
    if u is None:
        if db.scalar(select(User).where(func.lower(User.login) == login.strip().lower())):
            raise HTTPException(400, f"Логін «{login}» уже зайнятий")
        if len(password) < 8:
            raise HTTPException(400, "Пароль — щонайменше 8 символів")
        u = User(login=login.strip(), name=name.strip(), role=role, password_hash=hash_password(password))
        db.add(u)
        action = "user.create"
    else:
        action = "user.update"
        if u.id == user.id and (role != "owner" or not active):
            raise HTTPException(400, "Не можна зняти з себе роль власника або деактивувати себе")
        u.login, u.name, u.role = login.strip(), name.strip(), role
        u.active = bool(active)
        if password:
            if len(password) < 8:
                raise HTTPException(400, "Пароль — щонайменше 8 символів")
            u.password_hash = hash_password(password)
    u.doctor_id = int(doctor_id) if role == "doctor" and doctor_id.isdigit() else None
    db.commit()
    audit(db, action, "user", u.id, user=user, login=u.login, role=u.role, active=u.active,
          password_changed=bool(password))
    return RedirectResponse("/access", 303)


@app.get("/access/api", response_class=HTMLResponse)
def access_api(request: Request, user: User = Depends(require("api")), db: Session = Depends(db_dep)):
    return render(request, "access.html", tab="api",
                  keys=db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())).all(),
                  hooks=db.scalars(select(Webhook).order_by(Webhook.created_at.desc())).all(),
                  new_key=request.session.pop("new_key", None), new_secret=request.session.pop("new_secret", None))


@app.post("/access/api/keys")
def key_create(request: Request, name: str = Form(...), scopes: list[str] = Form([]),
               user: User = Depends(require("api")), db: Session = Depends(db_dep)):
    raw, prefix, h = new_api_key()
    k = ApiKey(name=name.strip(), prefix=prefix, key_hash=h,
               scopes=[s for s in scopes if s in ("read", "write", "medical", "bot")] or ["read"], created_by=user.id)
    db.add(k)
    db.commit()
    audit(db, "api_key.create", "api_key", k.id, user=user, name=k.name, scopes=k.scopes)
    request.session["new_key"] = raw          # показуємо один раз
    return RedirectResponse("/access/api", 303)


@app.post("/access/api/keys/{kid}/revoke")
def key_revoke(kid: int, user: User = Depends(require("api")), db: Session = Depends(db_dep)):
    k = db.get(ApiKey, kid)
    k.active = False
    db.commit()
    audit(db, "api_key.revoke", "api_key", k.id, user=user, name=k.name)
    return RedirectResponse("/access/api", 303)


@app.post("/access/api/hooks")
def hook_create(request: Request, url: str = Form(...), events: list[str] = Form([]),
                user: User = Depends(require("api")), db: Session = Depends(db_dep)):
    if not url.startswith("https://"):
        raise HTTPException(400, "Вебхук має бути на https://")
    secret = secrets.token_hex(24)
    h = Webhook(url=url.strip(), events=[e for e in events if e in webhooks.EVENTS], secret=secret)
    db.add(h)
    db.commit()
    audit(db, "webhook.create", "webhook", h.id, user=user, url=h.url, events=h.events)
    request.session["new_secret"] = secret
    return RedirectResponse("/access/api", 303)


@app.post("/access/api/hooks/{hid}/toggle")
def hook_toggle(hid: int, user: User = Depends(require("api")), db: Session = Depends(db_dep)):
    h = db.get(Webhook, hid)
    h.active = not h.active
    db.commit()
    audit(db, "webhook." + ("enable" if h.active else "disable"), "webhook", h.id, user=user)
    return RedirectResponse("/access/api", 303)


@app.post("/access/api/hooks/{hid}/test")
def hook_test(hid: int, user: User = Depends(require("api")), db: Session = Depends(db_dep)):
    h = db.get(Webhook, hid)
    import json
    body = json.dumps({"event": "test", "sent_at": datetime.now().isoformat(timespec="seconds"),
                       "data": {"message": "Тестова подія MOLODIST CRM"}}, ensure_ascii=False).encode()
    webhooks._deliver(h.id, h.url, h.secret, body)
    return RedirectResponse("/access/api", 303)


@app.get("/access/audit", response_class=HTMLResponse)
def access_audit(request: Request, user: User = Depends(require("audit")), db: Session = Depends(db_dep)):
    return render(request, "access.html", tab="audit",
                  log=db.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(300)).all())


@app.get("/api/openapi.json", include_in_schema=False)
def openapi_json(user: User = Depends(require("api"))):
    return app.openapi()


@app.get("/api/docs", include_in_schema=False)
def api_docs(user: User = Depends(require("api"))):
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="MOLODIST CRM API")


# ---------------- дії ----------------

@app.post("/actions/sync")
async def action_sync(user: User = Depends(require("sync")), db: Session = Depends(db_dep)):
    await job_sync()
    audit(db, "sync.manual", user=user)
    return RedirectResponse("/", 303)


@app.post("/actions/sheets")
async def action_sheets(user: User = Depends(require("sheets"))):
    try:
        url = await asyncio.to_thread(run_sheets)
        return RedirectResponse(url, 303)
    except Exception as e:          # noqa: BLE001
        state["last_error"] = f"Sheets: {e}"
        return RedirectResponse("/", 303)


@app.post("/actions/digest")
async def action_digest(user: User = Depends(require("send_digest"))):
    await job_digest()
    return RedirectResponse("/", 303)


# ---------------- дані: експорт / імпорт / перенос / резервна копія ----------------

import os
import tempfile
from starlette.background import BackgroundTask
from fastapi.responses import Response
from .services import exporter, importer, jobs

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LAST_IMPORT: dict[int, list[dict]] = {}       # результати останнього імпорту по користувачу (не в cookie — може бути великим)


def _attach(name: str) -> dict:
    from urllib.parse import quote
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


def _tmp_response(path: str, name: str, media: str = "application/zip"):
    return FileResponse(path, media_type=media, headers=_attach(name), background=BackgroundTask(os.unlink, path))


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, user: User = Depends(require("export_reports")), db: Session = Depends(db_dep)):
    last = db.get(SyncState, "last_migration")
    cb_success = db.get(SyncState, "cleverbox_last_success")
    cb_error = db.get(SyncState, "cleverbox_last_error")
    job = state.get("scheduler").get_job("cleverbox_sync") if state.get("scheduler") else None
    from .services import backups as bk
    return render(request, "data.html", jobs=jobs.recent(), cb=bool(settings.cleverbox_token),
                  backups=bk.list_backups() if can(user, "backup") else [], settings=settings,
                  last_migration=last.value if last else None, reports=LAST_IMPORT.pop(user.id, None),
                  cb_last_success=cb_success.value if cb_success else None,
                  cb_last_error=cb_error.value if cb_error else None,
                  cb_next_sync=job.next_run_time if job else None,
                  cb_auto_sync=auto_sync_enabled(),
                  cb_notice=request.query_params.get("cb_notice"),
                  wh=cb_webhooks.status(db) if user.role == "owner" else None,
                  wh_url=cb_webhooks.hook_url(db) if user.role == "owner" else None,
                  wh_subs=cb_webhooks.subscriptions(db) if user.role == "owner" and settings.cleverbox_token
                  and request.query_params.get("wh") else None)


@app.get("/export/report.xlsx")
def export_report(df: str | None = None, dt_: str | None = None, user: User = Depends(require("export_reports")),
                  db: Session = Depends(db_dep)):
    tables = exporter.report_tables(db, user, date.fromisoformat(df) if df else None,
                                    date.fromisoformat(dt_) if dt_ else None)
    audit(db, "export.report", user=user, format="xlsx", df=df, dt=dt_)
    return Response(exporter.to_xlsx(tables), media_type=XLSX,
                    headers=_attach(f"MOLODIST_звіт_{datetime.now():%Y-%m-%d}.xlsx"))


@app.get("/export/report.csv.zip")
def export_report_csv(df: str | None = None, dt_: str | None = None, user: User = Depends(require("export_reports")),
                      db: Session = Depends(db_dep)):
    tables = exporter.report_tables(db, user, date.fromisoformat(df) if df else None,
                                    date.fromisoformat(dt_) if dt_ else None)
    audit(db, "export.report", user=user, format="csv", df=df, dt=dt_)
    return Response(exporter.to_csv_zip(tables), media_type="application/zip",
                    headers=_attach(f"MOLODIST_звіт_{datetime.now():%Y-%m-%d}_csv.zip"))


@app.get("/export/files.zip")
def export_files(user: User = Depends(require("export_files")), db: Session = Depends(db_dep)):
    pats = [p for p in db.scalars(select(Patient)) if patient_visible(user, p) and p.files]
    audit(db, "export.files", user=user, patients=len(pats))
    return _tmp_response(exporter.files_zip(db, pats), f"MOLODIST_файли_{datetime.now():%Y-%m-%d}.zip")


@app.get("/patients/{pid}/files.zip")
def export_patient_files(pid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    audit(db, "export.files", "patient", pid, user=user)
    return _tmp_response(exporter.files_zip(db, [p]), f"{p.full_name}_файли.zip")


@app.get("/patients/{pid}/print", response_class=HTMLResponse)
def patient_print(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    statuses = [compute_plan(db, pl) for pl in p.plans if plan_visible(user, pl)]
    return render(request, "print.html", p=p, statuses=statuses, now=datetime.now())


@app.get("/import/template.xlsx")
def import_template(user: User = Depends(require("import"))):
    return Response(importer.template_xlsx(), media_type=XLSX, headers=_attach("MOLODIST_шаблон_імпорту.xlsx"))


@app.post("/import/table")
async def import_table(request: Request, file: UploadFile = File(...), kind: str = Form(""),
                       user: User = Depends(require("import")), db: Session = Depends(db_dep)):
    data = await file.read()
    try:
        reports = importer.import_file(db, data, file.filename or "data.xlsx", kind or None)
    except Exception as e:          # noqa: BLE001
        reports = [{"sheet": file.filename, "error": str(e)}]
    audit(db, "import.table", user=user, file=file.filename,
          summary=[{k: r.get(k) for k in ("sheet", "type", "plans", "visits", "sales", "patients_created", "error")}
                   for r in reports])
    LAST_IMPORT[user.id] = reports
    return RedirectResponse("/data#import", 303)


@app.post("/import/pdf-zip")
async def import_pdf_zip(request: Request, file: UploadFile = File(...), user: User = Depends(require("import")),
                         db: Session = Depends(db_dep)):
    try:
        rep = importer.import_pdf_zip(db, await file.read())
    except Exception as e:          # noqa: BLE001
        rep = {"error": str(e)}
    rep["sheet"] = file.filename
    rep["type"] = "PDF Observ"
    audit(db, "import.pdf_zip", user=user, file=file.filename, plans=rep.get("plans"))
    LAST_IMPORT[user.id] = [rep]
    return RedirectResponse("/data#import", 303)


@app.post("/migrate/cleverbox")
def migrate_cleverbox(months: int = Form(24), all_clients: str = Form(""), all_history: str = Form(""),
                      user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    from .integrations.cleverbox import migrate
    full_history = all_history == "1"
    job = jobs.start("Перенос з CleverBox", migrate, months=max(1, min(months, 120)),
                     all_clients=bool(all_clients), start_date=date(2000, 1, 1) if full_history else None)
    audit(db, "migrate.cleverbox", user=user, months="all" if full_history else months,
          all_clients=bool(all_clients), job=job.id)
    return RedirectResponse("/data#migrate", 303)


@app.post("/settings/cleverbox-token")
async def cleverbox_token_save(token: str = Form(...), user: User = Depends(require("migrate")),
                               db: Session = Depends(db_dep)):
    """Validate first; never display or audit the supplied secret."""
    token = token.strip()
    if not token:
        return RedirectResponse("/data?cb_notice=Введіть+токен#migrate", 303)
    try:
        from .integrations.cleverbox import CleverBox, save_token
        await asyncio.to_thread(lambda: CleverBox(token=token).filials())
        save_token(db, token)
        ensure_sync_schedule()
        audit(db, "cleverbox.token_saved", user=user)
        return RedirectResponse("/data?cb_notice=Токен+перевірено+і+збережено#migrate", 303)
    except Exception as e:  # noqa: BLE001
        log.warning("CleverBox token validation failed: %s", type(e).__name__)
        audit(db, "cleverbox.token_rejected", user=user)
        return RedirectResponse("/data?cb_notice=Токен+не+пройшов+перевірку#migrate", 303)


@app.post("/actions/cleverbox-sync")
async def cleverbox_manual_sync(user: User = Depends(require("sync")), db: Session = Depends(db_dep)):
    if not settings.cleverbox_token:
        return RedirectResponse("/data?cb_notice=Спершу+збережіть+токен#migrate", 303)
    await job_sync()
    audit(db, "cleverbox.sync.manual", user=user)
    return RedirectResponse("/data#migrate", 303)


@app.post("/settings/cleverbox-auto-sync")
def cleverbox_auto_sync_save(enabled: str = Form("0"), user: User = Depends(require("migrate")),
                             db: Session = Depends(db_dep)):
    value = "1" if enabled == "1" else "0"
    item = db.get(SyncState, "cleverbox_auto_sync") or SyncState(key="cleverbox_auto_sync")
    item.value = value
    db.merge(item)
    db.commit()
    ensure_sync_schedule()
    audit(db, "cleverbox.auto_sync", user=user, enabled=value == "1")
    notice = "Автооновлення+увімкнено" if value == "1" else "Автооновлення+вимкнено"
    return RedirectResponse(f"/data?cb_notice={notice}#migrate", 303)


@app.get("/jobs/{jid}")
def job_status(jid: str, user: User = Depends(require("import"))):
    j = jobs.JOBS.get(jid)
    if not j:
        raise HTTPException(404, "Задачу не знайдено")
    return {"id": j.id, "title": j.title, "status": j.status, "progress": j.progress[-30:],
            "result": j.result, "error": j.error}


@app.get("/backup/download")
def backup_download(with_files: int = 1, user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    audit(db, "backup.download", user=user, with_files=bool(with_files))
    return _tmp_response(exporter.backup_zip(db, bool(with_files)),
                         f"MOLODIST_CRM_backup_{datetime.now():%Y-%m-%d_%H%M}.zip")


@app.post("/backup/now")
async def backup_now(request: Request, user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import backups
    try:
        rep = await asyncio.to_thread(backups.run_backup, "manual")
        audit(db, "backup.create", user=user, **{k: v for k, v in rep.items() if k != "trigger"})
    except Exception as e:          # noqa: BLE001
        state["last_error"] = f"Бекап: {e}"
    return RedirectResponse("/data#backup", 303)


@app.get("/backup/file/{name}")
def backup_file(name: str, user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import backups
    try:
        p = backups.backup_path(name)
    except FileNotFoundError:
        raise HTTPException(404, "Копію не знайдено")
    audit(db, "backup.download", user=user, file=name)
    return FileResponse(p, media_type="application/zip", headers=_attach(name))


@app.post("/backup/restore")
async def backup_restore(request: Request, file: UploadFile = File(...), confirm: str = Form(""),
                         password: str = Form(""), user: User = Depends(require("backup")),
                         db: Session = Depends(db_dep)):
    from .services import backups
    if confirm.strip().upper() != "ВІДНОВИТИ":
        raise HTTPException(400, "Для підтвердження введіть слово ВІДНОВИТИ")
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    while chunk := await file.read(1 << 20):
        tmp.write(chunk)
    tmp.close()
    plain = tmp.name
    try:
        if backups.is_encrypted(tmp.name):           # копія з Telegram (.zip.enc)
            if not password:
                raise HTTPException(400, "Копія зашифрована — введіть пароль (BACKUP_PASSWORD)")
            plain = tmp.name + ".dec"
            try:
                backups.decrypt_file(tmp.name, plain, password)
            except ValueError as e:
                raise HTTPException(400, str(e))
        try:
            rep = exporter.restore_backup(db, plain)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        for f in {tmp.name, plain}:
            if os.path.exists(f):
                os.unlink(f)
    request.session.clear()            # користувачі могли змінитися — входимо заново
    log.warning("Backup restored: %s", rep)
    with SessionLocal() as db2:
        audit(db2, "backup.restore", details_rows=sum(rep["rows"].values()), files=rep["files"],
              backup_created=rep["backup_created"])
    return RedirectResponse("/login", 303)


# ---------------- Клієнти / Календар / Задачі / Продукти / Склад / Фінанси ----------------

from datetime import timedelta
from .models import Task, Product, StockLevel, Check
from .services import clients as clients_svc, tasks as tasks_svc, calendar as cal_svc, catalog, finance as fin_svc


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, seg: str = "all", q: str = "", doctor: int | None = None, page: int = 1,
                 user: User = Depends(current_user), db: Session = Depends(db_dep)):
    words = set(norm(q).split()) if q else set()
    if user.role == "doctor":
        doctor = user.doctor_id
    pats = []
    from sqlalchemy.orm import selectinload
    for p in db.scalars(select(Patient).options(selectinload(Patient.plans)).order_by(Patient.full_name)):
        if not patient_visible(user, p):
            continue
        if words and not (words <= set(norm(p.full_name).split()) or (p.phone and q.strip() in (p.phone or ""))):
            continue
        if doctor and p.doctor_id != doctor and not any(pl.doctor_id == doctor for pl in p.plans):
            continue
        pats.append(p)
    rows = clients_svc.client_rows(db, pats)
    counts = clients_svc.segment_counts(rows)
    rows = [r for r in rows if seg in r.segment_flags]
    order = {"red": 0, "yellow": 1, "green": 2, "done": 3}
    rows.sort(key=lambda r: (order.get(r.plan_status.flag, 4) if r.plan_status else 5,
                             -(r.last_visit.timestamp() if r.last_visit else 0)))
    per = 100
    total = len(rows)
    rows = rows[(page - 1) * per: page * per]
    return render(request, "clients.html", rows=rows, counts=counts, seg=seg, q=q, doctor=doctor, page=page,
                  pages=max(1, (total + per - 1) // per), total=total, SEGMENTS=clients_svc.SEGMENTS,
                  doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all(),
                  unassigned=db.scalar(select(func.count()).select_from(PatientFile)
                                       .where(PatientFile.patient_id.is_(None))) if can(user, "files_assign") else 0)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, start: str | None = None, doc: str = "", view: str = "week",
                  user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .services import specialists as spec_svc
    today = date.today()
    try:
        s = date.fromisoformat(start) if start else today
    except ValueError:
        s = today
    view = "day" if view == "day" else "week"
    days = 1 if view == "day" else 7
    if view == "week":
        s = s - timedelta(days=s.weekday())
    doc_id = int(doc) if doc.isdigit() else None
    ev = cal_svc.events(db, user, s, days, doc_id)
    cols = cal_svc.columns(db, ev, fixed=(view == "day" and not doc_id))
    if doc_id:                                   # фільтр по спеціалісту — лише його колонка
        cols = {d: [c for c in cs if c.doctor and c.doctor.id == doc_id] or
                   [cal_svc.Column(str(doc_id), db.get(Doctor, doc_id), [])] for d, cs in cols.items()}
    return render(request, "calendar.html", ev=ev, cols=cols, start=s, days=days, view=view, today=today,
                  doc=doc_id, specialists=spec_svc.calendar_doctors(db), color=spec_svc.color,
                  initials=spec_svc.initials, prev=s - timedelta(days=days), next=s + timedelta(days=days),
                  end=s + timedelta(days=days - 1))


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, f: str = "open", user: User = Depends(current_user), db: Session = Depends(db_dep)):
    q = tasks_svc.visible_query(user)
    today = date.today()
    if f == "open":
        q = q.where(Task.status == "open")
    elif f == "mine":
        q = q.where(Task.status == "open", Task.assignee_id == user.id)
    elif f == "overdue":
        q = q.where(Task.status == "open", Task.due_date < today)
    elif f == "done":
        q = q.where(Task.status != "open")
    tasks = db.scalars(q.order_by(Task.status, Task.due_date.is_(None), Task.due_date, Task.id.desc()).limit(500)).all()
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all()
    pats = [p for p in db.scalars(select(Patient).order_by(Patient.full_name)) if patient_visible(user, p)]
    from .services import scripts as _sc
    from .integrations import messaging as _msg
    scripts = {}
    if can(user, "message"):
        for t in [t for t in tasks if t.status == "open" and t.patient_id][:100]:
            scripts[t.id] = _sc.for_task(db, t, user.name)
    return render(request, "tasks.html", tasks=tasks, f=f, today=today, users=users, patients=pats,
                  KINDS=tasks_svc.KINDS, PRIORITY=tasks_svc.PRIORITY, scripts=scripts,
                  tg_on=_msg.telegram_available(), ts_on=_msg.turbosms_available(),
                  avail={t.id: _msg.available_channels(t.patient) for t in tasks if t.patient})


@app.post("/tasks")
def task_create(title: str = Form(...), description: str = Form(""), patient_id: str = Form(""),
                assignee: str = Form(""), due_date: str = Form(""), priority: str = Form("normal"),
                back: str = Form("/tasks"), user: User = Depends(current_user), db: Session = Depends(db_dep)):
    t = Task(title=title.strip(), description=description.strip() or None, kind="manual", created_by=user.id,
             priority=priority if priority in tasks_svc.PRIORITY else "normal",
             due_date=date.fromisoformat(due_date) if due_date else None)
    if patient_id.isdigit():
        _patient_or_403(db, user, int(patient_id))
        t.patient_id = int(patient_id)
    if assignee.isdigit():
        t.assignee_id = int(assignee)
    elif assignee in ("admin", "owner"):
        t.assignee_role = assignee
    else:
        t.assignee_id = user.id
    db.add(t)
    db.commit()
    audit(db, "task.create", "task", t.id, user=user, title=t.title)
    _notify_assignee(db, t)
    return RedirectResponse(back if back.startswith("/") else "/tasks", 303)


def _notify_assignee(db, t: Task):
    """Особисте сповіщення в Telegram, якщо виконавець вказав свій chat ID у «Мій кабінет»."""
    if not state["bot"] or not t.assignee_id:
        return
    u = db.get(User, t.assignee_id)
    if u and u.telegram_chat_id:
        from html import escape
        txt = f"📝 Нова задача: <b>{escape(t.title)}</b>" + (f"\nТермін: {t.due_date:%d.%m}" if t.due_date else "") + \
              f'\n<a href="{settings.public_url.rstrip("/")}/tasks">Задачі</a>'
        try:
            asyncio.get_event_loop().create_task(state["bot"].send_message(u.telegram_chat_id, txt))
        except Exception:      # noqa: BLE001
            pass


@app.post("/tasks/{tid}")
def task_update(tid: int, status: str = Form(...), result: str = Form(""), back: str = Form("/tasks"),
                user: User = Depends(current_user), db: Session = Depends(db_dep)):
    t = db.get(Task, tid)
    if not t or t not in db.scalars(tasks_svc.visible_query(user).where(Task.id == tid)).all():
        raise HTTPException(403, "Немає доступу до задачі")
    if status in ("open", "done", "cancelled"):
        t.status = status
        t.done_at = datetime.utcnow() if status != "open" else None
        t.result = result.strip() or t.result
        if status == "open" and not t.assignee_id and not t.assignee_role:
            t.assignee_id = user.id
    elif status == "take":
        t.assignee_id, t.assignee_role = user.id, None
    db.commit()
    audit(db, "task." + status, "task", t.id, user=user)
    return RedirectResponse(back if back.startswith("/") else "/tasks", 303)


@app.post("/tasks-generate")
def tasks_generate(user: User = Depends(require("reminders")), db: Session = Depends(db_dep)):
    rep = tasks_svc.generate_auto(db)
    audit(db, "task.generate", user=user, **rep)
    return RedirectResponse("/tasks", 303)


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request, tab: str = "care", user: User = Depends(require("products")),
                  db: Session = Depends(db_dep)):
    sts = all_statuses(db)
    return render(request, "products.html", tab=tab,
                  products=catalog.product_rows(db, sts) if tab == "care" else None,
                  procedures=catalog.procedure_rows(sts) if tab == "procedures" else None)


@app.post("/products/{pid}")
def product_update(pid: int, supply_days: str = Form(""), min_stock: str = Form(""), url: str = Form(""),
                   apply: str = Form(""), user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    prod = db.get(Product, pid) or (_ for _ in ()).throw(HTTPException(404))
    prod.supply_days = int(supply_days) if supply_days.strip().isdigit() else None
    prod.min_stock = int(min_stock) if min_stock.strip().isdigit() else None
    prod.url = url.strip() or None
    n = 0
    if apply and prod.supply_days:
        for it in db.scalars(select(PlanItem).where(PlanItem.product_id == prod.id)):
            if it.plan.status == "active":
                it.supply_days = prod.supply_days
                n += 1
    db.commit()
    audit(db, "product.update", "product", prod.id, user=user, supply_days=prod.supply_days,
          min_stock=prod.min_stock, applied_to_items=n)
    return RedirectResponse("/products#p" + str(pid), 303)


@app.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, only: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    rows = catalog.product_rows(db)
    rows = [r for r in rows if r.prescribed or (r.stock or 0) > 0]
    if only == "deficit":
        rows = [r for r in rows if r.deficit]
    rows.sort(key=lambda r: (-(r.deficit or 0), -r.need_30))
    lv = db.scalar(select(StockLevel).limit(1))
    others = db.scalars(select(StockLevel).order_by(StockLevel.name)).all()
    linked = {r.product.cb_product_id for r in rows if r.product.cb_product_id}
    return render(request, "stock.html", rows=rows, only=only, updated=lv.updated_at if lv else None,
                  others=[o for o in others if o.cb_product_id not in linked][:300], cb=bool(settings.cleverbox_token))


@app.get("/finance", response_class=HTMLResponse)
def finance_page(request: Request, df: str | None = None, dt_: str | None = None,
                 user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    today = date.today()
    d_from = date.fromisoformat(df) if df else today.replace(day=1)
    d_to = date.fromisoformat(dt_) if dt_ else today
    fs = fin_svc.summary(db, d_from, d_to)
    fc = fin_svc.plan_forecast(db)
    sts = all_statuses(db, d_from, d_to)
    total, _ = kpi_summary(sts)
    m_max = max([m["revenue"] for m in fs.months] + [r["total"] for r in fc] + [1])
    return render(request, "finance.html", fs=fs, fc=fc, total=total, d_from=d_from, d_to=d_to, m_max=m_max)


@app.get("/book/{item_id}", response_class=HTMLResponse)
def book_page(request: Request, item_id: int, day: str | None = None, user: User = Depends(require("book")),
              db: Session = Depends(db_dep)):
    from .services import booking
    item = db.get(PlanItem, item_id)
    if not item or item.kind != "procedure" or not plan_visible(user, item.plan):
        raise HTTPException(404, "Процедуру не знайдено")
    ps = compute_plan(db, item.plan)
    d = date.fromisoformat(day) if day else max(date.today(), (ps.due_date.date() if ps.due_date else date.today()))
    svc, slots, error = None, [], None
    if not settings.cleverbox_token:
        error = "CleverBox не підключено (CLEVERBOX_TOKEN)"
    else:
        try:
            svc, slots = booking.get_slots(db, item, d)
        except Exception as e:      # noqa: BLE001
            error = str(e)
    return render(request, "book.html", item=item, ps=ps, day=d, svc=svc, slots=slots, error=error,
                  prev=d - timedelta(days=1), next=d + timedelta(days=1))


@app.post("/book/{item_id}")
def book_save(item_id: int, day: str = Form(...), slot: str = Form(...), comment: str = Form(""),
              user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .services import booking
    item = db.get(PlanItem, item_id)
    if not item or not plan_visible(user, item.plan):
        raise HTTPException(404, "Процедуру не знайдено")
    time_, master = slot.split("|")
    try:
        booking.book(db, user, item, date.fromisoformat(day), time_, int(master), comment)
    except Exception as e:          # noqa: BLE001
        raise HTTPException(400, f"CleverBox не прийняв запис: {e}")
    return RedirectResponse(f"/patients/{item.plan.patient_id}#plan{item.plan_id}", 303)

# ---------------- Скрипти, повідомлення пацієнтам, розсилки ----------------

from .models import ScriptTemplate, ProcedureNote, Campaign, CampaignRecipient, MessageLog
from .services import scripts as scripts_svc, campaigns as camp_svc
from .integrations import messaging


def _channels_ctx():
    return {"tg_on": messaging.telegram_available(), "ts_on": messaging.turbosms_available(),
            "viber_on": bool(settings.turbosms_sender_viber), "sms_on": bool(settings.turbosms_sender_sms)}


@app.post("/tasks/{tid}/send")
def task_send(tid: int, text: str = Form(...), channel: str = Form("auto"), close: str = Form(""),
              user: User = Depends(require("message")), db: Session = Depends(db_dep)):
    t = db.get(Task, tid)
    if not t or not t.patient_id or t not in db.scalars(tasks_svc.visible_query(user).where(Task.id == tid)).all():
        raise HTTPException(403, "Немає доступу до задачі")
    p = _patient_or_403(db, user, t.patient_id)
    res = camp_svc.send_single(db, p, text.strip(), channel, user, task_id=t.id)
    if not res.ok:
        raise HTTPException(400, f"Не надіслано ({res.channel}): {res.error}")
    note = f"Надіслано повідомлення ({res.channel}) {datetime.now():%d.%m %H:%M}"
    t.result = (t.result + "\n" if t.result else "") + note
    if close:
        t.status, t.done_at = "done", datetime.utcnow()
    db.commit()
    audit(db, "message.send", "patient", p.id, user=user, channel=res.channel, task=t.id)
    return RedirectResponse("/tasks", 303)


@app.post("/patients/{pid}/message")
def patient_message(pid: int, text: str = Form(...), channel: str = Form("auto"),
                    user: User = Depends(require("message")), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    res = camp_svc.send_single(db, p, text.strip(), channel, user)
    if not res.ok:
        raise HTTPException(400, f"Не надіслано ({res.channel}): {res.error}")
    audit(db, "message.send", "patient", p.id, user=user, channel=res.channel)
    return RedirectResponse(f"/patients/{pid}#contact", 303)


@app.post("/patients/{pid}/optout")
def patient_optout(pid: int, value: str = Form(...), user: User = Depends(require("patient_edit")),
                   db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    p.marketing_opt_out = value == "1"
    p.opt_out_at = datetime.now() if p.marketing_opt_out else None
    db.commit()
    audit(db, "patient.optout" if p.marketing_opt_out else "patient.optin", "patient", p.id, user=user)
    return RedirectResponse(f"/patients/{pid}#contact", 303)


@app.get("/scripts", response_class=HTMLResponse)
def scripts_page(request: Request, pid: int | None = None, user: User = Depends(require("scripts_edit")),
                 db: Session = Depends(db_dep)):
    tpls = db.scalars(select(ScriptTemplate).order_by(ScriptTemplate.kind, ScriptTemplate.channel)).all()
    notes = db.scalars(select(ProcedureNote).order_by(ProcedureNote.id)).all()
    sample = db.get(Patient, pid) if pid else next((p for p in db.scalars(select(Patient).join(BeautyPlan)).all()), None)
    previews = {}
    if sample:
        for kind in ("not_booked", "care_ending", "follow_up"):
            for sc in scripts_svc.for_patient(db, sample, kind, user.name):
                previews[sc.title] = sc.text
    return render(request, "scripts.html", tpls=tpls, notes=notes, sample=sample, previews=previews,
                  PLACEHOLDERS=scripts_svc.PLACEHOLDERS, KINDS=tasks_svc.KINDS)


@app.post("/scripts/templates")
async def scripts_tpl_save(request: Request, user: User = Depends(require("scripts_edit")), db: Session = Depends(db_dep)):
    f = await request.form()
    t = db.get(ScriptTemplate, int(f["id"])) if str(f.get("id", "")).isdigit() else ScriptTemplate()
    t.kind, t.channel = f.get("kind", "follow_up"), f.get("channel", "call")
    t.title, t.text = f.get("title", "").strip() or "Скрипт", f.get("text", "").strip()
    t.active = bool(f.get("active"))
    t.updated_at = datetime.utcnow()
    db.add(t)
    db.commit()
    audit(db, "script.update", user=user, template=t.title)
    return RedirectResponse("/scripts", 303)


@app.post("/scripts/notes")
async def scripts_note_save(request: Request, user: User = Depends(require("scripts_edit")), db: Session = Depends(db_dep)):
    f = await request.form()
    n = db.get(ProcedureNote, int(f["id"])) if str(f.get("id", "")).isdigit() else ProcedureNote()
    if f.get("delete") and n.id:
        db.delete(n)
    else:
        n.keyword, n.why, n.benefit = f.get("keyword", "").strip(), f.get("why", "").strip(), f.get("benefit", "").strip() or None
        if n.keyword and n.why:
            db.add(n)
    db.commit()
    audit(db, "script.note", user=user, keyword=f.get("keyword"))
    return RedirectResponse("/scripts#notes", 303)


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, user: User = Depends(require("campaigns")), db: Session = Depends(db_dep)):
    cs = db.scalars(select(Campaign).order_by(Campaign.created_at.desc()).limit(100)).all()
    st = {c.id: camp_svc.stats(db, c) for c in cs if c.status in ("sending", "done", "scheduled")}
    balance = messaging.turbosms_balance() if messaging.turbosms_available() else None
    return render(request, "campaigns.html", cs=cs, st=st, balance=balance, SEGMENTS=clients_svc.SEGMENTS, settings=settings,
                  IDEAS=camp_svc.SEGMENT_IDEAS, doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all(),
                  **_channels_ctx())


def _campaign_form(c: Campaign, f) -> None:
    c.name = (f.get("name") or "Розсилка").strip()
    c.segment = f.get("segment") if f.get("segment") in clients_svc.SEGMENTS else "sleeping"
    c.text = (f.get("text") or "").strip()
    c.channels = [ch for ch in ("telegram", "viber", "sms") if f.get(f"ch_{ch}")] or ["telegram"]
    c.filters = {k: f.get(k) for k in ("doctor_id", "source", "min_ltv", "max_ltv") if f.get(k)}
    if str(f.get("attribution_days", "")).isdigit():
        c.attribution_days = int(f["attribution_days"])


@app.post("/campaigns")
async def campaign_create(request: Request, user: User = Depends(require("campaigns")), db: Session = Depends(db_dep)):
    f = await request.form()
    c = Campaign(created_by=user.id, status="draft")
    _campaign_form(c, f)
    if not c.text:
        raise HTTPException(400, "Текст розсилки порожній")
    db.add(c)
    db.commit()
    audit(db, "campaign.create", "campaign", c.id, user=user, name=c.name, segment=c.segment)
    return RedirectResponse(f"/campaigns/{c.id}", 303)


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_page(request: Request, cid: int, user: User = Depends(require("campaigns")), db: Session = Depends(db_dep)):
    c = db.get(Campaign, cid) or (_ for _ in ()).throw(HTTPException(404, "Розсилку не знайдено"))
    aud = camp_svc.build_audience(db, c) if c.status == "draft" else None
    samples = [(p, ch, camp_svc.rendered_text(c, p)) for p, ch in (aud.rows[:5] if aud else [])]
    st = camp_svc.stats(db, c) if c.status != "draft" else None
    recips = c.recipients[:500] if c.status != "draft" else []
    return render(request, "campaign.html", c=c, aud=aud, samples=samples, st=st, recips=recips, settings=settings,
                  SEGMENTS=clients_svc.SEGMENTS, doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all(),
                  parts=messaging.sms_parts(c.text + "\n" + settings.marketing_footer), quiet=messaging.is_quiet_time(),
                  **_channels_ctx())


@app.post("/campaigns/{cid}")
async def campaign_update(request: Request, cid: int, user: User = Depends(require("campaigns")),
                          db: Session = Depends(db_dep)):
    c = db.get(Campaign, cid)
    if not c or c.status != "draft":
        raise HTTPException(400, "Змінювати можна лише чернетку")
    _campaign_form(c, await request.form())
    db.commit()
    return RedirectResponse(f"/campaigns/{cid}", 303)


@app.post("/campaigns/{cid}/test")
def campaign_test(cid: int, to: str = Form(...), user: User = Depends(require("campaigns")), db: Session = Depends(db_dep)):
    """Тест собі: на свій телефон (Viber/SMS) або Telegram chat ID у боті пацієнтів."""
    c = db.get(Campaign, cid)
    to = to.strip()
    text = "[ТЕСТ] " + camp_svc.rendered_text(c, Patient(full_name=user.name))
    if messaging.normalize_phone(to):          # номер телефону → Viber/SMS через TurboSMS
        res = messaging.send_turbosms(to, text + "\n" + settings.marketing_footer,
                                      [ch for ch in ("viber", "sms") if ch in c.channels] or ["sms"])
    else:                                      # інакше — Telegram chat ID у боті пацієнтів
        res = messaging.send_telegram(to, text)
    audit(db, "campaign.test", "campaign", c.id, user=user, ok=res.ok, channel=res.channel)
    if not res.ok:
        raise HTTPException(400, f"Тест не надіслано ({res.channel}): {res.error}")
    return RedirectResponse(f"/campaigns/{cid}", 303)


@app.post("/campaigns/{cid}/launch")
def campaign_launch(cid: int, when: str = Form(""), confirm: str = Form(""),
                    user: User = Depends(require("campaigns_send")), db: Session = Depends(db_dep)):
    c = db.get(Campaign, cid)
    if not c or c.status != "draft":
        raise HTTPException(400, "Запустити можна лише чернетку")
    if confirm != "1":
        raise HTTPException(400, "Підтвердіть запуск")
    if when:
        c.scheduled_at = datetime.fromisoformat(when)
        c.status = "scheduled"
        db.commit()
        audit(db, "campaign.schedule", "campaign", c.id, user=user, at=when)
        return RedirectResponse(f"/campaigns/{cid}", 303)
    n = camp_svc.start(db, c)
    audit(db, "campaign.launch", "campaign", c.id, user=user, recipients=n)
    if messaging.is_quiet_time():
        c.status = "scheduled"
        db.commit()
    else:
        jobs.start(f"Розсилка #{c.id}", camp_svc.deliver, campaign_id=c.id)
    return RedirectResponse(f"/campaigns/{cid}", 303)


@app.post("/campaigns/{cid}/cancel")
def campaign_cancel(cid: int, user: User = Depends(require("campaigns_send")), db: Session = Depends(db_dep)):
    c = db.get(Campaign, cid)
    if c and c.status in ("draft", "scheduled", "sending"):
        c.status = "cancelled"
        for r in c.recipients:
            if r.status == "queued":
                r.status, r.skip_reason = "skipped", "розсилку скасовано"
        db.commit()
        audit(db, "campaign.cancel", "campaign", c.id, user=user)
    return RedirectResponse(f"/campaigns/{cid}", 303)


@app.post("/campaigns/{cid}/copy")
def campaign_copy(cid: int, user: User = Depends(require("campaigns")), db: Session = Depends(db_dep)):
    c = db.get(Campaign, cid)
    n = Campaign(name=c.name + " (копія)", segment=c.segment, filters=c.filters, channels=c.channels, text=c.text,
                 attribution_days=c.attribution_days, created_by=user.id)
    db.add(n)
    db.commit()
    return RedirectResponse(f"/campaigns/{n.id}", 303)


@app.get("/patients")
def patients_redirect(request: Request):
    return RedirectResponse("/clients" + (("?" + request.url.query) if request.url.query else ""), 303)


_VERSION_FILE = Path(__file__).with_name("VERSION")
APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"


@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION, "last_sync": str(state["last_sync"][0]) if state["last_sync"] else None}


# ---------------- Спеціалісти ----------------
from .services import specialists as spec_svc   # noqa: E402

COLOR_RE = __import__("re").compile(r"^#[0-9a-fA-F]{6}$")


@app.get("/specialists", response_class=HTMLResponse)
def specialists_page(request: Request, show: str = "active", user: User = Depends(require("specialists")),
                     db: Session = Depends(db_dep)):
    cs = spec_svc.cards(db)
    active = [c for c in cs if c.doctor.active]
    fired = [c for c in cs if not c.doctor.active]
    return render(request, "specialists.html", cards=fired if show == "fired" else active, show=show,
                  n_active=len(active), n_fired=len(fired), staff_fields=spec_svc.cb_staff_fields(db),
                  palette=spec_svc.PALETTE)


@app.post("/specialists")
def specialist_create(name: str = Form(...), position: str = Form(""), user: User = Depends(require("specialists_edit")),
                      db: Session = Depends(db_dep)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Вкажіть імʼя")
    if db.scalar(select(Doctor).where(Doctor.name == name)):
        raise HTTPException(400, "Спеціаліст з таким імʼям уже є")
    d = Doctor(name=name, position=position.strip() or None, source="manual", active=True, in_calendar=True)
    db.add(d)
    db.commit()
    audit(db, "specialist.create", "doctor", d.id, user=user)
    return RedirectResponse(f"/specialists/{d.id}", 303)


@app.get("/specialists/{did:int}", response_class=HTMLResponse)
def specialist_page(did: int, request: Request, user: User = Depends(require("specialists")),
                    db: Session = Depends(db_dep)):
    d = db.get(Doctor, did)
    if not d:
        raise HTTPException(404)
    card = spec_svc.cards(db, [did])[0]
    now = datetime.now()
    idx = spec_svc.DoctorIndex(spec_svc.all_doctors(db))
    upcoming = [v for v in db.scalars(select(Visit).where(Visit.status == "booked", Visit.starts_at > now,
                                                          Visit.starts_at <= now + timedelta(days=14),
                                                          Visit.master_name.is_not(None)).order_by(Visit.starts_at))
                if (m := idx.find(v.master_name)) and m.id == did][:30]
    patients = db.scalars(select(Patient).where(Patient.doctor_id == did).order_by(Patient.full_name).limit(200)).all()
    return render(request, "specialist.html", d=d, c=card, upcoming=upcoming, patients=patients,
                  palette=spec_svc.PALETTE)


@app.post("/specialists/{did:int}")
def specialist_save(did: int, name: str = Form(...), position: str = Form(""), phone: str = Form(""),
                    email: str = Form(""), color: str = Form(""), bio: str = Form(""), work_days: str = Form(""),
                    telegram_chat_id: str = Form(""), photo_url: str = Form(""), status: str = Form("active"),
                    in_calendar: str = Form(""), user: User = Depends(require("specialists_edit")),
                    db: Session = Depends(db_dep)):
    d = db.get(Doctor, did)
    if not d:
        raise HTTPException(404)
    name = name.strip()
    if name and name != d.name and not db.scalar(select(Doctor).where(Doctor.name == name)):
        d.name = name
    d.position, d.phone, d.email = position.strip() or None, phone.strip() or None, email.strip() or None
    d.bio, d.work_days = bio.strip() or None, work_days.strip() or None
    d.telegram_chat_id = telegram_chat_id.strip() or None
    photo_url = photo_url.strip()
    d.photo_url = photo_url if photo_url.startswith("https://") else (None if not photo_url else d.photo_url)
    d.color = color if COLOR_RE.match(color or "") else None
    want_active = status != "fired"
    if want_active != bool(d.active):
        d.active, d.status_locked = want_active, True
        d.fired_at = None if want_active else date.today()
    d.in_calendar = in_calendar == "1"
    db.commit()
    audit(db, "specialist.update", "doctor", d.id, user=user)
    return RedirectResponse(f"/specialists/{did}?saved=1", 303)


@app.post("/specialists/sync")
async def specialists_sync(user: User = Depends(require("sync")), db: Session = Depends(db_dep)):
    """Оновити спеціалістів з CleverBox зараз (не чекаючи щоденного синку)."""
    if not settings.cleverbox_token:
        return RedirectResponse("/specialists?msg=Спершу+збережіть+токен+CleverBox+у+розділі+Дані", 303)
    from .integrations.cleverbox import CleverBox, sync_staff
    try:
        n = await asyncio.to_thread(lambda: sync_staff(db, CleverBox()))
        audit(db, "specialists.sync", user=user, linked=n)
        return RedirectResponse(f"/specialists?msg=Оновлено+з+CleverBox", 303)
    except Exception as e:  # noqa: BLE001
        log.warning("specialists sync: %s", type(e).__name__)
        return RedirectResponse("/specialists?msg=CleverBox+не+відповів,+спробуйте+пізніше", 303)


@app.post("/specialists/{did:int}/unlock")
def specialist_unlock(did: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    d = db.get(Doctor, did)
    if d:
        d.status_locked = False
        db.commit()
        audit(db, "specialist.status_auto", "doctor", d.id, user=user)
    return RedirectResponse(f"/specialists/{did}", 303)


# ---------------- Вебхуки CleverBox (події в реальному часі) ----------------
from .integrations import cb_webhooks   # noqa: E402


@app.post("/hooks/cleverbox/{secret}")
async def cleverbox_hook(secret: str, request: Request, db: Session = Depends(db_dep)):
    if not cb_webhooks.check_secret(db, secret):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > 512_000:
        raise HTTPException(413)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        form = await request.form()
        payload = {k: v for k, v in form.items()}
        if "data" in payload and isinstance(payload["data"], str):
            try:
                payload["data"] = json.loads(payload["data"])
            except ValueError:
                pass
    ev = await asyncio.to_thread(cb_webhooks.handle, payload)
    return {"ok": True, "event": ev["event"]}


@app.post("/settings/cleverbox-webhooks")
async def cleverbox_webhooks_subscribe(user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    if not settings.cleverbox_token:
        return RedirectResponse("/data?cb_notice=Спершу+збережіть+токен#migrate", 303)
    if not settings.public_url.startswith("https://"):
        return RedirectResponse("/data?cb_notice=PUBLIC_URL+у+.env+має+бути+https://crm.molodist.com.ua#migrate", 303)
    res = await asyncio.to_thread(cb_webhooks.subscribe, db)
    audit(db, "cleverbox.webhooks.subscribe", user=user, ok=sum(r["ok"] for r in res))
    ok = all(r["ok"] for r in res)
    msg = "Вебхуки+CleverBox+підключено" if ok else "CleverBox+відхилив+підписку+—+деталі+нижче"
    return RedirectResponse(f"/data?cb_notice={msg}#webhooks", 303)
