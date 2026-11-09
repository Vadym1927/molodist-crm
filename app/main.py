"""MOLODIST Beauty Plan CRM — веб-інтерфейс (з ролями), REST API, планувальник синхронізації та Telegram-бот."""
from __future__ import annotations

import asyncio
import hmac
from collections import defaultdict
import json
import re
import logging
import mimetypes
import shutil
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, date
import httpx
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, Response, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .models import (init_db, SessionLocal, Patient, Doctor, BeautyPlan, PlanItem, PatientFile, ProductFile, Alias,
                     CbService, Visit, SyncState, User, ApiKey, Webhook, AuditLog, ROLES, CashDay)
from .parsers.observ import norm
from urllib.parse import quote
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
templates.env.filters["uah"] = lambda v: f"{(v or 0):,.2f}".replace(",", " ").replace(".", ",") + " ₴"   # копійка в копійку
templates.env.globals["call_dialog"] = lambda t, e=None: __import__("app.services.telephony", fromlist=["dialog"]).dialog(t, e)
templates.env.filters["dt"] = lambda v, f="%d.%m.%Y": v.strftime(f) if v else "—"


def _local(v):
    """UTC (datetime.utcnow у БД) → київський час для показу."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


templates.env.filters["local"] = _local


def _telephony_on() -> bool:
    """Підключено IP-телефонію (провайдер обрано) — у шаблоні вмикається спливаюче вікно вхідних дзвінків."""
    try:
        with SessionLocal() as _db:
            st = _db.get(SyncState, "telephony_cfg")
            return bool(st and st.value and json.loads(st.value).get("provider"))
    except Exception:  # noqa: BLE001
        return False


templates.env.globals["telephony_on"] = _telephony_on


def _call_who(calls) -> dict:
    """Для журналу дзвінків: id → {line (назва лінії), resp (відповідальний), taker (хто взяв)}."""
    from .services import telephony as _tel
    calls = list(calls or [])
    if not calls:
        return {}
    with SessionLocal() as db_:
        d = _tel._raw(db_)
        un = {u.id: u.name for u in db_.scalars(select(User))}
    out = {}
    for c in calls:
        li = _tel.line_info(d, getattr(c, "line", None))
        out[c.id] = {"line": (li.get("name") or li.get("number")) if li else (getattr(c, "line", None) or ""),
                     "resp": un.get(li.get("user_id")) if li else None,
                     "taker": un.get(c.user_id) or (c.employee if c.employee and c.employee != "0" else None)}
    return out


templates.env.globals["call_who"] = _call_who
FIN_TABS = [("/finance", "Огляд"), ("/sales", "Каса CRM"), ("/finance/ops", "Фіноперації"), ("/finance/accruals", "Нарахування зарплати"),
            ("/finance/payroll", "ЗП за місяць"),
            ("/finance/settle", "Взаєморозрахунки по з/п"), ("/finance/day", "Продажі за день"), ("/finance/sales", "Продажі"),
            ("/finance/balances", "Баланс пацієнтів"), ("/finance/bonuses", "Рух бонусів"), ("/payreq", "Рахунки на оплату"),
            ("/finance/calendar", "Платіжний календар"), ("/finance/fop-schedule", "Графік ФОПів"),
            ("/finance/pricing", "Планування ціни реалізації"), ("/finance/service-pricing", "Планування ціни послуг"),
            ("/amort", "Амортизація обладнання")]
def _site_url(request) -> str:
    """Абсолютна адреса CRM для og:image / og:url: PUBLIC_URL (https) або адреса запиту."""
    u = (settings.public_url or "").rstrip("/")
    if u.startswith("https://"):
        return u
    return str(request.base_url).rstrip("/")


SITE_DESC = "Робочий простір команди медичного простору MOLODIST: записи, пацієнти, бюті-плани, склад і фінанси."
templates.env.globals.update(site_url=_site_url, SITE_DESC=SITE_DESC)
templates.env.globals.update(
    can=can, ROLES=ROLES, EVENTS=webhooks.EVENTS, FIN_TABS=FIN_TABS,
    FLAG={"green": ("записана", "ok"), "yellow": ("без запису", "warn"),
          "red": ("прострочено", "bad"), "done": ("завершено", "done")})

def _pay_accounts() -> list[dict]:
    """Готові підключення платіжних систем — для вибору ФОП у формі посилання на оплату."""
    from .services import paylinks as _pls
    try:
        with SessionLocal() as _db:
            t_ = _pls.public(_db)
            return [{"id": a_["id"], "name": a_["name"], "default": int(t_.get("default") or 0) == int(a_["id"])} for a_ in t_["accounts"] if a_["ready"]]
    except Exception:  # noqa: BLE001
        return []


templates.env.globals["pay_accounts"] = _pay_accounts


def _work_mode() -> dict:
    from .services import autonomy as _aut
    try:
        return _aut.cfg()
    except Exception:  # noqa: BLE001
        return {}


templates.env.globals["work_mode"] = _work_mode
templates.env.globals["user_photo"] = lambda u: __import__("app.services.shifts", fromlist=["x"]).photo_url(u) if u else None
templates.env.globals["ui_rules"] = lambda role: __import__("app.services.ui_access", fromlist=["x"]).client_rules(role)
templates.env.globals["pdisc"] = lambda p: __import__("app.services.personal_discounts", fromlist=["x"]).pcts(p)

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
    from .services import autonomy as _aut
    if settings.cleverbox_token and auto_sync_enabled() and not _aut.offline():
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
    from .services import stock as stock_svc
    with SessionLocal() as db:
        rep["tasks"] = generate_auto(db)
        try:
            rep["stock_auto"] = stock_svc.auto_moves(db)      # продажі й процедури списують склад
        except Exception as e:      # noqa: BLE001
            log.warning("stock auto moves failed: %s", e)
        try:
            from .services import cashflow as _cf
            rep["cashflow_days"] = _cf.auto_fill(db)        # нові дні — надходження з чеків до наступного звіту
        except Exception as e:      # noqa: BLE001
            log.warning("cashflow auto fill failed: %s", e)
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
    from .services import alerts, autonomy
    if autonomy.offline():
        return
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


async def job_price_monitor():
    """Щогодини: ціни після синку CleverBox / поставок — зони подорожчання й сповіщення."""
    def _run():
        from .services import price_monitor as pm
        with SessionLocal() as db:
            return pm.scan(db)
    try:
        await asyncio.to_thread(_run)
    except Exception:              # noqa: BLE001
        log.exception("price monitor failed")


async def job_message_statuses():
    from .services import campaigns as camp
    def _refresh():
        with SessionLocal() as db:
            return camp.refresh_statuses(db)
    try:
        await asyncio.to_thread(_refresh)
    except Exception:              # noqa: BLE001
        log.exception("statuses refresh failed")



async def job_shop():
    """molodist.store (Shop-Express): щоранку — посилання, фото й ціни товарів у картки CRM."""
    from .integrations import shop_express as sx
    def run():
        with SessionLocal() as db:
            if sx.creds(db).get("login"):
                return sx.sync(db)
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("shop-express sync: %s", e)


async def job_expiry():
    """Терміни придатності: щодня — нові межі 30/7/0 днів, у вибраний день — підсумок по складах."""
    from .services import expiry as ex
    def run():
        with SessionLocal() as db:
            return ex.daily(db)
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("expiry notify: %s", e)


async def job_amort():
    """16-го — нарахування за 1–15, 1-го — за 16–кінець минулого місяця; звіт власнику в Telegram."""
    from .services import amort as _am, alerts
    try:
        with SessionLocal() as db:
            txt = await asyncio.to_thread(_am.run_scheduled, db)
        if txt:
            await asyncio.to_thread(alerts.send, txt + f"\n{settings.public_url.rstrip('/')}/amort")
    except Exception as e:          # noqa: BLE001
        log.warning("amort accrual failed: %s", e)


async def job_loyalty():
    """Щодня: бонуси до дня народження, згоряння бонусів з минулим терміном."""
    from .services import loyalty as ly
    def run():
        with SessionLocal() as db:
            if ly.settings(db).get("enabled"):
                return {"birthday": ly.birthday_bonuses(db), "expired": ly.expire(db)}
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("loyalty: %s", e)


async def job_rematch():
    """Перерозносити платежі, що вже є в CRM (без запиту в monobank): нові правила, частково рознесені."""
    from .services import payments as pay
    def run():
        with SessionLocal() as db:
            return pay.rematch_all(db)
    try:
        n = await asyncio.to_thread(run)
        log.info("rematch: %s", n)
    except Exception as e:  # noqa: BLE001
        log.warning("rematch: %s", e)


async def job_mono():
    """monobank: щогодини — списання за 2 доби, рознесення оплат постачальникам."""
    from .integrations import monobank
    def run():
        with SessionLocal() as db:
            if monobank.saved(db):
                return monobank.sync(db, days=2)
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("monobank sync: %s", e)


async def job_energy():
    """Звіт Deye: ціна WOG А-95 на сьогодні (раз на день) і дані станції з Deye Cloud за останні 3 дні."""
    from .services import energy as en
    def run():
        with SessionLocal() as db:
            en.capture_fuel(db)
            try:
                return en.sync_deye(db)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "msg": str(e)[:200]}
    try:
        r = await asyncio.to_thread(run)
        if not r.get("ok") and "не підключено" not in r.get("msg", ""):
            log.warning("energy: %s", r.get("msg"))
    except Exception as e:  # noqa: BLE001
        log.warning("job_energy: %s", e)


async def job_paycal():
    """09:00 — платіжний календар: що платити сьогодні, прострочене, що чекає погодження — власнику в Telegram."""
    from .services import paycal as _pc, alerts as _al
    def run():
        with SessionLocal() as db:
            return _pc.digest_text(db)
    try:
        txt = await asyncio.to_thread(run)
        if txt:
            _al.send(txt)
    except Exception as e:  # noqa: BLE001
        log.warning("job_paycal: %s", e)


async def job_energy_report():
    """08:00 — звіт Deye за вчора власнику в Telegram (перед цим оновити дані з Deye)."""
    from .services import energy as en
    def run():
        with SessionLocal() as db:
            try:
                en.sync_deye(db, 3)
            except Exception as e:  # noqa: BLE001
                log.warning("energy report sync: %s", e)
            return en.send_daily(db)
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("job_energy_report: %s", e)


async def job_payreq():
    """Чернетки оплат у monobank: стан (підписано → у виписці / відхилено) — щогодини; картку в Telegram оновити."""
    from .services import pay_requests as prq
    def run():
        with SessionLocal() as db:
            ch = prq.refresh_all(db)
            for x in ch:
                db.expunge(x)
            return ch
    try:
        ch = await asyncio.to_thread(run)
        if ch and state.get("bot"):
            from .integrations.telegram_bot import pay_notify
            await pay_notify(state["bot"], ch)
        paid = [x.id for x in ch if x.status == "paid"]
        if paid:                                              # «✅ Оплачено» окремим повідомленням у групі під карткою рахунку
            def _reply():
                with SessionLocal() as db:
                    from .models import PayRequest as _PR
                    for i in paid:
                        x = db.get(_PR, i)
                        if x:
                            prq.tg_paid_reply(db, x)
            await asyncio.to_thread(_reply)
    except Exception as e:  # noqa: BLE001
        log.warning("payreq refresh: %s", e)


async def job_tg_notify():
    """Сповіщення співробітникам у Telegram за ролями й часом (Налаштування → Співробітники → Сповіщення в Telegram)."""
    from .services import tg_notify
    def run():
        with SessionLocal() as db:
            return tg_notify.run(db)
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("tg_notify: %s", e)


async def job_checkbox():
    """Оборот ФОП з Checkbox (Z-звіти) — поточний і попередній місяць."""
    from .integrations import checkbox as chb
    if not any(a["configured"] for a in chb.accounts()):
        return
    def run():
        with SessionLocal() as db:
            return chb.sync(db)
    try:
        await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        log.warning("checkbox sync: %s", e)

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
        from .services import clinic as _clinic
        _clinic.apply(_clinic.get(db))                      # часовий пояс з «Налаштування → Загальні»
        from .services import roles as _roles
        _roles.apply(db)                                    # свої ролі й права з «Права доступу»
        from .services import channels as _channels
        _channels.apply(db)
        from .services import cashboxes as _cbx
        _cbx.apply_seed(db)
        if not db.get(SyncState, "supply_finops_backfill_v1"):     # уже рознесені оплати поставок → журнал фіноперацій (один раз)
            from .services import payments as _pay0
            _n0 = _pay0.backfill_finops(db)
            db.merge(SyncState(key="supply_finops_backfill_v1", value=str(_n0)))
            db.commit()
        from .services.techcards import migrate_from_norms as _tcm, fix_unit_costs_v2 as _tcf   # техкарти CRM з норм списання (один раз)
        _tcm(db)
        _tcf(db)                                                     # собівартість витратних: без подвійного ділення на коеф. (один раз)
        if not db.get(SyncState, "cb_calendar_freeze_20261031"):   # 31.10.2026: календар — лише вручну в CRM (рішення власника)
            from .integrations.cleverbox import set_calendar_frozen as _fz
            _fz(db, True)
            db.merge(SyncState(key="cb_calendar_freeze_20261031", value="1"))
            db.commit()
        from .services import autonomy as _aut_w
        _aut_w.cfg(db)                                          # режим роботи — у кеш процесу
        # Записи CleverBox з дати переходу в CRM не переносяться: адміністратори дублюють їх у CRM вручну (рішення власника 02.11.2026)
        if not db.get(SyncState, "work_mode_init_20261101"):      # 01.11.2026: тестування — CRM автономно, без фіскалізації (рішення власника)
            from .services import autonomy as _aut
            _aut.save(db, autonomous=True, since="2026-11-01", test=True)
            db.merge(SyncState(key="work_mode_init_20261101", value="1"))
            db.commit()
        if not db.get(SyncState, "revert_unrecorded_v1"):        # відкат: оплати без поставки НЕ списуємо — спишуться з поставкою
            from .services import payments as _pay1
            _pay1.revert_booked_without_supply(db)
            db.merge(SyncState(key="revert_unrecorded_v1", value="1"))
            db.commit()                                 # залишки кас на 30.10.2026 (один раз)                                 # канали (TurboSMS, бот) з «Налаштування → Канали»
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
        sched.add_job(job_price_monitor, "cron", minute=40)
        sched.add_job(job_checkbox, "cron", hour="8,14,23", minute=50)
        sched.add_job(job_mono, "cron", minute=20)
        sched.add_job(job_payreq, "cron", minute=35)
        sched.add_job(job_tg_notify, "cron", minute="*/5")
        sched.add_job(job_rematch, "date", run_date=datetime.now() + timedelta(seconds=40))   # після оновлення — одразу
        sched.add_job(job_expiry, "cron", hour=9, minute=15)
        sched.add_job(job_loyalty, "cron", hour=9, minute=5)
        sched.add_job(job_reconcile, "cron", hour=9, minute=0)
        sched.add_job(job_amort, "cron", day="1,16", hour=9, minute=10)
        sched.add_job(job_shop, "cron", hour=6, minute=40)
        sched.add_job(job_energy, "cron", hour="0,6,12,18", minute=7)
        sched.add_job(job_energy_report, "cron", hour=8, minute=0)
        sched.add_job(job_paycal, "cron", hour=9, minute=0)
        sched.start()
    except Exception as e:          # noqa: BLE001
        log.warning("scheduler not started: %s", e)
    try:
        from .services import amort as _am
        with SessionLocal() as db:
            _r = _am.ensure_setup(db)
        if _r:
            log.info("amort setup: %s", _r)
    except Exception as e:          # noqa: BLE001
        log.warning("amort setup failed: %s", e)
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
class _DropEmptyQuery:
    """Порожні параметри з форм фільтрів (?doctor=&page=) — як відсутні, а не помилка 422."""
    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("query_string") and not scope["path"].startswith("/api/"):
            from urllib.parse import parse_qsl, urlencode
            pairs = parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if v != ""]
            if len(kept) != len(pairs):
                scope = dict(scope, query_string=urlencode(kept).encode("latin-1"))
        await self.app(scope, receive, send)


class _BankFees:
    """Після операцій з грошима (продаж, оплата, повернення, фіноперація, онлайн-оплата) — списання комісії банку
    в журнал фіноперацій за налаштуваннями кас (cashboxes.sync_fees, ідемпотентно)."""
    PREFIX = ("/sales", "/finance/ops", "/paylinks", "/pay/", "/api/", "/stock", "/products")

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("method") == "POST" and scope["path"].startswith(self.PREFIX):
            await asyncio.to_thread(self.run)

    @staticmethod
    def run():
        try:
            from .services import cashboxes as _cbx
            with SessionLocal() as _db:
                _cbx.sync_fees(_db)
        except Exception as e:  # noqa: BLE001
            log.warning("Комісія банку: %s", e)
        try:                                             # оплачені візити — списання витратних; повернені — назад
            from .services import techcards as _tc
            with SessionLocal() as _db:
                _tc.sync(_db)
        except Exception as e:  # noqa: BLE001
            log.warning("Закриття візитів: %s", e)
        try:                                             # центр моніторингу цін: новий прихід → зони подорожчання
            from .services import price_monitor as _pm
            with SessionLocal() as _db:
                _pm.scan(_db)
        except Exception as e:  # noqa: BLE001
            log.warning("Моніторинг цін: %s", e)


app.add_middleware(_BankFees)
app.add_middleware(_DropEmptyQuery)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 14,
                   https_only=settings.public_url.startswith("https"))
app.include_router(api_module.router)
from fastapi.staticfiles import StaticFiles   # noqa: E402
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if "json" in (request.headers.get("content-type") or "") or "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"error": exc.detail, "detail": exc.detail}, status_code=exc.status_code)
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
            if can(user, "stock"):
                try:
                    from .services.price_monitor import unseen as _pu
                    counts = dict(counts or {}, prices=_pu(db))
                except Exception:  # noqa: BLE001
                    pass
            if can(user, "appeals"):
                try:
                    from .models import Appeal as _Ap
                    counts = dict(counts or {}, appeals=db.scalar(select(func.count()).select_from(_Ap).where(_Ap.status == "Новий")) or 0)
                except Exception:  # noqa: BLE001
                    pass
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
    from .services import ui_access as _ua                      # перше вікно після входу — календар (якщо роль його бачить)
    return RedirectResponse("/" if _ua.blocked(u.role, "GET", "/calendar", {}) else "/calendar", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/me/panel", response_class=HTMLResponse)
def me_panel(request: Request, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    """Панель профілю (клік по аватару): фото, вихід, касова зміна, відкриті зміни, хто онлайн."""
    from .services import shifts as sh
    cur = sh.current(db, user)
    users = {u.id: u.name for u in db.scalars(select(User))}
    others = [s_ for s_ in sh.open_shifts(db) if s_.opened_by != user.id]
    return templates.TemplateResponse(request, "_profile_panel.html", {
        "request": request, "user": user, "can": can, "photo": sh.photo_url(user), "cur": cur,
        "cur_sum": sh.sales_summary(db, cur) if cur else None, "boxes": sh.boxes(db) if can(user, "sales") else [],
        "recent": sh.recent(db), "online": sh.online(db), "users": users, "sh": sh, "others": others, "busy": sh.busy(db, user.id),
        "rros": [r for r in __import__("app.services.fiscal", fromlist=["x"]).rros(db) if r["on"] and r["has_key"]] if can(user, "sales") else []})


@app.post("/me/photo")
async def me_photo(request: Request, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .services import ingest
    data = await file.read()
    nm = (file.filename or "photo").lower()
    back = request.headers.get("referer") or "/calendar"
    if not data or len(data) > 5 * 1024 * 1024 or not (nm.endswith((".jpg", ".jpeg", ".png", ".webp")) or (file.content_type or "").startswith("image/")):
        return RedirectResponse(_with_msg(back, "Фото: jpg, png або webp до 5 МБ", True), 303)
    user.photo_path = ingest.store(data, "user_" + nm[-40:])
    user.photo_mime = (file.content_type or "image/jpeg")[:60]
    db.commit()
    audit(db, "user.photo", "user", user.id, user=user)
    db.commit()
    return RedirectResponse(back, 303)


@app.get("/me/photo/{uid:int}")
def me_photo_get(uid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    u = db.get(User, uid)
    if not u or not u.photo_path or not Path(u.photo_path).exists():
        raise HTTPException(404)
    return FileResponse(u.photo_path, media_type=u.photo_mime or "image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.get("/rro/{rid:int}/status")
async def rro_status_get(rid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    try:
        return await asyncio.to_thread(_fis.rro_status, db, rid)
    except (_fis.FiscalError, httpx.HTTPError) as e:
        return {"id": rid, "state": "error", "label": f"Checkbox: {str(e)[:120]}"}


@app.post("/rro/{rid:int}/{action}")
async def rro_action_post(rid: int, action: str, request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """Каса Checkbox з панелі профілю: відкрити / закрити зміну (Z-звіт), внести / винести готівку, X-звіт."""
    from .services import fiscal as _fis
    if action not in ("open", "close", "in", "out", "x"):
        raise HTTPException(404)
    f = await request.form()
    try:
        amt = float(str(f.get("amount") or "0").replace(",", ".").replace(" ", ""))
    except ValueError:
        amt = 0
    try:
        msg = await asyncio.to_thread(_fis.rro_action, db, rid, action, amt)
    except (_fis.FiscalError, httpx.HTTPError) as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=400)
    audit(db, f"fiscal.{action}", user=user, rro=rid, amount=amt or None)
    db.commit()
    return {"ok": True, "msg": msg}


@app.post("/shift/open")
async def shift_open(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import shifts as sh
    f = await request.form()
    try:
        s_ = sh.open_shift(db, user, [int(x) for x in f.getlist("box") if str(x).isdigit()])
    except sh.ShiftError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    audit(db, "shift.open", "shift", s_.id, user=user, boxes=[b["name"] for b in s_.boxes])
    db.commit()
    return {"ok": True, "id": s_.id}


@app.post("/shift/close")
async def shift_close(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import shifts as sh
    f = await request.form()
    try:
        sid_ = str(f.get("shift") or "")
        s_ = sh.close_shift(db, user, str(f.get("note") or ""), int(sid_) if sid_.isdigit() else None)
    except sh.ShiftError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    audit(db, "shift.close", "shift", s_.id, user=user, total=(s_.summary or {}).get("total"))
    db.commit()
    return {"ok": True, "id": s_.id, "summary": s_.summary}


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


def _patient_visit_rows(db: Session, p: Patient) -> list[dict]:
    from .models import Appointment
    """Візити пацієнта: записи CRM (і перенесена історія CleverBox) — клік відкриває сам візит у календарі;
    рядки CleverBox показуються лише за дні, яких ще немає серед записів CRM."""
    docs = {d_.id: d_.name for d_ in db.scalars(select(Doctor))}
    ST = {"booked": "запис", "confirmed": "підтверджено", "arrived": "прийшов", "paid": "оплачено", "done": "виконано", "no_show": "неявка", "cancelled": "скасовано"}
    out, days = [], set()
    for a in db.scalars(select(Appointment).where(Appointment.patient_id == p.id).order_by(Appointment.start.desc())):
        days.add(a.start.date())
        out.append({"appt": a.id, "at": a.start, "name": ", ".join(str(x.get("name") or "") for x in a.services or []) or "—",
                    "doctor": docs.get(a.doctor_id, ""), "sum": a.cb_paid if a.historic and a.cb_paid is not None else a.total,
                    "status": "done" if a.status == "paid" else a.status, "label": ST.get(a.status, a.status), "cb": a.historic})
    for v in p.visits:
        if v.starts_at and v.starts_at.date() not in days:
            out.append({"appt": None, "visit": v.id, "at": v.starts_at, "name": v.service_name or f"послуга #{v.service_id}", "doctor": v.master_name or "",
                        "sum": v.amount, "status": "done" if v.status == "done" else "booked", "label": "виконано" if v.status == "done" else "запис", "cb": True})
    out.sort(key=lambda r: r["at"], reverse=True)
    return out


def _sale_sellers(db: Session, sales) -> dict[int, str]:
    """Ініціатор продажу товару: каса CRM — спеціаліст позиції; CleverBox — initiator_name позиції чека."""
    from .models import SaleItem, Check
    out, crm, cbk = {}, {}, {}
    for s_ in sales:
        ext = s_.external_id or ""
        if ext.startswith("crm:"):
            try:
                crm[s_.id] = int(ext.split(":")[2])
            except (IndexError, ValueError):
                pass
        else:
            head = ext.split("-", 1)[0]
            if head.isdigit():
                cbk[s_.id] = int(head)
    if crm:
        docs = {d_.id: d_.name for d_ in db.scalars(select(Doctor))}
        items = {i.id: i for i in db.scalars(select(SaleItem).where(SaleItem.id.in_(set(crm.values()))))}
        for sid, iid in crm.items():
            it = items.get(iid)
            if it and it.doctor_id in docs:
                out[sid] = docs[it.doctor_id]
    if cbk:
        checks = {c.id: c for c in db.scalars(select(Check).where(Check.id.in_(set(cbk.values()))))}
        byid = {s_.id: s_ for s_ in sales}
        for sid, chk in cbk.items():
            c = checks.get(chk)
            if not c:
                continue
            prods = [it for it in (c.items or []) if (it.get("item_type") or "") == "product"]
            s_ = byid[sid]
            pick = next((it for it in prods if s_.product_id and str(it.get("item_id")) == str(s_.product_id)), None) \
                or next((it for it in prods if (it.get("item_name") or "").strip() == (s_.product_name or "").strip()), None) \
                or (prods[0] if len({(x.get("initiator_name") or "").strip() for x in prods}) == 1 and prods else None)
            name = ((pick or {}).get("initiator_name") or "").strip()
            if name:
                out[sid] = name
    return out


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
    from .services import patient_match as _pm
    dupes = _pm.suggestions(db, p) if not p.cleverbox_id and can(user, "patients_all") else []
    from .services import certificates as _cert
    from .models import PayLink as _PL
    paylinks = db.scalars(select(_PL).where(_PL.patient_id == p.id).order_by(_PL.id.desc()).limit(10)).all() if can(user, "sales") else []
    from .models import Call as _Call
    pcalls = db.scalars(select(_Call).where(_Call.patient_id == p.id).order_by(_Call.at.desc()).limit(50)).all() if (can(user, "appeals") or can(user, "book")) else []
    pvis = _patient_visit_rows(db, p)
    pchats = []                                             # діалоги Instagram / Messenger, привʼязані до пацієнта
    if can(user, "appeals"):
        from .models import ChatMessage as _CM, Appeal as _ApM
        from .services import meta_inbox as _mi
        _th: dict = {}
        for m_ in db.scalars(select(_CM).where(_CM.patient_id == p.id).order_by(_CM.at, _CM.id)):
            _th.setdefault(m_.thread, []).append(m_)
        _aps = {a_.id: a_ for a_ in db.scalars(select(_ApM).where(_ApM.id.in_({m_.appeal_id for v in _th.values() for m_ in v if m_.appeal_id} or {0})))}
        pchats = sorted((s_ for v in _th.values() for s_ in _mi.sessions(v, _aps)), key=lambda x: x["start"], reverse=True)
    from .services import patient_photos as _pp
    _ph_items = [{"f": f, "cat": _pp.cat(f), "img": _pp.is_image(f)} for f in
                 sorted(_pp.all_files(db, p.id), key=lambda f: (f.taken_at or f.created_at.date(), f.id), reverse=True)]
    _ph_counts = {k: sum(1 for x in _ph_items if x["cat"] == k) for k in _pp.CATS}
    _sellers = _sale_sellers(db, p.sales)
    phist = [dict(r, kind="visit") for r in pvis] + [
        {"kind": "sale", "at": s_.sold_at, "name": s_.product_name, "doctor": _sellers.get(s_.id, ""), "sum": s_.amount, "status": "sale",
         "label": "продаж", "appt": None, "qty": s_.quantity} for s_ in p.sales if s_.sold_at]
    phist.sort(key=lambda r: r["at"], reverse=True)
    from .services import patient_tags as _ptg
    _lv = _ptg.tags(db)
    _st = _ptg.stats(db, [p.id]).get(p.id) or {"visits": 0, "planned": 0, "last_days": None}
    _vd = _st["visits"] or int(p.cb_visits or 0)
    ptag = _ptg.tag_for(_lv, _vd, _st["planned"], _st["last_days"])
    ptag_color = _ptg.colors(_lv).get(ptag, "#8a8496")
    page_age = None
    if p.birthday:
        _t = date.today()
        page_age = _t.year - p.birthday.year - ((_t.month, _t.day) < (p.birthday.month, p.birthday.day))
    from .models import CbService as _CbS
    pe_services = sorted({(n_, pr_) for n_, pr_ in db.execute(select(_CbS.name, _CbS.price).where(_CbS.active.is_not(False))) if n_}, key=lambda x: x[0])
    pe_products = sorted({(n_, pr_) for n_, pr_ in db.execute(select(Product.name, Product.price).where(Product.active.is_not(False))) if n_}, key=lambda x: x[0])
    return render(request, "patient.html", p=p, statuses=statuses, pe_services=pe_services, pe_products=pe_products, ptasks=ptasks, msgs=msgs, dupes=dupes, paylinks=paylinks, pcalls=pcalls, pchats=pchats,
                  call_thr=float(__import__("app.services.telephony", fromlist=["x"]).public(db).get("ai_alert_below") or 0),
                  certs=_cert.for_patient(db, p.id), CERT_STATES=_cert.STATES, today=date.today(),
                  subs=_cert.subs_for_patient(db, p.id), SUB_STATES=_cert.SUB_STATES, packages=_cert.package_items(db),
                  CB_LABELS=CB_LABELS, loyal=_loyal_ctx(db, p),
                  pscripts=_sc.for_patient(db, p, kind, user.name) if can(user, "message") else [],
                  avail=_msg.available_channels(p),
                  users=db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all(),
                  doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all(),
                  photos=[f for f in p.files if f.kind == "photo"],
                  docs=[f for f in p.files if f.kind != "photo"], now=datetime.now(),
                  items=_ph_items, counts=_ph_counts, CATS=_pp.CATS,
                  pgroups=_ptg.groups(db),
                  pvisits=pvis, phist=phist, ptag=ptag, ptag_color=ptag_color, page_age=page_age,
                  patient_kpi=dict(done=len({r["at"].date() for r in pvis if r["status"] == "done"}),   # записи CRM мають пріоритет над історією CB того ж дня
                                   planned=len({r["at"] for r in pvis if r["at"] >= datetime.now() and r["status"] not in ("done", "cancelled", "no_show")}),
                                   first=min((r["at"] for r in pvis if r["status"] == "done"), default=None),
                                   next=min((r["at"] for r in pvis if r["at"] >= datetime.now() and r["status"] not in ("done", "cancelled", "no_show")), default=None),
                                   service_revenue=service_revenue, product_revenue=product_revenue,
                                   revenue=service_revenue + product_revenue))


def _loyal_ctx(db: Session, p: Patient) -> dict:
    from .services import loyalty as ly
    from .models import BonusTxn
    return {"s": ly.settings(db), "balance": ly.balance(db, p.id), "KL": ly.KIND_LABELS,
            "txns": db.scalars(select(BonusTxn).where(BonusTxn.patient_id == p.id).order_by(BonusTxn.at.desc()).limit(30)).all()}


CB_LABELS = {"sex": "Стать", "gender": "Стать", "address": "Адреса", "city": "Місто", "source": "Джерело",
             "source_name": "Джерело", "groups": "Групи", "categories": "Категорії", "category_name": "Категорія",
             "tags": "Теги", "balance": "Баланс", "deposit": "Депозит", "bonus": "Бонуси", "discount": "Знижка, %",
             "card": "Картка лояльності", "card_number": "Картка лояльності", "comment": "Коментар", "note": "Нотатка",
             "notes": "Нотатки", "instagram": "Instagram", "telegram": "Telegram", "viber": "Viber",
             "status": "Статус", "vip": "VIP", "visits": "Візитів", "last_visit": "Останній візит", "ltv": "LTV",
             "total_paid": "Сплачено всього", "average_check": "Середній чек", "master": "Майстер", "master_name": "Майстер",
             "filial": "Філія", "filial_name": "Філія", "blacklist": "Чорний список", "sms": "SMS-розсилка",
             "notification": "Сповіщення", "fields": "Додаткові поля", "custom_fields": "Додаткові поля"}


@app.post("/patients/{pid:int}/merge")
def patient_merge(pid: int, target: int = Form(...), user: User = Depends(require("patients_all")),
                  db: Session = Depends(db_dep)):
    """Картка-дубль (напр. створена з PDF без CleverBox) → у картку CleverBox; дубль видаляється."""
    from .services import patient_match as _pm
    src, dst = db.get(Patient, pid), db.get(Patient, target)
    if not src or not dst:
        raise HTTPException(404)
    name = src.full_name
    moved = _pm.merge(db, src, dst)
    audit(db, "patient.merge", "patient", dst.id, user=user, merged_from=pid, merged_name=name, moved=moved)
    return RedirectResponse(f"/patients/{dst.id}?merged=1", 303)


@app.post("/patients/{pid:int}")
def patient_update(pid: int, full_name: str = Form(""), last_name: str = Form(""), first_name: str = Form(""), middle_name: str = Form(""),
                   birthday: str = Form(""), instagram: str = Form(None), viber: str = Form(None), telegram_contact: str = Form(None),
                   drink: str = Form(None), preferred_doctor: str = Form(None),
                   phone: str = Form(""), cleverbox_id: str = Form(""),
                   doctor_id: str = Form(""), notes: str = Form(""), email: str = Form(""),
                   gender: str = Form(""), address: str = Form(""), source: str = Form(""),
                   communication_channel: str = Form(""), client_groups: str | None = Form(None),
                   cb_balance: str = Form(""), cb_bonus: str = Form(""),
                   user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    _nm = " ".join(x.strip() for x in (last_name, first_name, middle_name) if x and x.strip()) or " ".join(full_name.split())
    if not _nm:
        raise HTTPException(400, "Вкажіть імʼя")
    p.full_name, p.phone, p.notes = _nm[:200], phone.strip() or None, notes.strip() or None
    if birthday.strip():
        try:
            p.birthday = date.fromisoformat(birthday.strip())
        except ValueError:
            pass
    else:
        p.birthday = None
    for _k, _v in (("instagram", instagram), ("viber", viber), ("telegram_contact", telegram_contact), ("drink", drink),
                   ("preferred_doctor", preferred_doctor)):
        if _v is not None:
            setattr(p, _k, _v.strip()[:200] or None)
    p.cleverbox_id = int(cleverbox_id) if cleverbox_id.strip().isdigit() else None
    p.email, p.gender, p.address = email.strip() or None, gender.strip() or None, address.strip() or None
    p.source, p.communication_channel = source.strip() or None, communication_channel.strip() or None
    if client_groups is not None:          # групи тепер змінюються лише чіпами в шапці картки (з довідника)
        p.client_groups = [x.strip() for x in client_groups.split(",") if x.strip()]
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


@app.post("/patients/{pid:int}/cb-sync")
async def patient_cb_sync(pid: int, cb_id: str = Form(""), user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    """Синхронізувати одну картку з CleverBox (лише читання з CleverBox): привʼязка, поля, історія послуг, майбутні записи."""
    from .integrations import cleverbox as _cbx
    p = _patient_or_403(db, user, pid)
    try:
        r = await asyncio.to_thread(_cbx.sync_one, db, p, int(cb_id) if cb_id.strip().isdigit() else None)
    except _cbx.CleverBoxError as e:
        return RedirectResponse(_with_err(f"/patients/{pid}", str(e)), 303)
    audit(db, "patient.cb_sync", "patient", pid, user=user, **r)
    return RedirectResponse(_with_msg(f"/patients/{pid}", f"Синхронізовано з CleverBox #{r['cb_id']}" + (f" ({r['how']})" if r['how'] else "")
                                      + f" · послуг в історії: {r['visits']} · майбутніх записів: {r['future']}"), 303)


@app.post("/plans/{plan_id}/items")
async def plan_items_update(request: Request, plan_id: int, user: User = Depends(require("plan_edit")),
                            db: Session = Depends(db_dep)):
    form = await request.form()
    plan = db.get(BeautyPlan, plan_id)
    if not plan or not plan_visible(user, plan):
        raise HTTPException(403, "Немає доступу до цього плану")
    changes = {}
    from .models import PlanItem
    for it in list(plan.items):                         # редагування позицій: видалити / назва / ціна
        if form.get(f"del_{it.id}"):
            changes[f"{it.name[:40]}"] = ["позиція", "видалено"]
            plan.items.remove(it)
            continue
        nm = " ".join(str(form.get(f"name_{it.id}") or "").split())[:300]
        if nm and nm != it.name:
            changes[f"{it.name[:40]}.name"] = [it.name, nm]
            it.name = nm
        pr = _num(str(form.get(f"price_{it.id}") or ""))
        if pr is not None and pr >= 0 and abs(pr - (it.unit_price or 0)) > 0.004:
            changes[f"{it.name[:40]}.price"] = [it.unit_price, pr]
            it.unit_price = pr
    for k_, n_, p_, q_ in zip(form.getlist("new_kind"), form.getlist("new_name"), form.getlist("new_price"), form.getlist("new_qty")):
        n_ = " ".join(str(n_ or "").split())[:300]
        if not n_:
            continue
        q = int(_num(str(q_)) or 1)
        pr = _num(str(p_)) or 0.0
        kind = "product" if k_ == "product" else "procedure"
        prod = db.scalar(select(Product).where(Product.name == n_)) if kind == "product" else None
        plan.items.append(PlanItem(kind=kind, name=n_, qty=max(1, q), unit_price=pr, total=max(1, q) * pr,
                                   product_id=prod.id if prod else None, manual_done=0))
        changes[f"+{n_[:40]}"] = [None, f"{q} × {pr:g}"]
    if "complaints" in form:
        cpl = str(form.get("complaints") or "").strip()[:2000] or None
        if cpl != plan.complaints:
            changes["complaints"] = [plan.complaints, cpl]
            plan.complaints = cpl
    if str(form.get("session_at") or "").strip():
        try:
            sa = datetime.fromisoformat(str(form.get("session_at")))
            if not plan.session_at or sa.date() != plan.session_at.date():
                changes["session_at"] = [str(plan.session_at), str(sa)]
                plan.session_at = sa
        except ValueError:
            pass
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
def plan_delete(plan_id: int, del_password: str = Form(""), user: User = Depends(require("plan_delete")),
                db: Session = Depends(db_dep)):
    plan = db.get(BeautyPlan, plan_id)
    pid = plan.patient_id
    err = _delpw(db, user, del_password, "plan", "plan", plan_id)
    if err:
        return RedirectResponse(_with_err(f"/patients/{pid}#plan{plan_id}", err), 303)
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
async def upload_photos(pid: int, files: list[UploadFile] = File(...), caption: str = Form(""), stage: str = Form(""),
                        zone: str = Form(""), back: str = Form(""), category: str = Form(""), user: User = Depends(require("upload")),
                        db: Session = Depends(db_dep)):
    p = _patient_or_403(db, user, pid)
    for f in files:
        pf = ingest.ingest_photo(db, await f.read(), f.filename or "photo.jpg", caption or None, patient=p)
        if category in ("face", "body", "consent", "doc"):
            pf.category = category
            db.commit()
        if stage in ("before", "after", "progress") or zone.strip():
            pf.stage, pf.zone, pf.taken_at = (stage if stage in ("before", "after", "progress") else None), zone.strip()[:80] or None, date.today()
            db.commit()
    return RedirectResponse(back if back.startswith(("/calendar", f"/patients/{pid}/photos", f"/patients/{pid}#")) else f"/patients/{pid}#t-files", 303)


# ---------------- Звернення (заявки) ----------------
def _appeal_filters(request: Request) -> dict:
    qp = request.query_params
    f = {k: qp.get(k, "") for k in ("source", "status", "cause", "resp", "mark", "q", "due", "view")}
    f["d_from"], f["d_to"] = _day(qp.get("df", "")), _day(qp.get("dt_", ""))
    if not qp.get("df") and not qp.get("all"):
        f["d_from"] = date.today() - timedelta(days=60)
    return f


@app.get("/appeals", response_class=HTMLResponse)
def appeals_page(request: Request, user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    from .models import Appeal
    f = _appeal_filters(request)
    rows = ap.listing(db, f)
    d = ap.dirs(db)
    users = {u_.id: u_.name for u_ in db.scalars(select(User).where(User.active.is_(True)))}
    pats = {p_.id: p_ for p_ in db.scalars(select(Patient).where(Patient.id.in_([a.patient_id for a in rows if a.patient_id] or [0])))}
    due_n = len([a for a in db.scalars(select(Appeal).where(Appeal.deleted_at.is_(None), Appeal.closed_at.is_(None),
                                                            Appeal.next_contact <= datetime.now().replace(hour=23, minute=59)))])
    return render(request, "appeals.html", nav="appeals", rows=rows, d=d, f=f, st=ap.stats(db, rows), users=users, pats=pats,
                  ap=ap, due_n=due_n, now=datetime.now(), open_=int(request.query_params.get("open") or 0) or None)


@app.get("/appeals/{aid:int}", response_class=HTMLResponse)
def appeal_card(request: Request, aid: int, user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    from .models import Appeal, AppealNote
    a = db.get(Appeal, aid)
    if not a or a.deleted_at:
        raise HTTPException(404)
    notes = db.scalars(select(AppealNote).where(AppealNote.appeal_id == aid).order_by(AppealNote.at.desc())).all()
    users = {u_.id: u_.name for u_ in db.scalars(select(User))}
    other = db.scalars(select(Appeal).where(Appeal.id != aid, Appeal.deleted_at.is_(None), Appeal.phone.is_not(None))).all()
    pk = ap.phone_key(a.phone)
    history = [x for x in other if pk and ap.phone_key(x.phone) == pk][:20]
    return templates.TemplateResponse(request, "_appeal_card.html", {"request": request, "user": user, "a": a, "notes": notes, "users": users,
                                      "d": ap.dirs(db), "ap": ap, "p": db.get(Patient, a.patient_id) if a.patient_id else None,
                                      "history": history, "match": None if a.patient_id else ap.find_patient(db, a.phone),
                                      "ign_one": ap.is_ignored(db, a.phone)})


@app.post("/appeals")
async def appeal_save(request: Request, user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    from .models import Appeal
    f = await request.form()
    back = str(f.get("back") or "/appeals")
    back = back if back.startswith("/appeals") or back.startswith("/patients/") else "/appeals"
    nc = str(f.get("next_contact") or "")
    try:
        next_contact = datetime.fromisoformat(nc) if nc else None
    except ValueError:
        next_contact = None
    data = {k: str(f.get(k) or "") for k in ("name", "phone", "source", "cause", "interest", "comment", "status", "utm")}
    data["marks"] = [str(x) for x in f.getlist("marks")]
    data["responsible_id"] = int(f["responsible_id"]) if str(f.get("responsible_id") or "").isdigit() else None
    data["next_contact"], data["amount"] = next_contact, _num(str(f.get("amount") or ""))
    aid = str(f.get("aid") or "")
    if aid.isdigit():
        a = db.get(Appeal, int(aid))
        if not a:
            raise HTTPException(404)
        changes = []
        for k in ("name", "phone", "source", "cause", "interest", "comment", "utm"):
            v = data[k].strip() or None
            if (getattr(a, k) or None) != v:
                changes.append(k)
                setattr(a, k, v)
        if (a.marks or []) != data["marks"]:
            a.marks = data["marks"] or None
        a.responsible_id, a.next_contact, a.amount = data["responsible_id"], next_contact, data["amount"]
        a.updated_at = datetime.utcnow()
        if not a.patient_id:
            m = ap.find_patient(db, a.phone)
            a.patient_id = m.id if m else None
        db.commit()
        if data["status"] and data["status"] != a.status:
            ap.set_status(db, a, data["status"], user.id, str(f.get("deny_reason") or "") or None)
        elif str(f.get("deny_reason") or "") and ap.status_type(ap.dirs(db), a.status) == "fail":
            a.deny_reason = str(f.get("deny_reason"))[:120]
            db.commit()
        if changes:
            ap.note(db, a, "Змінено: " + ", ".join(changes), user.id, "system")
            db.commit()
        audit(db, "appeal.edit", "appeal", a.id, user=user, status=a.status)
    else:
        if not (data["name"].strip() or data["phone"].strip()):
            return RedirectResponse(_with_msg(back, "Вкажіть імʼя або телефон", True), 303)
        a = ap.create(db, data, user.id)
        if data["status"] and ap.status_type(ap.dirs(db), data["status"]) == "fail" and f.get("deny_reason"):
            a.deny_reason = str(f.get("deny_reason"))[:120]
            db.commit()
        audit(db, "appeal.create", "appeal", a.id, user=user, source=a.source)
    if a.next_contact and not a.closed_at:
        _appeal_task(db, a, user)
    return RedirectResponse(back if "open=" in back or back.startswith("/patients/") else _with_msg(back, f"Звернення №{a.id} збережено"), 303)


def _appeal_task(db: Session, a, user: User) -> None:
    """Наступний контакт → задача в «Задачах» (одна на звернення, оновлюється)."""
    t = db.scalar(select(Task).where(Task.source_key == f"appeal:{a.id}"))
    t = t or Task(source_key=f"appeal:{a.id}", kind="follow_up", created_by=user.id)
    t.title = f"Звернення №{a.id}: звʼязатися — {a.name or a.phone or ''}"[:300]
    t.description = "\n".join(x for x in [a.interest and f"Цікавить: {a.interest}", a.comment, f"/appeals?open={a.id}"] if x)
    t.due_date, t.patient_id, t.assignee_id = a.next_contact.date(), a.patient_id, a.responsible_id
    t.assignee_role = None if a.responsible_id else "admin"
    t.status = "open"
    db.add(t)
    db.commit()


@app.post("/appeals/{aid:int}/note")
def appeal_note(aid: int, text: str = Form(...), user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    from .models import Appeal
    a = db.get(Appeal, aid)
    if a and text.strip():
        ap.note(db, a, text.strip(), user.id)
        a.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/appeals?open={aid}", 303)


@app.post("/appeals/{aid:int}/status")
def appeal_status(aid: int, status: str = Form(...), deny_reason: str = Form(""), back: str = Form("/appeals"), ignore: str = Form(""),
                  user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    from .models import Appeal
    a = db.get(Appeal, aid)
    if a:
        ap.set_status(db, a, status, user.id, deny_reason or None)
        if ap.status_type(ap.dirs(db), status) == "other" and a.phone:
            ap.set_ignored(db, a.phone, bool(ignore), deny_reason or "", a.name or "", user.id)
        if a.closed_at:
            t = db.scalar(select(Task).where(Task.source_key == f"appeal:{aid}", Task.status == "open"))
            if t:
                t.status, t.done_at, t.result = "done", datetime.utcnow(), f"Звернення: {a.status}"
                db.commit()
        audit(db, "appeal.status", "appeal", aid, user=user, status=status, deny=deny_reason or None)
    return RedirectResponse(back if back.startswith("/appeals") else "/appeals", 303)


@app.post("/appeals/{aid:int}/patient")
def appeal_to_patient(aid: int, user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    from .models import Appeal
    a = db.get(Appeal, aid) or (_ for _ in ()).throw(HTTPException(404))
    created = not a.patient_id and not ap.find_patient(db, a.phone)
    p = ap.to_patient(db, a)
    ap.note(db, a, ("Створено картку пацієнта" if created else "Привʼязано до картки пацієнта") + f" №{p.id}", user.id, "system")
    db.commit()
    audit(db, "appeal.patient", "appeal", aid, user=user, patient=p.id, created=created)
    return RedirectResponse(f"/patients/{p.id}", 303)


@app.post("/appeals/{aid:int}/delete")
def appeal_delete(aid: int, del_password: str = Form(""), user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .models import Appeal
    err = _delpw(db, user, del_password, "appeal", "appeal", aid)
    if err:
        return RedirectResponse(_with_err("/appeals", err), 303)
    a = db.get(Appeal, aid)
    if a:
        a.deleted_at = datetime.utcnow()
        db.commit()
        audit(db, "appeal.delete", "appeal", aid, user=user)
    return RedirectResponse(_with_msg("/appeals", f"Звернення №{aid} видалено"), 303)


@app.get("/settings/appeals", response_class=HTMLResponse)
def settings_appeals(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    return render(request, "appeal_dirs.html", nav="settings", d=ap.dirs(db), ap=ap, open_=request.query_params.get("open", ""), ign=ap.ignored(db))


@app.post("/settings/appeals-ignore")
def settings_appeals_ignore(phone: str = Form(""), reason: str = Form(""), name: str = Form(""), action: str = Form("add"),
                            user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import appeals as ap
    ap.set_ignored(db, phone, action == "add", reason, name, user.id)
    audit(db, "appeal.ignore", user=user, phone=phone[-4:], op=action)
    return RedirectResponse("/settings/appeals?open=ignore#ignore", 303)


@app.post("/settings/appeals/{what}")
def settings_appeals_save(what: str, action: str = Form(...), idx: str = Form(""), name: str = Form(""), type_: str = Form("work", alias="type"),
                          color: str = Form(""), del_password: str = Form(""), user: User = Depends(require("users")),
                          db: Session = Depends(db_dep)):
    import re as _re
    from .services import appeals as ap
    if what not in ap.DIR_LABELS:
        raise HTTPException(404)
    d = ap.dirs(db)
    items = d[what]
    i = int(idx) if idx.isdigit() and int(idx) < len(items) else None
    nm = name.strip()[:80]
    is_st = what == "statuses"
    if action == "add" and nm:
        items.append({"name": nm, "type": type_ if type_ in ap.STATUS_TYPES else "work", "color": color if _re.fullmatch(r"#[0-9a-fA-F]{6}", color or "") else "#888888"} if is_st else nm)
    elif i is not None and action == "save" and nm:
        if is_st:
            items[i].update(name=nm, type=type_ if type_ in ap.STATUS_TYPES else items[i]["type"],
                            color=color if _re.fullmatch(r"#[0-9a-fA-F]{6}", color or "") else items[i].get("color"))
        else:
            items[i] = nm
    elif i is not None and action in ("up", "down"):
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
    elif i is not None and action == "delete":
        err = _delpw(db, user, del_password, f"appeal_{what}", None, None)
        if err:
            return RedirectResponse(_with_err(f"/settings/appeals?open={what}", err), 303)
        if is_st and len(items) <= 1:
            return RedirectResponse(_with_msg(f"/settings/appeals?open={what}", "Має лишитися хоча б один статус", True), 303)
        items.pop(i)
    ap.save_dirs(db, d)
    audit(db, "settings.appeals", user=user, what=what, op=action, name=nm or None)
    return RedirectResponse(f"/settings/appeals?open={what}#{what}", 303)


# ---------------- Запис у CRM ----------------
@app.get("/lookup/patients")
def lookup_patients(q: str = "", user: User = Depends(current_user), db: Session = Depends(db_dep)):
    import re as _re
    q = q.strip()
    digits = _re.sub(r"\D", "", q)
    is_phone = bool(digits) and not _re.search(r"[^\d\s+()\-]", q)          # шукаємо за номером з першої цифри
    if not is_phone and len(q) < 2:
        return JSONResponse([])
    ql = q.lower()
    from .services import search as _srch
    _qw = _srch.qwords(q)
    d9 = digits.lstrip("0")[-9:] if len(digits) >= 10 else digits                # 0981234567 / 380981234567 → однаково
    out = []
    for p_ in db.scalars(select(Patient).order_by(Patient.full_name)):
        ph = _re.sub(r"\D", "", p_.phone or "")
        if (is_phone and ph and (digits in ph or (d9 and d9 in ph))) or (not is_phone and _srch.match(_qw, p_.full_name)):
            if patient_visible(user, p_):
                out.append({"id": p_.id, "name": p_.full_name, "phone": p_.phone or "", "bonus": None})
        if len(out) >= 15:
            break
    return JSONResponse(out)


@app.get("/lookup/services")
def lookup_services(q: str = "", user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import CbService
    ql = q.strip().lower()
    rows = [s for s in db.scalars(select(CbService).order_by(CbService.name)) if s.active is not False and (not ql or ql in (s.name or "").lower()
            or ql in (s.category or "").lower())]
    return JSONResponse([{"id": s.id, "name": s.name, "price": s.price or 0, "minutes": s.time_min or 30, "category": s.category or ""}
                         for s in rows[:25]])


def _vstat_menu(db: Session, cur: str | None = None) -> dict:
    """Статуси для меню запису: увімкнені в «Статуси візитів» + поточний статус запису (навіть якщо його вимкнули)."""
    from .services import visit_statuses as _vs
    every = _vs.labels(db)
    on = _vs.labels(db, only_on=True)
    if cur and cur not in on:
        on[cur] = every.get(cur, cur)
    return on


def _vstat_colors(db: Session) -> dict:
    from collections import defaultdict
    from .services import visit_statuses as _vs
    d = defaultdict(lambda: "#8a8496")
    d.update(_vs.colors(db))
    return d


def _appt_ctx(db: Session) -> dict:
    from .services import clinic as cl, appointments as aps
    data = cl.get(db)
    loc = next((x for x in data["locations"] if x.get("active")), {"places": [], "resources": []})
    return {"doctors": spec_svc.calendar_doctors(db), "places": [p_["name"] for p_ in loc.get("places", []) if p_.get("active", True)],
            "resources": [r_["name"] for r_ in loc.get("resources", []) if r_.get("active", True)], "STATUSES": _vstat_menu(db),
            "COLORS": _vstat_colors(db), "SOURCES": aps.sources(db), "cb_on": bool(settings.cleverbox_token or db.get(SyncState, "cleverbox_token_enc"))}


@app.get("/appointments/new", response_class=HTMLResponse)
def appointment_new(request: Request, doc: int = 0, start: str = "", patient: int = 0, appeal: int = 0,
                    user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    try:
        st_ = datetime.fromisoformat(start) if start else None
    except ValueError:
        st_ = None
    if not st_:
        n_ = datetime.now() + timedelta(hours=1)
        st_ = n_.replace(minute=0 if n_.minute < 30 else 30, second=0, microsecond=0)
    pt = db.get(Patient, patient) if patient else None
    ap_ = None
    if appeal:
        from .models import Appeal
        ap_ = db.get(Appeal, appeal)
        pt = pt or (db.get(Patient, ap_.patient_id) if ap_ and ap_.patient_id else None)
    return templates.TemplateResponse(request, "_appt_form.html", {"request": request, "user": user, "a": None, "doc": doc, "st": st_,
                                      "pt": pt, "appeal": ap_, **_appt_ctx(db)})


@app.post("/patients/{pid:int}/avatar")
def patient_avatar(pid: int, file_id: int = Form(0), back: str = Form(""), user: User = Depends(require("upload")), db: Session = Depends(db_dep)):
    """Аватар пацієнта — одне з його фото (галочка в «Фото»); 0 — прибрати."""
    from .models import PatientFile
    p_ = _patient_or_403(db, user, pid)
    f_ = db.get(PatientFile, file_id) if file_id else None
    p_.avatar_file_id = f_.id if f_ and f_.patient_id == p_.id else None
    db.commit()
    audit(db, "patient.avatar", "patient", pid, user=user, file=p_.avatar_file_id)
    return RedirectResponse(back if back.startswith("/patients/") else f"/patients/{pid}/photos", 303)


@app.get("/appointments/services")
def appointment_services(doc: int = 0, pt: int = 0, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .services import appointments as aps
    return JSONResponse(aps.services_for(db, doc or None, pt or None))


@app.get("/appointments/calls")
def appointment_calls(user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Останні дзвінки для вибору пацієнта у вікні запису."""
    from .models import Call
    rows = db.scalars(select(Call).order_by(Call.at.desc()).limit(40)).all()
    pts = {p_.id: p_ for p_ in db.scalars(select(Patient).where(Patient.id.in_({c_.patient_id for c_ in rows if c_.patient_id})))} if rows else {}
    out = []
    for c_ in rows:
        p_ = pts.get(c_.patient_id)
        if p_ and not patient_visible(user, p_):
            p_ = None
        out.append({"at": c_.at.strftime("%d.%m %H:%M"), "phone": c_.phone or "", "dir": c_.direction, "status": c_.status or "",
                    "patient_id": p_.id if p_ else None, "name": p_.full_name if p_ else None})
    return JSONResponse(out)


@app.get("/appointments/{aid:int}", response_class=HTMLResponse)
def appointment_card(request: Request, aid: int, edit: int = 0, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import Appointment
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    _patient_or_403(db, user, a.patient_id)
    return templates.TemplateResponse(request, "_appt_form.html", {"request": request, "user": user, "a": a, "doc": a.doctor_id,
                                      "st": a.start, "pt": a.patient, "appeal": None, "view_only": not edit, **_appt_ctx(db),
                                      "doctor": db.get(Doctor, a.doctor_id) if a.doctor_id else None})


@app.post("/appointments")
async def appointment_save(request: Request, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .services import appointments as aps
    from .services.booking import BookingError
    from .models import Appointment, Appeal
    f = await request.json()
    pid = f.get("patient_id")
    if not pid and (f.get("new_name") or "").strip():
        from .services.appeals import find_patient
        pt = find_patient(db, f.get("new_phone")) if f.get("new_phone") else None
        if not pt:
            pt = Patient(full_name=f["new_name"].strip()[:200], phone=(f.get("new_phone") or "").strip()[:64] or None, source="Запис у CRM")
            db.add(pt)
            db.commit()
        pid = pt.id
    if not pid:
        return JSONResponse({"error": "Оберіть пацієнта або введіть імʼя нового"}, status_code=400)
    p_ = _patient_or_403(db, user, int(pid))
    try:
        start = datetime.fromisoformat(f.get("start") or "")
    except ValueError:
        return JSONResponse({"error": "Вкажіть дату й час"}, status_code=400)
    svc = []
    for s in f.get("services") or []:
        try:
            svc.append({"id": int(s["id"]), "name": str(s.get("name") or "")[:300], "category": s.get("category"),
                        "price": round(float(s.get("price") or 0), 2), "minutes": max(5, int(s.get("minutes") or 30))})
        except (KeyError, TypeError, ValueError):
            continue
    data = {"patient_id": p_.id, "doctor_id": int(f["doctor_id"]) if str(f.get("doctor_id") or "").isdigit() else None,
            "services": svc, "start": start, "minutes": int(f.get("minutes") or 0) or None, "place": f.get("place"),
            "resource": f.get("resource"), "comment": (f.get("comment") or "").strip()[:2000],
            "appeal_id": int(f["appeal_id"]) if str(f.get("appeal_id") or "").isdigit() else None,
            "booking_source": str(f.get("booking_source") or "").strip()[:60] or None}
    appt = db.get(Appointment, int(f["id"])) if str(f.get("id") or "").isdigit() else None
    if not appt and "booking_source" in f and not data["booking_source"]:
        return JSONResponse({"error": "Вкажіть джерело запису"}, status_code=400)
    mins = data["minutes"] or sum(s["minutes"] for s in svc) or 30
    warn = aps.conflicts(db, data["doctor_id"], start, start + timedelta(minutes=mins), data["place"], data["resource"],
                         appt.id if appt else None, p_.id)
    if warn and not f.get("force"):
        return JSONResponse({"warnings": warn})
    _old = (appt.start, appt.doctor_id) if appt else None           # для статистики дня: «Візитів перенесено»
    a = aps.save(db, data, user.id, appt)
    try:
        from .services import techcards as _tc
        _tc.seed(db, a)                                   # витратні з техкарт послуг візиту
    except Exception:  # noqa: BLE001
        log.exception("visit materials seed")
    msg = None
    if f.get("to_cb") and not a.cb_record_id:
        try:
            rec = await asyncio.to_thread(aps.mirror_to_cleverbox, db, a)
            msg = f"Записано й у CleverBox (№{rec})"
        except (BookingError, Exception) as e:  # noqa: BLE001
            msg = f"Запис у CRM є, але в CleverBox не створено: {str(e)[:200]}"
    if a.appeal_id:
        from .services import appeals as apl
        ap_ = db.get(Appeal, a.appeal_id)
        if ap_:
            ok = next((s_["name"] for s_ in apl.dirs(db)["statuses"] if s_["type"] == "success"), None)
            if ok:
                apl.set_status(db, ap_, ok, user.id)
            ap_.patient_id = ap_.patient_id or p_.id
            apl.note(db, ap_, f"Записано на {a.start:%d.%m.%Y %H:%M}", user.id, "system")
            db.commit()
    audit(db, "appointment.save" if appt else "appointment.create", "patient", p_.id, user=user, appt=a.id,
          start=a.start.isoformat(timespec="minutes"), doctor=a.doctor_id, forced=bool(warn), cb=a.cb_record_id,
          moved=bool(_old and _old[0] != a.start) or None,
          frm=_old[0].isoformat(timespec="minutes") if _old and _old[0] != a.start else None,
          frm_doc=_old[1] if _old and _old[0] != a.start and _old[1] != a.doctor_id else None)
    return JSONResponse({"ok": True, "id": a.id, "msg": msg, "day": a.start.date().isoformat()})


@app.post("/appointments/{aid:int}/time")
async def appointment_time(aid: int, request: Request, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Олівець у вікні візиту: змінити дату, час початку й тривалість послуг. {day, time, minutes:[хв по послугах], force}"""
    from .services import appointments as aps
    from .models import Appointment
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    if a.historic:
        return JSONResponse({"error": "Це візит з історії CleverBox — час не змінюється"})
    f = await request.json()
    try:
        start = datetime.fromisoformat(f"{f.get('day')}T{f.get('time')}")
    except (TypeError, ValueError):
        return JSONResponse({"error": "Вкажіть дату й час"})
    mins = [max(5, min(600, int(x))) for x in (f.get("minutes") or []) if str(x).strip().isdigit()]
    svcs = [dict(x) for x in a.services or []]
    for i, m in enumerate(mins[:len(svcs)]):
        svcs[i]["minutes"] = m
    total = sum(int(x.get("minutes") or 30) for x in svcs) or (mins[0] if mins else a.minutes)
    end = start + timedelta(minutes=total)
    warn = aps.conflicts(db, a.doctor_id, start, end, a.place, a.resource, a.id, a.patient_id)
    if warn and not f.get("force"):
        return JSONResponse({"warnings": warn})
    old = a.start
    a.services, a.start, a.end, a.updated_at = svcs, start, end, datetime.utcnow()
    db.commit()
    audit(db, "appointment.save", "patient", a.patient_id, user=user, appt=a.id, start=a.start.isoformat(timespec="minutes"),
          doctor=a.doctor_id, forced=bool(warn), moved=(old != a.start) or None,
          frm=old.isoformat(timespec="minutes") if old != a.start else None, via="time")
    return JSONResponse({"ok": True, "day": a.start.date().isoformat()})


@app.post("/appointments/{aid:int}/services/{idx:int}/delete")
def appointment_service_delete(aid: int, idx: int, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .services import appointments as aps
    from .models import Appointment
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    _patient_or_403(db, user, a.patient_id)
    if a.historic or a.sale_id:
        return JSONResponse({"error": "Оплачений або історичний візит змінити не можна"}, status_code=400)
    gone = aps.remove_service(db, a, idx)
    if not gone:
        raise HTTPException(404)
    audit(db, "appointment.save", "patient", a.patient_id, user=user, appt=a.id, via="service_delete", service=gone.get("name"))
    return JSONResponse({"ok": True})


@app.post("/appointments/{aid:int}/doctor")
async def appointment_doctor(aid: int, request: Request, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Заміна лікаря у візиті (з картки візиту): попередження про зайнятість, «force» — замінити все одно."""
    from .services import appointments as aps
    from .models import Appointment
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    _patient_or_403(db, user, a.patient_id)
    if a.historic or a.sale_id:
        return JSONResponse({"error": "Оплачений або історичний візит змінити не можна"}, status_code=400)
    f = await request.json()
    did = int(f.get("doctor_id") or 0) or None
    if did and not db.get(Doctor, did):
        raise HTTPException(404)
    warn = aps.conflicts(db, did, a.start, a.end, a.place, a.resource, a.id, a.patient_id) if did else []
    if warn and not f.get("force"):
        return JSONResponse({"warnings": warn})
    old = a.doctor_id
    a.doctor_id, a.updated_at = did, datetime.utcnow()
    db.commit()
    audit(db, "appointment.save", "patient", a.patient_id, user=user, appt=a.id, doctor=did, frm_doc=old, via="doctor")
    return JSONResponse({"ok": True})


@app.post("/appointments/{aid:int}/status")
def appointment_status(aid: int, status: str = Form(...), reason: str = Form(""), back: str = Form(""), ajax: str = Form(""),
                       user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .services import appointments as aps
    from .models import Appointment
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    if status == "cancelled" and len(reason.strip()) < 3:
        return RedirectResponse(_with_msg(back or f"/calendar?start={a.start.date()}", "Вкажіть причину скасування", True), 303)
    from .services import shifts as _shq
    _err = _shq.close_block(db) if status == "done" and a.status != "done" else None
    if not _err:
        try:
            aps.set_status(db, a, status, reason.strip()[:200] or None)
        except ValueError as e:
            _err = str(e)
    if _err:
        if ajax:
            return JSONResponse({"error": _err}, status_code=400)
        return RedirectResponse(_with_msg(back or f"/calendar?start={a.start.date()}", _err, True), 303)
    audit(db, "appointment.status", "patient", a.patient_id, user=user, appt=aid, status=status, reason=reason or None)
    if ajax:
        return JSONResponse({"ok": True})
    b = back if back.startswith("/calendar") or back.startswith("/patients/") else f"/calendar?start={a.start.date()}"
    return RedirectResponse(b, 303)


# ---------------- Каса: продаж у CRM ----------------
@app.get("/pos/visits")
def pos_visits(d: str = "", user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import pos
    day_ = _day(d) or date.today()
    rows = [r for r in pos.visits(db, day_) if not r["patient_id"] or patient_visible(user, db.get(Patient, r["patient_id"]))]
    return JSONResponse({"day": day_.isoformat(), "rows": rows})


@app.get("/pos/patient/{pid:int}")
def pos_patient(pid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """Картка пацієнта в касі: мітка, бонуси, баланс, персональна знижка."""
    from .services import loyalty as ly, calendar as _cal
    from sqlalchemy import func as _f
    p_ = _patient_or_403(db, user, pid)
    n = __import__("app.services.patient_tags", fromlist=["x"]).visit_days(db, [p_.id]).get(p_.id, 0)
    from .services import certificates as _cert
    certs = [c_ for c_ in _cert.for_patient(db, p_.id) if c_.state() in ("active", "expiring")]
    subs = [s_ for s_ in _cert.subs_for_patient(db, p_.id) if s_.state() in ("active", "expiring")]
    labels = []
    for g_ in (p_.client_groups or []):
        nm = g_.get("name") or g_.get("title") if isinstance(g_, dict) else str(g_)
        if nm and nm not in labels:
            labels.append(str(nm)[:40])
    if db.scalar(select(User.id).where(User.active.is_(True), User.name == p_.full_name)) or \
            db.scalar(select(Doctor.id).where(Doctor.active.is_(True), Doctor.name == p_.full_name)):
        labels.insert(0, "Співробітник") if "Співробітник" not in labels else None
    return JSONResponse({"id": p_.id, "name": p_.full_name, "phone": p_.phone or "", "tag": _cal.tag_for(n, __import__("app.services.patient_tags", fromlist=["x"]).tags(db)), "visits": n,
                         "bonus": round(ly.balance(db, p_.id), 2), "balance": p_.cb_balance, "discount": p_.discount_pct,
                         "pdisc": (lambda t_: {"products": t_[0], "services": t_[1]})(__import__("app.services.personal_discounts", fromlist=["pcts"]).pcts(p_)),
                         "labels": labels[:6],
                         "certs": {"n": len(certs), "sum": round(sum(c_.balance or 0 for c_ in certs), 2),
                                   "items": [{"id": c_.id, "code": c_.code or str(c_.id), "balance": c_.balance or 0} for c_ in certs]},
                         "subs": {"n": len(subs), "names": [(s_.type_name or s_.code or "")[:40] for s_ in subs][:3],
                                  "items": [{"id": s_.id, "name": (s_.type_name or s_.code or f"№{s_.id}")[:80], "balance": s_.balance,
                                             "visits": s_.visits_total is not None, "left": s_.visits_left, "services": (s_.services or "")[:300]}
                                            for s_ in subs]}})


@app.post("/pos/patient")
async def pos_patient_new(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services.appeals import find_patient
    f = await request.json()
    name, phone = str(f.get("name") or "").strip()[:200], str(f.get("phone") or "").strip()[:64]
    if len(name) < 2:
        return JSONResponse({"error": "Вкажіть імʼя пацієнта"}, status_code=400)
    p_ = find_patient(db, phone) if phone else None
    if not p_:
        p_ = Patient(full_name=name, phone=phone or None, source="Каса CRM")
        db.add(p_)
        db.commit()
        audit(db, "patient.create", "patient", p_.id, user=user, via="pos")
    return JSONResponse({"id": p_.id, "name": p_.full_name, "phone": p_.phone or "", "existed": p_.full_name != name})


@app.get("/pos/services")
def pos_services(group: str = "category", key: str | None = None, q: str = "", user: User = Depends(require("sales")),
                 db: Session = Depends(db_dep)):
    from .services import pos
    return JSONResponse(pos.services(db, group, key, q))


@app.get("/pos/products")
def pos_products(group: str = "warehouse", key: str | None = None, q: str = "", stock: int = 0, user: User = Depends(require("sales")),
                 db: Session = Depends(db_dep)):
    from .services import pos
    return JSONResponse(pos.products(db, group, key, q, bool(stock)))


@app.get("/pos/lots/{pid:int}")
def pos_lots(pid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import stock as st
    from .models import Product as _P
    p_ = db.get(_P, pid) or (_ for _ in ()).throw(HTTPException(404))
    wh = p_.warehouse or "vitrina"
    lots = []
    if st.ledger_active(db):
        lots = sorted([{"expiry": e.isoformat(), "qty": round(qq, 3)} for (pid_, w_, e), qq in st.batch_balances(db, p_.id, wh).items()
                       if e and qq > 1e-6], key=lambda x: x["expiry"])
    return JSONResponse(lots)


@app.get("/lookup/sale-products")
def lookup_sale_products(q: str = "", user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import stock as st
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse([])
    words, ql = set(norm(q).split()), q.lower()
    led = st.ledger_active(db)
    out = []
    for r in catalog.product_rows(db, []):
        p_ = r.product
        if p_.active is False:
            continue
        _m = catalog.product_match(p_, q)
        if _m is None:
            continue
        exact = _m <= 1
        wh = p_.warehouse or "vitrina"
        lots = []
        if led:
            lots = [{"expiry": e.isoformat(), "qty": round(qq, 3)} for (pid_, w_, e), qq in st.batch_balances(db, p_.id, wh).items() if e and qq > 1e-6]
            lots.sort(key=lambda x: x["expiry"])
        out.append((0 if exact else (0 if wh == "vitrina" else 1), {"id": p_.id, "name": p_.name, "brand": p_.brand or "", "sku": p_.sku or "", "photo": p_.photo_url or "", "category": p_.brand or p_.category or "",
                    "price": p_.price or p_.price_manual or p_.shop_price or 0, "stock": r.stock, "warehouse": wh,
                    "wh": catalog.WAREHOUSES.get(wh), "lots": lots}))
    out.sort(key=lambda x: (x[0], x[1]["name"].lower()))
    return JSONResponse([x[1] for x in out[:25]])


@app.get("/sales", response_class=HTMLResponse)
def sales_list(request: Request, df: str = "", dt_: str = "", q: str = "", d: str = "", user: User = Depends(require("sales")),
               db: Session = Depends(db_dep)):
    from .models import Sale
    if not (df or dt_ or q):                                     # каса за добу (як «Продажі за добу» у CleverBox)
        from .services import daybook, clinic as _cl
        day_ = _day(d) or date.today()
        return render(request, "fin_cash_day.html", nav="finance", day=day_, v=daybook.day(db, day_), timedelta=timedelta,
                      today=date.today(), locs=_cl.get(db).get("locations") or [])
    d_from, d_to = _period(df, dt_)
    a_, b_ = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    rows = db.scalars(select(Sale).where(Sale.at >= a_, Sale.at <= b_).order_by(Sale.at.desc()).limit(2000)).all()
    if q.strip():
        ql = q.strip().lower()
        rows = [s_ for s_ in rows if ql == str(s_.id) or (s_.patient and __import__("app.services.search", fromlist=["x"]).match(ql, s_.patient.full_name)) or any(ql in i.name.lower() for i in s_.items)]
    paid = [s_ for s_ in rows if s_.status == "paid"]
    by_m: dict = {}
    for s_ in paid:
        for p_ in s_.payments:
            by_m[p_.method] = round(by_m.get(p_.method, 0) + p_.amount, 2)
    users = {u_.id: u_.name for u_ in db.scalars(select(User))}
    return render(request, "sales.html", nav="finance", rows=rows, d_from=d_from, d_to=d_to, q=q, users=users, by_m=by_m,
                  tot={"n": len(paid), "total": round(sum(s_.total for s_ in paid), 2), "discount": round(sum(s_.discount for s_ in paid), 2),
                       "refunded": round(sum(s_.total for s_ in rows if s_.status == "refunded"), 2)})


@app.get("/sales/new", response_class=HTMLResponse)
def sale_new(request: Request, appt: str = "", patient: int = 0, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .models import Appointment, DiscountRule, GiftCertificate
    from .services import sales as sl
    ids = list(dict.fromkeys(int(x) for x in str(appt).split(",") if x.strip().isdigit()))[:10]
    appts = [x for x in (db.get(Appointment, i) for i in ids) if x]
    if appts:                                                  # кілька візитів одного пацієнта — один чек
        appts = [x for x in appts if x.patient_id == appts[0].patient_id]
    a_ = appts[0] if appts else None
    pt = a_.patient if a_ else (db.get(Patient, patient) if patient else None)
    items = []
    for x_ in appts:
        items += [{"kind": "service", "ref_id": s_.get("id"), "name": s_.get("name"), "category": s_.get("category") or "", "qty": 1,
                   "price": float(s_.get("price") or 0), "doctor_id": x_.doctor_id} for s_ in x_.services or []]
    others = []
    if a_:                                                     # інші нерозраховані візити цього пацієнта того ж дня
        d0 = datetime.combine(a_.start.date(), datetime.min.time())
        others = [x for x in db.scalars(select(Appointment).where(Appointment.patient_id == a_.patient_id, Appointment.start >= d0,
                                                                   Appointment.start < d0 + timedelta(days=1),
                                                                   Appointment.status.notin_(("cancelled", "no_show")),
                                                                   Appointment.sale_id.is_(None)).order_by(Appointment.start))
                  if x.id not in ids]
    dnames = {d_.id: d_.name for d_ in db.scalars(select(Doctor))}
    certs = db.scalars(select(GiftCertificate).where(GiftCertificate.balance > 0)).all()
    from .services import fiscal as _fis
    _fc = _fis.cfg(db)
    from .services import clinic as _cl
    _locs = [l_ for l_ in (_cl.get(db).get("locations") or []) if l_.get("active", True)]
    from .services import price_cats as _pcat
    return render(request, "sale_new.html", nav="finance", appt=a_, appts=appts, others=others, dnames=dnames, pt=pt, items=items,
                  shift_msg=__import__("app.services.shifts", fromlist=["x"]).sale_block(db, user),
                  methods=sl.methods(db), today=date.today().isoformat(),
                  pcat=_pcat.doctor_prices(db),
                  loc_name=_locs[0]["name"] if _locs else "MOLODIST",
                  mkinds={m_: sl.method_kind(m_) for m_ in sl.methods(db)}, rros=[r_ for r_ in _fis.rros(db) if r_["on"]],
                  link_for=_pos_link_map(sl.methods(db), sl), rro_m=_fis.rro_methods(db),
                  pl_term={t_["method"]: t_["name"] for t_ in _fis.terminals(_fc)["items"] if t_.get("on") and t_.get("method")},
                  fauto=bool(_fc.get("auto")), fdef=_fc.get("default_rro"),
                  doctors=[d_ for d_ in db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)) if d_.sale_initiator is not False],
                  rules=__import__("app.services.loyalty", fromlist=["listable"]).listable(db),
                  pd_manual=(lambda c_: None if c_["auto"] else c_["name"])(__import__("app.services.personal_discounts", fromlist=["cfg"]).cfg(db)),
                  certs=[{"id": c_.id, "code": c_.code or str(c_.id), "balance": c_.balance, "patient_id": c_.patient_id,
                          "name": (c_.client_name or "") + (" · одноразовий" if c_.single_use else "")} for c_ in certs if c_.state() in ("active", "expiring")][:500],
                  default_doc=user.doctor_id, gift_types=__import__("app.services.gift_types", fromlist=["pos_items"]).pos_items(db),
                  packages=__import__("app.services.certificates", fromlist=["package_items"]).package_items(db))


def _pos_link_map(methods: list[str], sl) -> dict:
    """Термінал ФОП X → «Посилання · monobank ФОП X» (за прізвищем ФОП); непривʼязані посилання — під ключем ''."""
    import re as _re
    links = [m_ for m_ in methods if sl.method_kind(m_) == "link"]
    out, used = {}, set()
    for m_ in methods:
        if sl.method_kind(m_) != "card":
            continue
        mm = _re.search(r"ФОП\s+(\S+)", m_)
        if mm:
            hit = [l_ for l_ in links if mm.group(1).lower() in l_.lower()]
            if hit:
                out[m_] = hit[0]
                used.add(hit[0])
    out[""] = [l_ for l_ in links if l_ not in used]
    return out


def _sale_payload(f: dict) -> dict:
    items = []
    for it in f.get("items") or []:
        try:
            items.append({"kind": it.get("kind") if it.get("kind") in ("service", "product", "certificate", "subscription", "other") else "other",
                          "ref_id": int(it["ref_id"]) if str(it.get("ref_id") or "").isdigit() else None,
                          "name": str(it.get("name") or "")[:300], "category": str(it.get("category") or ""),
                          "qty": max(0.001, float(it.get("qty") or 1)), "price": max(0.0, float(it.get("price") or 0)),
                          "doctor_id": int(it["doctor_id"]) if str(it.get("doctor_id") or "").isdigit() else None,
                          "warehouse": it.get("warehouse") or None, "expiry": _day(str(it.get("expiry") or "")),
                          "rule_id": int(it["rule_id"]) if str(it.get("rule_id") or "").isdigit() else None})
        except (TypeError, ValueError):
            continue
    return {"patient_id": int(f["patient_id"]) if str(f.get("patient_id") or "").isdigit() else None,
            "appointment_id": int(f["appointment_id"]) if str(f.get("appointment_id") or "").isdigit() else None,
            "appointment_ids": [int(x) for x in (f.get("appointment_ids") or []) if str(x).isdigit()][:10],
            "items": items, "rule_ids": [int(x) for x in f.get("rule_ids") or [] if str(x).isdigit()],
            "cert_for": str(f.get("cert_for") or "")[:100],
            "manual_discount": float(f.get("manual_discount") or 0), "bonus_pay": float(f.get("bonus_pay") or 0),
            "fiscal_rro": (int(f["fiscal_rro"]) if str(f.get("fiscal_rro")).isdigit() else None) if "fiscal_rro" in f else None,
            "payments": f.get("payments") or [], "comment": str(f.get("comment") or "")}


@app.post("/sales/quote")
async def sale_quote(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import sales as sl
    d_ = _sale_payload(await request.json())
    pt = db.get(Patient, d_["patient_id"]) if d_["patient_id"] else None
    return JSONResponse(sl.quote(db, d_["items"], pt, d_["rule_ids"], d_["manual_discount"], d_["bonus_pay"]))


async def _sale_links(request: Request, db: Session, s_, user: User) -> list[str]:
    """Посилання на оплату для чека (оплати «Посилання на оплату · …» без створеного посилання) + надсилання пацієнту."""
    from .services import paylinks as _pls, sales as sl
    from .models import PayLink
    from .integrations.messaging import send_to_patient
    base = str(request.base_url).rstrip("/").replace("http://", "https://")
    have = {(p_.account_id, round(p_.amount, 2)) for p_ in db.scalars(select(PayLink).where(PayLink.sale_id == s_.id,
                                                                                        PayLink.status.in_(("created", "paid"))))}
    basket = [{"name": i.name, "qty": i.qty, "total": i.total, "code": f"{i.kind[:1]}{i.ref_id or i.id}"} for i in s_.items]
    msgs = []
    for p_ in s_.payments:
        if not p_.method.startswith(sl.PAYLINK):
            continue
        acc = _pls.account_by_name(_pls._raw(db), p_.method.split(" · ", 1)[-1])
        if acc and (acc["id"], round(p_.amount, 2)) in have:
            continue
        try:
            pl = await asyncio.to_thread(_pls.create, db, p_.amount, f"Чек Ч-{s_.id} · MOLODIST", base, s_.patient_id, s_.appointment_id,
                                         user.id, None, acc["id"] if acc else None, basket)
        except _pls.PayError as e:
            msgs.append(f"посилання не створено: {e}")
            continue
        pl.sale_id = s_.id
        db.commit()
        if s_.patient:
            res = await asyncio.to_thread(send_to_patient, s_.patient, _pls.message(db, pl))
            pl.info = {**(pl.info or {}), "sent": {"ok": res.ok, "channel": res.channel, "at": datetime.now().strftime("%d.%m.%Y %H:%M")}}
            db.commit()
            msgs.append(f"посилання на {pl.amount:.2f} ₴ " + (f"надіслано ({res.channel})" if res.ok else f"створено, не надіслано: {res.error}"))
    return msgs


@app.post("/sales/{sid:int}/paylink")
async def sale_paylink_retry(request: Request, sid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    if s_.status != "awaiting":
        return RedirectResponse(_with_msg(f"/sales/{sid}", "Чек не очікує оплати", True), 303)
    msgs = await _sale_links(request, db, s_, user)
    bad = any("не створено" in m_ for m_ in msgs)
    return RedirectResponse(_with_msg(f"/sales/{sid}", "; ".join(msgs) or "Посилання вже створено", bad), 303)


@app.get("/sales/{sid:int}/pay-status")
async def sale_pay_status(sid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """Для сторінки чека: перепитати monobank і повернути статуси посилань і чека."""
    from .models import Sale, PayLink
    from .services import paylinks as _pls
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    for pl in db.scalars(select(PayLink).where(PayLink.sale_id == sid, PayLink.status == "created")).all():
        try:
            await asyncio.to_thread(_pls.check_status, db, pl)
        except Exception:  # noqa: BLE001
            pass
    for pl in db.scalars(select(PayLink).where(PayLink.sale_id == sid, PayLink.status == "paid")).all():
        if not (s_.fiscal or {}).get("fiscal_code"):
            try:
                await asyncio.to_thread(_pls.mono_fiscal, db, pl)
            except Exception:  # noqa: BLE001
                pass
    db.refresh(s_)
    return JSONResponse({"sale": s_.status, "fiscal": bool((s_.fiscal or {}).get("fiscal_code")),
                         "links": [{"id": p_.id, "status": p_.status} for p_ in db.scalars(select(PayLink).where(PayLink.sale_id == sid))]})


@app.get("/paylinks/{lid:int}/fiscal.pdf")
async def paylink_fiscal_pdf(lid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """PDF фіскального чека monobank (base64 з /invoice/fiscal-checks)."""
    import base64 as _b64
    from fastapi.responses import Response
    from .models import PayLink
    from .services import paylinks as _pls
    pl = db.get(PayLink, lid) or (_ for _ in ()).throw(HTTPException(404))
    checks = await asyncio.to_thread(_pls.mono_fiscal, db, pl, None, True)
    f_ = next((c_.get("file") for c_ in checks if c_.get("file")), None)
    if not f_:
        raise HTTPException(404, "Фіскальний чек ще не готовий")
    return Response(_b64.b64decode(f_), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="check-{lid}.pdf"'})


@app.post("/sales")
async def sale_create(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import sales as sl
    d_ = _sale_payload(await request.json())
    from .services import shifts as _shq
    if (_blk := _shq.sale_block(db, user)):
        return JSONResponse({"error": _blk}, status_code=400)
    if d_["patient_id"]:
        _patient_or_403(db, user, d_["patient_id"])
    if any(not it_.get("doctor_id") for it_ in d_["items"]):
        return JSONResponse({"error": "Оберіть спеціаліста / продавця (ініціатора продажу) для кожної позиції"}, status_code=400)
    d_["manual_discount"] = 0                                   # знижка — лише зі списку в «Налаштування → Знижки»
    from .services import stock as _st0
    if _st0.ledger_active(db):                                  # кілька термінів дії товару — термін обовʼязковий
        from .models import Product as _P0
        for it_ in d_["items"]:
            if it_.get("kind") == "product" and it_.get("ref_id") and not it_.get("expiry"):
                p0_ = db.get(_P0, int(it_["ref_id"]))
                if p0_ and len([1 for (_a, _b, e_), q_ in _st0.batch_balances(db, p0_.id, it_.get("warehouse") or p0_.warehouse or "vitrina").items()
                                if e_ and q_ > 1e-6]) > 1:
                    return JSONResponse({"error": f"«{p0_.name}»: оберіть термін дії для списання"}, status_code=400)
    from .services import fiscal as _fis0, sales as _sl0, autonomy as _aut0
    _test = _aut0.test_mode(db)
    if _test:                                                   # тестовий режим: без Checkbox і без термінала
        d_["fiscal_rro"] = None
    _allowed = _fis0.rro_methods(db).get(d_.get("fiscal_rro") or 0) if d_.get("fiscal_rro") else None
    if _allowed:
        bad_ = [p_.get("method") for p_ in d_["payments"] if _sl0.method_kind(str(p_.get("method") or "")) in ("cash", "card")
                and p_.get("method") not in _allowed]
        if bad_:
            return JSONResponse({"error": "Ця каса РРО не приймає: " + ", ".join(bad_)}, status_code=400)
    _fc0 = _fis0.cfg(db)
    term_wait = {}                                              # оплата на терміналі PayLink: чек закриється лише після оплати
    for p_ in d_["payments"]:
        m_ = str(p_.get("method") or "")
        if _test or float(p_.get("amount") or 0) <= 0 or _sl0.method_kind(m_) != "card" or m_.startswith(_sl0.PAYLINK):
            continue
        t_ = _fis0.terminal_for(_fc0, m_)
        if not t_:
            return JSONResponse({"error": f"«{m_}»: термінал не підключено до CRM — без відповіді термінала чек не проводиться. "
                                          "Додайте термінал (ID з PayLink) у «Налаштування → Checkbox → Термінали» і привʼяжіть до цього способу оплати."},
                                status_code=400)
        if True:
                w_ = term_wait.setdefault(p_["method"], {"device": t_["device_id"], "name": t_["name"], "amount": 0.0})
                w_["amount"] = round(w_["amount"] + float(p_["amount"]), 2)
    d_["await_terminal"] = bool(term_wait)
    try:
        s_ = sl.create(db, d_, user.id)
    except sl.SaleError as e:
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=400)
    if term_wait:
        s_.fiscal = {**(s_.fiscal or {}), "term_wait": term_wait, "rro_pending": d_.get("fiscal_rro")}
        db.commit()
    if _test:
        s_.fiscal = {**(s_.fiscal or {}), "test_sale": True}
        db.commit()
    audit(db, "sale.create", "sale", s_.id, user=user, total=s_.total, patient=s_.patient_id, appt=s_.appointment_id)
    link_msgs = await _sale_links(request, db, s_, user) if s_.status == "awaiting" and not term_wait else []
    if term_wait and any(str(p_.get("method") or "").startswith(sl.PAYLINK) for p_ in d_["payments"]):
        link_msgs = await _sale_links(request, db, s_, user)
    from .services import fiscal as _fis
    rro_ = d_.get("fiscal_rro")
    if _test or (s_.fiscal or {}).get("skipped"):                   # повна оплата сертифікатом / бонусами — без РРО
        pass
    elif rro_ and s_.status == "paid":                              # вибрана фіскальна каса в POS
        import threading
        threading.Thread(target=_fis.auto, args=(s_.id, rro_), daemon=True).start()
    elif _fis.cfg(db).get("auto") and rro_ != 0 and s_.status == "paid":
        import threading
        threading.Thread(target=_fis.auto, args=(s_.id,), daemon=True).start()
    return JSONResponse({"ok": True, "id": s_.id, "msg": "; ".join(link_msgs),
                         "term": list(term_wait),
                         "title": "Тестовий чек проведено (без фіскалізації й термінала)" if _test else "Продаж проведено — оплата сертифікатом, без РРО" if (s_.fiscal or {}).get("skipped") else "Чек створено — оплата на терміналі" if term_wait else ("Чек створено — очікує оплати" if s_.status == "awaiting" else "Продаж проведено")})


@app.get("/sales/{sid:int}", response_class=HTMLResponse)
def sale_view(request: Request, sid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    docs = {d_.id: d_.name for d_ in db.scalars(select(Doctor))}
    from .services import fiscal as _fis
    return render(request, "sale_view.html", nav="finance", s=s_, docs=docs, author=db.get(User, s_.created_by) if s_.created_by else None,
                  print_=bool(request.query_params.get("print")), rros=[r for r in _fis.rros(db) if r["on"]], fcfg=_fis.cfg(db),
                  links=db.scalars(select(__import__("app.models", fromlist=["PayLink"]).PayLink).where(
                      __import__("app.models", fromlist=["PayLink"]).PayLink.sale_id == s_.id)).all())


def _term_has_data(r) -> bool:
    return isinstance(r, dict) and any(r.get(k) for k in ("rrn", "auth_code", "card_mask", "receipt_no"))


def _term_done(db: Session, s_, method: str, result: dict, user) -> dict:
    """Оплата на терміналі пройшла: дані картки в чек → чек «оплачено» → фіскалізація в Checkbox (каса з POS)."""
    from .services import fiscal as _fis
    from .models import PayLink
    f = dict(s_.fiscal or {})
    wait = dict(f.get("term_wait") or {})
    wait.pop(method, None)
    f["term_wait"] = wait
    f["term"] = {**(f.get("term") or {}), method: {k: result.get(k) for k in _fis.TERM_FIELDS if result.get(k) not in (None, "")}}
    s_.fiscal = f
    db.commit()
    open_links = db.query(PayLink).filter(PayLink.sale_id == s_.id, PayLink.status == "created").count()
    if s_.status == "awaiting" and not wait and not open_links:
        s_.status = "paid"
        db.commit()
        audit(db, "sale.terminal_paid", "sale", s_.id, user=user, method=method, rrn=result.get("rrn"))
        rro_ = f.get("rro_pending")
        if rro_ and not f.get("fiscal_code"):
            try:
                _fis.fiscalize(db, s_, rro_)
                _fis.send_receipt(db, s_)
            except (_fis.FiscalError, httpx.HTTPError) as e:
                f2 = dict(s_.fiscal or {})
                f2["error"] = str(e)[:200]
                s_.fiscal = f2
                db.commit()
    return {"sale": s_.status, "fiscal": (s_.fiscal or {}).get("fiscal_code"), "error": (s_.fiscal or {}).get("error")}


@app.post("/sales/{sid:int}/terminal/start")
async def sale_terminal_start(sid: int, request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """Передати суму на термінал. PayLink на компʼютері каси → повертаємо адресу для браузера; інакше платить сервер."""
    from .services import fiscal as _fis
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    js = await request.json()
    method = str(js.get("method") or "")
    w = ((s_.fiscal or {}).get("term_wait") or {}).get(method)
    if s_.status != "awaiting" or not w:
        return JSONResponse({"error": "Цей чек не очікує оплати на терміналі"}, status_code=400)
    c = _fis.cfg(db)
    now_ = datetime.now()
    started = w.get("started")
    if started and not js.get("force") and (now_ - datetime.fromisoformat(started)).total_seconds() < 150:
        return JSONResponse({"already": True})
    f = dict(s_.fiscal)
    f["term_wait"] = {**f["term_wait"], method: {**w, "started": now_.isoformat(timespec="seconds")}}
    s_.fiscal = f
    db.commit()
    audit(db, "sale.terminal_start", "sale", sid, user=user, method=method, amount=w["amount"])
    if _fis.paylink_local(c):
        return JSONResponse({"mode": "browser", "url": f"{_fis.paylink_url(c)}/api/pos/{w['device']}/purchase", "amount": int(round(w["amount"] * 100))})
    try:
        res = await asyncio.to_thread(_fis.purchase, c, w["device"], w["amount"])
    except httpx.HTTPError as e:
        return JSONResponse({"mode": "server", "ok": False, "error": f"PayLink недоступний: {e}"[:200]})
    if res.get("success") and not res.get("error") and _term_has_data(res.get("result")):
        out = await asyncio.to_thread(_term_done, db, s_, method, res.get("result") or {}, user)
        return JSONResponse({"mode": "server", "ok": True, **out})
    err_ = str(res.get("error") or ("термінал не повернув даних оплати" if res.get("success") else "оплату не проведено"))[:200]
    audit(db, "sale.terminal_fail", "sale", sid, user=user, method=method, error=err_)
    return JSONResponse({"mode": "server", "ok": False, "error": err_})


@app.post("/sales/{sid:int}/terminal/result")
async def sale_terminal_result(sid: int, request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """Результат PayLink від браузера (або «Оплата пройшла» вручну) → чек оплачено + фіскалізація."""
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    js = await request.json()
    method = str(js.get("method") or "")
    if s_.status != "awaiting" or method not in ((s_.fiscal or {}).get("term_wait") or {}):
        return JSONResponse({"error": "Цей чек не очікує оплати на терміналі", "sale": s_.status}, status_code=400)
    r = js.get("result") if isinstance(js.get("result"), dict) else {}
    if not js.get("ok") or not _term_has_data(r):               # лише підтверджена терміналом оплата з даними картки
        audit(db, "sale.terminal_fail", "sale", sid, user=user, method=method, error=str(js.get("error") or "немає даних оплати")[:200])
        return JSONResponse({"ok": False, "error": str(js.get("error") or "Термінал не повернув даних оплати — чек не закрито")[:200]})
    out = await asyncio.to_thread(_term_done, db, s_, method, r, user)
    return JSONResponse({"ok": True, **out})


@app.post("/sales/{sid:int}/fiscalize")
async def sale_fiscalize(sid: int, rro: int = Form(0), ret: str = Form(""), user: User = Depends(require("sales")),
                         db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    is_ret = bool(ret)
    if is_ret and not can(user, "sale_refund"):
        raise HTTPException(403)
    try:
        info = await asyncio.to_thread(_fis.fiscalize, db, s_, rro or None, None, 1.0, is_ret)
    except (_fis.FiscalError, httpx.HTTPError) as e:
        return RedirectResponse(_with_msg(f"/sales/{sid}", f"Checkbox: {e}", True), 303)
    audit(db, "sale.fiscal_return" if is_ret else "sale.fiscalize", "sale", sid, user=user, receipt=info.get("id"), code=info.get("fiscal_code"))
    if not is_ret and _fis.cfg(db)["send"].get("on"):
        await asyncio.to_thread(_fis.send_receipt, db, s_)
    return RedirectResponse(_with_msg(f"/sales/{sid}", ("Чек повернення" if is_ret else "Фіскальний чек") + f" № {info.get('fiscal_code') or info.get('id')}"), 303)


@app.post("/sales/{sid:int}/send-receipt")
async def sale_send_receipt(sid: int, ch: str = Form(""), user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    c = _fis.cfg(db)
    c["send"]["on"] = True
    err = None
    if not (s_.patient and s_.fiscal and s_.fiscal.get("url")):
        err = "Немає пацієнта або фіскального чека"
    else:
        from .integrations.messaging import send_to_patient
        text = (c["send"].get("text") or _fis.DEFAULT["send"]["text"]).replace("{url}", s_.fiscal["url"]).replace("{sum}", f"{s_.total:.2f}") \
            .replace("{name}", (s_.patient.full_name or "").split(" ")[0])
        prefer_ = [ch] if ch in ("telegram", "viber", "sms") else (c["send"].get("channels") or None)
        res = await asyncio.to_thread(send_to_patient, s_.patient, text, prefer_)
        err = None if res.ok else (res.error or "не надіслано")
        f = dict(s_.fiscal)
        f["sent"] = {"ok": res.ok, "channel": res.channel, "at": datetime.now().strftime("%d.%m.%Y %H:%M")}
        s_.fiscal = f
        db.commit()
    audit(db, "sale.send_receipt", "sale", sid, user=user, ok=err is None)
    lbl_ = {"telegram": " в Telegram", "viber": " у Viber", "sms": " в SMS"}.get(ch, "")
    return RedirectResponse(_with_msg(f"/sales/{sid}", err or f"Чек надіслано пацієнту{lbl_}", bool(err)), 303)


@app.get("/sales/{sid:int}/fiscal.png")
def sale_fiscal_png(sid: int, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    """Зображення фіскального чека Checkbox (GET /receipts/{id}/png)."""
    from .services import fiscal as _fis
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    f = s_.fiscal or {}
    if not f.get("id") or f.get("source") == "monobank":
        raise HTTPException(404)
    png = _fis.receipt_png(db, f)
    if not png:
        raise HTTPException(404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


def _safe_back(back: str, default: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") else default


@app.post("/sales/{sid:int}/cancel")
def sale_cancel(sid: int, reason: str = Form(""), back: str = Form(""), user: User = Depends(require("sales")),
                      db: Session = Depends(db_dep)):
    """Скасувати чек, що очікує оплати (посилання на оплату інвалідуються)."""
    from .services import sales as sl
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    back = _safe_back(back, f"/sales/{sid}")
    try:
        sl.cancel(db, s_, reason.strip() or "скасовано в касі", user.id)
    except sl.SaleError as e:
        return RedirectResponse(_with_msg(back, str(e), True), 303)
    audit(db, "sale.cancel", "sale", sid, user=user, total=s_.total, reason=reason)
    return RedirectResponse(_with_msg(back, f"Чек Ч-{sid} скасовано"), 303)


@app.post("/sales/{sid:int}/archive")
def sale_archive(sid: int, on: str = Form("1"), back: str = Form(""), user: User = Depends(require("sales")),
                 db: Session = Depends(db_dep)):
    """Видалити скасований чек з каси в архів (on=1) або повернути з архіву (on=0)."""
    from .services import sales as sl
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    back = _safe_back(back, "/sales/archive")
    try:
        if on == "1" and s_.status == "awaiting":
            sl.cancel(db, s_, "видалено з каси", user.id)
            audit(db, "sale.cancel", "sale", sid, user=user, total=s_.total, reason="видалено з каси")
        sl.archive(db, s_, on == "1")
    except sl.SaleError as e:
        return RedirectResponse(_with_msg(back, str(e), True), 303)
    audit(db, "sale.archive" if on == "1" else "sale.unarchive", "sale", sid, user=user)
    return RedirectResponse(_with_msg(back, f"Чек Ч-{sid} " + ("перенесено в архів" if on == "1" else "повернуто з архіву")), 303)


@app.get("/sales/archive", response_class=HTMLResponse)
def sales_archive(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .models import Sale
    rows = db.scalars(select(Sale).where((Sale.archived_at.is_not(None)) | (Sale.status == "cancelled"))
                      .order_by(Sale.at.desc()).limit(1000)).all()
    users = {u_.id: u_.name for u_ in db.scalars(select(User))}
    return render(request, "sales_archive.html", nav="finance", rows=rows, users=users)


@app.post("/sales/{sid:int}/refund")
def sale_refund(sid: int, reason: str = Form(""), del_password: str = Form(""), back: str = Form(""),
                user: User = Depends(require("sale_refund")), db: Session = Depends(db_dep)):
    from .services import sales as sl
    from .models import Sale
    s_ = db.get(Sale, sid) or (_ for _ in ()).throw(HTTPException(404))
    ret = back if back.startswith("/finance") else f"/sales/{sid}"        # відміна з журналу фіноперацій — назад у журнал
    if len(reason.strip()) < 3:
        return RedirectResponse(_with_msg(ret, "Вкажіть причину повернення", True), 303)
    err = _delpw(db, user, del_password, "sale_refund", "sale", sid)
    if err:
        return RedirectResponse(_with_err(ret, err), 303)
    try:
        sl.refund(db, s_, reason.strip(), user.id)
    except sl.SaleError as e:
        return RedirectResponse(_with_msg(ret, str(e), True), 303)
    audit(db, "sale.refund", "sale", sid, user=user, total=s_.total, reason=reason)
    from .services import fiscal as _fis
    if s_.fiscal and s_.fiscal.get("fiscal_code") and not s_.fiscal.get("return") and _fis.cfg(db).get("auto"):
        import threading
        threading.Thread(target=_fis.auto_return, args=(sid,), daemon=True).start()
    return RedirectResponse(_with_msg(ret, "Чек відмінено (повернення)" if ret != f"/sales/{sid}" else "Чек повернено"), 303)


# ---------------- Знижки й бонусна програма ----------------
@app.get("/settings/loyalty", response_class=HTMLResponse)
def settings_loyalty(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import loyalty as ly
    from .models import DiscountRule, BonusTxn, CbService
    rules = db.scalars(select(DiscountRule).order_by(DiscountRule.active.desc(), DiscountRule.name)).all()
    cats = sorted({c for (c,) in db.execute(select(CbService.category).distinct()) if c} |
                  {c for (c,) in db.execute(select(Product.brand).distinct()) if c})
    bal = ly.balances(db)
    calc = None
    qp = request.query_params
    if qp.get("calc"):
        items = []
        for t, pr, cat in (("service", qp.get("sp"), qp.get("sc")), ("product", qp.get("pp"), qp.get("pc"))):
            v = _num(pr or "")
            if v:
                items.append({"type": t, "price": v, "qty": 1, "category": cat or ""})
        pt = db.get(Patient, int(qp["pid"])) if (qp.get("pid") or "").isdigit() else None
        if items:
            dsc = ly.discount_for(db, items, pt, _day(qp.get("day", "")) or date.today())
            for it, dl in zip(items, dsc["lines"]):
                it["net"] = it["price"] - dl
            calc = {**dsc, "bonus": ly.bonus_accrual(db, items), "patient": pt,
                    "max_pay": ly.max_bonus_pay(db, dsc["subtotal"] - dsc["total"], pt.id) if pt else None}
    return render(request, "loyalty.html", nav="settings", s=ly.settings(db), rules=rules, cats=cats, ly=ly, calc=calc,
                  n_holders=sum(1 for v in bal.values() if v > 0.005), total_bonus=round(sum(v for v in bal.values() if v > 0), 2),
                  n_txns=db.scalar(select(func.count(BonusTxn.id))) or 0,
                  n_cb=db.scalar(select(func.count(Patient.id)).where(Patient.cb_bonus > 0)) or 0,
                  personal=__import__("app.services.personal_discounts", fromlist=["patients"]).patients(db),
                  edit=next((r for r in rules if str(r.id) == qp.get("edit", "")), None), WD=ly.WD, **_disc_ctx(db, rules, qp))


def _disc_ctx(db, rules, qp) -> dict:
    """Розділи (теги), лічильники, довідники для форми знижки в стилі CleverBox."""
    from .services import loyalty as ly, patient_tags as ptg
    from .models import CbService
    tag = qp.get("tag", "")
    today = date.today()
    st = qp.get("st", "active")                     # active | expired | all
    def ok(r):
        if st == "active" and (not r.active or ly.expired(r, today)):
            return False
        if st == "expired" and not (ly.expired(r, today) or not r.active):
            return False
        return True
    base = [r for r in rules if ok(r)]
    shown = [r for r in base if (not tag) or (tag == "-" and not r.tag) or r.tag == tag]
    counts = {t: sum(1 for r in base if r.tag == t) for t in ly.tags(db)}
    form = qp.get("edit") or qp.get("new")
    ctx = {"dtag": tag, "dst": st, "shown": shown, "tag_counts": counts, "n_untagged": sum(1 for r in base if not r.tag),
           "n_base": len(base), "dtags": ly.tags(db), "form_open": bool(form), "today": today,
           "n_cb_seed": ly.cb_pending(db)}
    if form:
        ctx.update(groups=[g["name"] for g in ptg.groups(db)], statuses=[t["name"] for t in ptg.tags(db)],
                   svc_names=sorted({n for (n,) in db.execute(select(CbService.name).where(CbService.active.is_not(False))) if n}),
                   prod_names=sorted({n for (n,) in db.execute(select(Product.name)) if n}))
        r0 = next((r for r in rules if str(r.id) == qp.get("edit", "")), None)
        ctx["uses_n"] = ly.uses(db, r0) if r0 else 0
    return ctx


@app.get("/settings/bonuses", response_class=HTMLResponse)
def settings_bonuses(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import loyalty as ly
    from .models import BonusTxn
    bal = ly.balances(db)
    return render(request, "bonuses.html", nav="settings", s=ly.settings(db), n_holders=sum(1 for v in bal.values() if v > 0.005),
                  total_bonus=round(sum(v for v in bal.values() if v > 0), 2), n_txns=db.scalar(select(func.count(BonusTxn.id))) or 0,
                  n_cb=db.scalar(select(func.count(Patient.id)).where(Patient.cb_bonus > 0)) or 0)


@app.post("/settings/loyalty/bonus")
async def settings_loyalty_bonus(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import loyalty as ly
    f = await request.form()
    s = ly.settings(db)
    s["enabled"] = bool(f.get("enabled"))
    s["accrue_on_bonus_paid"] = bool(f.get("accrue_on_bonus_paid"))
    for k in ("pct_services", "pct_products", "max_pay_pct", "welcome", "birthday", "min_check"):
        v = _num(str(f.get(k) or ""))
        s[k] = max(0.0, v) if v is not None else 0.0
    v = _num(str(f.get("expire_days") or ""))
    s["expire_days"] = int(v) if v and v > 0 else 0
    ly.save_settings(db, s)
    audit(db, "loyalty.settings", user=user, **s)
    return RedirectResponse(f"/settings/bonuses?msg={_q('Бонусну програму збережено')}", 303)


@app.post("/settings/loyalty/discount")
async def settings_loyalty_discount(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import DiscountRule
    f = await request.form()
    rid = str(f.get("rid") or "")
    r = db.get(DiscountRule, int(rid)) if rid.isdigit() else DiscountRule()
    name = str(f.get("name") or "").strip()[:150]
    val = _num(str(f.get("value") or ""))
    if not name or not val or val <= 0:
        return RedirectResponse(_with_msg("/settings/loyalty", "Вкажіть назву і розмір знижки", True), 303)
    r.name, r.kind = name, "amount" if f.get("kind") == "amount" else "percent"
    r.value = min(val, 100) if r.kind == "percent" else val
    r.applies_to = str(f.get("applies_to")) if f.get("applies_to") in ("all", "services", "products") else "all"
    r.categories = [str(x) for x in f.getlist("categories") if str(x)] or None
    r.date_from, r.date_to = _day(str(f.get("date_from") or "")), _day(str(f.get("date_to") or ""))
    r.min_sum = _num(str(f.get("min_sum") or ""))
    r.segment = str(f.get("segment") or "") or None
    r.auto, r.combine, r.active = bool(f.get("auto")), bool(f.get("combine")), bool(f.get("active"))
    r.note = str(f.get("note") or "").strip()[:300] or None
    r.tag = " ".join(str(f.get("tag") or "").split())[:60] or None
    def _int(k):
        v = _num(str(f.get(k) or ""))
        return int(v) if v and v > 0 else None
    r.per_client_limit = _int("per_client_limit")
    r.uses_limit = None if f.get("unlimited") else _int("uses_limit")
    tw = {}
    for d in range(7):
        if f.get(f"wd{d}"):
            tw[str(d)] = [str(f.get(f"wf{d}") or "")[:5], str(f.get(f"wt{d}") or "")[:5]]
    if f.get("wd_on") and tw:
        r.time_windows = tw if any(a or b for a, b in tw.values()) else None
        r.weekdays = sorted(int(k) for k in tw) if len(tw) < 7 else None
    else:
        r.time_windows, r.weekdays = None, None
    def _lst(k):
        return [str(x).strip() for x in f.getlist(k) if str(x).strip()] or None
    r.client_groups, r.patient_statuses = _lst("client_groups"), _lst("patient_statuses")
    r.services, r.products = _lst("services"), _lst("products")
    r.on_certs, r.on_subs = bool(f.get("on_certs")), bool(f.get("on_subs"))
    if r.services and not r.products:
        r.applies_to = "services"
    elif r.products and not r.services:
        r.applies_to = "products"
    db.add(r)
    db.commit()
    audit(db, "loyalty.discount", "discount", r.id, user=user, name=r.name, value=r.value, kind=r.kind)
    back = f"&tag={_q(r.tag)}" if r.tag else ""
    return RedirectResponse(f"/settings/loyalty?msg={_q('Знижку збережено')}{back}#disc", 303)


@app.post("/settings/loyalty/import-discounts")
def settings_loyalty_import_disc(user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import loyalty as ly
    n, u = ly.import_cb_discounts(db)
    audit(db, "loyalty.import_discounts", user=user, n=n, upd=u)
    return RedirectResponse(f"/settings/loyalty?msg={_q(f'Перенесено знижок CleverBox: {n} нових, {u} оновлено')}#disc", 303)


@app.post("/settings/loyalty/discount/{rid:int}/delete")
def settings_loyalty_discount_delete(rid: int, del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import DiscountRule
    err = _delpw(db, user, del_password, "discount", "discount", rid)
    if err:
        return RedirectResponse(_with_err("/settings/loyalty#disc", err), 303)
    r = db.get(DiscountRule, rid)
    if r:
        audit(db, "loyalty.discount_delete", "discount", rid, user=user, name=r.name)
        db.delete(r)
        db.commit()
    return RedirectResponse("/settings/loyalty#disc", 303)


@app.post("/settings/loyalty/import")
def settings_loyalty_import(user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import loyalty as ly
    n = ly.import_cleverbox(db, user.id)
    audit(db, "loyalty.import", user=user, n=n)
    return RedirectResponse(f"/settings/bonuses?msg={_q(f'Перенесено залишки бонусів: {n} пацієнтів')}", 303)


@app.post("/patients/{pid:int}/bonus")
def patient_bonus(pid: int, amount: str = Form(...), sign: str = Form("+"), comment: str = Form(""),
                  user: User = Depends(require("loyalty_patient")), db: Session = Depends(db_dep)):
    from .services import loyalty as ly
    _patient_or_403(db, user, pid)
    v = _num(amount)
    if v and v > 0:
        try:
            ly.add(db, pid, -v if sign == "-" else v, "manual", comment.strip()[:300] or None, user_id=user.id)
        except ValueError as e:
            return RedirectResponse(_with_msg(f"/patients/{pid}#loyal", str(e), True), 303)
        audit(db, "loyalty.manual", "patient", pid, user=user, amount=-v if sign == "-" else v, comment=comment or None)
    return RedirectResponse(f"/patients/{pid}#loyal", 303)


@app.post("/patients/{pid:int}/discount")
def patient_discount(pid: int, discount_pct: str = Form(""), products: str = Form(None), services: str = Form(None),
                     user: User = Depends(require("loyalty_patient")), db: Session = Depends(db_dep)):
    from .services import personal_discounts as pdc
    p = _patient_or_403(db, user, pid)
    if products is None and services is None:              # стара форма — одна знижка на все
        products = services = discount_pct
    pdc.set_for(p, products, services)
    db.commit()
    audit(db, "loyalty.personal", "patient", pid, user=user, products=p.disc_products_pct, services=p.disc_services_pct)
    return RedirectResponse(f"/patients/{pid}#loyal", 303)


@app.post("/settings/personal-discounts/import-cb")
async def personal_discounts_import_cb(request: Request, user: User = Depends(require("loyalty_patient")), db: Session = Depends(db_dep)):
    """Персональні знижки з CleverBox (зібрані у веб-кабінеті): {"items": [[cb_client_id, % товари, % послуги]], "name", "auto"}.
    Додає / оновлює знижки пацієнтів CRM за id клієнта CleverBox; інших пацієнтів не чіпає."""
    from .services import personal_discounts as pdc
    data = await request.json()
    by_cb = {p_.cleverbox_id: p_ for p_ in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    rep = {"items": 0, "saved": 0, "no_patient": []}
    for it in data.get("items") or []:
        rep["items"] += 1
        try:
            p_ = by_cb.get(int(it[0]))
        except (TypeError, ValueError, IndexError):
            continue
        if not p_:
            rep["no_patient"].append(it[0])
            continue
        pdc.set_for(p_, it[1], it[2])
        rep["saved"] += 1
    if data.get("name") is not None:
        c_ = pdc.cfg(db)
        pdc.save_cfg(db, str(data.get("name") or c_["name"]), bool(int(data.get("auto") or 0)), c_["combine"])
    db.commit()
    audit(db, "loyalty.personal_import_cb", user=user, saved=rep["saved"], items=rep["items"])
    return rep


@app.get("/settings/personal-discounts", response_class=HTMLResponse)
def settings_personal_discounts(request: Request, user: User = Depends(require("loyalty_patient")), db: Session = Depends(db_dep)):
    from .services import personal_discounts as pdc
    rows = [(p_, *pdc.pcts(p_)) for p_ in pdc.patients(db) if patient_visible(user, p_)]
    return render(request, "personal_discounts.html", nav="settings", c=pdc.cfg(db), rows=rows)


@app.post("/settings/personal-discounts")
async def settings_personal_discounts_save(request: Request, user: User = Depends(require("loyalty_patient")), db: Session = Depends(db_dep)):
    """Зберігає налаштування і весь список: рядки pid/products/services; пацієнти, яких прибрали зі списку, — без знижки."""
    from .services import personal_discounts as pdc
    f = await request.form()
    pdc.save_cfg(db, str(f.get("name") or ""), bool(f.get("auto")), bool(f.get("combine")))
    keep = {}
    for pid_, pr_, sv_ in zip(f.getlist("pid"), f.getlist("products"), f.getlist("services")):
        if str(pid_).isdigit():
            keep[int(pid_)] = (pr_, sv_)
    n = 0
    for p_ in pdc.patients(db):
        if p_.id not in keep and patient_visible(user, p_):
            pdc.set_for(p_, 0, 0)
    for pid_, (pr_, sv_) in keep.items():
        p_ = db.get(Patient, pid_)
        if p_ and patient_visible(user, p_):
            pdc.set_for(p_, pr_, sv_)
            n += 1 if pdc.has(p_) else 0
    db.commit()
    audit(db, "loyalty.personal_list", user=user, patients=n)
    return RedirectResponse(_with_msg("/settings/personal-discounts", f"Збережено. Пацієнтів зі знижкою: {n}"), 303)


# ---------------- Медична картка й фото до/після ----------------
MED_KINDS = {"consultation": "Консультація", "procedure": "Процедура", "control": "Контрольний огляд"}
MED_FIELDS = ("title", "complaints", "anamnesis", "objective", "diagnosis", "protocol", "recommendations")
PHOTO_STAGES = {"before": "До", "after": "Після", "progress": "У процесі"}


def _med_patient(db, user, pid) -> Patient:
    if not can(user, "medical"):
        raise HTTPException(403, "Медична картка доступна лише лікарям і власнику")
    return _patient_or_403(db, user, pid)


@app.get("/patients/{pid}/medcard", response_class=HTMLResponse)
def medcard_page(request: Request, pid: int, edit: int = 0, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import MedRecord, MedTemplate
    p = _med_patient(db, user, pid)
    recs = db.scalars(select(MedRecord).where(MedRecord.patient_id == pid, MedRecord.deleted_at.is_(None))
                      .order_by(MedRecord.at.desc())).all()
    photos = [f for f in p.files if f.kind == "photo"]
    by_rec: dict[int, list] = {}
    for f in photos:
        if f.record_id:
            by_rec.setdefault(f.record_id, []).append(f)
    docs = {d_.id: d_ for d_ in db.scalars(select(Doctor))}
    inject = [x.name for x in db.scalars(select(Product).where(Product.warehouse.in_(("inject", "classic", "single"))).order_by(Product.name)).all()][:1500]
    return render(request, "medcard.html", nav="patient", p=p, recs=recs, by_rec=by_rec, docs=docs, KINDS=MED_KINDS, STAGES=PHOTO_STAGES,
                  doctors=db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all(),
                  templates_=db.scalars(select(MedTemplate).order_by(MedTemplate.name)).all(), products=inject,
                  edit=next((r for r in recs if r.id == edit), None), now=datetime.now(), photos=photos,
                  my_doctor=user.doctor_id)


@app.post("/patients/{pid}/medcard/summary")
def medcard_summary(pid: int, med_allergies: str = Form(""), med_conditions: str = Form(""), med_contra: str = Form(""),
                    phototype: str = Form(""), user: User = Depends(current_user), db: Session = Depends(db_dep)):
    p = _med_patient(db, user, pid)
    p.med_allergies, p.med_conditions = med_allergies.strip() or None, med_conditions.strip() or None
    p.med_contra, p.phototype = med_contra.strip() or None, phototype.strip()[:10] or None
    db.commit()
    audit(db, "medcard.summary", "patient", pid, user=user)
    return RedirectResponse(f"/patients/{pid}/medcard?msg={_q('Збережено')}", 303)


@app.post("/patients/{pid}/medcard/record")
async def medcard_record(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import MedRecord, MedTemplate
    p = _med_patient(db, user, pid)
    f = await request.form()
    rid = str(f.get("rid") or "")
    r = db.get(MedRecord, int(rid)) if rid.isdigit() else None
    if r and r.patient_id != pid:
        raise HTTPException(404)
    new = r is None
    r = r or MedRecord(patient_id=pid, created_by=user.id)
    at_s = str(f.get("at") or "")
    try:
        r.at = datetime.fromisoformat(at_s) if at_s else (r.at or datetime.now())
    except ValueError:
        r.at = r.at or datetime.now()
    r.kind = str(f.get("kind") or "procedure") if str(f.get("kind")) in MED_KINDS else "procedure"
    did = str(f.get("doctor_id") or "")
    r.doctor_id = int(did) if did.isdigit() else (user.doctor_id or None)
    for k in MED_FIELDS:
        v = str(f.get(k) or "").strip()
        setattr(r, k, (v[:200] if k == "title" else v) or None)
    drugs = []
    for i in range(12):
        nm = str(f.get(f"drug_{i}") or "").strip()
        if nm:
            drugs.append({k: str(f.get(f"{k}_{i}") or "").strip()[:120] for k in ("batch", "expiry", "dose", "zone")} | {"name": nm[:200]})
    r.drugs = drugs or None
    r.next_visit = _day(str(f.get("next_visit") or ""))
    r.consent = bool(f.get("consent"))
    if not new:
        r.updated_at = datetime.utcnow()
    db.add(r)
    db.commit()
    for up in f.getlist("photos"):
        if getattr(up, "filename", None):
            pf = ingest.ingest_photo(db, await up.read(), up.filename or "photo.jpg", str(f.get("photo_caption") or "") or None, patient=p)
            pf.stage = str(f.get("photo_stage") or "") if str(f.get("photo_stage") or "") in PHOTO_STAGES else None
            pf.zone = str(f.get("photo_zone") or "").strip()[:80] or None
            pf.taken_at, pf.record_id = r.at.date(), r.id
            db.commit()
    tname = str(f.get("save_template") or "").strip()
    if tname:
        db.add(MedTemplate(name=tname[:150], kind=r.kind, created_by=user.id,
                           fields={k: getattr(r, k) for k in MED_FIELDS if k not in ("anamnesis",) and getattr(r, k)}))
        db.commit()
    audit(db, "medcard.record_add" if new else "medcard.record_edit", "patient", pid, user=user, record=r.id, kind=r.kind)
    return RedirectResponse(f"/patients/{pid}/medcard?msg={_q('Запис збережено')}#r{r.id}", 303)


@app.post("/patients/{pid}/medcard/record/{rid:int}/delete")
def medcard_record_delete(pid: int, rid: int, del_password: str = Form(""), user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import MedRecord
    _med_patient(db, user, pid)
    err = _delpw(db, user, del_password, "med_record", "patient", pid)
    if err:
        return RedirectResponse(_with_err(f"/patients/{pid}/medcard", err), 303)
    r = db.get(MedRecord, rid)
    if r and r.patient_id == pid:
        r.deleted_at = datetime.utcnow()
        db.commit()
        audit(db, "medcard.record_delete", "patient", pid, user=user, record=rid)
    return RedirectResponse(f"/patients/{pid}/medcard?msg={_q('Запис видалено')}", 303)


@app.post("/medcard/templates/{tid:int}/delete")
def medcard_template_delete(tid: int, back: str = Form("/"), del_password: str = Form(""), user: User = Depends(require("medical")),
                            db: Session = Depends(db_dep)):
    from .models import MedTemplate
    back = back if back.startswith("/patients/") else "/"
    err = _delpw(db, user, del_password, "med_template", None, tid)
    if err:
        return RedirectResponse(_with_err(back, err), 303)
    t = db.get(MedTemplate, tid)
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse(back, 303)


@app.get("/patients/{pid}/medcard/print", response_class=HTMLResponse)
def medcard_print(request: Request, pid: int, rid: int = 0, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import MedRecord
    p = _med_patient(db, user, pid)
    q = select(MedRecord).where(MedRecord.patient_id == pid, MedRecord.deleted_at.is_(None))
    if rid:
        q = q.where(MedRecord.id == rid)
    recs = db.scalars(q.order_by(MedRecord.at)).all()
    audit(db, "medcard.print", "patient", pid, user=user, record=rid or None)
    return render(request, "medcard_print.html", nav="print", p=p, recs=recs, KINDS=MED_KINDS,
                  docs={d_.id: d_ for d_ in db.scalars(select(Doctor))}, now=datetime.now())


@app.get("/patients/{pid}/photos", response_class=HTMLResponse)
def patient_photos(request: Request, pid: int, a: int = 0, b: int = 0, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .services import patient_photos as pp
    p = _patient_or_403(db, user, pid)
    files = sorted(pp.all_files(db, pid), key=lambda f: (f.taken_at or f.created_at.date(), f.id), reverse=True)
    items = [{"f": f, "cat": pp.cat(f), "img": pp.is_image(f)} for f in files]
    counts = {k: sum(1 for x in items if x["cat"] == k) for k in pp.CATS}
    return render(request, "photos.html", nav="patient", p=p, items=items, counts=counts, CATS=pp.CATS, STAGES=PHOTO_STAGES,
                  a=a, b=b)


@app.get("/settings/patient-photos", response_class=HTMLResponse)
def settings_patient_photos(request: Request, tab: str = "face", q: str = "", page: int = 1, user: User = Depends(require("upload")),
                            db: Session = Depends(db_dep)):
    from .services import patient_photos as pp
    vis = None if user.role == "owner" else {x.id for x in db.scalars(select(Patient)) if patient_visible(user, x)}
    lib = pp.library(db, tab, q, page)
    if vis is not None:
        lib["items"] = [f for f in lib["items"] if f.patient_id in vis]
    return render(request, "patient_photos_lib.html", nav="settings", lib=lib, CATS=pp.CATS, img=pp.is_image)


def _files_back(back: str) -> str:
    return back if back.startswith(("/settings/patient-photos", "/patients/")) else "/settings/patient-photos"


@app.post("/files/category")
async def files_category(request: Request, user: User = Depends(require("upload")), db: Session = Depends(db_dep)):
    from .services import patient_photos as pp
    f = await request.form()
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    for pid_ in {x.patient_id for x in db.scalars(select(PatientFile).where(PatientFile.id.in_(ids or [0])))}:
        if pid_:
            _patient_or_403(db, user, pid_)
    n = pp.set_category(db, ids, str(f.get("category") or ""))
    audit(db, "files.category", user=user, files=ids, category=str(f.get("category") or ""))
    return RedirectResponse(_with_msg(_files_back(str(f.get("back") or "")), f"Перенесено файлів: {n}"), 303)


@app.post("/files/delete")
async def files_delete(request: Request, user: User = Depends(require("upload")), db: Session = Depends(db_dep)):
    from .services import patient_photos as pp
    f = await request.form()
    back = _files_back(str(f.get("back") or ""))
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    if not ids:
        return RedirectResponse(_with_msg(back, "Оберіть фото", True), 303)
    err = _delpw(db, user, str(f.get("del_password") or ""), "patient_files", None, None)
    if err:
        return RedirectResponse(_with_err(back, err), 303)
    for pid_ in {x.patient_id for x in db.scalars(select(PatientFile).where(PatientFile.id.in_(ids)))}:
        if pid_:
            _patient_or_403(db, user, pid_)
    gone = pp.delete(db, ids)
    for fid_, pid_ in gone:
        audit(db, "file.delete", "patient", pid_, user=user, file_id=fid_)
    return RedirectResponse(_with_msg(back, f"Видалено файлів: {len(gone)}"), 303)


@app.post("/files/{fid:int}/meta")
def file_meta(fid: int, stage: str = Form(""), zone: str = Form(""), taken_at: str = Form(""), caption: str = Form(""),
              user: User = Depends(require("upload")), db: Session = Depends(db_dep)):
    f = db.get(PatientFile, fid)
    if not f or not f.patient_id:
        raise HTTPException(404)
    _patient_or_403(db, user, f.patient_id)
    f.stage = stage if stage in PHOTO_STAGES else None
    f.zone, f.taken_at, f.caption = zone.strip()[:80] or None, _day(taken_at), caption.strip() or f.caption
    db.commit()
    return RedirectResponse(f"/patients/{f.patient_id}/photos?a={fid}", 303)


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
              delete_pw: str = Form(""), delete_pw_clear: str = Form(""),
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
    if delete_pw:
        if len(delete_pw) < 4:
            raise HTTPException(400, "Пароль для видалення — щонайменше 4 символи")
        if verify_password(delete_pw, u.password_hash or ""):
            raise HTTPException(400, "Пароль для видалення має відрізнятися від пароля входу")
        u.delete_pw_hash = hash_password(delete_pw)
    elif delete_pw_clear:
        u.delete_pw_hash = None
    db.commit()
    audit(db, action, "user", u.id, user=user, login=u.login, role=u.role, active=u.active,
          password_changed=bool(password), delete_pw=("set" if delete_pw else "cleared" if delete_pw_clear else None))
    return RedirectResponse("/access", 303)


@app.get("/access/roles", response_class=HTMLResponse)
def access_roles(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import roles as rl
    return render(request, "roles.html", nav="access", tab="roles", rs=rl.roles(db), rl=rl, counts=rl.users_by_role(db),
                  edit=request.query_params.get("edit", ""), view=request.query_params.get("view", ""),
                  copy=request.query_params.get("copy", ""))


@app.post("/access/roles")
async def access_roles_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import roles as rl
    f = await request.form()
    key = str(f.get("key") or "") or None
    if key == "owner":
        return RedirectResponse("/access/roles", 303)
    name = str(f.get("name") or "").strip()
    if not name:
        return RedirectResponse(_with_msg("/access/roles", "Вкажіть назву ролі", True), 303)
    k = rl.save_role(db, key, name, [str(x) for x in f.getlist("perms")], bool(f.get("hidden")))
    audit(db, "roles.save", user=user, role=k, name=name, perms=sorted(str(x) for x in f.getlist("perms")))
    return RedirectResponse(_with_msg("/access/roles", f"Роль «{name}» збережено") + f"#r-{k}", 303)


@app.get("/access/ui", response_class=HTMLResponse)
def access_ui(request: Request, role: str = "", user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import roles as rl, ui_access as ua
    rs = [r for r in rl.roles(db) if r["key"] != "owner"]
    role = role if any(r["key"] == role for r in rs) else (rs[0]["key"] if rs else "")
    return render(request, "access_ui.html", nav="access", tab="ui", rs=rs, role=role, ua=ua, deny=ua.deny_map(db).get(role, set()))


@app.post("/access/ui")
async def access_ui_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import ui_access as ua
    f = await request.form()
    role = str(f.get("role") or "")
    if not role or role == "owner":
        return RedirectResponse("/access/ui", 303)
    deny = ua.save(db, role, [str(x) for x in f.getlist("allow")])
    audit(db, "access.ui", user=user, role=role, denied=sorted(deny))
    db.commit()
    return RedirectResponse(_with_msg(f"/access/ui?role={role}", f"Доступи збережено: заборонено {len(deny)} елемент(ів)"), 303)


@app.post("/access/roles/{key}/reset")
def access_roles_reset(key: str, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import roles as rl
    rl.reset_role(db, key)
    audit(db, "roles.reset", user=user, role=key)
    return RedirectResponse(_with_msg("/access/roles", "Права ролі скинуто до заводських"), 303)


@app.post("/access/roles/{key}/delete")
def access_roles_delete(key: str, del_password: str = Form(""), user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import roles as rl
    err = _delpw(db, user, del_password, "role", None, None)
    if err:
        return RedirectResponse(_with_err("/access/roles", err), 303)
    e = rl.delete_role(db, key)
    if e:
        return RedirectResponse(_with_msg("/access/roles", e, True), 303)
    audit(db, "roles.delete", user=user, role=key)
    return RedirectResponse(_with_msg("/access/roles", "Роль видалено"), 303)


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
               scopes=[s for s in scopes if s in ("read", "write", "medical", "bot", "calls", "invoices")] or ["read"], created_by=user.id)
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
    from .services import autonomy as _aut
    if _aut.offline():
        return RedirectResponse(_with_msg("/data#mode", "Автономний режим: синхронізацію з CleverBox вимкнено", True), 303)
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


@app.post("/settings/cleverbox-calendar")
def cleverbox_calendar_toggle(on: str = Form("0"), user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    """Синхронізація календаря з CleverBox: on=1 — увімкнути, 0 — вимкнути (записи, що вже є, лишаються)."""
    from .integrations.cleverbox import set_calendar_frozen
    set_calendar_frozen(db, on != "1")
    audit(db, "cleverbox.calendar_sync", user=user, enabled=on == "1")
    return RedirectResponse(_with_msg("/data#cleverbox", "Синхронізацію календаря з CleverBox " + ("увімкнено" if on == "1" else "вимкнено")), 303)


@app.post("/settings/work-mode")
def work_mode_save(autonomous: str = Form(""), since: str = Form(""), test: str = Form(""), user: User = Depends(require("migrate")),
                   db: Session = Depends(db_dep)):
    """Режим роботи: автономний (без CleverBox, облік у CRM з дати) і тестовий (без фіскалізації й терміналів)."""
    from .services import autonomy
    if user.role != "owner":
        raise HTTPException(403)
    try:
        s_ = date.fromisoformat(since).isoformat() if since else None
    except ValueError:
        return RedirectResponse(_with_msg("/data#mode", "Невірна дата переходу", True), 303)
    d_ = autonomy.save(db, autonomous=autonomous == "1", since=s_, test=test == "1")
    if d_["autonomous"]:
        from .integrations.cleverbox import set_calendar_frozen, calendar_frozen
        if not calendar_frozen(db):
            set_calendar_frozen(db, True)
    ensure_sync_schedule()
    audit(db, "work_mode", user=user, autonomous=d_["autonomous"], since=d_["since"], test=d_["test"])
    db.commit()
    return RedirectResponse(_with_msg("/data#mode", "Режим роботи збережено"), 303)


@app.get("/data/coverage", response_class=HTMLResponse)
def data_coverage(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    from .services import data_coverage as dc
    return render(request, "data_coverage.html", nav="data", s=dc.summary(db))


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, user: User = Depends(require("export_reports")), db: Session = Depends(db_dep)):
    last = db.get(SyncState, "last_migration")
    cb_success = db.get(SyncState, "cleverbox_last_success")
    cb_error = db.get(SyncState, "cleverbox_last_error")
    job = state.get("scheduler").get_job("cleverbox_sync") if state.get("scheduler") else None
    from .services import backups as bk
    from .services import dropbox_backup as _dbx
    return render(request, "data.html", dbx=_dbx.status(db) if can(user, "backup") else None, jobs=jobs.recent(), cb=bool(settings.cleverbox_token),
                  backups=bk.list_backups() if can(user, "backup") else [], settings=settings,
                  last_migration=last.value if last else None, reports=LAST_IMPORT.pop(user.id, None),
                  cb_last_success=cb_success.value if cb_success else None,
                  cb_last_error=cb_error.value if cb_error else None,
                  cb_next_sync=job.next_run_time if job else None,
                  wmode=__import__("app.services.autonomy", fromlist=["x"]).cfg(db),
                  cb_auto_sync=auto_sync_enabled(), cal_frozen=__import__("app.integrations.cleverbox", fromlist=["x"]).calendar_frozen(db),
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


def _cash_imp():
    from .services import cashflow
    return cashflow


def _cert_imp():
    from .services import certificates
    return certificates


def _svc_imp():
    from .services import cb_services_import
    return cb_services_import


@app.post("/import/table")
async def import_table(request: Request, file: UploadFile = File(...), kind: str = Form(""),
                       user: User = Depends(require("import")), db: Session = Depends(db_dep)):
    data = await file.read()
    try:
        from .services import cb_clients_import as cbc
        sheets = importer.read_sheets(data, file.filename or "data.xlsx")
        if sheets and sheets[0][1] and cbc.is_clients_export(sheets[0][1][0]):     # вивантаження «Клієнти» CleverBox
            rep = await asyncio.to_thread(cbc.import_clients, db, sheets[0][1][1:], sheets[0][1][0])
            rep["sheet"] = file.filename
            reports = [rep]
        elif sheets and sheets[0][1] and _cash_imp().is_profit_export(sheets[0][1][0]):   # «Прибуток» → cashflow
            rep = await asyncio.to_thread(_cash_imp().import_profit, db, sheets[0][1][1:], sheets[0][1][0])
            _cash_imp().auto_fill(db)
            rep["sheet"] = file.filename
            reports = [rep]
        elif sheets and sheets[0][1] and _cert_imp().is_certificates_export(sheets[0][1][0]):   # «Сертифікати»
            rep = await asyncio.to_thread(_cert_imp().import_certificates, db, sheets[0][1][1:], sheets[0][1][0])
            rep["sheet"] = file.filename
            reports = [rep]
        elif sheets and sheets[0][1] and _cert_imp().is_subscriptions_export(sheets[0][1][0]):   # «Абонементи»
            rep = await asyncio.to_thread(_cert_imp().import_subscriptions, db, sheets[0][1][1:], sheets[0][1][0])
            rep["sheet"] = file.filename
            reports = [rep]
        elif sheets and sheets[0][1] and _svc_imp().is_services_export(sheets[0][1][0]):   # «Послуги» CleverBox
            rep = await asyncio.to_thread(_svc_imp().import_services, db, sheets[0][1][1:], sheets[0][1][0])
            rep["sheet"] = file.filename
            reports = [rep]
        else:
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
def migrate_cleverbox(months: int = Form(24), all_clients: str = Form(""), all_history: str = Form(""), from_date: str = Form(""),
                      user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    from .integrations.cleverbox import migrate
    full_history = all_history == "1"
    try:
        sd = date(2000, 1, 1) if full_history else (date.fromisoformat(from_date) if from_date else None)
    except ValueError:
        sd = None
    job = jobs.start("Перенос з CleverBox", migrate, months=max(1, min(months, 120)),
                     all_clients=bool(all_clients), start_date=sd)
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


# ---------------- Копія в Dropbox ----------------
@app.post("/settings/dropbox/connect")
def dropbox_connect(app_key: str = Form(""), user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import dropbox_backup as dbx
    try:
        url = dbx.start_auth(db, app_key)
    except dbx.DropboxError as e:
        return RedirectResponse(_with_msg("/data", str(e), True) + "#dropbox", 303)
    audit(db, "dropbox.connect_start", user=user)
    return RedirectResponse(url, 303)


@app.get("/settings/dropbox/callback")
def dropbox_callback(code: str = "", state: str = "", error: str = "", user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import dropbox_backup as dbx
    if error:
        return RedirectResponse(_with_msg("/data", "Dropbox: доступ не надано", True) + "#dropbox", 303)
    try:
        d = dbx.finish_auth(db, code, state)
    except dbx.DropboxError as e:
        return RedirectResponse(_with_msg("/data", str(e), True) + "#dropbox", 303)
    audit(db, "dropbox.connected", user=user, account=d.get("account"))
    return RedirectResponse(_with_msg("/data", "Dropbox підключено" + (f": {d.get('account')}" if d.get("account") else "")
                                      + ("" if d["secrets"].get("password") else " — задайте пароль шифрування")) + "#dropbox", 303)


@app.post("/settings/dropbox/password")
def dropbox_password(password: str = Form(""), password2: str = Form(""), user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import dropbox_backup as dbx
    if password != password2:
        return RedirectResponse(_with_msg("/data", "Паролі не збігаються", True) + "#dropbox", 303)
    try:
        dbx.set_password(db, password)
    except dbx.DropboxError as e:
        return RedirectResponse(_with_msg("/data", str(e), True) + "#dropbox", 303)
    audit(db, "dropbox.password", user=user)
    return RedirectResponse(_with_msg("/data", "Пароль шифрування збережено — запишіть його окремо, без нього копію не відновити") + "#dropbox", 303)


@app.post("/settings/dropbox/disconnect")
def dropbox_disconnect(user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import dropbox_backup as dbx
    dbx.disconnect(db)
    audit(db, "dropbox.disconnect", user=user)
    return RedirectResponse(_with_msg("/data", "Dropbox відключено") + "#dropbox", 303)


@app.post("/settings/dropbox/now")
async def dropbox_now(user: User = Depends(require("backup")), db: Session = Depends(db_dep)):
    from .services import backups
    audit(db, "dropbox.backup_now", user=user)

    def _run():
        try:
            backups.run_backup("dropbox")
        except Exception:  # noqa: BLE001
            pass

    __import__("threading").Thread(target=_run, daemon=True).start()
    return RedirectResponse(_with_msg("/data", "Копію запущено у фоні: база — за хвилину, фото й PDF першого разу — довше (до 20 хв за раз, решта — вночі)") + "#dropbox", 303)


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
from .models import Task, Product, StockLevel, Check, StockDoc
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
        if words and not __import__("app.services.search", fromlist=["x"]).patient(q, p):
            continue
        if doctor and p.doctor_id != doctor and not any(pl.doctor_id == doctor for pl in p.plans):
            continue
        pats.append(p)
    rows = clients_svc.client_rows(db, pats)
    from .services import patient_tags as _ptg
    _lv = _ptg.tags(db)
    _vd = _ptg.stats(db, [p_.id for p_ in pats])
    tags_of = {p_.id: _ptg.tag_for(_lv, (_vd.get(p_.id) or {}).get("visits", 0) or int(p_.cb_visits or 0),
                                   (_vd.get(p_.id) or {}).get("planned"), (_vd.get(p_.id) or {}).get("last_days")) for p_ in pats}
    qp = request.query_params
    rows = clients_svc.apply_filters(rows, qp, tags_of)
    counts = clients_svc.segment_counts(rows)
    rows = [r for r in rows if seg in r.segment_flags]
    order = {"red": 0, "yellow": 1, "green": 2, "done": 3}
    srt = qp.get("sort", "")
    if srt in ("name", "-visits", "-revenue", "-last", "next"):
        key = {"name": lambda r: r.patient.full_name.lower(), "-visits": lambda r: -r.visits, "-revenue": lambda r: -r.revenue,
               "-last": lambda r: -(r.last_visit.timestamp() if r.last_visit else 0),
               "next": lambda r: r.next_visit.timestamp() if r.next_visit else 9e12}[srt]
        rows.sort(key=key)
    else:
        rows.sort(key=lambda r: (order.get(r.plan_status.flag, 4) if r.plan_status else 5,
                                 -(r.last_visit.timestamp() if r.last_visit else 0)))
    per = 100
    total = len(rows)
    rows = rows[(page - 1) * per: page * per]
    # довідники для фільтрів
    allp = pats
    uniq = lambda xs: sorted({x for x in xs if x}, key=str.lower)                  # noqa: E731
    fopts = {"tags": [t_["name"] for t_ in _lv], "tag_colors": _ptg.colors(_lv), "groups": [g_["name"] for g_ in _ptg.groups(db)],
             "sources": uniq(p_.source for p_ in allp), "dirs": uniq(d_ for p_ in allp for d_ in (p_.directions or [])),
             "channels": uniq(p_.communication_channel for p_ in allp), "genders": uniq(p_.gender for p_ in allp)}
    MONTHS = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
    labels = {"tag": "Статус", "grp": "Група", "mark": "Мітка", "gender": "Стать", "src": "Джерело", "dirs": "Напрямок", "ch": "Канал",
              "bmonth": "ДН у місяці", "age_from": "Вік від", "age_to": "Вік до", "vis_from": "Візитів від", "vis_to": "Візитів до",
              "lv_from": "Останній візит з", "lv_to": "Останній візит по", "nv": "Майбутній запис", "ltv_from": "LTV від", "ltv_to": "LTV до",
              "plan": "Бюті-план", "bonus": "Є бонуси", "bal": "Є баланс", "tg": "Підключений бот", "optout": "Розсилки",
              "reg_from": "Додано з", "reg_to": "Додано по", "noph": "Без телефону"}
    vals = {"nv": {"yes": "є", "no": "немає"}, "plan": {"yes": "є", "no": "немає"}, "mark": {"vip": "VIP", "bad": "BAD", "none": "без мітки"},
            "optout": {"1": "відписані", "0": "не відписані"}}
    chips = []
    from urllib.parse import urlencode
    items = [(k_, v_) for k_, v_ in qp.multi_items() if k_ != "page"]
    for i_, (k_, v_) in enumerate(items):
        if k_ in clients_svc.FILTER_KEYS and v_:
            vv = vals.get(k_, {}).get(v_, MONTHS[int(v_) - 1] if k_ == "bmonth" and v_.isdigit() and 1 <= int(v_) <= 12 else v_)
            chips.append({"label": f"{labels.get(k_, k_)}: {vv}" if k_ not in ("bonus", "bal", "tg", "noph") else labels[k_],
                          "url": "?" + urlencode([x for j_, x in enumerate(items) if j_ != i_])})
    base_q = urlencode([x for x in items if x[0] not in ("seg",)])
    page_q = urlencode(items)
    from .services import loyalty as _ly
    return render(request, "clients.html", rows=rows, counts=counts, seg=seg, q=q, doctor=doctor, page=page,
                  pages=max(1, (total + per - 1) // per), total=total, SEGMENTS=clients_svc.SEGMENTS,
                  doctors=db.scalars(select(Doctor).order_by(Doctor.name)).all(), cols=clients_svc.columns(db), COLS=clients_svc.COLUMNS,
                  tags_of=tags_of, fopts=fopts, chips=chips, base_q=base_q, page_q=page_q, qp=qp, MONTHS=MONTHS, age=clients_svc.age,
                  n_filters=len(chips), dnames={d_.id: d_.name for d_ in db.scalars(select(Doctor))},
                  unassigned=db.scalar(select(func.count()).select_from(PatientFile)
                                       .where(PatientFile.patient_id.is_(None))) if can(user, "files_assign") else 0)


@app.post("/settings/clients-columns")
async def settings_clients_columns(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    f = await request.form()
    clients_svc.save_columns(db, f.getlist("cols"))
    audit(db, "settings.clients_columns", user=user)
    back = str(f.get("back") or "")
    return RedirectResponse(back if back.startswith("/clients") else _with_msg("/settings/patient-tags#cols", "Колонки списку збережено"), 303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, start: str | None = None, doc: str = "", view: str = "day",
                  user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .services import specialists as spec_svc
    today = date.today()
    try:
        s = date.fromisoformat(start) if start else today
    except ValueError:
        s = today
    view = view if view in ("day", "week") else "day"            # місяць прибрано
    doc_id = int(doc) if doc.isdigit() else None
    specs = spec_svc.calendar_doctors(db)
    if view == "week" and not doc_id and specs:                 # тиждень — розклад одного спеціаліста
        doc_id = specs[0].id
    ctx = dict(view=view, today=today, doc=doc_id, specialists=specs, color=spec_svc.color, initials=spec_svc.initials)
    days = 1 if view == "day" else 7
    ev = cal_svc.events(db, user, s, days, doc_id)
    from .services import clinic as _cl, schedule as _sch
    _dflt = (settings.work_hour_from, settings.work_hour_to)
    if view == "day":
        _work = {(did_, s) for did_ in _sch.for_day(db, s)}
        cols = cal_svc.columns(db, ev, fixed=not doc_id, working=_work)
        if doc_id:
            cols = {d: [c for c in cs if c.doctor and c.doctor.id == doc_id] or
                       [cal_svc.Column(str(doc_id), db.get(Doctor, doc_id), [])] for d, cs in cols.items()}
        _wh = _cl.hours_for(db, s) or _dflt
        grid = cal_svc.time_grid(s, cols[s], _wh[0], _wh[1])
        sh = _sch.for_day(db, s)
        for g in grid["cols"]:
            g["day"], g["now_top"] = s, grid["now_top"]
            g["shift"] = sh.get(g["col"].doctor.id) if g["col"].doctor else None
        n_ev = sum(1 for e in ev.get(s, []) if e.kind in ("visit", "attended"))
    else:                                                       # 7 днів від обраної дати, дні, коли клініка зачинена, пропускаємо
        d_obj = db.get(Doctor, doc_id) if doc_id else None
        open_days = [d for d in sorted(ev) if _cl.hours_for(db, d)] or sorted(ev)
        hrs = [_cl.hours_for(db, d) or _dflt for d in open_days]
        wf, wt = min(h[0] for h in hrs), max(h[1] for h in hrs)
        timed = [e for d in open_days for e in ev[d] if not e.all_day]
        h0 = min([wf] + [e.when.hour for e in timed])
        h1 = max([wt] + [min(24, (e.ends or e.when).hour + (1 if (e.ends or e.when).minute else 0)) for e in timed])
        sh = _sch.shifts(db, s, s + timedelta(days=6))
        gcols, now_top = [], None
        for d in open_days:
            col = cal_svc.Column(d.isoformat(), d_obj, [e for e in ev[d] if e.doctor_id == doc_id or (not e.doctor_id and e.all_day)])
            g1 = cal_svc.time_grid(d, [col], h0, h1)
            gc = g1["cols"][0]
            gc["day"], gc["now_top"], gc["shift"] = d, g1["now_top"], sh.get((doc_id, d))
            if g1["now_top"] is not None:
                now_top = g1["now_top"]
            gcols.append(gc)
        grid = {"h0": h0, "h1": h1, "cols": gcols, "now_top": now_top}
        cols = {}
        n_ev = sum(1 for d in open_days for e in ev[d] if e.kind in ("visit", "attended") and e.doctor_id == doc_id)
    ctx["now_h"] = min(max(datetime.now().hour + 1, 8), 20)
    ctx["locs"] = [l_ for l_ in (_cl.get(db).get("locations") or []) if l_.get("active")]
    ctx["now_hm"] = datetime.now().strftime("%H:%M")
    ids = [d_.id for d_ in specs]
    if doc_id in ids:
        i_ = ids.index(doc_id)
        ctx["doc_prev"], ctx["doc_next"] = ids[i_ - 1], ids[(i_ + 1) % len(ids)]
    _pids = {e.patient_id for lst in ev.values() for e in lst if e.patient_id}
    ctx["phones"] = dict(db.query(Patient.id, Patient.phone).filter(Patient.id.in_(_pids)).all()) if _pids else {}
    from .services import visit_statuses as _vs, patient_tags as _ptg
    ctx["vstats"], ctx["vstat_css"] = _vs.all_(db), _vs.css(db)
    ctx["tag_colors"] = _ptg.colors(_ptg.tags(db))
    ctx["pinfo"] = _ptg.calendar_info(db, [e for lst in ev.values() for e in lst], can(user, "medical"))
    ctx["pinfo_id"] = id
    return render(request, "calendar.html", ev=ev, cols=cols, grid=grid, start=s, days=days, n_ev=n_ev,
                  prev=s - timedelta(days=days), next=s + timedelta(days=days), end=s + timedelta(days=days - 1),
                  month=None, **ctx)


@app.post("/calendar/visit/adopt")
def calendar_visit_adopt(v: str = Form(""), user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Візит CleverBox → запис CRM (ним далі керують у CRM)."""
    from .models import Visit as _V
    from .services import appointments as aps
    ids = [int(x) for x in v.split(",") if x.strip().isdigit()][:20]
    vs = [x for x in (db.get(_V, i) for i in ids) if x and x.source == "cb_visit"]
    rid = next((aps._rid(x) for x in vs if aps._rid(x)), None)
    a = aps.adopt_cb(db, rid, user.id) if rid else None
    if not a:
        return JSONResponse({"error": "Це не запис календаря CleverBox — керувати ним у CRM не можна"}, status_code=400)
    audit(db, "appointment.adopt_cb", "appointment", a.id, user=user, cb=rid)
    db.commit()
    return {"ok": True, "id": a.id}


@app.get("/calendar/day-stats", response_class=HTMLResponse)
def calendar_day_stats(request: Request, d: str = "", doc: int = 0, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Статистика дня (кнопка в календарі)."""
    from .services import day_stats
    try:
        day = date.fromisoformat(d) if d else date.today()
    except ValueError:
        day = date.today()
    if user.role == "doctor" and user.doctor_id:
        doc = user.doctor_id
    s_ = day_stats.compute(db, day, doc or None)
    return templates.TemplateResponse(request, "_day_stats.html", {"request": request, "user": user, "s": s_, "can": can,
                                      "money": can(user, "finance") or can(user, "sales")})


@app.get("/calendar/visit", response_class=HTMLResponse)
def calendar_visit(request: Request, appt: int = 0, v: str = "", tab: str = "visit", user: User = Depends(current_user),
                   db: Session = Depends(db_dep)):
    """Вікно візиту (як у CleverBox): відкривається з календаря кліком по запису."""
    from .services import visit_card as vc, appointments as aps, calendar as _cal, loyalty as ly
    ids = [int(x) for x in v.split(",") if x.strip().isdigit()][:20]
    from .services import autonomy as _aut_v
    if not appt and ids and _aut_v.offline() and can(user, "book"):      # автономно: минулий візит CleverBox відкривається як запис CRM (історія)
        _since = _aut_v.since(db)
        _vs = [x for x in (db.get(Visit, i) for i in ids) if x and x.source == "cb_visit"
               and not (_since and x.starts_at >= datetime.combine(_since, datetime.min.time()))]
        _rid = next((aps._rid(x) for x in _vs if aps._rid(x)), None)
        _a = aps.adopt_cb(db, _rid) if _rid else None
        if _a:
            appt, ids = _a.id, []
    if appt:
        from .models import Appointment as _ApF
        _af = db.get(_ApF, appt)
        if _af:
            aps.fill_prices(db, _af)
    c = vc.card(db, appt, ids)
    if not c:
        raise HTTPException(404, "Візит не знайдено")
    p_ = _patient_or_403(db, user, c["p"].id)
    from sqlalchemy import func as _f
    from .services import patient_tags as _ptg
    n = _ptg.visit_days(db, [p_.id]).get(p_.id, 0)
    _done_here = bool(c["a"] and c["a"].status in ("done", "paid"))
    _lv = _ptg.tags(db)
    stats = {"tag": _cal.tag_for(n + (0 if _done_here else 1), _lv), "tag_color": "", "visits": p_.cb_visits if p_.cb_visits is not None else n, "total": p_.cb_total or 0,
             "bonus": round(ly.balance(db, p_.id), 2), "discount": p_.discount_pct}
    stats["tag_color"] = _ptg.colors(_lv).get(stats["tag"], "")
    from .services import certificates as _cert2
    from .models import Subscription as _Sub
    _td = date.today()
    stats["certs"] = sum(1 for x in _cert2.for_patient(db, p_.id) if x.state() in ("active", "expiring"))
    stats["subs"] = sum(1 for x in db.scalars(select(_Sub).where(_Sub.patient_id == p_.id))
                        if (not x.valid_to or x.valid_to >= _td) and ((x.visits_left or 0) > 0 or (x.balance or 0) > 0))
    _cal_docs = spec_svc.calendar_doctors(db)
    from .models import Appointment as _Ap2
    _docn = {d_.id: d_.name for d_ in spec_svc.all_doctors(db)}
    stats["planned_list"] = [{"id": x.id, "start": x.start, "doctor": _docn.get(x.doctor_id, ""),
                              "services": ", ".join(s_.get("name", "") for s_ in x.services or [])}
                             for x in db.scalars(select(_Ap2).where(
        _Ap2.patient_id == p_.id, _Ap2.start > datetime.now(), _Ap2.status.notin_(("cancelled", "no_show", "done", "paid")),
        _Ap2.id != (c["a"].id if c["a"] else 0)).order_by(_Ap2.start).limit(30))]
    stats["planned"] = len(stats["planned_list"])
    from .models import Task
    _tasks = db.scalars(tasks_svc.visible_query(user).where(Task.patient_id == p_.id).order_by(Task.status != "open", Task.due_date.is_(None), Task.due_date, Task.id.desc()).limit(60)).all()
    stats["tasks_open"] = sum(1 for t_ in _tasks if t_.status == "open")
    mats, closed_, wdocs = None, False, []
    if c["a"]:
        from .services import techcards as _tc
        from .models import StockDoc
        _tc.seed(db, c["a"])
        mats, closed_ = _tc.materials(db, c["a"]), _tc.closed(db, c["a"])
        c["mat_editing"] = c["a"].id in _tc.editing(db)
        from .models import Appointment as _Ap
        _d0 = datetime.combine(c["a"].start.date(), datetime.min.time())
        c["same_day"] = [x.id for x in db.scalars(select(_Ap).where(_Ap.patient_id == c["a"].patient_id, _Ap.start >= _d0,
                                                                  _Ap.start < _d0 + timedelta(days=1), _Ap.id != c["a"].id,
                                                                  _Ap.sale_id.is_(None), _Ap.status.notin_(("cancelled", "no_show"))))]
        wdocs = db.scalars(select(StockDoc).where(StockDoc.ext_no == f"visit:{c['a'].id}", StockDoc.status == "posted")).all()
    sale_ = None
    if c["a"] and c["a"].sale_id:
        from .models import Sale as _Sl
        _s = db.get(_Sl, c["a"].sale_id)
        if _s:
            sale_ = {"id": _s.id, "status": _s.status, "services": round(sum(float(i.total or 0) + float(i.discount or 0) for i in _s.items if i.kind == "service"), 2),
                     "products": round(sum(float(i.total or 0) + float(i.discount or 0) for i in _s.items if i.kind == "product"), 2),
                     "discount": round(float(_s.discount or 0) + sum(float(i.discount or 0) for i in _s.items), 2), "total": round(float(_s.total or 0), 2)}
    return templates.TemplateResponse(request, "_visit_drawer.html", {"request": request, "user": user, "c": c, "pt": p_, "stats": stats, "sale": sale_,
                                      "tab": tab, "STATUSES": _vstat_menu(db, c["a"].status if c.get("a") else None), "COLORS": _vstat_colors(db), "can": can,
                                      "color": spec_svc.color, "initials": spec_svc.initials, "mats": mats, "closed": closed_,
                                      "wdocs": wdocs, "WH": catalog.WAREHOUSES, "cal_docs": _cal_docs, "dnames": _docn, "ptasks": _tasks, "PRIORITY": tasks_svc.PRIORITY,
                                      "popts": _product_options(db, consumables=True) if mats is not None and not closed_ else []})


# ---------------- Витратні візиту ----------------
def _appt_editable(db: Session, user: User, aid: int):
    from .models import Appointment
    from .services import techcards as tc
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    _patient_or_403(db, user, a.patient_id)
    if tc.closed(db, a):
        raise HTTPException(409, "Візит закрито — матеріали вже списано зі складу")
    return a


@app.post("/appointments/{aid:int}/materials")
async def visit_material_add(aid: int, request: Request, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .models import VisitMaterial, CbService, TechCard
    from .services import techcards as tc
    a = _appt_editable(db, user, aid)
    f = await request.json()
    pr = _pick_product(db, str(f.get("product") or ""))
    if not pr:
        return JSONResponse({"error": "Товар не знайдено — оберіть зі списку"}, status_code=400)
    idx = int(f.get("svc_idx") or 0)
    svc = (a.services or [])[idx] if idx < len(a.services or []) else {}
    sv = db.get(CbService, svc.get("id")) if svc.get("id") else None
    card = db.get(TechCard, sv.techcard_id) if sv and sv.techcard_id else None
    cat = str(f.get("cat") or "").strip() or tc.WH_CAT.get(pr.warehouse or "single", "Одноразка")
    m = VisitMaterial(appt_id=a.id, svc_idx=idx, service_id=svc.get("id"), cat=cat[:80], product_id=pr.id,
                      qty=max(0.0, _num(str(f.get("qty") or "1").replace(",", ".")) or 1),
                      unit_cost=tc.unit_costs(db).get(pr.id, 0.0), warehouse=tc.cat_wh(card, cat, pr))
    db.add(m)
    db.commit()
    audit(db, "visit.material_add", "patient", a.patient_id, user=user, appt=a.id, product=pr.id, qty=m.qty)
    return JSONResponse({"ok": True})


@app.post("/appointments/{aid:int}/materials/{mid:int}")
async def visit_material_edit(aid: int, mid: int, request: Request, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .models import VisitMaterial
    a = _appt_editable(db, user, aid)
    m = db.get(VisitMaterial, mid)
    if not m or m.appt_id != a.id:
        raise HTTPException(404)
    f = await request.json()
    if f.get("delete"):
        db.delete(m)
        audit(db, "visit.material_delete", "patient", a.patient_id, user=user, appt=a.id, product=m.product_id)
    else:
        _oq = m.qty
        m.qty = max(0.0, round(_num(str(f.get("qty") or "0").replace(",", ".")) or 0, 3))
        if abs((_oq or 0) - m.qty) > 1e-9:
            audit(db, "visit.material_qty", "patient", a.patient_id, user=user, appt=a.id, product=m.product_id, frm=_oq, qty=m.qty)
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/appointments/{aid:int}/materials-reset")
def visit_material_reset(aid: int, svc_idx: int = Form(...), user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Повернути витратні послуги до техкарти."""
    from .models import VisitMaterial
    from .services import techcards as tc
    a = _appt_editable(db, user, aid)
    for m in db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id, VisitMaterial.svc_idx == svc_idx)):
        db.delete(m)
    db.commit()
    tc.seed(db, a)
    audit(db, "visit.material_reset", "patient", a.patient_id, user=user, appt=a.id, svc=svc_idx)
    return JSONResponse({"ok": True})


@app.post("/appointments/{aid:int}/materials-unlock")
def visit_material_unlock(aid: int, del_password: str = Form(""), user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Оплачений візит: відкрити витратні для виправлення (без повернення чека). Потрібен пароль для видалення:
    списання скасовується, матеріали повертаються на склад, після правок — «Списати зі складу»."""
    from .models import Appointment
    from .services import techcards as tc
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    _patient_or_403(db, user, a.patient_id)
    back = f"/calendar?start={a.start.strftime('%Y-%m-%d')}&open={a.id}"
    err = _delpw(db, user, del_password, "visit_materials_unlock", "appointment", a.id)
    if err:
        return RedirectResponse(_with_err(back, err), 303)
    if getattr(a, "historic", False):
        return RedirectResponse(_with_err(back, "Візит з історії CleverBox — витратні не змінюються"), 303)
    tc.set_editing(db, a.id, True)
    n = tc.reopen(db, a)
    audit(db, "visit.materials_unlock", "patient", a.patient_id, user=user, appt=a.id, docs=n)
    return RedirectResponse(back + "&tab=cost", 303)


@app.post("/appointments/{aid:int}/materials-close")
def visit_material_close(aid: int, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Після виправлення: списати витратні зі складу знову (лише якщо чек візиту оплачено)."""
    from .models import Appointment, Sale
    from .services import techcards as tc
    a = db.get(Appointment, aid) or (_ for _ in ()).throw(HTTPException(404))
    _patient_or_403(db, user, a.patient_id)
    back = f"/calendar?start={a.start.strftime('%Y-%m-%d')}&open={a.id}"
    tc.set_editing(db, a.id, False)
    s_ = db.get(Sale, a.sale_id) if a.sale_id else None
    docs = tc.close(db, a, user.id) if s_ and s_.status == "paid" else []
    audit(db, "visit.materials_close", "patient", a.patient_id, user=user, appt=a.id, docs=len(docs))
    return RedirectResponse(back + "&tab=cost", 303)


@app.get("/settings/reply-templates", response_class=HTMLResponse)
def settings_reply_templates(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import reply_templates as rt
    rows = rt.all_(db)
    return render(request, "reply_templates.html", nav="settings", rows=rows, VARS=rt.VARS, CHANNELS=rt.CHANNELS,
                  cats=sorted({r["cat"] for r in rows if r["cat"]}))


@app.post("/settings/reply-templates")
async def settings_reply_templates_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import reply_templates as rt
    f = await request.form()
    ids, titles, cats, texts = f.getlist("id"), f.getlist("title"), f.getlist("cat"), f.getlist("text")
    rows = [{"id": ids[i] if i < len(ids) else "", "title": titles[i], "cat": cats[i] if i < len(cats) else "", "text": texts[i] if i < len(texts) else "",
             "channels": f.getlist(f"ch_{i}")} for i in range(len(titles))]
    rt.save(db, rows)
    audit(db, "reply_templates.save", user=user, count=len(rows))
    return RedirectResponse(_with_msg("/settings/reply-templates", f"Збережено шаблонів: {len([r for r in rows if r['text'].strip()])}"), 303)


@app.get("/settings/shared-materials", response_class=HTMLResponse)
def settings_shared_materials(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import Product
    from .services import techcards as tc
    cfg = tc.shared_cfg(db)
    prods = db.scalars(select(Product).where(Product.warehouse == "single", Product.active.isnot(False)).order_by(Product.name)).all()
    return render(request, "shared_materials.html", nav="settings", cfg=cfg, prods=prods, sel=set(cfg["products"]), sug=tc.suggested_shared(db))


@app.post("/settings/shared-materials")
async def settings_shared_materials_save(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import techcards as tc
    f = await request.form()
    ids = [int(x) for x in f.getlist("p") if str(x).isdigit()]
    tc.save_shared(db, f.get("on") == "1", ids)
    audit(db, "settings.shared_materials", user=user, on=f.get("on") == "1", n=len(ids))
    return RedirectResponse(_with_msg("/settings/shared-materials", "Збережено"), 303)


# ---------------- Техкарти ----------------
@app.get("/techcards", response_class=HTMLResponse)
def techcards_page(request: Request, q: str = "", open: int = 0, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import TechCard, CbService
    from .services import techcards as tc
    cards = db.scalars(select(TechCard).order_by(TechCard.name)).all()
    used: dict = defaultdict(list)
    for sv in db.scalars(select(CbService).where(CbService.techcard_id.is_not(None))):
        used[sv.techcard_id].append(sv)
    if q.strip():
        ql = q.strip().lower()
        cards = [c for c in cards if ql in c.name.lower() or ql in (c.category or "").lower()
                 or any(ql in sv.name.lower() for sv in used.get(c.id, []))]
    uc = tc.unit_costs(db)
    cur = db.get(TechCard, open) if open else None
    services = db.scalars(select(CbService).order_by(CbService.category, CbService.name)).all()
    return render(request, "techcards.html", nav="stock", cards=cards, used=used, uc=uc, tc=tc, cur=cur, q=q, services=services,
                  products=_product_options(db, consumables=True), WAREHOUSES=catalog.WAREHOUSES,
                  cats=sorted({c.category for c in db.scalars(select(TechCard)) if c.category} | {s_.category for s_ in services if s_.category}))


@app.post("/techcards")
async def techcard_save(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import TechCard, TechCardLine, CbService
    f = await request.form()
    cid = int(f.get("id")) if str(f.get("id") or "").isdigit() else None
    card = db.get(TechCard, cid) if cid else TechCard()
    name = str(f.get("name") or "").strip()[:200]
    if not name:
        return RedirectResponse(_with_msg("/techcards", "Вкажіть назву техкарти", True), 303)
    card.name, card.category = name, str(f.get("category") or "").strip()[:200] or None
    card.max_cost = _num(str(f.get("max_cost") or "").replace(",", ".")) or None
    card.mode = "simple" if f.get("mode") == "simple" else "extended"
    card.comment = str(f.get("comment") or "").strip()[:2000] or None
    cats = []
    for nm, wh in zip(f.getlist("cat_name"), f.getlist("cat_wh")):
        nm = str(nm).strip()[:80]
        if nm and not any(c["name"] == nm for c in cats):
            cats.append({"name": nm, "wh": wh if wh in catalog.WAREHOUSES else "single"})
    card.cats = cats or None
    lines = []
    for cat, prod, qty in zip(f.getlist("ln_cat"), f.getlist("ln_product"), f.getlist("ln_qty")):
        pr = _pick_product(db, str(prod))
        if pr:
            lines.append(TechCardLine(cat=str(cat)[:80] or (cats[0]["name"] if cats else "Одноразка"), product_id=pr.id,
                                      qty=_num(str(qty).replace(",", ".")) or 1))
    card.lines = lines
    if not cid:
        db.add(card)
    db.flush()
    ids = {int(x) for x in f.getlist("services") if str(x).isdigit()}
    for sv in db.scalars(select(CbService).where((CbService.techcard_id == card.id) | (CbService.id.in_(ids or {0})))):
        sv.techcard_id = card.id if sv.id in ids else None
    db.commit()
    audit(db, "techcard.save", user=user, card=card.id, name=card.name, lines=len(lines), services=len(ids))
    return RedirectResponse(_with_msg(f"/techcards?open={card.id}", f"Техкарту «{card.name}» збережено"), 303)


@app.get("/techcards/import-cb", response_class=HTMLResponse)
def techcards_import_page(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    st = db.get(SyncState, "techcards_cb_import")
    return render(request, "techcards_import.html", nav="stock", last=st.value if st else None)


@app.post("/techcards/import-cb")
async def techcards_import(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import techcards as tc
    try:
        data = await request.json()
        assert isinstance(data, dict) and isinstance(data.get("cards"), list)
    except Exception:
        return JSONResponse({"error": "Невірний формат даних"}, 400)
    rep = tc.import_cb(db, data)
    audit(db, "techcards_import_cb", "techcard", None, user=user, **{k: v for k, v in rep.items() if k != "services_missing"})
    db.commit()
    return rep


@app.post("/techcards/{cid:int}/delete")
def techcard_delete(cid: int, del_password: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import TechCard, CbService
    err = _delpw(db, user, del_password, "techcard", None, cid)
    if err:
        return RedirectResponse(_with_err(f"/techcards?open={cid}", err), 303)
    c = db.get(TechCard, cid)
    if c:
        for sv in db.scalars(select(CbService).where(CbService.techcard_id == cid)):
            sv.techcard_id = None
        db.delete(c)
        db.commit()
        audit(db, "techcard.delete", user=user, card=cid)
    return RedirectResponse(_with_msg("/techcards", "Техкарту видалено"), 303)


# ---------------- Послуги (прайс) ----------------
@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request, q: str = "", cat: str = "", user: User = Depends(require("products|stock")), db: Session = Depends(db_dep)):
    from .models import CbService, TechCard
    from .services.service_costs import techcard_costs
    rows = db.scalars(select(CbService).order_by(CbService.category, CbService.name)).all()
    cats = sorted({r.category for r in rows if r.category})
    if cat:
        rows = [r for r in rows if r.category == cat]
    if q.strip():
        ql = q.strip().lower()
        rows = [r for r in rows if ql in r.name.lower()]
    return render(request, "services.html", nav="products", rows=rows, cats=cats, cat=cat, q=q, cost=techcard_costs(db),
                  cards=db.scalars(select(TechCard).order_by(TechCard.name)).all())


SVC_TABS = {"about": "Про послугу", "price": "Ціна й маржа", "staff": "Персонал", "history": "Історія"}


@app.get("/services/{sid:int}", response_class=HTMLResponse)
def service_card_page(request: Request, sid: int, tab: str = "about", np: str = "", on: str = "", doc: str = "",
                      user: User = Depends(require("products|stock")), db: Session = Depends(db_dep)):
    from .models import CbService, TechCard
    from .services import service_card as sc
    sv = db.get(CbService, sid)
    if not sv:
        raise HTTPException(404, "Послугу не знайдено")
    tab = tab if tab in SVC_TABS else "about"
    allrows = db.scalars(select(CbService)).all()
    pl = sc.planner(db, sv, _day(on) if on else None, _num(np.replace(",", ".")) if np.strip() else None, int(doc) if doc.isdigit() else None)
    users = {u.id: u.name for u in db.scalars(select(User))}
    return render(request, "service_card.html", nav="products", sv=sv, tab=tab, TABS=SVC_TABS, pl=pl, COLORS=sc.COLORS,
                  FIELDS=sc.FIELDS, hist=sc.history(db, sid), users=users,
                  doctors=db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all(),
                  cards=db.scalars(select(TechCard).order_by(TechCard.name)).all(),
                  cats=sorted({r.category for r in allrows if r.category}), dirs=sorted({r.direction for r in allrows if r.direction}))


@app.post("/services/{sid:int}/card")
async def service_card_save(request: Request, sid: int, user: User = Depends(require("products|stock")), db: Session = Depends(db_dep)):
    from .models import CbService
    from .services import service_card as sc
    sv = db.get(CbService, sid)
    if not sv:
        raise HTTPException(404)
    f = await request.form()
    tab = str(f.get("tab") or "about")
    g = lambda k: (str(f.get(k) or "").strip())  # noqa: E731
    if tab == "about":
        from .models import TechCard as _TC
        _tcn = lambda i: (db.get(_TC, i).name if i and db.get(_TC, i) else None)  # noqa: E731
        before = {"name": sv.name, "duration": sv.duration or sv.time_min, "techcard": _tcn(sv.techcard_id), "category": sv.category}
        sv.name = " ".join(g("name").split())[:300] or sv.name
        sv.sms_name, sv.check_name = g("sms_name")[:300] or None, g("check_name")[:300] or None
        sv.direction, sv.category = g("direction")[:200] or None, g("category")[:200] or None
        d_ = int(g("duration")) if g("duration").isdigit() else None
        sv.duration = d_
        if d_:
            sv.time_min = d_
        sv.repeat_days = int(g("repeat_days")) if g("repeat_days").isdigit() else None
        sv.techcard_id = int(g("techcard_id")) if g("techcard_id").isdigit() else None
        sv.prepay = _num(g("prepay").replace(",", ".")) if g("prepay") else None
        sv.color = g("color")[:10] or None
        sv.online, sv.online_name = bool(f.get("online")), g("online_name")[:300] or None
        sv.allow_price_change = bool(f.get("allow_price_change"))
        sv.active = bool(f.get("active"))
        sv.description, sv.specialist_note = g("description")[:5000] or None, g("specialist_note")[:10000] or None
        after = {"name": sv.name, "duration": sv.duration or sv.time_min, "techcard": _tcn(sv.techcard_id), "category": sv.category}
        for k in before:
            if before[k] != after[k]:
                sc.log(db, sv.id, k, before[k], after[k], user.id, commit=False)
    elif tab == "staff":
        sv.doctor_ids = sorted({int(x) for x in f.getlist("doctor") if str(x).isdigit()})
    elif tab == "price":
        np_ = _num(g("price").replace(",", ".")) if g("price") else None
        day = _day(g("price_set_on")) if g("price_set_on") else None
        if np_ is not None and np_ != sv.price:
            sc.log(db, sv.id, "price", sv.price, np_, user.id, commit=False)
            sv.price, sv.price_locked = np_, True
            day = day or date.today()
        if day and day != sv.price_set_on:
            sc.log(db, sv.id, "price_set_on", sv.price_set_on.strftime("%d.%m.%Y") if sv.price_set_on else None, day.strftime("%d.%m.%Y"), user.id, commit=False)
            sv.price_set_on = day
        if "zp_fixed" in f:
            zf = _num(g("zp_fixed").replace(",", ".")) if g("zp_fixed") else None
            if zf != sv.zp_fixed:
                sc.log(db, sv.id, "zp", sv.zp_fixed, zf, user.id, commit=False)
                sv.zp_fixed = zf
        if f.get("unlock"):
            sv.price_locked = False
            if sv.cb_price is not None and sv.cb_price != sv.price:
                sc.log(db, sv.id, "price", sv.price, sv.cb_price, user.id, commit=False)
                sv.price = sv.cb_price
    db.commit()
    audit(db, "service.card", user=user, service=sv.id, tab=tab)
    return RedirectResponse(_with_msg(f"/services/{sv.id}?tab={tab}", "Збережено"), 303)


@app.post("/services")
def service_save(id: str = Form(""), name: str = Form(...), category: str = Form(""), price: str = Form(""), duration: str = Form(""),
                 techcard_id: str = Form(""), active: str = Form(""), back: str = Form("/services"),
                 user: User = Depends(require("products|stock")), db: Session = Depends(db_dep)):
    from .models import CbService
    from .services import service_card as _sc
    sv = db.get(CbService, int(id)) if id.isdigit() else None
    created = sv is None
    if not sv:
        nid = max(9_000_000, (db.scalar(select(func.max(CbService.id))) or 0) + 1)   # послуги CRM — окремий діапазон ID
        sv = CbService(id=nid, name=name.strip()[:300], source="crm")
        db.add(sv)
        db.flush()
        _sc.log(db, sv.id, "created", None, sv.name, user.id, commit=False)
    old_price = sv.price
    sv.name = name.strip()[:300] or sv.name
    sv.category = category.strip()[:200] or None
    sv.price = _num(price.replace(",", ".")) if price.strip() else sv.price
    d_ = int(duration) if duration.strip().isdigit() else None
    sv.duration = d_
    if d_:
        sv.time_min = d_
    sv.techcard_id = int(techcard_id) if techcard_id.isdigit() else None
    sv.active = active != "0"
    if not created and (old_price or 0) != (sv.price or 0):
        _sc.log(db, sv.id, "price", old_price, sv.price, user.id, commit=False)
        sv.price_locked, sv.price_set_on = True, date.today()
    elif created and sv.price:
        sv.price_locked, sv.price_set_on = True, date.today()
    db.commit()
    audit(db, "service.save", user=user, service=sv.id, name=sv.name, price=sv.price, techcard=sv.techcard_id)
    if created:
        return RedirectResponse(_with_msg(f"/services/{sv.id}", f"Послугу «{sv.name}» створено"), 303)
    return RedirectResponse(_with_msg(back if back.startswith("/services") else "/services", f"Послугу «{sv.name}» збережено"), 303)


@app.post("/calendar/visit/note")
async def calendar_visit_note(request: Request, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .models import VisitNote
    f = await request.json()
    key, text = str(f.get("key") or "")[:40], str(f.get("text") or "").strip()[:4000]
    if not text or not (key.startswith("appt:") or key.startswith("cb:")) or not key.split(":")[1].isdigit():
        return JSONResponse({"error": "Порожня нотатка"}, status_code=400)
    from .services import visit_card as vc
    kid = int(key.split(":")[1])
    c = vc.card(db, kid if key.startswith("appt:") else 0, [kid] if key.startswith("cb:") else [])
    if not c:
        raise HTTPException(404)
    _patient_or_403(db, user, c["p"].id)
    db.add(VisitNote(key=key, patient_id=c["p"].id, text=text, user_id=user.id))
    db.commit()
    audit(db, "visit.note", "patient", c["p"].id, user=user, visit=key)
    return JSONResponse({"ok": True})


@app.post("/calendar/blocks")
def calendar_block_add(doctor_id: int = Form(...), day: str = Form(...), t_from: str = Form(...), t_to: str = Form(...),
                       title: str = Form(""), user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    """Заблокувати час спеціаліста (зйомка, навчання, обід)."""
    from .models import TimeBlock
    d_ = _day(day) or date.today()
    try:
        a_ = datetime.combine(d_, datetime.strptime(t_from, "%H:%M").time())
        b_ = datetime.combine(d_, datetime.strptime(t_to, "%H:%M").time())
    except ValueError:
        return RedirectResponse(f"/calendar?start={d_}&msg=" + _q("Вкажіть час у форматі 10:00"), 303)
    if b_ <= a_:
        return RedirectResponse(f"/calendar?start={d_}&msg=" + _q("Кінець має бути пізніше за початок"), 303)
    tb = TimeBlock(doctor_id=doctor_id, start=a_, end=b_, title=(title.strip() or "Заблоковано")[:120], created_by=user.id)
    db.add(tb)
    db.commit()
    audit(db, "calendar.block", "doctor", doctor_id, user=user, name=tb.title)
    return RedirectResponse(f"/calendar?start={d_}&msg=" + _q(f"Час заблоковано: {tb.title}"), 303)


@app.post("/calendar/blocks/{bid:int}/delete")
def calendar_block_delete(bid: int, user: User = Depends(require("book")), db: Session = Depends(db_dep)):
    from .models import TimeBlock
    tb = db.get(TimeBlock, bid) or (_ for _ in ()).throw(HTTPException(404))
    d_ = tb.start.date()
    db.delete(tb)
    db.commit()
    audit(db, "calendar.unblock", "doctor", tb.doctor_id, user=user, name=tb.title)
    return RedirectResponse(f"/calendar?start={d_}&msg=" + _q("Блокування знято"), 303)


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


PRODUCT_FILTERS = {
    "low": "Закінчуються", "out": "Немає в наявності", "expiring": "Термін ≤ 90 дн", "expired": "Прострочені",
    "prescribed": "Призначені в планах", "selling": "Продаються (90 дн)", "nosales": "Без продажів 90 дн",
    "lowmargin": "Маржа < 30%", "nocost": "Без ціни закупівлі", "nobrand": "Без бренду",
}
PRODUCT_SORTS = {"brand": "Бренд, назва", "stock": "Залишок ↑", "expiry": "Термін ↑", "margin": "Маржа ↓",
                 "sold": "Продажі 90 дн ↓", "revenue": "Виручка з планів ↓", "value": "Вартість залишку ↓"}


def _product_flags(r) -> set[str]:
    f = set()
    if r.stock is not None and r.stock <= 0:
        f.add("out")
    if r.stock is not None and (r.deficit or (r.product.min_stock and 0 < r.stock <= r.product.min_stock)):
        f.add("low")
    d = r.days_to_expiry
    if d is not None and d < 0:
        f.add("expired")
    elif d is not None and d <= 90:
        f.add("expiring")
    if r.prescribed:
        f.add("prescribed")
    f.add("selling" if r.sold_90 else "nosales")
    if r.margin_pct is not None and r.margin_pct < 30:
        f.add("lowmargin")
    if not r.cost:
        f.add("nocost")
    if not r.product.brand:
        f.add("nobrand")
    return f


PRODUCT_XL_COLS = [   # key, назва, за замовчуванням
    ("brand", "Бренд", True), ("name", "Засіб", True), ("sku", "Артикул", True), ("barcode", "Штрихкод", False),
    ("warehouse", "Склад", False), ("supplier", "Постачальник", True), ("cost", "Закупівля (остання), ₴", True),
    ("cost_date", "Дата закупівлі", False), ("cost_avg", "Закупівля середня, ₴", False), ("price", "Ціна продажу, ₴", True),
    ("margin_pct", "Маржа, %", True), ("markup", "Націнка, %", True), ("stock", "Залишок", True), ("min_stock", "Мін. залишок", False),
    ("days_left", "Вистачить, дн", False), ("expiry", "Термін придатності", False), ("stock_value", "Вартість залишку, ₴", True),
    ("sold_90", "Продано за 90 дн, шт", False), ("sold_90_sum", "Продано за 90 дн, ₴", False), ("prescribed", "Призначено в планах", False),
    ("cost_fx", "Закупівля у валюті", False), ("cost_currency", "Валюта", False), ("rrp_fx", "РРЦ (вал.)", False),
    ("planned_price", "Рекомендована ціна", False), ("unit", "Одиниця", False), ("category", "Категорія", False),
]


def _product_xl_val(r, k):
    p_ = r.product
    return {"brand": p_.brand, "name": p_.name, "sku": p_.sku, "barcode": p_.barcode,
            "warehouse": catalog.WAREHOUSES.get(p_.warehouse or "vitrina"), "supplier": p_.supplier or r.last_supplier,
            "cost": r.cost, "cost_date": r.last_bought, "cost_avg": r.cost_avg, "price": r.sale_price, "margin_pct": r.margin_pct,
            "markup": r.margin, "stock": r.stock, "min_stock": p_.min_stock, "days_left": r.days_left, "expiry": r.expiry,
            "stock_value": round(r.stock_value, 2) if r.stock_value else None, "sold_90": r.sold_90 or None,
            "sold_90_sum": round(r.sold_90_sum, 2) if r.sold_90_sum else None, "prescribed": r.prescribed or None,
            "cost_fx": p_.cost_fx, "cost_currency": p_.cost_currency, "rrp_fx": p_.rrp_fx, "planned_price": p_.planned_price,
            "unit": p_.unit, "category": p_.category}.get(k)


@app.get("/products/labels", response_class=HTMLResponse)
def products_labels(request: Request, w: int = 30, h: int = 20, show: str = "name,price", auto: int = 1,
                    user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    """Сторінка друку цінників: кожен цінник — окрема «сторінка» w×h мм (термопринтер етикеток) з обраними елементами."""
    from .services import labels as lb
    qp = request.query_params
    ids = [int(x) for x in qp.getlist("ids") if x.isdigit()]
    prods = {p_.id: p_ for p_ in db.scalars(select(Product).where(Product.id.in_(ids)))} if ids else {}
    w, h = max(15, min(w, 150)), max(10, min(h, 150))
    els = {x for x in show.split(",") if x in ("name", "sku", "barcode", "price")} or {"name"}
    items = []
    for i in ids:
        p_ = prods.get(i)
        if not p_:
            continue
        try:
            n = max(0, min(int(qp.get(f"q_{i}") or 1), 200))
        except ValueError:
            n = 1
        code = p_.barcode or p_.sku or ""
        items += [{"name": p_.short_name or p_.name, "sku": p_.sku, "price": p_.price or p_.price_manual,
                   "bc": lb.barcode_svg(code) if "barcode" in els and code else "", "code": code}] * n
    audit(db, "products.labels", user=user, n=len(items))
    return templates.TemplateResponse(request, "labels_print.html", {"request": request, "items": items, "w": w, "h": h, "els": els, "auto": auto})


@app.get("/products.xlsx")
def products_xlsx(request: Request, q: str = "", brand: str = "", supplier: str = "", f: str = "", sort: str = "brand",
                  wh: str = "vitrina", user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    """Excel товарів: рядки — за фільтрами сторінки або лише обрані (ids), стовпчики — обрані (cols)."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    ids = {int(x) for x in request.query_params.getlist("ids") if x.isdigit()}
    cols = [c for c in PRODUCT_XL_COLS if c[0] in set(request.query_params.getlist("cols"))] or [c for c in PRODUCT_XL_COLS if c[2]]
    sts = all_statuses(db)
    rows = [r for r in catalog.product_rows(db, sts) if r.product.active is not False]
    if ids:
        rows = [r for r in rows if r.product.id in ids]
    else:
        wh = wh if wh in catalog.WAREHOUSES or wh == "all" else "vitrina"
        if wh != "all":
            rows = [r for r in rows if (r.product.warehouse or "vitrina") == wh]
        if q:
            rows = [r for r in rows if catalog.product_match(r.product, q) is not None]
        if brand:
            rows = [r for r in rows if (r.product.brand or "") == ("" if brand == "—" else brand)]
        if supplier:
            rows = [r for r in rows if (r.product.supplier or r.last_supplier or "") == supplier]
        if f in PRODUCT_FILTERS:
            rows = [r for r in rows if f in _product_flags(r)]
    keyf = {"stock": lambda r: (r.stock if r.stock is not None else 1e9), "expiry": lambda r: (r.expiry or date(2999, 1, 1)),
            "margin": lambda r: -(r.margin_pct if r.margin_pct is not None else -1e9), "sold": lambda r: -r.sold_90, "revenue": lambda r: -r.revenue,
            "value": lambda r: -r.stock_value}.get(sort) or (lambda r: ((r.product.brand or "яяя").lower(), r.product.name.lower()))
    rows = sorted(rows, key=keyf)
    wb = Workbook()
    ws = wb.active
    ws.title = "Товари"
    ws.append([c[1] for c in cols])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="3B3050")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    import re as _re
    for r in rows:
        ws.append([(_re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", v) if isinstance(v, str) else v) for v in (_product_xl_val(r, c[0]) for c in cols)])
    for i, c in enumerate(cols):
        ws.column_dimensions[ws.cell(row=1, column=i + 1).column_letter].width = 46 if c[0] == "name" else (18 if c[0] in ("brand", "supplier") else 14)
    ws.freeze_panes = "C2" if len(cols) > 2 else "A2"
    buf = io.BytesIO()
    wb.save(buf)
    audit(db, "products.export", user=user, rows=len(rows), cols=len(cols))
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=tovary-{date.today()}.xlsx"})


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request, tab: str = "care", q: str = "", brand: str = "", supplier: str = "",
                  f: str = "", sort: str = "brand", wh: str = "vitrina", user: User = Depends(require("products")),
                  db: Session = Depends(db_dep)):
    sts = all_statuses(db)
    if tab == "procedures":
        return render(request, "products.html", tab=tab, procedures=catalog.procedure_rows(sts))
    from .models import ProductPurchase
    every = [r for r in catalog.product_rows(db, sts) if r.product.active is not False]
    wh = wh if wh in catalog.WAREHOUSES or wh == "all" else "vitrina"
    wh_counts = {k: 0 for k in catalog.WAREHOUSES}
    for r in every:
        wh_counts[r.product.warehouse or "vitrina"] = wh_counts.get(r.product.warehouse or "vitrina", 0) + 1
    rows = every if wh == "all" else [r for r in every if (r.product.warehouse or "vitrina") == wh]
    all_rows = rows
    counts = {k: 0 for k in PRODUCT_FILTERS}
    flags = {}
    for r in rows:
        flags[r.product.id] = _product_flags(r)
        for k in flags[r.product.id]:
            if k in counts:
                counts[k] += 1
    if q:
        rows = [r for r in rows if catalog.product_match(r.product, q) is not None]
    if brand:
        rows = [r for r in rows if (r.product.brand or "") == ("" if brand == "—" else brand)]
    if supplier:
        rows = [r for r in rows if (r.product.supplier or r.last_supplier or "") == supplier]
    if f in PRODUCT_FILTERS:
        rows = [r for r in rows if f in flags[r.product.id]]
    keyf = {"stock": lambda r: (r.stock if r.stock is not None else 1e9),
            "expiry": lambda r: (r.expiry or date(2999, 1, 1)),
            "margin": lambda r: -(r.margin_pct if r.margin_pct is not None else -1e9), "sold": lambda r: -r.sold_90,
            "revenue": lambda r: -r.revenue, "value": lambda r: -r.stock_value}.get(sort)
    if keyf:
        rows = sorted(rows, key=keyf)
    groups = catalog.brand_groups(rows) if sort == "brand" else [("", rows)]
    brands = sorted({r.product.brand for r in all_rows if r.product.brand}, key=str.lower)
    suppliers = sorted({x for r in all_rows for x in (r.product.supplier, r.last_supplier) if x}, key=str.lower)
    tot = dict(n=len(rows), stock=sum(r.stock or 0 for r in rows), value=sum(r.stock_value for r in rows),
               sold=sum(r.sold_90_sum for r in rows))
    supplier_rows = None
    if tab == "suppliers":
        agg: dict[str, dict] = {}
        for pp in db.scalars(select(ProductPurchase).order_by(ProductPurchase.bought_on)):
            a = agg.setdefault(pp.supplier or "Без постачальника", {"n": 0, "sum": 0.0, "last": None, "products": set()})
            a["n"] += 1; a["sum"] += (pp.qty or 0) * (pp.unit_cost or 0); a["last"] = pp.bought_on; a["products"].add(pp.product_id)
        for r in every:
            if r.product.supplier:
                agg.setdefault(r.product.supplier, {"n": 0, "sum": 0.0, "last": None, "products": set()})["products"].add(r.product.id)
        supplier_rows = sorted(agg.items(), key=lambda x: x[0].lower())
    return render(request, "products.html", XL_COLS=PRODUCT_XL_COLS, dirs=catalog.product_dirs(db), tab=tab, groups=groups, flags=flags, counts=counts, FILTERS=PRODUCT_FILTERS,
                  SORTS=PRODUCT_SORTS, q=q, brand=brand, supplier=supplier, f=f, sort=sort, brands=brands,
                  suppliers=suppliers, tot=tot, supplier_rows=supplier_rows, today=date.today(), wh=wh,
                  WAREHOUSES=catalog.WAREHOUSES, wh_counts=wh_counts, total_all=len(every),
                  imported=LAST_IMPORT.pop(f"products-{user.id}", None))


def _num(v: str):
    try:
        return float(v.replace(" ", "").replace(",", ".")) if v and v.strip() else None
    except ValueError:
        return None


def _day(v: str):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _exp_day(v) -> date | None:
    """Термін придатності з тексту: 2027-12-31 · 31.12.2027 · 31.12.27 · 12.2027 · 12/27 · 31122027 (місяць-рік → останній день)."""
    import calendar as _cal
    s = re.sub(r"[/\-\s,]+", ".", str(v or "").strip()).strip(".")
    if not s:
        return None
    pats = [(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", "ymd"), (r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", "dmy"), (r"(\d{1,2})\.(\d{2}|\d{4})", "my"),
            (r"(\d{2})(\d{2})(\d{4})", "dmy"), (r"(\d{2})(\d{2})(\d{2})", "dmy"), (r"(\d{2})(\d{2})", "my")]
    for rx, kind in pats:
        m_ = re.fullmatch(rx, s)
        if not m_:
            continue
        g = [int(x) for x in m_.groups()]
        y, mo, d_ = (g[0], g[1], g[2]) if kind == "ymd" else ((g[2], g[1], g[0]) if kind == "dmy" else (g[1], g[0], 0))
        y = y + 2000 if y < 100 else y
        if not (1 <= mo <= 12 and 2000 <= y <= 2100):
            return None
        last = _cal.monthrange(y, mo)[1]
        d_ = d_ or last
        return date(y, mo, d_) if 1 <= d_ <= last else None
    return None


@app.get("/products/{pid:int}", response_class=HTMLResponse)
def product_card(pid: int, request: Request, user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    from .models import ProductPurchase, ProductSale
    prod = db.get(Product, pid)
    if not prod:
        raise HTTPException(404)
    row = next(r for r in catalog.product_rows(db) if r.product.id == pid)
    purchases = db.scalars(select(ProductPurchase).where(ProductPurchase.product_id == pid)
                           .order_by(ProductPurchase.bought_on.desc(), ProductPurchase.id.desc())).all()
    from sqlalchemy import and_ as _and, or_ as _or
    _mine = _or(_and(ProductSale.external_id.like("crm:%"), ProductSale.product_id == pid),           # каса CRM
                _and(~ProductSale.external_id.like("crm:%"), ProductSale.product_id == (prod.cb_product_id or -1)))   # CleverBox
    sales = db.scalars(select(ProductSale).where(_mine).order_by(ProductSale.sold_at.desc()).limit(50)).all()
    items = db.scalars(select(PlanItem).where(PlanItem.product_id == pid)).all()
    plans = [(it, it.plan) for it in items if it.plan and it.plan.status != "cancelled"]
    # продажі по місяцях (6 міс) для міні-графіка
    months = []
    today = date.today()
    for k in range(5, -1, -1):
        m0 = (today.replace(day=1) - timedelta(days=31 * k)).replace(day=1)
        months.append([m0, 0])
    for (q, at) in db.execute(select(ProductSale.quantity, ProductSale.sold_at).where(
            _mine, ProductSale.sold_at >= datetime.combine(months[0][0], datetime.min.time()))):
        for mm in months:
            if (at.year, at.month) == (mm[0].year, mm[0].month):
                mm[1] += q or 0
    stock_rows = db.scalars(select(StockLevel).where(StockLevel.cb_product_id == prod.cb_product_id)).all() if prod.cb_product_id else []
    buys, buy_stats = catalog.purchase_history(prod, purchases)
    from .services import stock as _st
    from .models import StockMove as _SM
    ledger_on = _st.ledger_active(db)
    batches = _st.batches_view(db, pid) if ledger_on else []
    wh_tot: dict[str, float] = {}
    for b in batches:
        wh_tot[b["wh"]] = round(wh_tot.get(b["wh"], 0) + b["qty"], 4)
    out_moves = db.scalars(select(_SM).where(_SM.product_id == pid, _SM.qty < 0, _SM.kind.in_(("sale", "procedure", "writeoff")))
                           .order_by(_SM.at.desc()).limit(30)).all() if ledger_on else []
    batch_opts: dict[str, list] = {}
    for b in batches:
        batch_opts.setdefault(b["wh"], []).append(b["expiry"])
    for m in out_moves:                              # поточний термін руху теж має бути в списку
        if m.expiry_date not in batch_opts.setdefault(m.warehouse, []):
            batch_opts[m.warehouse].append(m.expiry_date)
    return render(request, "product.html", dirs=catalog.product_dirs(db), wh=prod.warehouse,
                  WAREHOUSES=catalog.WAREHOUSES, p=prod, r=row, purchases=purchases, buys=buys, buy_stats=buy_stats,
                  batches=batches, out_moves=out_moves, batch_opts=batch_opts, wh_tot=wh_tot, ledger_on=ledger_on, sales=sales, plans=plans, months=months,
                  m_max=max([m[1] for m in months] + [1]), stock_rows=stock_rows, today=today,
                  brands=sorted({x for (x,) in db.execute(select(Product.brand).where(Product.brand.is_not(None)).distinct())}),
                  suppliers=sorted({x for (x,) in db.execute(select(Product.supplier).where(Product.supplier.is_not(None)).distinct())}),
                  pfiles=db.scalars(select(ProductFile).where(ProductFile.product_id == pid).order_by(ProductFile.kind.desc(), ProductFile.uploaded_at.desc())).all())


# ---------------- Фото й документи товару ----------------
@app.get("/products/files/{fid:int}")
def product_file_get(fid: int, dl: int = 0, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    f = db.get(ProductFile, fid)
    if not f or not Path(f.path).exists():
        raise HTTPException(404, "Файл не знайдено")
    return FileResponse(f.path, media_type=f.mime or mimetypes.guess_type(f.path)[0] or "application/octet-stream",
                        filename=f.name if dl else None)


@app.post("/products/{pid:int}/files")
async def product_files_upload(pid: int, files: list[UploadFile] = File(...), user: User = Depends(require("products")),
                               db: Session = Depends(db_dep)):
    from .services import product_files as pfs
    p = db.get(Product, pid) or (_ for _ in ()).throw(HTTPException(404))
    n = 0
    for up in files:
        data = await up.read()
        if not data:
            continue
        nm = (up.filename or "file")[-200:]
        kind = "photo" if pfs.IMG.search(nm) or (up.content_type or "").startswith("image/") else "doc"
        f = ProductFile(product_id=pid, path=ingest.store(data, f"product_{nm}"), name=nm, kind=kind,
                        mime=(up.content_type or "")[:100] or None, uploaded_by=user.id)
        db.add(f)
        db.flush()
        if kind == "photo" and not p.photo_url:
            p.photo_url = pfs.photo_link(f)
        n += 1
    db.commit()
    audit(db, "product.files_upload", "product", pid, user=user, n=n)
    return RedirectResponse(_with_msg(f"/products/{pid}#pfiles", f"Додано файлів: {n}"), 303)


@app.post("/products/files/{fid:int}/main")
def product_file_main(fid: int, user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    from .services import product_files as pfs
    f = db.get(ProductFile, fid) or (_ for _ in ()).throw(HTTPException(404))
    p = db.get(Product, f.product_id)
    p.photo_url = pfs.photo_link(f)
    db.commit()
    return RedirectResponse(_with_msg(f"/products/{p.id}#pfiles", "Головне фото змінено"), 303)


@app.post("/products/files/{fid:int}/delete")
def product_file_delete(fid: int, del_password: str = Form(""), user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    from .services import product_files as pfs
    f = db.get(ProductFile, fid) or (_ for _ in ()).throw(HTTPException(404))
    pid = f.product_id
    err = _delpw(db, user, del_password, "product_file", "product", pid)
    if err:
        return RedirectResponse(_with_err(f"/products/{pid}#pfiles", err), 303)
    p = db.get(Product, pid)
    if p and p.photo_url == pfs.photo_link(f):
        p.photo_url = None
    shared = db.scalar(select(func.count()).select_from(ProductFile).where(ProductFile.path == f.path, ProductFile.id != f.id))
    if not shared:
        try:
            trash = Path(settings.files_dir) / "_trash" / datetime.now().strftime("%Y-%m")
            trash.mkdir(parents=True, exist_ok=True)
            shutil.move(f.path, str(trash / f"p{f.id}_{Path(f.path).name}"))
        except OSError:
            pass
    db.delete(f)
    db.commit()
    audit(db, "product.file_delete", "product", pid, user=user, file_id=fid)
    return RedirectResponse(_with_msg(f"/products/{pid}#pfiles", "Файл видалено"), 303)


@app.post("/products/files/import-cb")
async def product_files_import_cb(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    """Фото й документи товарів з файл-менеджера CleverBox (список зібрано у веб-кабінеті) → у фоні до карток товарів."""
    from .services import product_files as pfs
    data = await request.json()
    rows = data.get("rows") or []
    job = jobs.start("Фото й документи товарів з CleverBox", pfs.run, rows)
    audit(db, "products.files_import_cb", user=user, files=len(rows), job=job.id)
    db.commit()
    return {"ok": True, "job": job.id, "files": len(rows)}


@app.post("/products/new")
async def product_create(request: Request, user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    """Новий товар вручну (форма як у CleverBox) на обраний склад; початковий залишок — документом «Інвентаризація»."""
    f = await request.form()
    name = str(f.get("name") or "").strip()[:300]
    wh = str(f.get("warehouse") or "") if str(f.get("warehouse") or "") in catalog.WAREHOUSES else "vitrina"
    back = f"/products?tab=care&wh={wh}"
    if not name:
        return RedirectResponse(_with_msg(back, "Вкажіть назву товару", True), 303)
    dup = db.scalar(select(Product).where(Product.name_norm == norm(name), Product.warehouse == wh))
    if dup:
        return RedirectResponse(_with_msg(f"/products/{dup.id}", "Такий товар на цьому складі вже є — відкрито його картку", True), 303)
    p = Product(name=name, name_norm=norm(name), warehouse=wh, active=True)
    catalog.apply_form(p, f)
    p.cost = _num(str(f.get("cost") or "").replace(",", ".")) or None
    db.add(p)
    db.flush()
    q = _num(str(f.get("qty") or "").replace(",", "."))
    msg = f"Товар «{name}» додано на склад «{catalog.WAREHOUSES[p.warehouse or wh]}»"
    if q and q > 0:
        from .services import stock as _st
        if _st.ledger_active(db):
            doc = _st.new_doc(db, "inventory", user_id=user.id, wh_from=p.warehouse or wh, note=f"Початковий залишок нового товару «{name}»")
            doc.lines.append(_st.StockDocLine(product_id=p.id, qty=q, unit_cost=p.cost or 0))
            db.flush()
            _st.post(db, doc)
            msg += f", залишок {q:g}"
    db.commit()
    audit(db, "product.create", "product", p.id, user=user, warehouse=p.warehouse, qty=q or None)
    db.commit()
    return RedirectResponse(_with_msg(f"/products?tab=care&wh={p.warehouse or wh}", msg), 303)


@app.post("/products/{pid:int}/full")
async def product_full_save(pid: int, request: Request, user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    """Повна форма товару в картці (як у CleverBox)."""
    p = db.get(Product, pid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    if not str(f.get("name") or "").strip():
        return RedirectResponse(_with_msg(f"/products/{pid}", "Вкажіть назву товару", True), 303)
    catalog.apply_form(p, f)
    db.commit()
    audit(db, "product.update", "product", pid, user=user, full=True)
    db.commit()
    return RedirectResponse(_with_msg(f"/products/{pid}", "Товар збережено"), 303)


@app.post("/products/{pid:int}")
def product_update(pid: int, supply_days: str = Form(""), min_stock: str = Form(""), url: str = Form(""),
                   apply: str = Form(""), brand: str = Form(None), supplier: str = Form(None), sku: str = Form(None),
                   expiry_date: str = Form(None), price_manual: str = Form(None), back: str = Form(""),
                   warehouse: str = Form(None),
                   user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    prod = db.get(Product, pid) or (_ for _ in ()).throw(HTTPException(404))
    prod.supply_days = int(supply_days) if supply_days.strip().isdigit() else None
    prod.min_stock = int(min_stock) if min_stock.strip().isdigit() else None
    prod.url = url.strip() or None
    if brand is not None:
        prod.brand = brand.strip()[:120] or None
    if supplier is not None:
        prod.supplier = supplier.strip()[:200] or None
    if sku is not None:
        prod.sku = sku.strip()[:100] or None
    if expiry_date is not None:
        prod.expiry_date = _day(expiry_date)
    if price_manual is not None:
        prod.price_manual = _num(price_manual)
    if warehouse is not None and warehouse in catalog.WAREHOUSES:
        prod.warehouse = warehouse
    if prod.url:                                         # посилання потрапляє в засоби бюті-планів без власного
        for it in db.scalars(select(PlanItem).where(PlanItem.product_id == prod.id, PlanItem.url.is_(None))):
            it.url = prod.url
    n = 0
    if apply and prod.supply_days:
        for it in db.scalars(select(PlanItem).where(PlanItem.product_id == prod.id)):
            if it.plan.status == "active":
                it.supply_days = prod.supply_days
                n += 1
    db.commit()
    audit(db, "product.update", "product", prod.id, user=user, supply_days=prod.supply_days,
          min_stock=prod.min_stock, applied_to_items=n)
    return RedirectResponse(back if back.startswith("/products") else f"/products/{pid}?saved=1", 303)


@app.post("/products/{pid:int}/batches")
async def product_batches_set(pid: int, request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Ручне внесення термінів: розподіл залишку складу на кілька партій (термін + к-сть)."""
    from .services import stock as st
    f = await request.form()
    wh = f.get("wh") if f.get("wh") in catalog.WAREHOUSES else None
    if not wh or not st.ledger_active(db):
        return RedirectResponse(f"/products/{pid}?err=Облік+складу+ще+не+розпочато#batches", 303)
    prod = db.get(Product, pid)
    k = prod.coef if prod and prod.coef and prod.coef > 0 else 1
    targets = []
    for e, q in zip(f.getlist("exp"), f.getlist("qty")):
        qq = _num(q)
        if qq and qq > 0:
            targets.append((_exp_day(e), round(qq * (k if f.get("in_packs") else 1), 4)))
    try:
        rep = st.set_batches(db, pid, wh, targets, note=f"терміни вручну · {user.name}")
    except st.StockError as e:
        return RedirectResponse(f"/products/{pid}?err={_q(str(e))}#batches", 303)
    audit(db, "stock.batches_manual", "product", pid, user=user, wh=wh, batches=len(targets), changed=rep["changed"])
    return RedirectResponse(f"/products/{pid}?saved=1#batches", 303)


@app.post("/stock/moves/{mid:int}/batch")
def stock_move_batch(mid: int, expiry: str = Form(""), back: str = Form(""), user: User = Depends(require("stock")),
                     db: Session = Depends(db_dep)):
    """Який термін продали / списали — для автоматичних продажів CleverBox і процедур."""
    from .services import stock as st
    m = st.reassign_batch(db, mid, _day(expiry))
    if m:
        audit(db, "stock.batch", user=user, move=mid, expiry=expiry or None)
    return RedirectResponse(back if back.startswith("/") else f"/products/{m.product_id if m else ''}#batches", 303)


@app.post("/products/{pid:int}/purchases")
def product_purchase_add(pid: int, bought_on: str = Form(""), qty: str = Form("1"), unit_cost: str = Form(""),
                         supplier: str = Form(""), expiry_date: str = Form(""), doc_no: str = Form(""),
                         user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    from .models import ProductPurchase
    prod = db.get(Product, pid) or (_ for _ in ()).throw(HTTPException(404))
    q, c = _num(qty), _num(unit_cost)
    if not q or q <= 0 or c is None or c < 0:
        return RedirectResponse(f"/products/{pid}?err=Вкажіть+кількість+і+ціну+закупівлі#buy", 303)
    k = prod.coef if prod.coef and prod.coef > 0 else 1   # у формі — одиниці приходу, зберігаємо за одиницю обліку
    pp = ProductPurchase(product_id=pid, bought_on=_day(bought_on) or date.today(), qty=q * k, unit_cost=c / k,
                         supplier=supplier.strip()[:200] or prod.supplier, expiry_date=_day(expiry_date),
                         doc_no=doc_no.strip()[:100] or None)
    db.add(pp)
    if pp.supplier and not prod.supplier:
        prod.supplier = pp.supplier
    db.commit()
    audit(db, "product.purchase", "product", pid, user=user, qty=q, unit_cost=c)
    return RedirectResponse(f"/products/{pid}?saved=1#buy", 303)


@app.post("/products/{pid:int}/purchases/{ppid:int}/delete")
def product_purchase_delete(pid: int, ppid: int, del_password: str = Form(""), user: User = Depends(require("products")),
                            db: Session = Depends(db_dep)):
    from .models import ProductPurchase
    err = _delpw(db, user, del_password, "purchase", "product", pid)
    if err:
        return RedirectResponse(_with_err(f"/products/{pid}#buy", err), 303)
    pp = db.get(ProductPurchase, ppid)
    if pp and pp.product_id == pid:
        db.delete(pp)
        db.commit()
        audit(db, "product.purchase_delete", "product", pid, user=user, purchase=ppid)
    return RedirectResponse(f"/products/{pid}#buy", 303)


@app.post("/products/import")
async def products_import(file: UploadFile = File(...), user: User = Depends(require("import")), db: Session = Depends(db_dep)):
    """Excel-вивантаження товарів з CleverBox (Склад → Товари → Експорт): склади, бренди, постачальники, ціни, залишки."""
    import tempfile
    suffix = ".xls" if (file.filename or "").lower().endswith(".xls") else ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
    try:
        kind = await asyncio.to_thread(catalog.detect_export, tmp.name)
        if kind == "supply":                         # переплутали форму — поставки теж приймаємо
            from .services import stock as st_
            rep = await asyncio.to_thread(st_.import_supplier_invoices, db, tmp.name)
            if not rep.get("error"):
                rep = {"error": f"Це був файл «Поставки» — імпортовано в Склад → Постачальники ({rep['rows']} поставок)."}
        elif kind == "balances":                     # «Залишки по складах» — теж приймаємо
            b = await _import_balances_file(db, tmp.name)
            rep = {"error": f"Це був звіт «Залишки по складах» — {b['rows']} позицій завантажено, собівартість оновлено. "
                            "Далі: Склад → Залишки (старт обліку або звірка)."}
        elif kind != "products":
            rep = {"error": catalog.WRONG_FILE[kind]}
        else:
            rep = await asyncio.to_thread(catalog.import_products, db, tmp.name)
    except Exception as e:  # noqa: BLE001
        rep = {"error": f"Не вдалося прочитати файл: {type(e).__name__}"}
    finally:
        os.unlink(tmp.name)
    audit(db, "products.import", user=user, **{k: v for k, v in rep.items() if k != "by_warehouse"})
    LAST_IMPORT[f"products-{user.id}"] = rep
    return RedirectResponse("/products?wh=all", 303)


@app.post("/products/import-turnover")
async def products_import_turnover(file: UploadFile = File(...), d_from: str = Form(...), d_to: str = Form(...),
                                   user: User = Depends(require("import")), db: Session = Depends(db_dep)):
    """Звіт «Рух товарів» CleverBox за період → ціни закупівлі (середня за період, остання — з найсвіжішого періоду)."""
    import tempfile
    a, b = _day(d_from), _day(d_to)
    if not a or not b or a > b:
        LAST_IMPORT[f"products-{user.id}"] = {"error": "Вкажіть період звіту (з … по …)"}
        return RedirectResponse("/products?wh=all", 303)
    suffix = ".xls" if (file.filename or "").lower().endswith(".xls") else ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
    try:
        kind = await asyncio.to_thread(catalog.detect_export, tmp.name)
        if kind != "turnover":
            raise ValueError(catalog.WRONG_FILE[kind])
        rep = await asyncio.to_thread(catalog.import_turnover, db, tmp.name, a, b)
    except ValueError as e:
        rep = {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        rep = {"error": f"Не вдалося прочитати файл: {type(e).__name__}"}
    finally:
        os.unlink(tmp.name)
    rep["kind"] = "turnover"
    rep["period"] = f"{a:%d.%m.%Y}–{b:%d.%m.%Y}"
    audit(db, "products.import_turnover", user=user, period=rep["period"], matched=rep.get("matched"))
    LAST_IMPORT[f"products-{user.id}"] = rep
    return RedirectResponse("/products?wh=all", 303)


@app.post("/products/bulk")
async def products_bulk(request: Request, user: User = Depends(require("products")), db: Session = Depends(db_dep)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    field_, value = form.get("field"), (form.get("value") or "").strip()
    if field_ == "warehouse" and value not in catalog.WAREHOUSES:
        field_ = None
    if field_ in ("brand", "supplier", "warehouse") and ids:
        for p_ in db.scalars(select(Product).where(Product.id.in_(ids))):
            setattr(p_, field_, value[:200] or None)
        db.commit()
        audit(db, "product.bulk", user=user, field=field_, n=len(ids))
    back = str(form.get("back") or "/products")
    return RedirectResponse(back if back.startswith("/products") else "/products", 303)


STOCK_TABS = {"balances": "Залишки", "critical": "Критичні залишки", "supply": "Поставки", "order": "Замовлення",
              "transfer": "Переміщення", "writeoff": "Списання", "return": "Повернення", "inventory": "Інвентаризація",
              "suppliers": "Постачальники", "norms": "Норми списання", "moves": "Рух товарів"}
templates.env.globals["STOCK_TABS"] = STOCK_TABS
templates.env.globals["REPORT_TABS"] = __import__("app.services.reports", fromlist=["REPORTS"]).REPORTS


def _critical_rows(rows):
    from .services import stock as st
    return [r for r in rows if st.is_critical(r) or (r.deficit or 0) > 0]


# ---------------- Реалізація (товар під реалізацію: TEBISKIN, AG SKIN) ----------------
@app.get("/stock/consign", response_class=HTMLResponse)
def consign_page(request: Request, s: str = "", df: str = "", dt_: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    cs.ensure_setup(db)
    sups = cs.suppliers(db)
    sel = s if s in sups else (sups[0] if sups else "")
    v = cs.overview(db, sel) if sel else None
    draft = None
    a, b = _day(df), _day(request.query_params.get("dt") or dt_)
    if sel and a and b and a <= b:
        draft = cs.build_report(db, sel, a, b)
    rep = None
    if request.query_params.get("r"):
        from .models import ConsignReport
        rep = db.get(ConsignReport, int(request.query_params.get("r") or 0))
    products = cs.candidate_products(db, sel) if sel else []
    from .services import cashboxes as _cbx
    return render(request, "stock_consign.html", nav="stock", sups=sups, sel=sel, v=v, draft=draft, rep=rep, products=products,
                  boxes=[x["name"] for x in _cbx.items(db) if x.get("active") and not x.get("deleted")],
                  articles=[a for a in _fa().names(db, "expense") if "закуп" in a.lower()] or _fa().names(db, "expense"))


@app.get("/stock/consign/export")
def consign_export(s: str, df: str = "", dt: str = "", r: int = 0, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    from .models import ConsignReport
    if r:
        rep = db.get(ConsignReport, r)
        if not rep:
            raise HTTPException(404)
        data, title = {"lines": rep.lines or [], "total": rep.total}, f"Реалізація {rep.supplier} · {rep.d_from:%d.%m.%Y}–{rep.d_to:%d.%m.%Y}"
    else:
        a, b = _day(df), _day(dt)
        if not (a and b):
            raise HTTPException(400)
        data, title = cs.build_report(db, s, a, b), f"Реалізація {s} · {a:%d.%m.%Y}–{b:%d.%m.%Y}"
    fn = "realizatsiya.xlsx"
    return Response(cs.to_xlsx(data, title), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={fn}"})


@app.post("/stock/consign/report")
def consign_fix(s: str = Form(...), df: str = Form(...), dt: str = Form(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    a, b = _day(df), _day(dt)
    if not (a and b) or a > b:
        return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}", "Вкажіть період", err=True), 303)
    rep = cs.fix_report(db, s, a, b, user.id)
    audit(db, "consign.report", "consign_report", rep.id, user=user, supplier=s, total=rep.total)
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}&r={rep.id}", f"Звіт реалізації зафіксовано: {rep.total:,.2f} ₴".replace(",", " ")), 303)


@app.post("/stock/consign/report/{rid:int}/cancel")
def consign_cancel(rid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    from .models import ConsignReport
    rep = db.get(ConsignReport, rid)
    if not rep:
        raise HTTPException(404)
    try:
        cs.cancel_report(db, rep)
    except ValueError as e:
        return RedirectResponse(_with_msg(f"/stock/consign?s={quote(rep.supplier)}", str(e), err=True), 303)
    audit(db, "consign.report_cancel", "consign_report", rid, user=user)
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(rep.supplier)}", "Звіт скасовано — банки повернуто в поставки"), 303)


@app.post("/stock/consign/pay")
def consign_pay(s: str = Form(...), amount: str = Form(...), day: str = Form(""), cashbox: str = Form(""), article: str = Form(""),
                note: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    amt = _num(amount)
    if not amt or amt <= 0:
        return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}", "Вкажіть суму оплати", err=True), 303)
    p = cs.pay(db, s, amt, _day(day) or date.today(), cashbox.strip() or None, article.strip() or None, user.id, note.strip())
    audit(db, "consign.pay", "consign_pay", p.id, user=user, supplier=s, amount=amt)
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}#pays", f"Оплату {amt:,.2f} ₴ рознесено по поставках".replace(",", " ")), 303)


@app.post("/stock/consign/pay/{pid:int}/cancel")
def consign_pay_cancel(pid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    from .models import ConsignPay
    p = db.get(ConsignPay, pid)
    if not p:
        raise HTTPException(404)
    sup = p.supplier
    cs.cancel_pay(db, p)
    audit(db, "consign.pay_cancel", "consign_pay", pid, user=user)
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(sup)}#pays", "Оплату скасовано"), 303)


@app.post("/stock/consign/lot")
def consign_lot(s: str = Form(...), invoice: str = Form(""), inv_date: str = Form(...), product_id: int = Form(...), qty: str = Form(...),
                cost: str = Form(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    q, c, d = _num(qty), _num(cost), _day(inv_date)
    if not (q and q > 0 and c is not None and d):
        return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}#lots", "Заповніть товар, к-сть, ціну й дату", err=True), 303)
    cs.add_lot(db, s, invoice.strip(), d, product_id, q, c)
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}#lots", "Позицію поставки додано"), 303)


@app.post("/stock/consign/lot/{lid:int}/map")
def consign_lot_map(lid: int, product_id: int = Form(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import ConsignLot
    l = db.get(ConsignLot, lid)
    if not l:
        raise HTTPException(404)
    l.product_id = product_id
    db.commit()
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(l.supplier)}#lots", "Товар зіставлено"), 303)


@app.post("/stock/suppliers/{sid:int}/consign")
def supplier_consign_toggle(sid: int, on: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import Supplier
    x = db.get(Supplier, sid)
    if not x:
        raise HTTPException(404)
    x.consignment = bool(on) or None
    db.commit()
    audit(db, "supplier.consignment", "supplier", sid, user=user, on=bool(on))
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(x.name)}" if on else "/stock?tab=suppliers",
                                      f"{x.name}: " + ("під реалізацію" if on else "звичайні закупівлі")), 303)


@app.get("/stock/consign/act", response_class=HTMLResponse)
def consign_act_page(request: Request, s: str = "", on: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign as cs, consign_act as ca
    cs.ensure_setup(db)
    sups = cs.suppliers(db)
    sel = s if s in sups else (sups[0] if sups else "")
    act = ca.crm_act(db, sel, _day(on) or date.today()) if sel else None
    return render(request, "stock_consign_act.html", nav="stock", sups=sups, sel=sel, act=act, cmp=None)


@app.get("/stock/consign/act.xlsx")
def consign_act_xlsx(s: str, on: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign_act as ca
    act = ca.crm_act(db, s, _day(on) or date.today())
    return Response(ca.to_xlsx(act), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=akt-zvirky.xlsx"})


@app.post("/stock/consign/act", response_class=HTMLResponse)
async def consign_act_upload(request: Request, s: str = Form(...), on: str = Form(""), file: UploadFile = File(...),
                             user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    import tempfile
    from .services import consign as cs, consign_act as ca
    sups = cs.suppliers(db)
    raw = await file.read()
    suffix = os.path.splitext(file.filename or "")[1].lower() or ".xlsx"
    if suffix not in (".xlsx", ".xls", ".csv", ".txt", ".xlsm"):
        return RedirectResponse(_with_msg(f"/stock/consign/act?s={quote(s)}", "Потрібен Excel (.xlsx / .xls) або CSV. PDF — збережіть як Excel.", err=True), 303)
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(raw)
    tmp.close()
    try:
        parsed = ca.parse(ca.read_rows(tmp.name, raw))
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(_with_msg(f"/stock/consign/act?s={quote(s)}", f"Не вдалося прочитати файл: {str(e)[:150]}", err=True), 303)
    finally:
        os.unlink(tmp.name)
    cmp = ca.compare(db, s, parsed, _day(on) or date.today())
    audit(db, "consign.act_compare", user=user, supplier=s, file=file.filename, goods=cmp["n_goods"], docs=cmp["n_docs"])
    return render(request, "stock_consign_act.html", nav="stock", sups=sups, sel=s, act=cmp["act"], cmp=cmp, parsed=parsed, fname=file.filename)


@app.post("/stock/consign/start")
def consign_start(s: str = Form(...), d: str = Form(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import consign as cs
    dd = _day(d)
    if dd:
        cs.set_start(db, s, dd)
    return RedirectResponse(_with_msg(f"/stock/consign?s={quote(s)}", "Дату старту збережено"), 303)


@app.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, tab: str = "balances", only: str = "", wh: str = "", q: str = "", sup: str = "",
               user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import consign as _cs
    _cs.ensure_setup(db)
    from .services import stock as st
    from .models import Supplier, ProcedureNorm, StockMove
    if only == "deficit":
        tab = "critical"
    tab = tab if tab in STOCK_TABS else "balances"
    if tab in ("supply", "order"):
        st.ensure_suppliers(db)
        _sup_names = sorted({x for (x,) in db.execute(select(Supplier.name))} |
                            {x for (x,) in db.execute(select(Product.supplier).where(Product.supplier.is_not(None)).distinct())},
                            key=str.lower)
    else:
        _sup_names = []
    from .integrations.checkbox import accounts as _cb_acc
    ctx = dict(tab=tab, TABS=STOCK_TABS, WAREHOUSES=catalog.WAREHOUSES, STATUS=st.STATUS, KINDS=st.DOC_KINDS, sup_names=_sup_names,
               fops=[a["name"] for a in _cb_acc(db)] if tab in ("supply", "order", "suppliers") else [],
               cash_boxes=__import__("app.services.payments", fromlist=["cash_boxes"]).cash_boxes(db) if tab in ("supply", "order", "suppliers") else [],
               MOVE_KINDS=st.MOVE_KINDS, active=st.ledger_active(db), start=st.ledger_start(db), wh=wh, q=q, sup=sup)
    if tab in ("balances", "critical"):
        rows = [r for r in catalog.product_rows(db) if r.product.active is not False]
        ctx["crit_n"] = len(_critical_rows(rows))
        per = st.balances(db) if ctx["active"] else {}
        if wh in catalog.WAREHOUSES:
            if ctx["active"]:
                rows = [r for r in rows if per.get((r.product.id, wh)) or (r.product.warehouse or "vitrina") == wh]
            else:
                rows = [r for r in rows if (r.product.warehouse or "vitrina") == wh]
        if q:
            rows = [r for r in rows if catalog.product_match(r.product, q) is not None]
        if tab == "critical":
            rows = _critical_rows(rows)
            if sup:
                rows = [r for r in rows if (r.product.supplier or r.last_supplier or "") == sup]
            rows.sort(key=lambda r: ((r.product.supplier or r.last_supplier or "яяя").lower(), r.product.name))
        else:
            order = {k: i for i, k in enumerate(catalog.WAREHOUSES)}
            if not request.query_params.get("all"):                 # за замовчуванням — лише те, що є в наявності
                rows = [r for r in rows if (r.stock or 0) != 0]
            rows.sort(key=lambda r: (order.get(r.product.warehouse or "vitrina", 9), (r.product.brand or "яяя").lower(), r.product.name))
        wh_tot = defaultdict(lambda: {"n": 0, "value": 0.0})
        costs = {r.product.id: (r.cost or 0) / (r.product.coef if r.product.coef and r.product.coef > 0 else 1) for r in rows}   # ціна флакона → за одиницю обліку (мл, шт)
        from .models import AmortAsset as _AA
        _amort_ids = {x for (x,) in db.execute(select(_AA.product_id).where(_AA.product_id.is_not(None)))}   # лічильники амортизації — не товар
        if ctx["active"]:
            for (pid, w), qty in per.items():
                if qty and pid in costs and pid not in _amort_ids:
                    wh_tot[w]["n"] += 1
                    wh_tot[w]["value"] += qty * costs[pid]
        ctx.update(rows=rows, per=per, wh_tot=dict(wh_tot), suggested=st.suggested_qty,
                   suppliers=sorted({r.product.supplier or r.last_supplier for r in rows if (r.product.supplier or r.last_supplier)}))
        lv = db.scalar(select(StockLevel).limit(1))
        ctx["cb_updated"] = lv.updated_at if lv else None
        ctx["snap"] = _balances_snapshot(db)
    elif tab in st.DOC_KINDS:
        arch = bool(request.query_params.get("archive"))
        ctx["docs"] = st.doc_rows(db, tab, archived=arch)
        ctx["archive"] = arch
        from .services.payments import MODE_LABELS
        ctx["MODES"] = MODE_LABELS
        ctx["archive_n"] = db.scalar(select(func.count(StockDoc.id)).where(StockDoc.kind == tab, StockDoc.archived_at.is_not(None))) or 0
        ctx["user_names"] = {u_.id: u_.name for u_ in db.scalars(select(User))} if arch else {}
    elif tab == "suppliers":
        from .models import SupplierInvoice
        st.ensure_suppliers(db)
        per = request.query_params.get("per", "365")
        d_from = None if per == "all" else datetime.now() - timedelta(days=int(per) if per.isdigit() else 365)
        ctx.update(sstats=st.supplier_stats(db, d_from), per=per, sel=request.query_params.get("s", ""),
                   debt_only=bool(request.query_params.get("debt")), imported=LAST_IMPORT.pop(f"inv-{user.id}", None))
        if ctx["sel"]:
            ctx["invoices"] = db.scalars(select(SupplierInvoice).where(SupplierInvoice.supplier == ctx["sel"])
                                         .order_by(SupplierInvoice.inv_date.desc()).limit(300)).all()
        elif ctx["debt_only"]:
            ctx["invoices"] = [x for x in db.scalars(select(SupplierInvoice).where(SupplierInvoice.status != "Чернетка")
                                                     .order_by(SupplierInvoice.inv_date.desc())) if x.debt > 1]
        agg = defaultdict(lambda: {"products": 0, "orders": 0, "supplies": 0, "sum": 0.0, "last": None})
        for p_ in db.scalars(select(Product).where(Product.supplier.is_not(None))):
            agg[p_.supplier.lower()]["products"] += 1
        for d in db.scalars(select(StockDoc).where(StockDoc.kind.in_(("supply", "order")), StockDoc.status != "cancelled")):
            a = agg[(d.supplier or "").lower()]
            a["orders" if d.kind == "order" else "supplies"] += 1
            if d.kind == "supply" and d.status == "posted":
                a["sum"] += sum((ln.qty or 0) * (ln.unit_cost or 0) for ln in d.lines)
                a["last"] = max(filter(None, [a["last"], d.doc_date]))
        ctx.update(suppliers=db.scalars(select(Supplier).order_by(Supplier.name)).all(), agg=agg)
        # оплати: monobank + накладні CRM
        from .models import BankTxn
        from .services import payments as pay
        from .integrations import monobank
        from .integrations.checkbox import accounts as _cb_acc
        sel_sup = next((x for x in ctx["suppliers"] if x.name == ctx["sel"]), None)
        ctx.update(sel_sup=sel_sup, mono=monobank.public(db), fops=[a["name"] for a in _cb_acc(db)],
                   txns=db.scalars(select(BankTxn).where(BankTxn.at >= datetime.now() - timedelta(days=60))
                                   .order_by(BankTxn.at.desc()).limit(200)).all(),
                   open_groups=pay.groups(db, only_open=True), PAY=pay.PAY_LABELS,
                   weak_ids={w.id for w in pay.weak_auto(db)},
                   crm_groups=pay.groups(db, ctx["sel"]) if ctx["sel"] else [],
                   sup_by_id={x.id: x for x in ctx["suppliers"]},
                   job=jobs.JOBS.get(request.query_params.get("job") or "") if request.query_params.get("job") else None)
    elif tab == "norms":
        from .models import CbService
        services = db.scalars(select(CbService).order_by(CbService.category, CbService.name)).all()
        norms = defaultdict(list)
        for n in db.scalars(select(ProcedureNorm)):
            norms[n.service_id].append(n)
        if q:
            words = set(norm(q).split())
            services = [x for x in services if words <= set(norm(" ".join(filter(None, (
                x.name, x.category, x.techcard, x.direction)))).split())]
        elif request.query_params.get("only") != "all":
            services = [x for x in services if x.id in norms] or services[:60]
        # групи: одна техкартка = одна норма на всі її послуги; послуги без техкартки — окремо
        groups: dict[str, dict] = {}
        for x in services:
            key = f"t:{x.techcard}" if x.techcard else f"s:{x.id}"
            g_ = groups.setdefault(key, {"key": key, "title": x.techcard or x.name, "techcard": x.techcard,
                                         "services": [], "norms": {}, "direction": x.direction})
            g_["services"].append(x)
        all_by_card = defaultdict(list)
        if any(g_["techcard"] for g_ in groups.values()):
            for x in db.scalars(select(CbService).where(CbService.techcard.is_not(None))):
                all_by_card[x.techcard].append(x)
        for g_ in groups.values():
            if g_["techcard"]:
                g_["services"] = all_by_card.get(g_["techcard"], g_["services"])
            n_svc = len(g_["services"])
            for x in g_["services"]:
                for n_ in norms.get(x.id, []):
                    k_ = (n_.product_id, n_.qty, n_.warehouse)
                    item = g_["norms"].setdefault(k_, {"n": n_, "count": 0})
                    item["count"] += 1
            g_["norms"] = [dict(v, total=n_svc) for v in g_["norms"].values()]
        glist = sorted(groups.values(), key=lambda g_: ((g_["direction"] or "яяя"), g_["title"].lower()))
        from .services.service_costs import techcard_costs
        ctx.update(services=services, groups=glist, norms=norms, products=_product_options(db, consumables=True), tcost=techcard_costs(db))
    elif tab == "moves":
        mq = select(StockMove).order_by(StockMove.at.desc(), StockMove.id.desc()).limit(300)
        if wh in catalog.WAREHOUSES:
            mq = mq.where(StockMove.warehouse == wh)
        ctx["moves"] = db.scalars(mq).all()
        ctx["pnames"] = {p_.id: p_ for p_ in db.scalars(select(Product))}
    return render(request, "stock.html", **ctx)


def _product_options(db: Session, consumables: bool = False):
    q = select(Product).where(Product.active.is_not(False)).order_by(Product.name)
    return [(p_.id, f"{p_.name}{(' · арт. ' + p_.sku) if p_.sku else ''} [#{p_.id}]", p_.warehouse) for p_ in db.scalars(q)]


def _pick_product(db: Session, ref: str) -> Product | None:
    import re as _re
    m = _re.search(r"\[#(\d+)\]\s*$", ref or "")
    if m:
        return db.get(Product, int(m.group(1)))
    ref = (ref or "").strip()
    if not ref:
        return None
    return db.scalar(select(Product).where((Product.sku == ref) | (Product.barcode == ref))) or catalog.match_product(db, ref)


@app.post("/stock/start")
def stock_start(from_file: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock as st
    snap = _balances_snapshot(db) if from_file else None
    if from_file and not snap:
        return RedirectResponse("/stock?msg=Спершу+завантажте+звіт+«Залишки+по+складах»", 303)
    try:
        doc = st.start_ledger(db, user.id, snapshot=snap)
    except st.StockError as e:
        return RedirectResponse(f"/stock?msg={e}", 303)
    audit(db, "stock.start", "stock_doc", doc.id, user=user, lines=len(doc.lines), from_file=bool(snap))
    if snap:
        _balances_snapshot(db, clear=True)
    return RedirectResponse("/stock?msg=Облік+у+CRM+розпочато", 303)


def _balances_snapshot(db: Session, rep: dict | None = None, clear: bool = False) -> dict | None:
    """Знімок останнього імпортованого звіту «Залишки по складах» (SyncState wh_snapshot)."""
    import json
    stt = db.get(SyncState, "wh_snapshot")
    if clear:
        if stt:
            db.delete(stt)
            db.commit()
        return None
    if rep is not None:
        stt = stt or SyncState(key="wh_snapshot")
        stt.value, stt.updated_at = json.dumps(rep, ensure_ascii=False), datetime.utcnow()
        db.merge(stt)
        db.commit()
        return rep
    try:
        return json.loads(stt.value) if stt and stt.value else None
    except ValueError:
        return None


async def _import_balances_file(db: Session, path: str) -> dict:
    rep = await asyncio.to_thread(catalog.parse_balances, db, path)
    return _balances_snapshot(db, rep)


@app.post("/stock/reconcile")
async def stock_reconcile_upload(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Знімок залишків CleverBox (звіт «Залишки») для звірки: {rows: [[cb_id, wh, qty, value, name], …]}."""
    import json
    data = await request.json()
    rows = [r for r in (data.get("rows") or []) if isinstance(r, list) and len(r) >= 4]
    stt = db.get(SyncState, "wh_reconcile") or SyncState(key="wh_reconcile")
    stt.value, stt.updated_at = json.dumps({"rows": rows, "at": datetime.now().isoformat(timespec="minutes")}, ensure_ascii=False), datetime.utcnow()
    db.merge(stt)
    db.commit()
    audit(db, "stock.reconcile_upload", user=user, n=len(rows))
    known = {x for (x,) in db.execute(select(Product.cb_product_id).where(Product.cb_product_id.is_not(None)))}
    return {"ok": True, "n": len(rows), "unmatched": sorted({int(r[0]) for r in rows if str(r[0]).isdigit() and int(r[0]) not in known
                                                             and not (len(r) > 4 and r[4])})}


def _reconcile(db: Session) -> dict | None:
    import json
    from .services import stock as st
    stt = db.get(SyncState, "wh_reconcile")
    if not stt or not stt.value:
        return None
    snap = json.loads(stt.value)
    prods = db.scalars(select(Product)).all()
    by_cb = {p.cb_product_id: p for p in prods if p.cb_product_id}
    by_name = {p.name_norm: p for p in prods}
    cost = {r.product.id: (r.cost or 0) / (r.product.coef if r.product.coef and r.product.coef > 0 else 1) for r in catalog.product_rows(db)}
    crm = st.balances(db) if st.ledger_active(db) else {}
    cb: dict = {}
    unknown = []
    has_v = snap.get("source") != "api"           # API CleverBox віддає лише кількість — вартість не звіряємо
    for cid, wh, q, v, *rest in snap["rows"]:
        nm = rest[0] if rest else ""
        p = by_cb.get(int(cid)) if str(cid).isdigit() else None
        p = p or by_name.get(norm(nm))
        if not p:
            if abs(float(q or 0)) > 1e-6:
                unknown.append({"name": nm, "wh": wh, "qty": float(q or 0), "value": float(v or 0)})
            continue
        a = cb.setdefault((p.id, wh), [0.0, 0.0])
        a[0] += float(q or 0)
        a[1] += float(v or 0)
    names = {p.id: p for p in prods}
    rows, tot = [], {}
    for key in set(cb) | {k for k, q in crm.items() if abs(q) > 1e-6}:
        pid, wh = key
        cq, cv = cb.get(key, [0.0, 0.0])
        mq = round(crm.get(key, 0.0), 3)
        mv = round(mq * cost.get(pid, 0.0), 2)
        t = tot.setdefault(wh, {"cb_q": 0, "cb_v": 0.0, "crm_v": 0.0, "n_bad": 0, "n_vbad": 0})
        t["cb_v"] += cv
        t["crm_v"] += mv
        dq, dv = round(mq - cq, 3), round(mv - cv, 2)
        bad_q, bad_v = abs(dq) > 0.01, has_v and abs(dv) > max(1.0, abs(cv) * 0.01)
        t["n_bad"] += bad_q
        t["n_vbad"] += bad_v and not bad_q
        if bad_q or bad_v:
            p = names.get(pid)
            rows.append({"pid": pid, "name": p.name if p else str(pid), "wh": wh, "unit": (p.unit if p else "") or "",
                         "cb_q": round(cq, 3), "crm_q": mq, "dq": dq, "cb_v": round(cv, 2), "crm_v": mv, "dv": dv,
                         "kind": "qty" if bad_q else "value",
                         "cb_unit_cost": round(cv / cq, 2) if cq else None, "crm_unit_cost": round(cost.get(pid, 0.0), 2)})
    rows.sort(key=lambda r: (r["kind"] != "qty", list(catalog.WAREHOUSES).index(r["wh"]) if r["wh"] in catalog.WAREHOUSES else 9, -abs(r["dv"])))
    return {"at": snap.get("at"), "rows": rows, "tot": tot, "unknown": unknown, "n_cb": len(snap["rows"]), "has_v": has_v,
            "source": snap.get("source") or "report"}


def reconcile_from_api(db: Session) -> dict | None:
    """Свіжі залишки CleverBox через API (/products/stock) → знімок звірки (лише кількість)."""
    import json
    from .integrations.cleverbox import CleverBox, _first
    rows, skipped, raw_n = [], set(), 0
    for r in CleverBox().products_stock():
        raw_n += 1
        pid = _first(r, "product_id", "item_id", "id")
        sname = _first(r, "storage_name", "storage", "store_name")
        wh = catalog.warehouse_of(sname)
        if pid in (None, "") or not wh:
            skipped.add(str(sname or "?")[:40])
            continue
        q = float(_first(r, "quantity", "qty", "stock", "balance", "count", "amount", default=0) or 0)
        rows.append([int(pid), wh, q, 0.0, str(_first(r, "product_name", "name", "title", default=""))])
    if not rows:
        raise RuntimeError(f"API CleverBox не дав залишків по складах (рядків {raw_n}, склади без відповідності: {', '.join(sorted(skipped)) or '—'})")
    stt = db.get(SyncState, "wh_reconcile") or SyncState(key="wh_reconcile")
    stt.value = json.dumps({"rows": rows, "at": datetime.now().isoformat(timespec="minutes"), "source": "api"}, ensure_ascii=False)
    stt.updated_at = datetime.utcnow()
    db.merge(stt)
    db.commit()
    return _reconcile(db)


def run_daily_reconcile() -> str:
    from .services import alerts
    with SessionLocal() as db:
        rec = reconcile_from_api(db)
    if not rec:
        return "нема даних"
    bad = [r for r in rec["rows"] if r["kind"] == "qty"]
    real = [r for r in bad if not (r["name"].startswith("Амортизація") or "манімула" in r["name"] or r["cb_q"] < 0)]
    if not bad:
        txt = "⚖ <b>Звірка складів з CleverBox</b>: усе сходиться ✅"
    else:
        lines = [f"• {r['name'][:60]} ({catalog.WAREHOUSES.get(r['wh'], r['wh'])}): CB {r['cb_q']:g} / CRM {r['crm_q']:g} ({r['dq']:+g})"
                 for r in real[:15]]
        txt = (f"⚖ <b>Звірка складів з CleverBox</b>: розбіжностей кількості {len(bad)}, з них товар/розхідники {len(real)}"
               f" (технічні — амортизація, маніпули, мінуси CB — {len(bad) - len(real)}).\n" + "\n".join(lines)
               + (f"\n… ще {len(real) - 15}" if len(real) > 15 else "")
               + f"\n\n{settings.public_url.rstrip('/')}/stock/reconcile")
    alerts.send(txt)
    return f"{len(bad)} розбіжн."


async def job_reconcile():
    from .services import autonomy
    if autonomy.offline() or not settings.cleverbox_token:
        return
    try:
        await asyncio.to_thread(run_daily_reconcile)
    except Exception as e:          # noqa: BLE001
        log.warning("daily reconcile failed: %s", e)
        from .services import alerts
        await asyncio.to_thread(alerts.send, f"⚖ Звірка складів з CleverBox не вдалася: {str(e)[:300]}")


@app.post("/stock/reconcile/inventory")
async def stock_reconcile_inventory(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Вибрані розбіжності кількості → чернетки інвентаризації (факт = CleverBox), по одній на склад. Нічого не проводиться."""
    from .services import stock as st
    from .models import StockDocLine
    f = await request.form()
    pick = {tuple(x.split("|", 1)) for x in f.getlist("pick") if "|" in str(x)}
    rec = _reconcile(db)
    if not rec or not pick:
        return RedirectResponse("/stock/reconcile?err=Нічого+не+вибрано", 303)
    bal = st.balances(db)
    by_wh: dict = defaultdict(list)
    for r in rec["rows"]:
        if r["kind"] == "qty" and (str(r["pid"]), r["wh"]) in pick:
            by_wh[r["wh"]].append(r)
    ids = []
    for wh, rs in by_wh.items():
        doc = st.new_doc(db, "inventory", user_id=user.id, wh_from=wh, note=f"Звірка з CleverBox (знімок {rec['at']}): факт = залишок CleverBox")
        for r in rs:
            doc.lines.append(StockDocLine(product_id=r["pid"], qty=max(r["cb_q"], 0.0), sys_qty=bal.get((r["pid"], wh), 0.0)))
        db.commit()
        ids.append(doc.id)
    audit(db, "stock.reconcile_inventory", user=user, docs=ids)
    if len(ids) == 1:
        return RedirectResponse(f"/stock/docs/{ids[0]}?msg=Чернетка+інвентаризації+—+перевірте+і+проведіть", 303)
    return RedirectResponse(f"/stock?tab=inventory&msg=Створено+{len(ids)}+чернеток+інвентаризації", 303)


@app.post("/stock/reconcile/refresh")
def stock_reconcile_refresh(user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    try:
        reconcile_from_api(db)
    except Exception as e:          # noqa: BLE001
        return RedirectResponse(f"/stock/reconcile?err={_q('CleverBox: ' + str(e)[:150])}", 303)
    audit(db, "stock.reconcile_refresh", user=user)
    return RedirectResponse("/stock/reconcile", 303)


@app.get("/stock/reconcile", response_class=HTMLResponse)
def stock_reconcile_page(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    return render(request, "stock_reconcile.html", nav="stock", rec=_reconcile(db), WAREHOUSES=catalog.WAREHOUSES)


@app.post("/stock/balances/import")
async def stock_balances_import(file: UploadFile = File(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Звіт CleverBox «Залишки по складах» → собівартість залишку + знімок для старту обліку / звірки."""
    import tempfile
    suffix = ".xls" if (file.filename or "").lower().endswith(".xls") else ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
    msg = ""
    try:
        kind = await asyncio.to_thread(catalog.detect_export, tmp.name)
        if kind != "balances":
            raise ValueError(catalog.WRONG_FILE[kind])
        rep = await _import_balances_file(db, tmp.name)
        audit(db, "stock.balances_import", user=user, rows=rep["rows"], created=rep["created"], value=rep["value"])
    except ValueError as e:
        msg = str(e)
    except Exception as e:  # noqa: BLE001
        msg = f"Не вдалося прочитати файл: {type(e).__name__}"
    finally:
        os.unlink(tmp.name)
    return RedirectResponse("/stock?tab=balances" + (f"&msg={msg}" if msg else "#snap"), 303)


# ---------------- Дзвіночок, телефонія, центр спілкування ----------------
@app.get("/notifications", response_class=HTMLResponse)
def notifications_panel(request: Request, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .services import notifications as nt
    xs = nt.items(db, user)
    html = templates.get_template("_notifications.html").render(items=xs)
    u = db.get(User, user.id)
    u.notif_seen_at = datetime.utcnow()
    db.commit()
    return HTMLResponse(html)


@app.post("/notifications/hide")
async def notifications_hide(request: Request, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    """Видалити сповіщення зі списку (для себе): {"keys": [...]} або {"all": true}."""
    from .services import notifications as nt
    f = await request.json()
    keys = [x["key"] for x in nt.items(db, user)] if f.get("all") else [str(k)[:80] for k in (f.get("keys") or [])][:500]
    nt.hide(db, user, keys)
    return JSONResponse({"ok": True, "n": len(keys)})


@app.get("/notifications/poll")
def notifications_poll(since: str = "", user: User = Depends(current_user), db: Session = Depends(db_dep)):
    """Нові онлайн-записи з моменту since (ISO, UTC) — для спливаючого вікна; кількість непереглянутих — для значка."""
    from .services import notifications as nt
    try:
        s_ = datetime.fromisoformat(since) if since else datetime.utcnow() - timedelta(minutes=2)
    except ValueError:
        s_ = datetime.utcnow() - timedelta(minutes=2)
    new = nt.online(db, s_) if can(user, "book") else []
    return {"now": datetime.utcnow().isoformat(timespec="seconds"), "unseen": nt.unseen(db, user),
            "toasts": [{"title": x["title"], "text": x["text"], "url": x["url"]} for x in new][:5]}


@app.get("/telephony", response_class=HTMLResponse)
def telephony_page(request: Request, f: str = "", user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .models import Call
    q = select(Call).order_by(Call.at.desc())
    if f == "missed":
        q = q.where(Call.status == "missed")
    elif f in ("in", "out"):
        q = q.where(Call.direction == f)
    elif f == "low":
        from .services import telephony as _tl
        q = q.where(Call.ai_score.is_not(None), Call.ai_score < float(_tl.public(db).get("ai_alert_below") or 0))
    calls = db.scalars(q.limit(300)).all()
    pats = {p_.id: p_ for p_ in db.scalars(select(Patient).where(Patient.id.in_([c_.patient_id for c_ in calls if c_.patient_id] or [0])))}
    from .services import telephony as _tl2
    d_to = date.today()
    return render(request, "calls.html", nav="telephony", calls=calls, pats=pats, f=f, tcfg=_tl2.public(db),
                  kpi=_tl2.kpi(db, d_to.replace(day=1), d_to), cur=request.query_params.get("call", ""))


@app.get("/chats", response_class=HTMLResponse)
def chats_page(request: Request, t: str = "", ch: str = "", q: str = "", user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    from .models import ChatMessage
    th = mi.threads(db, ch, q)
    cur = next((x for x in th if x["thread"] == t), None) or (th[0] if th and not t else None)
    msgs, pat = [], None
    if cur:
        msgs = db.scalars(select(ChatMessage).where(ChatMessage.thread == cur["thread"]).order_by(ChatMessage.at, ChatMessage.id)).all()
        mi.mark_read(db, cur["thread"])
        pat = db.get(Patient, cur["patient_id"]) if cur.get("patient_id") else None
    c = mi.meta_channel(db)
    from .services import appeals as _ap
    _d = _ap.dirs(db)
    close_st = [s_ for s_ in _d["statuses"] if s_.get("type") in ("success", "fail", "other")]
    from .services import reply_templates as _rt
    tpls = []
    if cur:
        _ctx = _rt.context(db, pat, cur["name"], user.name)
        tpls = [dict(t_, out=_rt.render(t_["text"], _ctx)) for t_ in _rt.for_channel(db, cur["channel"])]
    pinfo = None
    if pat:
        from .services.kpi import compute_plan as _cpl
        _pl = next((pl_ for pl_ in pat.plans if pl_.status == "active"), pat.plans[0] if pat.plans else None)
        _ps = None
        if _pl:
            try:
                _ps = _cpl(db, _pl)
            except Exception:  # noqa: BLE001
                _ps = None
        _lastv = db.scalar(select(func.max(Visit.starts_at)).where(Visit.patient_id == pat.id, Visit.status.in_(("done", "attended")), Visit.starts_at <= datetime.now()))
        pinfo = {"upcoming": _rt.upcoming(db, pat.id, 3), "plan": _pl, "ps": _ps, "last_visit": _lastv}
    return render(request, "chats.html", nav="chats", threads=th, cur=cur, msgs=msgs, pat=pat, ch_=ch, q=q, tpls=tpls, pinfo=pinfo,
                  connected=bool(c and c.get("has", {}).get("page_token")), replies_on=mi.replies_enabled(db),
                  suggest=mi.suggest(db, cur["name"]) if cur and not pat else [], close_st=close_st,
                  deny_reasons=_d.get("deny_reasons") or [], other_reasons=_d.get("other_reasons") or [],
                  appeal=db.get(__import__("app.models", fromlist=["Appeal"]).Appeal, msgs[-1].appeal_id) if msgs and msgs[-1].appeal_id else None)


@app.get("/chats/{thread}/avatar")
def chats_avatar(thread: str, user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    av = mi.avatar(db, thread) if thread.isdigit() else None
    if not av:
        raise HTTPException(404)
    return Response(av[0], media_type=av[1], headers={"Cache-Control": "private, max-age=21600"})


@app.post("/chats/{thread}/close")
def chats_close(thread: str, status: str = Form(...), reason: str = Form(""), user: User = Depends(require("appeals")),
                db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    n = mi.close_thread(db, thread, status, reason.strip() or None, user.id)
    audit(db, "chats.close", user=user, thread=thread[-6:], status=status, appeals=n)
    return RedirectResponse(_with_msg(f"/chats?t={thread}", f"Діалог закрито · {status}" + ("" if n else " (відкритих звернень не було)")), 303)


@app.post("/chats/{thread}/reply")
def chats_reply(thread: str, text: str = Form(...), user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    try:
        m = mi.reply(db, thread, text.strip(), user.id)
    except ValueError as e:
        return RedirectResponse(_with_msg("/chats", str(e), True), 303)
    audit(db, "chats.reply", user=user, thread=thread[-6:], status=m.status)
    url = f"/chats?t={thread}"
    return RedirectResponse(url if m.status == "sent" else _with_msg(url, f"Не надіслано: {m.error}", True), 303)


@app.post("/chats/{thread}/patient")
def chats_link_patient(thread: str, patient_id: str = Form(""), user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .models import ChatMessage
    pid = int(patient_id) if patient_id.isdigit() else None
    from .models import Appeal as _Ap
    aids = set()
    for m in db.scalars(select(ChatMessage).where(ChatMessage.thread == thread)):
        m.patient_id = pid
        if m.appeal_id:
            aids.add(m.appeal_id)
    for a_ in db.scalars(select(_Ap).where(_Ap.id.in_(aids or {0}), _Ap.closed_at.is_(None))):
        a_.patient_id = pid                                    # звернення з цього діалогу — до картки пацієнта
    db.commit()
    audit(db, "chats.patient", user=user, thread=thread[-6:], patient=pid)
    return RedirectResponse(f"/chats?t={thread}", 303)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return render(request, "legal.html", nav="login", kind="privacy", title="Політика конфіденційності", today=date.today())


@app.get("/data-deletion", response_class=HTMLResponse)
def data_deletion_page(request: Request):
    return render(request, "legal.html", nav="login", kind="deletion", title="Видалення даних", today=date.today())


@app.get("/api/public/meta/webhook")
def meta_webhook_verify(request: Request, db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    qp = request.query_params
    if qp.get("hub.mode") == "subscribe" and qp.get("hub.verify_token") and hmac.compare_digest(qp.get("hub.verify_token"), mi.verify_token(db)):
        mi.hook_log(db, "verify", "Meta перевірила вебхук — OK")
        return Response(qp.get("hub.challenge") or "", media_type="text/plain")
    mi.hook_log(db, "verify-bad", "Перевірка вебхука: Verify token не збігається")
    raise HTTPException(403, "verify token mismatch")


@app.post("/api/public/meta/webhook")
async def meta_webhook(request: Request, db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    body = await request.body()
    if not mi.check_signature(db, body, request.headers.get("x-hub-signature-256")):
        mi.hook_log(db, "bad-signature", "Підпис не збігається — перевірте App Secret у CRM")
        raise HTTPException(403, "bad signature")
    try:
        import json as _j
        payload = _j.loads(body or b"{}")
        n = await asyncio.to_thread(mi.ingest, db, payload)
        mi.hook_log(db, "event", mi.summarize(payload) + f" → збережено {n}")
    except Exception as e:                # noqa: BLE001 — Meta має отримати 200, інакше вимкне вебхук
        logging.getLogger("meta_inbox").exception("webhook")
        db.rollback()
        mi.hook_log(db, "error", f"Помилка обробки: {type(e).__name__}")
        n = 0
    if n:
        try:
            from .services import alerts as _al
            _al.send(f"💬 Нове повідомлення в Instagram / Facebook ({n}) — {settings.public_url.rstrip('/')}/chats")
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@app.post("/settings/channels/meta-subscribe")
def settings_channels_meta_subscribe(user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    ok, msg = mi.subscribe(db)
    audit(db, "channels.meta_subscribe", user=user, op="ok" if ok else "fail")
    c = mi.meta_channel(db)
    return RedirectResponse(_with_msg(f"/settings/channels?open={c['id'] if c else ''}", ("✅ " if ok else "❌ ") + msg, not ok), 303)


@app.post("/settings/channels/replies")
def settings_channels_replies(on: str = Form(""), user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import meta_inbox as mi
    mi.set_replies(db, on == "1")
    audit(db, "channels.replies", user=user, op="on" if on == "1" else "off")
    return RedirectResponse(_with_msg("/settings/channels", "Відповіді в чатах " + ("увімкнено" if on == "1" else "вимкнено")), 303)


@app.get("/data/name-order")
def patients_name_order(apply: int = 0, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    """ПІБ «Ім'я Прізвище По батькові» (як у CleverBox) → «Прізвище Ім'я По батькові». Без apply — лише попередній перегляд."""
    from .services import name_order as no
    pl = no.plan(db)
    if apply:
        n = no.apply(db)
        audit(db, "patients.name_order", user=user, changed=n)
        db.commit()
        return {"changed": n, "unclear": len(pl["unclear"])}
    return {"total": pl["total"], "to_fix": len(pl["fix"]), "examples": pl["fix"][:40], "unclear_n": len(pl["unclear"]),
            "unclear": pl["unclear"][:60]}


@app.post("/migrate/cb-visits")
async def migrate_cb_visits(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    """Історія візитів CleverBox (зібрана у веб-кабінеті частинами) → візити CRM з послугами, оплатою й витратними."""
    from .services import cb_history, autonomy as _aut
    data = await request.json()
    s_ = _aut.since(db)
    until = datetime.combine(s_, datetime.min.time()) if s_ else None
    rep = await asyncio.to_thread(cb_history.import_visits, db, data.get("visits") or [], until)
    audit(db, "migrate.cb_visits", user=user, **{k: v for k, v in rep.items() if isinstance(v, int)})
    db.commit()
    return rep


@app.post("/patients/photos/import-cb")
async def patients_photos_import_cb(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    """Фото пацієнтів з CleverBox (список зібрано у веб-кабінеті) → у фоні завантажуються в картки пацієнтів."""
    from .services import cb_photos
    data = await request.json()
    files = data.get("files") or []
    by = "record" if data.get("by") == "record" else "client"
    job = jobs.start("Фото до/після з CleverBox" if by == "record" else "Фото пацієнтів з CleverBox", cb_photos.run, files, by)
    audit(db, "patients.photos_import_cb", user=user, files=len(files), job=job.id)
    db.commit()
    return {"ok": True, "job": job.id, "files": len(files)}


@app.get("/data/docs-archive", response_class=HTMLResponse)
def docs_archive_page(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    """Дані → рознести архів документів (висновки, бюті-плани, документація) по картках пацієнтів."""
    return render(request, "data_docs_archive.html", scan=None, rep=None, token="", group="")


def _bulk_dir(token: str) -> Path:
    d = Path(settings.files_dir) / "_bulk" / re.sub(r"[^a-f0-9]", "", token)[:16]
    return d


@app.post("/data/docs-archive/scan", response_class=HTMLResponse)
async def docs_archive_scan(request: Request, files: list[UploadFile] = File(...), group: str = Form("other"),
                            user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    from .services import bulk_docs
    import uuid
    token = uuid.uuid4().hex[:16]
    d = _bulk_dir(token)
    d.mkdir(parents=True, exist_ok=True)
    arcs = []
    for f in files:
        data = await f.read()
        name = re.sub(r"[^\w.\- ]+", "_", f.filename or "archive.zip")
        (d / name).write_bytes(data)
        arcs.append((name, data))
    group = group if group in ("medical", "plans", "other") else "other"
    scan = await asyncio.to_thread(bulk_docs.scan, db, arcs, group)
    audit(db, "docs_archive.scan", user=user, files=scan["total"], group=group)
    db.commit()
    return render(request, "data_docs_archive.html", scan=scan, rep=None, token=token, group=group)


@app.post("/data/docs-archive/apply", response_class=HTMLResponse)
async def docs_archive_apply(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    from .services import bulk_docs
    form = await request.form()
    token, group = str(form.get("token") or ""), str(form.get("group") or "other")
    d = _bulk_dir(token)
    if not token or not d.exists():
        return RedirectResponse(_with_msg("/data/docs-archive", "Архів не знайдено — завантажте ще раз", True), 303)
    arcs = [(p.name, p.read_bytes()) for p in sorted(d.iterdir()) if p.is_file()]
    overrides = {}
    for k, v in form.multi_items():
        if k.startswith("ov:") and str(v).strip().isdigit():
            overrides[k[3:]] = int(str(v).strip())
    rep = await asyncio.to_thread(bulk_docs.apply, db, arcs, group, overrides, user.id)
    audit(db, "docs_archive.apply", user=user, plans=rep["plans"], files=rep["files"], patients=rep["patients"])
    db.commit()
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return render(request, "data_docs_archive.html", scan=None, rep=rep, token="", group=group)


@app.post("/patients/photos/audit-cb")
async def patients_photos_audit_cb(request: Request, user: User = Depends(require("migrate")), db: Session = Depends(db_dep)):
    """Звірка фото з CleverBox без змін: у кого в CRM лежать чужі файли (список зібрано у веб-кабінеті)."""
    from .services import cb_photos
    data = await request.json()
    by = "record" if data.get("by") == "record" else "client"
    return await asyncio.to_thread(cb_photos.audit, data.get("files") or [], by)


@app.post("/finance/ops/import-cb-web")
async def finops_import_cb_web(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Журнал фіноперацій із веб-кабінету CleverBox (зібраний у браузері) → CbFinOp; в автономному режимі — лише до дати переходу."""
    from .services import cb_finops_import as cfi, autonomy as _aut
    data = await request.json()
    s_ = _aut.since(db)
    until = datetime.combine(s_, datetime.min.time()) - timedelta(seconds=1) if s_ else None
    rep = await asyncio.to_thread(cfi.import_web_rows, db, data.get("rows") or [], until)
    audit(db, "finops.import_cb_web", user=user, **{k: v for k, v in rep.items()})
    db.commit()
    return rep


@app.post("/stock/balances/snapshot-cb")
async def stock_snapshot_cb(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Залишки CleverBox (зібрані з веб-кабінету): {"date": "2026-10-31", "items": [[cb_product_id, склад, к-сть, сума]]}
    → знімок для звірки (як після імпорту звіту «Залишки по складах»). Невідомі товари повертаються списком."""
    data = await request.json()
    by_cb = {p.cb_product_id: p.id for p in db.scalars(select(Product).where(Product.cb_product_id.is_not(None)))}
    items, missing = [], []
    for cid, wh, q, cost in data.get("items") or []:
        pid = by_cb.get(int(cid))
        if not pid:
            missing.append(int(cid))
            continue
        if wh in catalog.WAREHOUSES:
            items.append([pid, wh, float(q or 0), float(cost or 0)])
    by_wh: dict = {}
    for _pid, wh_, _q, c_ in items:
        w_ = by_wh.setdefault(wh_, {"n": 0, "value": 0.0})
        w_["n"] += 1
        w_["value"] = round(w_["value"] + c_, 2)
    rep = {"date": str(data.get("date") or ""), "rows": len(items) + len(missing), "matched": len(items), "created": 0, "items": items,
           "by_wh": by_wh, "value": round(sum(x[3] for x in items), 2), "source": "cleverbox-web", "missing": missing[:200]}
    _balances_snapshot(db, rep)
    audit(db, "stock.snapshot_cb", user=user, rows=len(items), missing=len(missing), date=rep["date"])
    db.commit()
    return {"ok": True, "rows": len(items), "missing": missing, "value": rep["value"]}


@app.post("/stock/balances/reconcile")
def stock_balances_reconcile(as_of: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock as st
    snap = _balances_snapshot(db)
    if not snap:
        return RedirectResponse("/stock?msg=Спершу+завантажте+звіт+«Залишки+по+складах»", 303)
    try:
        ao = date.fromisoformat(as_of) if as_of else None
    except ValueError:
        ao = None
    try:
        docs = st.reconcile_from_snapshot(db, snap, user.id, ao)
    except st.StockError as e:
        return RedirectResponse(f"/stock?msg={e}", 303)
    audit(db, "stock.balances_reconcile", user=user, docs=[d.id for d in docs])
    _balances_snapshot(db, clear=True)
    return RedirectResponse(f"/stock?tab=inventory&msg=Створено+{len(docs)}+чернеток+інвентаризації+—+перевірте+різницю+і+проведіть", 303)


@app.post("/stock/balances/dismiss")
def stock_balances_dismiss(user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    _balances_snapshot(db, clear=True)
    return RedirectResponse("/stock", 303)


@app.post("/stock/docs")
def stock_doc_create(kind: str = Form(...), wh: str = Form(""), supplier: str = Form(""),
                     user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock as st
    kw = {}
    if kind in ("transfer", "writeoff", "inventory"):
        kw["wh_from"] = wh if wh in catalog.WAREHOUSES else "vitrina"
    if kind in ("supply", "order"):
        kw["wh_to"] = wh if wh in catalog.WAREHOUSES else "vitrina"
        kw["supplier"] = supplier.strip() or None
    if kind == "return":
        kw.update(reason="supplier", wh_from="vitrina")
    if kind == "writeoff":
        kw["reason"] = st.WRITEOFF_REASONS[0]
    doc = st.new_doc(db, kind, user_id=user.id, **kw)
    if kind == "inventory":
        st.inventory_prefill(db, doc)
    db.commit()
    audit(db, "stock.create", "stock_doc", doc.id, user=user, kind=kind)
    return RedirectResponse(f"/stock/docs/{doc.id}", 303)


@app.get("/stock/lookup/products")
def stock_lookup_products(q: str = "", wh: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Пошук товару для вікна «Нова поставка»: назва / артикул / штрихкод. Ціна й к-сть — в одиницях приходу."""
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse([])
    words = set(norm(q).split())
    ql = q.lower()
    rows = []
    for r in catalog.product_rows(db, []):
        p_ = r.product
        if p_.active is False:
            continue
        _m = catalog.product_match(p_, q)
        if _m is None:
            continue
        exact = _m <= 1
        k = p_.coef if p_.coef and p_.coef > 0 else 1
        rows.append((0 if exact else (1 if (p_.warehouse or "vitrina") == wh else 2), {
            "id": p_.id, "name": p_.name, "sku": p_.sku or "", "barcode": p_.barcode or "", "brand": p_.brand or "",
            "unit": "шт" if k == 1 else f"уп. ({k:g} {p_.unit or 'од.'})", "coef": k,
            "price": r.cost or p_.cost or 0, "stock": r.stock,
            "wh": catalog.WAREHOUSES.get(p_.warehouse or "vitrina"), "supplier": p_.supplier or r.last_supplier or ""}))
    rows.sort(key=lambda x: (x[0], x[1]["name"].lower()))
    return JSONResponse([x[1] for x in rows[:25]])


@app.post("/stock/recognize")
async def stock_recognize(file: UploadFile = File(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Файл накладної → постачальник, № , дата, товари (зіставлені з каталогом), склад — для вікна «Нова поставка»."""
    from .services import invoice_ai
    data = await file.read()
    if not data or len(data) > 20 * 1024 * 1024:
        return JSONResponse({"error": "Файл порожній або більший за 20 МБ"}, status_code=400)
    if invoice_ai.engine(db) == "agent" and invoice_ai.needs_ai(file.filename or ""):
        loc = await asyncio.to_thread(invoice_ai.try_local, data, file.filename or "")
        if not loc:
            j = invoice_ai.queue(db, data, file.filename or "file", file.content_type, user.id)
            audit(db, "stock.recognize_queue", user=user, file=file.filename, job=j.id)
            return JSONResponse({"queued": j.id, "message": "Передано агенту на розпізнавання"})
    try:
        parsed, src = await asyncio.to_thread(invoice_ai.extract, db, data, file.filename or "file", file.content_type)
        res = invoice_ai.match(db, parsed)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.warning("recognize: %s", e)
        return JSONResponse({"error": f"Не вдалося розпізнати: {str(e)[:160]}"}, status_code=400)
    res["source"] = src
    audit(db, "stock.recognize", user=user, file=file.filename, source=src, lines=len(res["lines"]), matched=res["matched"])
    return JSONResponse(res)


@app.get("/stock/recognize/{jid}")
def stock_recognize_poll(jid: int, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Стан накладної в черзі агента: pending / taken → чекати; done → результат як у /stock/recognize."""
    from .models import InvoiceJob
    from .services import invoice_ai
    j = db.get(InvoiceJob, jid)
    if not j:
        return JSONResponse({"error": "Задачу не знайдено"}, status_code=404)
    if j.status == "error":
        return JSONResponse({"error": "Агент не розпізнав: " + (j.error or "помилка")}, status_code=400)
    if j.status != "done":
        return JSONResponse({"queued": j.id, "status": j.status, "wait": int((datetime.now() - j.created_at).total_seconds())})
    res = invoice_ai.match(db, invoice_ai.normalize(j.result or {}))
    res["source"] = "agent"
    return JSONResponse(res)


@app.post("/stock/docs/quick")
async def stock_doc_quick(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Вікно «Нова поставка / замовлення»: шапка + рядки одним JSON. Рядки на різні склади → окремі документи
    з однією групою оплати (одна накладна). paid_by + paid=true — відмітити оплаченим цим ФОП. post=true — провести."""
    import uuid
    from .services import stock as st, payments as pay
    from .models import StockDocLine, Supplier
    data = await request.json()
    kind = data.get("kind") if data.get("kind") in ("supply", "order") else "supply"
    wh0 = data.get("wh") if data.get("wh") in catalog.WAREHOUSES else "vitrina"
    lines = [x for x in (data.get("lines") or []) if x.get("product_id") and (_num(str(x.get("qty"))) or 0) > 0]
    if not lines:
        return JSONResponse({"error": "Додайте хоча б один товар з кількістю"}, status_code=400)
    by_wh: dict[str, list] = {}
    for x in lines:
        by_wh.setdefault(x.get("wh") if x.get("wh") in catalog.WAREHOUSES else wh0, []).append(x)
    d = _day(str(data.get("date") or "")) or date.today()
    supplier = (data.get("supplier") or "").strip()[:200] or None
    paid_by = (data.get("paid_by") or "").strip()[:100] or None
    group = uuid.uuid4().hex[:12] if len(by_wh) > 1 else None
    docs = []
    for wh, ls in by_wh.items():
        doc = st.new_doc(db, kind, user_id=user.id, wh_to=wh, supplier=supplier, doc_date=d,
                         ext_no=(data.get("ext_no") or "").strip()[:100] or None,
                         note=(data.get("note") or "").strip()[:2000] or None)
        doc.pay_group, doc.paid_by = group, paid_by
        for x in ls:
            p_ = db.get(Product, int(x["product_id"]))
            if not p_:
                continue
            k = p_.coef if p_.coef and p_.coef > 0 else 1        # у вікні — одиниці приходу; в обліку — одиниці обліку
            qty, price = _num(str(x.get("qty"))) or 0, _num(str(x.get("price") or ""))
            doc.lines.append(StockDocLine(product_id=p_.id, qty=round(qty * k, 4),
                                          unit_cost=round(price / k, 4) if price is not None else None,
                                          expiry_date=_exp_day(x.get("expiry"))))
        db.flush()
        docs.append(doc)
    if supplier and not any(n.lower() == supplier.lower() for (n,) in db.execute(select(Supplier.name))):
        db.add(Supplier(name=supplier))
    db.commit()
    req = data.get("sup_req") if isinstance(data.get("sup_req"), dict) else {}
    if supplier and (req.get("edrpou") or req.get("iban")):          # реквізити з розпізнаної накладної → юр. особа
        from .services import suppliers as sp
        sx = next((x for x in db.scalars(select(Supplier)) if x.name.lower() == supplier.lower()), None)
        hit = sp.find(db, req.get("iban"), req.get("edrpou"))
        if sx and not (hit and hit[0].id == sx.id and hit[1]):
            try:
                sp.save_entity(db, sx, {"legal_name": str(req.get("name") or "")[:300], "edrpou": str(req.get("edrpou") or ""),
                                        "iban": str(req.get("iban") or "")})
            except ValueError:
                db.rollback()
    if kind == "supply" and data.get("paid") and paid_by:          # оплачено вже зараз (готівка / інший банк)
        g = pay.find_group(db, pay.group_key(docs[0]))
        if g:
            amt_ = g.left
            pay.allocate(db, g, amt_, fop=paid_by, user_id=user.id)
            db.commit()
            pay.cash_expense(db, g, amt_, paid_by, user.id)
            audit(db, "stock.pay", "stock_doc", docs[0].id, user=user, op="paid", fop=paid_by, amount=round(amt_, 2))
    for x_ in docs:
        audit(db, "stock.quick_doc", "stock_doc", x_.id, user=user, kind=kind, docs=[x.id for x in docs],
              lines=len(x_.lines))
    warn = None
    if data.get("post") and kind == "supply":
        if not st.ledger_active(db):
            warn = "Збережено чернеткою: облік складу ще не розпочато"
        else:
            for doc in docs:
                try:
                    st.post(db, doc)
                    audit(db, "stock.post", "stock_doc", doc.id, user=user, kind=kind)
                except st.StockError as e:
                    warn = f"Збережено чернеткою: {e}"
    if len(docs) > 1:
        warn = (warn + ". " if warn else "") + f"Накладну розділено на {len(docs)} склади: " + \
            ", ".join(f"{x.number} ({catalog.WAREHOUSES[x.wh_to]})" for x in docs)
    return JSONResponse({"id": docs[0].id, "ids": [x.id for x in docs], "url": f"/stock/docs/{docs[0].id}",
                         "number": docs[0].number, "warning": warn})


@app.post("/stock/docs/{did:int}/files")
async def stock_doc_files(did: int, files: list[UploadFile] = File(...), user: User = Depends(require("stock")),
                          db: Session = Depends(db_dep)):
    """Фото / скани накладної. Документи однієї накладної (кілька складів) отримують ті самі файли."""
    from .services import ingest
    from .models import StockDocFile
    doc = db.get(StockDoc, did) or (_ for _ in ()).throw(HTTPException(404))
    targets = db.scalars(select(StockDoc).where(StockDoc.pay_group == doc.pay_group)).all() if doc.pay_group else [doc]
    n = 0
    for f in files:
        data = await f.read()
        if not data or len(data) > 25 * 1024 * 1024:
            continue
        mime = f.content_type or ""
        if not (mime.startswith("image/") or mime == "application/pdf" or (f.filename or "").lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp"))):
            continue
        path = ingest.store(data, f.filename or "file")
        for t in targets:
            db.add(StockDocFile(doc_id=t.id, path=path, name=(f.filename or "файл")[:200], mime=mime[:100] or None))
        n += 1
    db.commit()
    audit(db, "stock.files", "stock_doc", did, user=user, files=n)
    return JSONResponse({"ok": True, "files": n})


@app.get("/stock/files/{fid:int}")
def stock_file(fid: int, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import StockDocFile
    f = db.get(StockDocFile, fid) or (_ for _ in ()).throw(HTTPException(404))
    return FileResponse(f.path, media_type=f.mime or None, filename=f.name,
                        content_disposition_type="inline" if (f.mime or "").startswith(("image/", "application/pdf")) else "attachment")


@app.post("/stock/files/{fid:int}/delete")
def stock_file_delete(fid: int, del_password: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import StockDocFile
    f = db.get(StockDocFile, fid)
    did = f.doc_id if f else 0
    err = _delpw(db, user, del_password, "stock_file", "stock_doc", did)
    if err:
        return RedirectResponse(_with_err(f"/stock/docs/{did}#files", err), 303)
    if f:
        name = f.name
        db.delete(f)
        db.commit()
        audit(db, "stock.file_delete", "stock_doc", did, user=user, name=name)
    return RedirectResponse(f"/stock/docs/{did}#files", 303)


@app.post("/stock/docs/{did:int}/pay")
def stock_doc_pay(did: int, action: str = Form("paid"), fop: str = Form(""), amount: str = Form(""), category: str = Form(""),
                  user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Оплата накладної вручну (усі склади групи): action=paid — оплатити залишок / суму; clear — зняти ручні відмітки."""
    from .services import payments as pay
    doc = db.get(StockDoc, did) or (_ for _ in ()).throw(HTTPException(404))
    g = pay.find_group(db, pay.group_key(doc))
    amt = None
    if g:
        if action == "clear":
            pay.clear_manual(db, g)
        else:
            amt = _num(amount) or g.left
            pay.allocate(db, g, amt, fop=fop.strip() or None, user_id=user.id)
            for d_ in g.docs:
                d_.paid_by = fop.strip() or d_.paid_by
            db.commit()
            pay.cash_expense(db, g, amt, fop.strip(), user.id, category.strip() or None)
        for d_ in g.docs:
            audit(db, "stock.pay", "stock_doc", d_.id, user=user, op=action, fop=fop or None,
                  amount=round(amt, 2) if amt and d_.id == did else None)
    return RedirectResponse(f"/stock/docs/{did}#pay", 303)


@app.get("/stock/drugs", response_class=HTMLResponse)
def stock_drugs_page(request: Request, m: int = 1, all: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import drug_stock as ds
    return render(request, "stock_drugs.html", nav="stock", v=ds.build(db, months=m if m in (1, 2, 3) else 1, show_all=bool(all)))


@app.post("/stock/drugs/order")
async def stock_drugs_order(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import order_builder as ob
    f = await request.form()
    sup = str(f.get("s") or "").strip()
    lines = []
    for k, v in f.items():
        if k.startswith("q_"):
            pid = int(k[2:])
            q, c = _num(str(v or "")), _num(str(f.get(f"c_{pid}") or ""))
            if q and q > 0:
                lines.append((pid, q, c))
    doc = ob.create(db, sup, lines, user.id)
    if not doc:
        return RedirectResponse(_with_err("/stock/drugs", f"{sup}: нічого замовляти"), 303)
    doc.note = "Сформовано із «Залишків препаратів» (критичний + потреба на місяць − в наявності)"
    db.commit()
    audit(db, "stock.drugs_order", "stock_doc", doc.id, user=user, supplier=sup, lines=len(lines))
    return RedirectResponse(_with_msg("/stock/paycal", f"Замовлення {sup} ({doc.number}) сформовано — сума в календарі оплат складу"), 303)


@app.post("/stock/drugs/promo")
def stock_drugs_promo(s: str = Form(...), text: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import drug_stock as ds
    ds.save_promo(db, s, text)
    return RedirectResponse(f"/stock/drugs#g-{quote(s)}", 303)


@app.get("/stock/paycal", response_class=HTMLResponse)
def stock_paycal_page(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock_paycal as sp
    sp.close_paid(db)
    return render(request, "stock_paycal.html", nav="stock", v=sp.build(db))


@app.post("/stock/paycal/{rid:int}")
async def stock_paycal_edit(rid: int, request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import StockPayRow
    from .services import stock_paycal as sp
    r = db.get(StockPayRow, rid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    act = str(f.get("act") or "save")
    if act == "approve":
        if user.role != "owner":
            return RedirectResponse(_with_err("/stock/paycal", "Дозвіл на оплату дає власник"), 303)
        r.approved, r.approved_by = not r.approved, user.id
    elif act == "paid":
        r.paid_at, r.paid_amount, r.paid_ref = date.today(), r.fact_amount, "manual"
    elif act == "unpaid":
        r.paid_at = r.paid_amount = r.paid_ref = r.paid_fop = None
    else:
        if "fop" in f:
            v = str(f.get("fop") or "")
            r.fop = v if v in sp.FOP_CHOICES else None
        if "fact" in f:
            r.fact_amount = _num(str(f.get("fact") or ""))
        if "plan_date" in f:
            r.plan_date = _day(str(f.get("plan_date") or ""))
        if "grp" in f and f.get("grp") in dict(sp.GROUPS):
            r.grp = str(f.get("grp"))
        if "note" in f:
            r.note = str(f.get("note") or "").strip()[:300] or None
    db.commit()
    audit(db, f"stock.paycal.{act}", "stock_pay_row", rid, user=user, supplier=r.supplier)
    return RedirectResponse(f"/stock/paycal#r{rid}", 303)


@app.get("/finance/fop-schedule", response_class=HTMLResponse)
def fop_schedule_page(request: Request, start: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fop_schedule as fs
    return render(request, "fin_fop_schedule.html", nav="finance", v=fs.view(db, _day(start) or date.today()), FIN_TABS=FIN_TABS)


@app.post("/finance/fop-schedule/weekly")
async def fop_schedule_weekly(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fop_schedule as fs
    f = await request.form()
    cfg = fs.load(db)
    weekly: dict = {}
    for k, v in f.items():
        if k.startswith("w_"):
            _, wd, did = k.split("_")
            if v in fs.FOPS:
                weekly.setdefault(wd, {})[did] = v
    cfg["weekly"] = weekly
    fs.save(db, cfg)
    audit(db, "finance.fop_schedule", user=user)
    return RedirectResponse(_with_msg("/finance/fop-schedule", "Шаблон тижня збережено"), 303)


@app.post("/finance/fop-schedule/targets")
async def fop_schedule_targets(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fop_schedule as fs
    f = await request.form()
    tg = fs.targets(db)
    vals = {fop: (_num(str(f.get(f"t_{i}") or "")) or tg["targets"][fop]) for i, fop in enumerate(fs.FOPS)}
    months = int(_num(str(f.get("months") or "")) or tg["months"])
    fs.set_targets(db, vals, max(1, min(months, 24)), _day(str(f.get("from") or "")) or tg["from"])
    audit(db, "finance.oper_fund_targets", user=user, targets=vals, months=months)
    return RedirectResponse(_with_msg("/finance/fop-schedule#targets", "Норму операційних фондів збережено"), 303)


@app.post("/finance/fop-schedule/day")
async def fop_schedule_day(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fop_schedule as fs
    f = await request.form()
    cfg = fs.load(db)
    d = _day(str(f.get("day") or ""))
    if d:
        day = cfg["dates"].setdefault(d.isoformat(), {})
        for k, v in f.items():
            if k.startswith("d_"):
                did = k[2:]
                if v == "__tpl":
                    day.pop(did, None)
                else:
                    day[did] = v if v in fs.FOPS else ""
        if not day:
            cfg["dates"].pop(d.isoformat(), None)
        cutoff = (date.today() - timedelta(days=60)).isoformat()
        cfg["dates"] = {k: v for k, v in cfg["dates"].items() if k >= cutoff}
        fs.save(db, cfg)
    return RedirectResponse(f"/finance/fop-schedule?start={quote(str(f.get('start') or ''))}#d{d.isoformat() if d else ''}", 303)


@app.get("/stock/order/build", response_class=HTMLResponse)
def stock_order_build(request: Request, s: str = "TEBISKIN", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import order_builder as ob
    from .models import Supplier
    ob.ensure_seed(db)
    sups = [x.name for x in db.scalars(select(Supplier).order_by(Supplier.name))]
    return render(request, "stock_order_build.html", nav="stock", sups=sups, sel=s, rows=ob.rows(db, s) if s else [])


@app.post("/stock/order/build")
async def stock_order_build_post(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import order_builder as ob
    f = await request.form()
    sup = str(f.get("s") or "").strip()
    lines = []
    for k, v in f.items():
        if k.startswith("q_"):
            pid = int(k[2:])
            q = _num(str(v or ""))
            c = _num(str(f.get(f"c_{pid}") or ""))
            if q and q > 0:
                lines.append((pid, q, c))
    doc = ob.create(db, sup, lines, user.id)
    if not doc:
        return RedirectResponse(_with_err(f"/stock/order/build?s={quote(sup)}", "Нічого замовляти — всі позиції в нормі"), 303)
    audit(db, "stock.order_build", "stock_doc", doc.id, user=user, supplier=sup, lines=len(lines))
    return RedirectResponse(f"/stock/docs/{doc.id}?msg=" + quote(f"Замовлення {sup} сформовано: {len(lines)} позицій — сума вже в «Календарі оплат складу»"), 303)


@app.post("/stock/order/min")
async def stock_order_min(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    f = await request.form()
    n = 0
    for k, v in f.items():
        if k.startswith("min_"):
            p = db.get(Product, int(k[4:]))
            val = _num(str(v or ""))
            if p is not None and val is not None and (p.min_stock or 0) != int(val):
                p.min_stock = int(val)
                n += 1
    db.commit()
    audit(db, "stock.min_stock", user=user, n=n)
    return JSONResponse({"ok": True, "changed": n})


@app.post("/stock/order-from-critical")
def stock_order_from_critical(supplier: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock as st
    rows = _critical_rows(catalog.product_rows(db))
    doc = st.order_from_critical(db, rows, supplier.strip() or None, user.id)
    if not doc:
        return RedirectResponse("/stock?tab=critical&msg=Немає+що+замовляти", 303)
    return RedirectResponse(f"/stock/docs/{doc.id}", 303)


@app.get("/stock/docs/{did:int}", response_class=HTMLResponse)
def stock_doc_page(did: int, request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock as st
    from .models import Supplier
    doc = db.get(StockDoc, did) or (_ for _ in ()).throw(HTTPException(404))
    bal = st.balances(db) if st.ledger_active(db) else {}
    lot_opts: dict[int, list] = {}                     # партії складу списання: товар → [(термін, к-сть)]
    src_wh = doc.wh_from or doc.wh_to
    if doc.kind in ("transfer", "writeoff", "return") and src_wh and bal:
        for (pid_, w_, e_), q_ in st.batch_balances(db, warehouse=src_wh).items():
            if e_ and q_ > 1e-6:
                lot_opts.setdefault(pid_, []).append((e_, round(q_, 3)))
        for v in lot_opts.values():
            v.sort()
    from .services import payments as pay
    from .models import StockDocFile, PaymentAlloc, BankTxn
    from .integrations.checkbox import accounts as _cb_acc
    grp = pay.find_group(db, pay.group_key(doc)) if doc.kind == "supply" else None
    allocs = db.scalars(select(PaymentAlloc).where(PaymentAlloc.doc_id == doc.id).order_by(PaymentAlloc.at)).all()
    txns = {t.id: t for t in db.scalars(select(BankTxn).where(BankTxn.id.in_([a.txn_id for a in allocs if a.txn_id] or [""])))}
    sup = next((x for x in db.scalars(select(Supplier)) if x.name.lower() == (doc.supplier or "").lower()), None) if doc.supplier else None
    return render(request, "stock_doc.html", d=doc, KINDS=st.DOC_KINDS, STATUS=st.STATUS, WAREHOUSES=catalog.WAREHOUSES,
                  files=db.scalars(select(StockDocFile).where(StockDocFile.doc_id == doc.id).order_by(StockDocFile.id)).all(),
                  grp=grp, allocs=allocs, txns=txns, PAY=pay.PAY_LABELS, MODES=pay.MODE_LABELS,
                  off_window={k for k, t_ in txns.items() if not pay.in_window(t_.at.date() if t_.at else None, doc.doc_date)},
                  win=(pay.PREPAY_DAYS, pay.POSTPAY_DAYS),
                  alloc_users={u_.id: u_.name for u_ in db.scalars(select(User))}, line_paid=pay.by_product(db, [doc.id]),
                  fops=[a["name"] for a in _cb_acc(db)], sup=sup, lot_opts=lot_opts, today=date.today(),
                  cash_boxes=pay.cash_boxes(db), sup_arts=pay.supply_articles(db), CASH_PREFIX=pay.CASH_PREFIX,
                  group_docs=[x for x in (grp.docs if grp else []) if x.id != doc.id],
                  REASONS=st.WRITEOFF_REASONS, products=_product_options(db), bal=bal, active=st.ledger_active(db),
                  suppliers=[x.name for x in db.scalars(select(Supplier).order_by(Supplier.name))],
                  history=__import__("app.services.doc_history", fromlist=["history"]).history(db, doc),
                  editable=doc.status in ("draft", "sent"))


@app.post("/stock/docs/{did:int}")
async def stock_doc_save(did: int, request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Шапка + усі рядки однією формою; action: save | post | add."""
    from .services import stock as st
    from .models import StockDocLine
    doc = db.get(StockDoc, did) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    action = f.get("action", "save")
    from .services import doc_history as dh
    before = dh.snapshot(doc)
    if doc.status in ("draft", "sent"):
        doc.doc_date = _day(f.get("doc_date", "")) or doc.doc_date
        for k in ("supplier", "ext_no", "note", "reason"):
            if k in f:
                setattr(doc, k, (f.get(k) or "").strip()[:300] or None)
        for k in ("wh_from", "wh_to"):
            if k in f:
                setattr(doc, k, f.get(k) if f.get(k) in catalog.WAREHOUSES else None)
        for ln in list(doc.lines):
            if f.get(f"del_{ln.id}"):
                db.delete(ln)
                continue
            if f"qty_{ln.id}" in f:
                ln.qty = _num(f.get(f"qty_{ln.id}")) or 0
            if f"cost_{ln.id}" in f:
                ln.unit_cost = _num(f.get(f"cost_{ln.id}"))
            if f"exp_{ln.id}" in f:
                ln.expiry_date = _exp_day(f.get(f"exp_{ln.id}"))
        new_ref = (f.get("new_product") or "").strip()
        if new_ref:
            pr = _pick_product(db, new_ref)
            if not pr:
                db.commit()
                return RedirectResponse(f"/stock/docs/{did}?err=Товар+не+знайдено:+{new_ref[:60]}#add", 303)
            new_exp = _exp_day(f.get("new_exp"))
            line = next((ln for ln in doc.lines if ln.product_id == pr.id and ln.expiry_date == new_exp), None)   # інший термін — нова партія
            if line and doc.kind != "inventory":
                line.qty = (line.qty or 0) + (_num(f.get("new_qty")) or 1)
            elif not line:
                cost = _num(f.get("new_cost"))
                if cost is None and doc.kind in ("supply", "order"):
                    rr = next((r for r in catalog.product_rows(db, []) if r.product.id == pr.id), None)
                    cost = (rr.cost) if rr else pr.cost
                doc.lines.append(StockDocLine(product_id=pr.id, qty=_num(f.get("new_qty")) or 1, unit_cost=cost,
                                              expiry_date=_exp_day(f.get("new_exp"))))
        db.commit()
        db.refresh(doc)
        changes = dh.diff(before, dh.snapshot(doc))
        if changes:
            audit(db, "stock.edit", "stock_doc", doc.id, user=user, changes=changes)
    try:
        if action == "post":
            st.post(db, doc)
            audit(db, "stock.post", "stock_doc", doc.id, user=user, kind=doc.kind)
            return RedirectResponse(f"/stock/docs/{did}?msg=Проведено", 303)
        if action == "send" and doc.kind == "order":
            doc.status = "sent"
            db.commit()
            audit(db, "stock.send", "stock_doc", doc.id, user=user)
            return RedirectResponse(f"/stock/docs/{did}?msg=Позначено+як+відправлене", 303)
        if action == "receive":
            sup = st.receive_order(db, doc, user.id)
            audit(db, "stock.receive", "stock_doc", sup.id, user=user, order=doc.number)
            return RedirectResponse(f"/stock/docs/{sup.id}?msg=Перевірте+кількості+й+ціни+та+проведіть+поставку", 303)
        if action == "cancel":
            st.cancel(db, doc)
            audit(db, "stock.cancel", "stock_doc", doc.id, user=user, kind=doc.kind)
            return RedirectResponse(f"/stock/docs/{did}?msg=Скасовано", 303)
        if action == "prefill" and doc.kind == "inventory":
            n = st.inventory_prefill(db, doc)
            return RedirectResponse(f"/stock/docs/{did}?msg=Додано+{n}+товарів", 303)
    except st.StockError as e:
        return RedirectResponse(f"/stock/docs/{did}?err={e}", 303)
    return RedirectResponse(f"/stock/docs/{did}" + ("#add" if f.get("new_product") else ""), 303)


@app.post("/stock/suppliers")
def stock_supplier_save(sid: str = Form(""), name: str = Form(...), contact: str = Form(""), phone: str = Form(""),
                        email: str = Form(""), terms: str = Form(""), note: str = Form(""),
                        legal_name: str = Form(""), edrpou: str = Form(""), iban: str = Form(""), bank: str = Form(""),
                        pay_purpose: str = Form(""), address: str = Form(""), contract: str = Form(""), vat: str = Form(""),
                        back: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import Supplier
    x = db.get(Supplier, int(sid)) if sid.isdigit() else None
    if back and x:                                         # з вікна постачальника: лише контакти, реквізити — в юр. особах
        old = x.name
        x.name, x.contact, x.phone, x.email = name.strip()[:200] or x.name, contact.strip() or None, phone.strip() or None, email.strip() or None
        x.terms, x.note = terms.strip()[:300] or None, note.strip() or None
        if old != x.name:
            for p_ in db.scalars(select(Product).where(Product.supplier == old)):
                p_.supplier = x.name
            for d_ in db.scalars(select(StockDoc).where(StockDoc.supplier == old)):
                d_.supplier = x.name
        db.commit()
        return RedirectResponse(_card_back(back, x.id), 303)
    if not x:
        x = db.scalar(select(Supplier).where(Supplier.name == name.strip())) or Supplier(name=name.strip()[:200])
        db.add(x)
    old = x.name
    x.name, x.contact, x.phone, x.email = name.strip()[:200], contact.strip() or None, phone.strip() or None, email.strip() or None
    x.terms, x.note = terms.strip()[:300] or None, note.strip() or None
    import re as _re
    x.legal_name, x.bank = legal_name.strip()[:300] or None, bank.strip()[:200] or None
    x.edrpou = _re.sub(r"\D", "", edrpou)[:20] or None
    x.iban = _re.sub(r"\s", "", iban).upper()[:40] or None
    x.pay_purpose, x.address = pay_purpose.strip()[:300] or None, address.strip()[:300] or None
    x.contract, x.vat = contract.strip()[:200] or None, bool(vat) or None
    if x.iban and not _re.fullmatch(r"UA\d{27}", x.iban):
        db.commit()
        return RedirectResponse(f"/stock?tab=suppliers&s={_q(x.name)}&msg=IBAN+має+формат+UA+і+27+цифр+—+перевірте#sup", 303)
    if old and old != x.name:                              # перейменування — і в товарах
        for p_ in db.scalars(select(Product).where(Product.supplier == old)):
            p_.supplier = x.name
    db.commit()
    return RedirectResponse(f"/stock?tab=suppliers&s={_q(x.name)}&card={x.id}#sup", 303)


@app.post("/stock/suppliers/{sid:int}/fx")
def stock_supplier_fx(sid: int, usd: str = Form(""), eur: str = Form(""), back: str = Form(""),
                      user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Курс постачальника (USD / EUR) — ним рахується закупівля в гривнях у «Плануванні ціни» для товарів цього постачальника."""
    from .models import Supplier
    x = db.get(Supplier, sid) or (_ for _ in ()).throw(HTTPException(404))
    old = (x.fx_usd, x.fx_eur)
    x.fx_usd, x.fx_eur = (_num(usd) or None), (_num(eur) or None)
    x.fx_at = datetime.now() if (x.fx_usd or x.fx_eur) else None
    db.commit()
    audit(db, "supplier.fx", "supplier", x.id, user=user, old=old, usd=x.fx_usd, eur=x.fx_eur)
    return RedirectResponse(_card_back(back, x.id, "Курс постачальника збережено"), 303)


def _card_back(back: str, sid: int, msg: str = "") -> str:
    from urllib.parse import urlencode, urlsplit, parse_qsl
    u = urlsplit(back if back.startswith("/") and not back.startswith("//") else "/stock?tab=suppliers")
    qs = [(k, v) for k, v in parse_qsl(u.query) if k not in ("card", "msg")] + [("card", str(sid))] + ([("msg", msg)] if msg else [])
    return f"{u.path}?{urlencode(qs)}"


@app.get("/stock/suppliers/card", response_class=HTMLResponse)
def stock_supplier_card_by_name(request: Request, name: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import Supplier
    key = name.strip().lower()
    x = next((s_ for s_ in db.scalars(select(Supplier)) if s_.name.lower() == key), None) if key else None
    if not x and key:
        x = Supplier(name=name.strip()[:200])
        db.add(x)
        db.commit()
    if not x:
        return HTMLResponse("Постачальника не знайдено", 404)
    return stock_supplier_card(request, x.id, user, db)


@app.get("/stock/suppliers/{sid}/card", response_class=HTMLResponse)
def stock_supplier_card(request: Request, sid: int, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import Supplier
    from .services import suppliers as sp, payments as pay
    x = db.get(Supplier, sid)
    if not x:
        return HTMLResponse("Постачальника не знайдено", 404)
    return templates.TemplateResponse(request, "_supplier_card.html", {"request": request, "user": user, "PAY": pay.PAY_LABELS,
                                                                        **sp.card(db, x)})


@app.post("/stock/suppliers/{sid}/entity")
async def stock_supplier_entity(request: Request, sid: int, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import Supplier
    from .services import suppliers as sp
    f = await request.form()
    x = db.get(Supplier, sid)
    back = str(f.get("back") or "/stock?tab=suppliers")
    if not x:
        return RedirectResponse(back, 303)
    eid = str(f.get("eid") or "")
    try:
        e = sp.save_entity(db, x, {k: str(v) for k, v in f.items()}, int(eid) if eid.isdigit() else None)
    except ValueError as err:
        db.rollback()
        return RedirectResponse(_card_back(back, sid, f"Не збережено: {err}"), 303)
    audit(db, "supplier.entity_save", "supplier", sid, user=user, entity_id_=e.id, kind=e.kind)
    return RedirectResponse(_card_back(back, sid), 303)


@app.post("/stock/suppliers/{sid}/entity/{eid}/delete")
def stock_supplier_entity_delete(sid: int, eid: int, back: str = Form(""), del_password: str = Form(""),
                                 user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import Supplier
    from .services import suppliers as sp
    err = _delpw(db, user, del_password, "supplier_entity", "supplier", sid)
    if err:
        return RedirectResponse(_card_back(back, sid, err), 303)
    x = db.get(Supplier, sid)
    if x:
        sp.delete_entity(db, x, eid)
        audit(db, "supplier.entity_delete", "supplier", sid, user=user, entity_id_=eid)
    return RedirectResponse(_card_back(back, sid), 303)


@app.post("/stock/docs/{did}/archive")
def stock_doc_archive(did: int, del_password: str = Form(""), note: str = Form(""), back: str = Form(""),
                      user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Видалення документа в архів — лише з паролем поточного користувача."""
    from .services import stock as st
    doc = db.get(StockDoc, did)
    back = back if back.startswith("/") and not back.startswith("//") else (f"/stock?tab={doc.kind}" if doc else "/stock")
    sep = "&" if "?" in back else "?"
    if not doc:
        return RedirectResponse(back, 303)
    if len(note.strip()) < 3:
        return RedirectResponse(f"{back}{sep}err={_q('Вкажіть причину видалення')}", 303)
    err = _delpw(db, user, del_password, "stock_doc", "stock_doc", did)
    if err:
        return RedirectResponse(f"{back}{sep}err={_q(err)}", 303)
    try:
        num, prev = doc.number, doc.status
        st.archive(db, doc, user.id, note)
    except st.StockError as e:
        return RedirectResponse(f"{back}{sep}err={_q(str(e))}", 303)
    audit(db, "stock.archive", "stock_doc", did, user=user, kind=doc.kind, number=num, was=prev, note=note[:300])
    if back.startswith(f"/stock/docs/{did}"):
        back, sep = f"/stock?tab={doc.kind}", "&"
    return RedirectResponse(f"{back}{sep}msg={_q(f'Документ {num} видалено в архів')}", 303)


@app.post("/stock/docs/{did}/restore")
def stock_doc_restore(did: int, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import stock as st
    doc = db.get(StockDoc, did)
    if not doc:
        return RedirectResponse("/stock", 303)
    st.restore(db, doc)
    audit(db, "stock.restore", "stock_doc", did, user=user, kind=doc.kind, number=doc.number)
    return RedirectResponse(f"/stock/docs/{did}?msg={_q('Відновлено з архіву' + (' як чернетку — перевірте й проведіть' if doc.status == 'draft' else ''))}", 303)


def _delpw(db: Session, user: User, pw: str, what: str = "", entity: str | None = None, eid: int | None = None) -> str | None:
    """Пароль видалення: None — можна; інакше текст помилки (і запис у журнал)."""
    from .security import delete_password_error
    err = delete_password_error(user, pw)
    if err:
        audit(db, "delete.denied", entity, eid, user=user, what=what, reason="no_pw" if not user.delete_pw_hash else "wrong")
    return err


def _with_err(url: str, err: str) -> str:
    base, _, frag = url.partition("#")
    return f"{base}{'&' if '?' in base else '?'}delerr={_q(err)}" + (f"#{frag}" if frag else "")


def _q(v: str) -> str:
    from urllib.parse import quote
    return quote(v or "")


# ---------------- monobank: оплати постачальникам ----------------
def _mono_job(job, days: int = 2):
    from .integrations import monobank
    with SessionLocal() as db:
        job.log("Завантажую виписку monobank…")
        return monobank.sync(db, days=days)


CB_SETUP = "https://molodist.cbox.online/new/#setup/"
# Налаштування: блоки як у CleverBox. Пункт: (назва, опис, посилання в CRM | None, право, шлях у CleverBox | None)
SETTINGS_BLOCKS = [
    ('Клініка', 'clinic', 'Загальні параметри, філії, команда й графік', [
        ('Загальні', 'Країна, часовий пояс, мова, валюта, ПДВ; налаштування звітів', '/settings/clinic', 'users', 'main_settings'),
        ('Філії', 'Локації: ресурси (апарати), режим роботи, робочі місця (кабінети)', '/settings/clinic#loc', 'users', 'salons'),
        ('Команда', 'Лікарі й співробітники: посади, KPI, привʼязка до CleverBox', '/specialists', 'specialists', 'sfields/staff'),
        ('Графік роботи', 'Зміни спеціалістів по днях і кабінетах', '/specialists/schedule', 'schedule', None),
        ('Джерела запису', 'Звідки пацієнт записався: дзвінок, Instagram, повторний…: список для вікна візиту й звіт', '/settings/booking-sources', 'users', None),
        ('Статуси візитів', 'Записаний, підтверджено, оплачено ₴, скасовано…: назви, кольори в календарі, свої статуси', '/settings/visit-statuses', 'users', None),
        ('Онлайн-бронювання', 'Віджет запису на сайті', 'dev', None, None),
    ]),
    ('Співробітники', 'team', 'Посади, оклади, категорії й нарахування', [
        ('Посади співробітників', 'Директор, адміністратори, лікарі…: довідник посад для картки спеціаліста', '/settings/positions', 'specialists_edit', 'dirs/prof'),
        ('Поля співробітників', 'Свої поля для картки співробітника: дата народження, медкнижка, паспорт, стать, клініка…', '/settings/staff-fields', 'specialists_edit', 'sfields/staff'),
        ('KPI лікарів', 'Цілі (завантаження, догляд, поверненість, дисципліна…), шкала премії, податки клініки й план обороту по лікарю — для звіту «KPI лікарів»', '/settings/doctor-kpi', 'finance', None),
        ('Налаштування окладів', 'Фіксована ставка, комісійні за послуги й товари, % від обороту — шаблони для зарплати', '/settings/salary', 'finance', 'salary'),
        ('Категорії спеціалістів', 'Ціни за категоріями: інші ціни на послуги для головного лікаря, топ-лікаря, стажера', '/settings/price-cats', 'finance', 'cmarket/customprice/main'),
        ('Статті для нарахувань', 'Премії, штрафи, лікарняні, податки, відпускні — для коригувань зарплати', '/settings/accrual-articles', 'finance', 'dirs/stype'),
        ('Бонус за продаж косметики', 'Відсоток лікарям з продажу товарів: без знижки, зі знижкою, поріг ціни; свої % для лікарів, брендів і товарів', '/settings/cosmetics-pct', 'finance', None),
        ('Сповіщення в Telegram', 'Бот для адміністраторів, лікарів, головного лікаря й власника: записи на день, нагадування по планах, ДН пацієнтів, задачі, підсумок дня / тижня / місяця — що й о котрій', '/settings/tg-notify', 'users', None),
        ('Застосунок для спеціаліста', 'Записи й нарахування у телефоні спеціаліста', 'dev', None, None),
    ]),
    ('Фінанси', 'finance', 'Каси, статті, оплати й фіскалізація', [
        ('Каси / рахунки', 'Готівка, рахунки ФОП, термінал, онлайн-оплати: залишок після кожної операції, локація, тип', '/settings/cashboxes', 'finance', None),
        ('Статті фінансових операцій', 'Довідник статей доходів і витрат: реклама, податки, персонал, закупки…', '/settings/articles', 'finance', 'dirs/ftype'),
        ('Знижки', 'Правила знижок: акції, дні тижня, категорії, автозастосування; персональні знижки', '/settings/loyalty', 'finance', None),
        ('Бонусна програма', '% нарахування бонусів, оплата бонусами, термін дії, вітальні бонуси, перенос залишків з CleverBox', 'dev', None, None),
        ('Сертифікати', 'Шаблони подарункових сертифікатів для продажу в касі (номінал, термін дії, ціна), нагадування адміністраторам, розрахунок сертифікатом', '/gifts?tab=types', 'finance', 'certs/types'),
        ('Платіжні системи', 'Посилання на оплату для пацієнтів: monobank еквайринг, LiqPay, WayForPay', '/settings/payments', 'finance', 'payment_system'),
        ('Рахунки на оплату', 'Рахунки постачальників з Telegram-групи → погодження власником → чернетка платежу в monobank на підпис', '/payreq', 'finance', None),
        ('Банк monobank', 'Виписки ФОП, оплати постачальникам і їх автоматичне рознесення', '/settings/keys?s=mono#mono', 'finance', None),
        ('Checkbox', 'Фіскалізація чеків CRM, каси (РРО), відправлення чеків; оборот ФОП — у «Звіти → Ліміти ФОП»', '/settings/checkbox', 'finance', 'cmarket/checkbox/main'),
    ]),
    ('Комунікації', 'chat', 'Канали звʼязку з пацієнтами', [
        ('Канали', 'SMS і Viber (TurboSMS), Telegram-бот, Instagram та Facebook', '/settings/channels', 'users', 'cmarket/channels/main'),
        ('Шаблони відповідей', 'Готові відповіді для Instagram, Facebook, SMS, Viber і Telegram зі змінними {імʼя}, {запис}… — вставляються в чат одним кліком', '/settings/reply-templates', 'users', None),
        ('Повідомлення пацієнтам · в розробці', 'Зараз нічого не надсилається пацієнтам. Розсилки й нагадування CRM; автоповідомлення — у CleverBox', '/campaigns', 'campaigns', 'messages'),
        ('IP-телефонія', 'Binotel, Stream Telecom, Ringostat: дзвінки → звернення', '/settings/telephony', 'users', 'cmarket/siphone/main'),
    ]),
    ('Звернення', 'inbox', 'Довідники для заявок і дзвінків', [
        ('Джерела звернень', None, '/settings/appeals?open=sources', 'users', 'dirs/appeal_type'),
        ('Мітки звернень', None, '/settings/appeals?open=marks', 'users', 'dirs/appeal_mark'),
        ('Статуси звернень', None, '/settings/appeals?open=statuses', 'users', 'dirs/appeal_status'),
        ('Причини відмов', None, '/settings/appeals?open=deny_reasons', 'users', 'dirs/appeal_deny'),
        ('Причини звернень', None, '/settings/appeals?open=causes', 'users', 'dirs/appeal_cause'),
    ]),
    ('Пацієнти', 'patients', 'Фото, документи й персональні знижки пацієнтів', [
        ('Статуси пацієнтів', 'Первинний, постійний, ФАНАТ…: назва, колір і з якої кількості візитів присвоюється', '/settings/patient-tags', 'users', None),
        ('Групи пацієнтів', 'Довідник груп (ЖК, Трихолог, -30% ЛЄ…): додати, перейменувати, колір — у картці пацієнта групи вибираються зі списку', '/settings/patient-tags#groups', 'users', None),
        ('Дані пацієнта в календарі', 'Які поля картки пацієнта показувати в блоці візиту: завжди або при наведенні мишки', '/settings/patient-tags#cal', 'users', None),
        ('Амортизація обладнання', 'Апарати й розхідники з ресурсом: вартість, ставка за процедуру/імпульс, рахунок фонду; перенесення з таблиці', '/settings/amort', 'finance', None),
        ('Колонки списку пацієнтів', 'Які колонки видно в розділі «Пацієнти»: телефон, статус, групи, бонуси, ДН, джерело…', '/settings/patient-tags#cols', 'users', None),
        ('Фото пацієнтів', 'Усі фото й файли пацієнтів: згоди, обличчя, тіло, документи — перегляд, перенесення між розділами, видалення', '/settings/patient-photos', 'upload', None),
        ('Персональні знижки', 'Знижка конкретному пацієнту: окремо % на товари і % на послуги; автозастосування й сумування з іншими знижками', '/settings/personal-discounts', 'loyalty_patient', None),
    ]),
    ('Склад і товари', 'box', 'Списання, терміни, накладні, сайт', [
        ('Норми списання', 'Що списується зі складу після процедури', '/stock?tab=norms', 'stock', None),
        ('Спільні розхідники візиту', 'Рукавички, маски, простирадла… — один комплект на візит, навіть якщо в ньому кілька послуг', '/settings/shared-materials', 'stock', None),
        ('Сповіщення про терміни', 'Кому й коли надсилати попередження про терміни придатності', '/reports?r=expiry#notify', 'finance', None),
        ('Розпізнавання накладних', 'Ключ Claude API — фото/PDF накладної → документ поставки', '/settings/keys?s=ai#ai', 'finance', None),
        ('molodist.store', 'Shop-Express: посилання й фото товарів з сайту', '/settings/keys?s=shop#shop', 'finance', None),
    ]),
    ('Доступ і безпека', 'lock', 'Хто що бачить і може змінювати', [
        ('Права доступу', 'Ролі та їх дозволи, користувачі CRM, паролі входу й видалення', '/access/roles', 'users', 'roles'),
        ('Паролі для видалення', 'Окремий у кожного користувача — без нього нічого не видаляється', '/access', 'users', None),
        ('Журнал дій', 'Хто що і коли робив у CRM', '/access/audit', 'users', None),
        ('Мій кабінет', 'Пароль входу, Telegram для сповіщень', '/me', None, None),
    ]),
    ('Дані й інтеграції', 'data', 'Синхронізація, копії, API', [
        ('Дані й синхронізація', 'Імпорт / експорт, синхронізація з CleverBox', '/data', 'export_reports', None),
        ('Резервні копії', 'Щоночі о 03:30 і вручну', '/data#backup', 'export_reports', None),
        ('REST API', 'API-ключі CRM і вебхуки; токен CleverBox — у «Дані й синхронізація»', '/access/api', 'api', 'cmarket/rest/main'),
        ('Зіставлення послуг', 'Позиції плану ↔ послуги CleverBox', '/mapping', 'mapping', None),
    ]),
]


@app.get("/settings/tg-notify", response_class=HTMLResponse)
def tg_notify_page(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import tg_notify as tn
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all()
    c = tn.cfg(db)
    have = {r: [u for u in users if u.telegram_chat_id and (u.role == r if r != "chief" else u.id in tn.chief_users(db))] for r in tn.ROLES}
    qp_ = request.query_params
    cur = next((x for x in c["custom"] if str(x["id"]) == qp_.get("edit")), None)
    return render(request, "tg_notify.html", nav="settings", cfg=c, TYPES=tn.TYPES, ROLES=tn.ROLES, DOW=tn.DOW, users=users, have=have, PER=tn.PER, cur=cur,
                  bot_ok=bool(settings.telegram_bot_token), chief_ids=tn.chief_users(db))


@app.post("/settings/tg-notify")
async def tg_notify_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import tg_notify as tn
    f = await request.form()
    act = str(f.get("action") or "")
    if act in ("custom_save", "custom_test"):
        x = tn.custom_save(db, {"id": f.get("cid"), "title": f.get("c_title"), "text": f.get("c_text"), "roles": f.getlist("c_roles"),
                                "time": f.get("c_time"), "per": f.get("c_per"), "dow": f.get("c_dow"), "day": f.get("c_day") or 1,
                                "date": f.get("c_date"), "on": bool(f.get("c_on"))})
        audit(db, "settings.tg_notify_custom", user=user, id=x["id"])
        if act == "custom_test" and user.telegram_chat_id:
            tn.send_text(user.telegram_chat_id, tn.custom_text(x))
            return RedirectResponse(_with_msg("/settings/tg-notify", "Збережено й надіслано вам у Telegram") + "#custom", 303)
        return RedirectResponse(_with_msg("/settings/tg-notify", f"Повідомлення «{x['title']}» збережено") + "#custom", 303)
    if act == "custom_del":
        tn.custom_delete(db, int(f.get("cid") or 0))
        return RedirectResponse(_with_msg("/settings/tg-notify", "Видалено") + "#custom", 303)
    if f.get("action") == "test":
        key, role = str(f.get("key") or ""), str(f.get("role") or "")
        if key not in tn.BUILDERS or role not in tn.ROLES:
            raise HTTPException(400)
        if not user.telegram_chat_id:
            return RedirectResponse(_with_err("/settings/tg-notify", "Укажіть свій Telegram у «Мій кабінет», щоб отримати тест"), 303)
        text = tn.BUILDERS[key](db, user, role) or f"({tn.TYPE[key][1]}: сьогодні нічого немає — повідомлення не надсилається)"
        ok = tn.send_text(user.telegram_chat_id, text)
        return RedirectResponse(_with_msg("/settings/tg-notify", "Тест надіслано вам у Telegram" if ok else "Не вдалося надіслати — перевірте бота", err=not ok), 303)
    roles = {}
    for role in tn.ROLES:
        roles[role] = {}
        for key, *_ in tn.TYPES:
            if f"{role}_{key}_time" in f:
                roles[role][key] = {"on": bool(f.get(f"{role}_{key}_on")), "time": str(f.get(f"{role}_{key}_time") or "09:00")[:5],
                                    "dow": str(f.get(f"{role}_{key}_dow") or "mon")}
    chiefs = [int(x) for x in f.getlist("chiefs") if str(x).isdigit()]
    tn.save(db, roles, chiefs, str(f.get("greeting") or ""), int(f.get("bday_days") or 3) if str(f.get("bday_days") or "").isdigit() else 3)
    audit(db, "settings.tg_notify", user=user)
    return RedirectResponse(_with_msg("/settings/tg-notify", "Збережено"), 303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    """Налаштування: усе в одному місці, блоками як у CleverBox. Що є в CRM — відкривається тут, решта — у CleverBox."""
    from .integrations import monobank, checkbox as chb, shop_express
    from .services import invoice_ai
    st_ = {}
    try:
        st_["/settings/keys?s=mono#mono"] = f"підключено {len(monobank.saved(db))} ФОП" if monobank.saved(db) else "не підключено"
        from .services import paylinks as _pls
        _pp = _pls.public(db)
        _ra = [a_ for a_ in _pp["accounts"] if a_["ready"]]
        st_["/settings/payments"] = f"підключено {len(_ra)}" if _ra else "не підключено"
        st_["/settings/keys?s=ai#ai"] = "агент на VPS" if invoice_ai.engine(db) == "agent" else ("ключ задано" if invoice_ai.api_key(db) else "не задано")
        st_["/settings/keys?s=shop#shop"] = "підключено" if shop_express.creds(db).get("login") else "не підключено"
        acc = chb.accounts(db)
        st_["/reports?r=fop"] = f"підключено {len(acc)} ФОП" if acc else "не підключено"
        n_keys = sum(bool(x) for x in (monobank.saved(db), acc, invoice_ai.api_key(db), shop_express.creds(db).get("login")))
        st_["/settings/keys"] = f"підключено {n_keys} з 4"
        users = db.scalars(select(User).where(User.active.is_(True))).all()
        st_["/access"] = f"паролі видалення: {sum(1 for u_ in users if u_.delete_pw_hash)} з {len(users)}"
    except Exception:  # noqa: BLE001
        pass
    blocks = []
    for title, icon, about, items in SETTINGS_BLOCKS:
        rows = [{"t": t, "d": d, "url": u if (u and u != "dev" and (not p or can(user, p))) else None, "cb": (CB_SETUP + c) if c else None,
                 "dev": u == "dev", "status": st_.get(u) if u else None} for t, d, u, p, c in items]
        rows = [r for r in rows if r["url"] or r["cb"] or r["dev"]]
        if rows:
            blocks.append({"title": title, "icon": icon, "about": about, "rows": rows, "ext": all(not r["url"] for r in rows)})
    return render(request, "settings.html", blocks=blocks)


# ---------------- Налаштування → Посади співробітників ----------------
@app.get("/settings/positions", response_class=HTMLResponse)
def settings_positions(request: Request, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import positions as ps
    return render(request, "positions.html", nav="settings", items=ps.items(db), use=ps.usage(db))


@app.post("/settings/positions")
def settings_positions_save(pid: str = Form(""), name: str = Form(""), user: User = Depends(require("specialists_edit")),
                            db: Session = Depends(db_dep)):
    from .services import positions as ps
    err = ps.save(db, int(pid) if pid.isdigit() else None, name)
    if not err:
        audit(db, "positions.save", "position", int(pid) if pid.isdigit() else None, user=user, name=name)
    return RedirectResponse(_with_msg("/settings/positions", err or "Збережено", bool(err)), 303)


@app.post("/settings/positions/{pid:int}/toggle")
def settings_positions_toggle(pid: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import positions as ps
    ps.toggle(db, pid)
    return RedirectResponse("/settings/positions", 303)


@app.post("/settings/positions/{pid:int}/delete")
def settings_positions_delete(pid: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import positions as ps
    ps.delete(db, pid)
    audit(db, "positions.delete", "position", pid, user=user)
    return RedirectResponse(_with_msg("/settings/positions", "Посаду видалено"), 303)


@app.post("/settings/positions/order")
async def settings_positions_order(request: Request, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import positions as ps
    js = await request.json()
    ps.reorder(db, [int(x) for x in js.get("ids") or [] if str(x).isdigit()])
    return JSONResponse({"ok": True})


# ---------------- Налаштування → Категорії спеціалістів (ціни за категоріями) ----------------
@app.get("/settings/price-cats", response_class=HTMLResponse)
def settings_price_cats(request: Request, tab: str = "cats", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import price_cats as pc
    d = pc.get(db)
    docs = db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all()
    groups: dict = {}
    for s_ in db.scalars(select(CbService).order_by(CbService.name)):
        groups.setdefault(s_.category or "Без категорії", []).append(s_)
    return render(request, "price_cats.html", nav="settings", d=d, docs=docs, groups=sorted(groups.items(), key=lambda kv: kv[0].lower()),
                  tab=tab if tab in ("cats", "staff", "prices") else "cats")


@app.post("/settings/price-cats")
async def settings_price_cats_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import price_cats as pc
    f = await request.form()
    tab = f.get("tab") or "cats"
    if tab == "cats":
        ids, names, ons = f.getlist("cat_id"), f.getlist("cat_name"), f.getlist("cat_on")
        pc.save_main(db, f.get("on") == "1", str(f.get("base_name") or ""),
                     [(int(i) if str(i).isdigit() else None, n, o == "1") for i, n, o in zip(ids, names, ons)])
    elif tab == "staff":
        pc.save_staff(db, {k[4:]: int(v) for k, v in f.items() if k.startswith("doc_") and str(v).isdigit()})
    else:
        prices: dict = {}
        for k, v in f.items():                                  # p_{cat}_{service}
            if k.startswith("p_") and str(v).strip():
                _, cid, sid = k.split("_", 2)
                val = _num(str(v))
                if val is not None:
                    prices.setdefault(cid, {})[sid] = val
        pc.save_prices(db, prices)
    audit(db, "price_cats.save", "price_cats", None, user=user, tab=tab)
    return RedirectResponse(_with_msg(f"/settings/price-cats?tab={tab}", "Збережено"), 303)


# ---------------- Налаштування → Статті для нарахувань ----------------
@app.get("/settings/accrual-articles", response_class=HTMLResponse)
def settings_accrual_articles(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import accrual_articles as aa
    from .models import SalaryAdj
    use: dict = {}
    for (n_,) in db.execute(select(SalaryAdj.article).where(SalaryAdj.article.is_not(None))):
        use[n_] = use.get(n_, 0) + 1
    return render(request, "accrual_articles.html", nav="settings", items=aa.items(db), KINDS=aa.KINDS, use=use)


@app.post("/settings/accrual-articles")
def settings_accrual_articles_save(aid: str = Form(""), name: str = Form(""), kind: str = Form("income"),
                                   user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import accrual_articles as aa
    err = aa.save(db, int(aid) if aid.isdigit() else None, name, kind)
    if not err:
        audit(db, "accrual_article.save", "accrual_article", int(aid) if aid.isdigit() else None, user=user, name=name, kind=kind)
    return RedirectResponse(_with_msg("/settings/accrual-articles", err or "Збережено", bool(err)), 303)


@app.post("/settings/accrual-articles/{aid:int}/toggle")
def settings_accrual_articles_toggle(aid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import accrual_articles as aa
    aa.toggle(db, aid)
    return RedirectResponse("/settings/accrual-articles", 303)


@app.post("/settings/accrual-articles/order")
async def settings_accrual_articles_order(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import accrual_articles as aa
    js = await request.json()
    aa.reorder(db, [int(x) for x in js.get("ids") or [] if str(x).isdigit()])
    return JSONResponse({"ok": True})


# ---------------- Налаштування → Оклади співробітників ----------------
@app.get("/settings/salary", response_class=HTMLResponse)
def settings_salary(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import salary_plans as spl
    plans = spl.items(db)
    use: dict = {}
    for d_ in db.scalars(select(Doctor).where(Doctor.extra.is_not(None))):
        nm = (d_.extra or {}).get("salary")
        if nm:
            use.setdefault(nm, []).append(d_.name)
    from .models import Product as _Pr
    lists = {"service": sorted({r_[0] for r_ in db.execute(select(CbService.name)) if r_[0]}),
             "service_cat": sorted({r_[0] for r_ in db.execute(select(CbService.category)) if r_[0]}),
             "product": sorted({r_[0] for r_ in db.execute(select(_Pr.name)) if r_[0]}),
             "product_cat": sorted({r_[0] for r_ in db.execute(select(_Pr.category)) if r_[0]}),
             "brand": sorted({r_[0] for r_ in db.execute(select(_Pr.brand)) if r_[0]})}
    return render(request, "salary_plans.html", nav="settings", plans=plans, use=use, spl=spl, lists=lists,
                  open_=request.query_params.get("open", ""))


@app.post("/settings/salary")
async def settings_salary_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import salary_plans as spl
    f = await request.form()
    pid = int(f.get("pid")) if str(f.get("pid") or "").isdigit() else None
    plan = spl.from_form(f)
    err = spl.save(db, pid, plan)
    if not err:
        audit(db, "salary_plan.save", "salary_plan", pid, user=user, name=plan["name"])
    return RedirectResponse(_with_msg("/settings/salary", err or f"Оклад «{plan['name']}» збережено", bool(err)), 303)


@app.post("/settings/salary/{pid:int}/copy")
def settings_salary_copy(pid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import salary_plans as spl
    nid = spl.copy_plan(db, pid)
    audit(db, "salary_plan.copy", "salary_plan", pid, user=user, new=nid)
    return RedirectResponse(f"/settings/salary?open={nid}&msg=" + _q("Копію створено"), 303)


@app.post("/settings/salary/{pid:int}/delete")
def settings_salary_delete(pid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import salary_plans as spl
    spl.delete(db, pid)
    audit(db, "salary_plan.delete", "salary_plan", pid, user=user)
    return RedirectResponse(_with_msg("/settings/salary", "Оклад видалено"), 303)


@app.post("/settings/salary/order")
async def settings_salary_order(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import salary_plans as spl
    js = await request.json()
    spl.reorder(db, [int(x) for x in js.get("ids") or [] if str(x).isdigit()])
    return JSONResponse({"ok": True})


# ---------------- Налаштування → Поля співробітників ----------------
@app.get("/settings/staff-fields", response_class=HTMLResponse)
def settings_staff_fields(request: Request, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import staff_fields as sf
    return render(request, "staff_fields.html", nav="settings", items=sf.items(db), TYPES=sf.TYPES)


@app.post("/settings/staff-fields")
def settings_staff_fields_save(fid: str = Form(""), name: str = Form(""), type: str = Form("text"), required: str = Form(""),
                               options: str = Form(""), user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import staff_fields as sf
    err = sf.save(db, int(fid) if fid.isdigit() else None, name, type, required == "1", options)
    if not err:
        audit(db, "staff_fields.save", "staff_field", int(fid) if fid.isdigit() else None, user=user, name=name)
    return RedirectResponse(_with_msg("/settings/staff-fields", err or "Збережено", bool(err)), 303)


@app.post("/settings/staff-fields/{fid:int}/toggle")
def settings_staff_fields_toggle(fid: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import staff_fields as sf
    sf.toggle(db, fid)
    return RedirectResponse("/settings/staff-fields", 303)


@app.post("/settings/staff-fields/{fid:int}/delete")
def settings_staff_fields_delete(fid: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import staff_fields as sf
    sf.delete(db, fid)
    audit(db, "staff_fields.delete", "staff_field", fid, user=user)
    return RedirectResponse(_with_msg("/settings/staff-fields", "Поле видалено"), 303)


@app.post("/settings/staff-fields/order")
async def settings_staff_fields_order(request: Request, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import staff_fields as sf
    js = await request.json()
    sf.reorder(db, [int(x) for x in js.get("ids") or [] if str(x).isdigit()])
    return JSONResponse({"ok": True})


# ---------------- Налаштування → Статті фінансових операцій ----------------
@app.get("/settings/articles", response_class=HTMLResponse)
def settings_articles(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    fa = _fa()
    xs = fa.items(db)
    use = fa.usage(db)
    groups: dict = {}
    for x in xs:
        groups.setdefault((x["kind"], fa.group(x["name"])), []).append({**x, "used": use.get(x["name"], 0)})
    return render(request, "fin_articles.html", nav="settings", groups=groups, KINDS=fa.KINDS, n=len(xs),
                  kind=request.query_params.get("kind", ""), q=request.query_params.get("q", ""))


@app.post("/settings/articles")
def settings_articles_save(aid: str = Form(""), name: str = Form(""), kind: str = Form(""), active: str = Form(""), salary: str = Form(""),
                           user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    try:
        x = _fa().save(db, int(aid) if aid.isdigit() else None, name, kind, bool(active), bool(salary))
    except ValueError as e:
        return RedirectResponse(_with_msg("/settings/articles", str(e), True), 303)
    audit(db, "fin.article_save", user=user, article=x["id"], name=x["name"], kind=x["kind"], active=x["active"])
    return RedirectResponse(_with_msg("/settings/articles", f"Статтю «{x['name']}» збережено"), 303)


@app.post("/settings/articles/{aid:int}/delete")
def settings_articles_delete(aid: int, del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    err = _delpw(db, user, del_password, "fin_article", None, aid)
    if err:
        return RedirectResponse(_with_err("/settings/articles", err), 303)
    gone = _fa().delete(db, aid)
    audit(db, "fin.article_delete", user=user, article=aid, removed=gone)
    return RedirectResponse(_with_msg("/settings/articles", "Статтю видалено" if gone else "По статті є операції — її вимкнено (історія лишається)"), 303)


# ---------------- Налаштування → Каси / рахунки ----------------
@app.get("/settings/cashboxes", response_class=HTMLResponse)
def settings_cashboxes(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import cashboxes as cbx, clinic as cl, sales as sl, fin_articles as fa
    boxes = cbx.overview(db)
    open_ = request.query_params.get("open", "")
    detail = next((b for b in boxes if str(b["id"]) == open_), None)
    return render(request, "cashboxes.html", nav="settings", boxes=boxes, TYPES=cbx.TYPES, locs=cl.get(db).get("locations") or [],
                  methods=sl.methods(db), open_=open_, detail=detail, dmoves=cbx.moves(db, detail, 100) if detail else [],
                  total=round(sum(b["balance"] for b in boxes if b.get("active")), 2), fee_articles=fa.names(db, "expense"),
                  FEE_ARTICLE=cbx.FEE_ARTICLE, shift_req=__import__("app.services.shifts", fromlist=["x"]).required(db))


@app.get("/settings/cosmetics-pct", response_class=HTMLResponse)
def settings_cosmetics_pct(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import cosmetics_pct as _cp
    from .models import Product as _P
    docs = sorted({d_.name for d_ in spec_svc.all_doctors(db) if d_.active} | set(_cp.cfg(db)["staff"]))
    brands = sorted({b_ for (b_,) in db.execute(select(_P.brand).where(_P.brand.is_not(None)).distinct()) if b_.strip()})
    prods = sorted({n_ for (n_,) in db.execute(select(_P.name).where(_P.active.is_not(False))) if n_})
    return render(request, "cosmetics_pct.html", nav="settings", c=_cp.cfg(db), BASES=_cp.BASES, docs=docs, brands=brands, prods=prods)


@app.post("/settings/cosmetics-pct")
async def settings_cosmetics_pct_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import cosmetics_pct as _cp
    f = await request.form()

    def rows(prefix, extra=()):
        out = {}
        names = f.getlist(prefix + "_name")
        cols = {k: f.getlist(f"{prefix}_{k}") for k in ("full", "disc", *extra)}
        for i, n_ in enumerate(names):
            r_ = {k: (v[i] if i < len(v) else "") for k, v in cols.items()}
            if prefix == "staff":
                r_["off"] = str(i) in f.getlist("staff_off")
            if str(n_).strip():
                out[str(n_).strip()] = r_
        return out
    offs_ = set(f.getlist("tier_off"))
    tiers_ = [{"from": a_, "pct": b_, "off": str(i_) in offs_} for i_, (a_, b_) in enumerate(zip(f.getlist("tier_from"), f.getlist("tier_pct")))]
    _cp.save(db, {**{k: f.get(k) for k in ("full", "disc", "min_price", "low", "disc_from", "base")},
                  "staff": rows("staff", ("min_price",)), "brands": rows("brand"), "products": rows("prod"), "tiers": tiers_})
    audit(db, "settings.cosmetics_pct", user=user)
    return RedirectResponse(_with_msg("/settings/cosmetics-pct", "Збережено. Звіт «Бонус за продаж косметики» рахується за новими правилами"), 303)


@app.post("/gifts/subs")
async def gifts_sub_save(request: Request, user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    """Абонемент з розділу «Сертифікати й абонементи»: пацієнт обирається у вікні."""
    f = await request.form()
    pid = str(f.get("pid") or "")
    if not pid.isdigit():
        return RedirectResponse(_with_err("/gifts?tab=subs", "Оберіть пацієнта"), 303)
    resp = await patient_sub_save(int(pid), request, user, db)
    return RedirectResponse("/gifts?tab=subs", 303) if resp.status_code == 303 else resp


@app.post("/patients/{pid:int}/subs")
async def patient_sub_save(pid: int, request: Request, user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    """Абонемент пацієнта вручну: створити / змінити / видалити (візити, термін, послуги, сплачено, закритий)."""
    from .models import Subscription
    from .services import certificates as _cert
    p_ = _patient_or_403(db, user, pid)
    f = await request.form()
    sid = str(f.get("sid") or "")
    s = db.get(Subscription, int(sid)) if sid.isdigit() else None
    if s and s.patient_id != p_.id:
        raise HTTPException(403)
    if f.get("action") == "delete":
        if s:
            db.delete(s)
            db.commit()
            audit(db, "patient.sub_delete", "patient", pid, user=user, sub=int(sid))
        return RedirectResponse(f"/patients/{pid}#subs", 303)
    if not s:
        from sqlalchemy import func as _f
        mx = db.scalar(select(_f.max(Subscription.id))) or 0
        s = Subscription(id=max(mx + 1, _cert.SUB_CRM_FROM), patient_id=p_.id, cb_client_id=p_.cleverbox_id, client_name=p_.full_name,
                         seller=user.name, price=0, paid=0)
        db.add(s)
    s.type_name = str(f.get("type_name") or "").strip()[:300] or s.type_name or "Абонемент"
    s.services = str(f.get("services") or "").strip() or None
    tot, used, bal = _num(str(f.get("visits_total") or "")), _num(str(f.get("visits_used") or "")), _num(str(f.get("balance") or ""))
    s.visits_total = tot if tot else None
    s.visits_used = used or 0
    s.balance = bal if (bal is not None and str(f.get("balance") or "").strip()) else (None if s.visits_total else s.balance)
    s.valid_to = _day(str(f.get("valid_to") or ""))
    sd = _day(str(f.get("sold_at") or ""))
    s.sold_at = datetime.combine(sd, datetime.min.time()).replace(hour=12) if sd else (s.sold_at or datetime.now())
    paid = _num(str(f.get("paid") or ""))
    s.paid = paid or 0
    s.price = s.price or s.paid
    s.comment = str(f.get("comment") or "").strip()[:500] or None
    s.cb_status = "Закритий" if f.get("closed") else ("Активний" if (s.cb_status or "").lower() in ("", "закритий", "активний") else s.cb_status)
    s.updated_at = datetime.utcnow()
    db.commit()
    audit(db, "patient.sub_save", "patient", pid, user=user, sub=s.id, visits=s.visits_total, used=s.visits_used)
    return RedirectResponse(f"/patients/{pid}#subs", 303)


@app.get("/settings/patient-tags", response_class=HTMLResponse)
def settings_patient_tags(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import patient_tags as _pt
    lv = _pt.tags(db)
    cnt: dict = {}
    try:                                                                 # скільки пацієнтів у кожному статусі зараз
        pids = [i for (i,) in db.execute(select(Patient.id))]
        st_all = _pt.stats(db, pids)
        for i in pids:
            x = st_all.get(i) or {"visits": 0, "planned": 0, "last_days": None}
            t = _pt.tag_for(lv, x["visits"], x["planned"], x["last_days"])
            cnt[t] = cnt.get(t, 0) + 1
    except Exception:  # noqa: BLE001
        cnt = {}
    return render(request, "patient_tags.html", nav="settings", tags=lv, tcounts=cnt, CATS=_pt.CATS, INF=_pt.INF, range_text=_pt.range_text,
                  fields=_pt.fields(db), FIELDS=_pt.FIELDS, MODES=_pt.MODES,
                  groups=_pt.groups(db), gcounts=_pt.group_counts(db), cols=clients_svc.columns(db), COLS=clients_svc.COLUMNS)


@app.post("/settings/patient-tags")
async def settings_patient_tags_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import patient_tags as _pt
    f = await request.form()
    if f.get("what") == "groups":
        rows = [{"name": n_, "color": c_, "orig": o_} for n_, c_, o_ in zip(f.getlist("g_name"), f.getlist("g_color"), f.getlist("g_orig"))]
        ren = 0
        for r in rows:
            nn, oo = _pt.group_name(r["name"]), _pt.group_name(r["orig"])
            if oo and nn and nn != oo:
                ren += _pt.rename_group(db, oo, nn)
        _pt.save_groups(db, rows)
        audit(db, "settings.patient_groups", user=user, n=len(rows), renamed=ren)
        return RedirectResponse(_with_msg("/settings/patient-tags#groups", "Групи збережено" + (f" · перейменовано в {ren} картках" if ren else "")), 303)
    if f.get("what") == "fields":
        _pt.save_fields(db, {k: str(f.get("f_" + k) or "off") for k in _pt.FIELDS})
        audit(db, "settings.calendar_patient_fields", user=user)
        return RedirectResponse(_with_msg("/settings/patient-tags#cal", "Поля календаря збережено"), 303)
    if f.get("what") == "reset":
        _pt.reset_tags(db)
        audit(db, "settings.patient_tags", user=user, reset=True)
        return RedirectResponse(_with_msg("/settings/patient-tags", "Статуси за замовчуванням відновлено"), 303)
    keys = ("name", "color", "cat", "v_from", "v_to", "p_from", "p_to", "l_from", "l_to")
    cols = {k: f.getlist(k) for k in keys}
    rows = [{k: cols[k][i] for k in keys if i < len(cols[k])} for i in range(len(cols["name"]))]
    _pt.save_tags(db, rows)
    audit(db, "settings.patient_tags", user=user, n=len(rows))
    return RedirectResponse(_with_msg("/settings/patient-tags", "Статуси пацієнтів збережено"), 303)


@app.get("/settings/booking-sources", response_class=HTMLResponse)
def settings_booking_sources(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import appointments as aps
    from .models import Appointment as _Ap
    from sqlalchemy import func as _fn
    used = dict(db.execute(select(_Ap.booking_source, _fn.count()).where(_Ap.booking_source.is_not(None)).group_by(_Ap.booking_source)).all())
    return render(request, "booking_sources.html", nav="settings", items=aps.sources_all(db), used=used)


@app.post("/settings/booking-sources")
async def settings_booking_sources_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import appointments as aps
    f = await request.form()
    on_ = set(f.getlist("on"))
    names = f.getlist("name")
    aps.save_sources(db, [{"name": n_, "on": str(i) in on_} for i, n_ in enumerate(names)])
    audit(db, "settings.booking_sources", user=user, n=len(names))
    return RedirectResponse(_with_msg("/settings/booking-sources", "Джерела запису збережено"), 303)


@app.get("/settings/visit-statuses", response_class=HTMLResponse)
def settings_visit_statuses(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import visit_statuses as _vs
    from .models import Appointment as _Ap
    from sqlalchemy import func as _fn
    used = dict(db.execute(select(_Ap.status, _fn.count()).group_by(_Ap.status)).all())
    return render(request, "visit_statuses.html", nav="settings", items=_vs.all_(db), used=used, css=_vs.css(db))


@app.post("/settings/visit-statuses")
async def settings_visit_statuses_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import visit_statuses as _vs
    f = await request.form()
    keys, labels_, colors_ = f.getlist("key"), f.getlist("label"), f.getlist("color")
    on_, cal_ = set(f.getlist("on")), set(f.getlist("cal"))
    items = []
    for i, k in enumerate(keys):
        tag = k or f"new{i}"
        items.append({"key": k, "label": labels_[i] if i < len(labels_) else "", "color": colors_[i] if i < len(colors_) else "",
                      "on": tag in on_ or (not k), "cal": tag in cal_})
    from .models import Appointment as _Ap
    used = {k for (k,) in db.execute(select(_Ap.status).distinct())}
    old = {x["key"]: x for x in _vs.all_(db) if not x["system"]}
    for k in used & set(old) - set(keys):        # статус уже стоїть у записах — не видаляємо, лише вимикаємо
        items.append({**old[k], "on": False})
    _vs.save(db, items)
    audit(db, "settings.visit_statuses", user=user, n=len(items))
    return RedirectResponse(_with_msg("/settings/visit-statuses", "Статуси візитів збережено"), 303)


@app.post("/settings/shift-required")
def settings_shift_required(on: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import shifts as _shq
    _shq.set_required(db, bool(on))
    audit(db, "settings.shift_required", user=user, on=bool(on))
    return RedirectResponse(_with_msg("/settings/cashboxes", "Збережено"), 303)


@app.post("/settings/cashboxes")
async def settings_cashboxes_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import cashboxes as cbx
    f = await request.form()
    bid = int(f.get("id")) if str(f.get("id") or "").isdigit() else None
    data = {"name": f.get("name"), "type": f.get("type"), "active": bool(f.get("active")), "note": f.get("note"),
            "location_id": int(f.get("location_id")) if str(f.get("location_id") or "").isdigit() else None,
            "opening": _num(str(f.get("opening") or "0")) or 0, "opening_date": _day(str(f.get("opening_date") or "")) and str(f.get("opening_date")),
            "methods": f.getlist("methods"), "color": str(f.get("color") or "")[:9], "pos": bool(f.get("pos")), "shift": bool(f.get("shift")),
            "fee_pct": _num(str(f.get("fee_pct") or "0").replace(",", ".")) or 0, "fee_article": f.get("fee_article"),
            "fee_from": str(f.get("fee_from")) if _day(str(f.get("fee_from") or "")) else None}
    try:
        x = cbx.save(db, bid, data)
    except ValueError as e:
        return RedirectResponse(_with_msg("/settings/cashboxes" + (f"?edit={bid}" if bid else ""), str(e), True), 303)
    audit(db, "cashbox.save", user=user, cashbox=x["id"], name=x["name"], type=x["type"], opening=x["opening"], methods=x["methods"],
          fee_pct=x.get("fee_pct"), fee_article=x.get("fee_article"))
    cbx.sync_fees(db)
    return RedirectResponse(_with_msg("/settings/cashboxes", f"Касу «{x['name']}» збережено"), 303)


@app.post("/settings/cashboxes/{bid:int}/delete")
def settings_cashboxes_delete(bid: int, del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import cashboxes as cbx
    err = _delpw(db, user, del_password, "cashbox", None, bid)
    if err:
        return RedirectResponse(_with_err("/settings/cashboxes", err), 303)
    cbx.delete(db, bid)
    audit(db, "cashbox.delete", user=user, cashbox=bid)
    return RedirectResponse(_with_msg("/settings/cashboxes", "Касу видалено"), 303)


@app.post("/settings/cashboxes/{bid:int}/restore")
def settings_cashboxes_restore(bid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import cashboxes as cbx
    cbx.restore(db, bid)
    audit(db, "cashbox.restore", user=user, cashbox=bid)
    return RedirectResponse(_with_msg("/settings/cashboxes", "Касу відновлено"), 303)


# ---------------- Налаштування → Платіжні системи (посилання на оплату) ----------------
@app.get("/settings/payments", response_class=HTMLResponse)
def settings_payments(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    from .models import PayLink
    t_ = pls.public(db)
    links = db.scalars(select(PayLink).order_by(PayLink.id.desc()).limit(40)).all()
    pats = {p_.id: p_ for p_ in db.scalars(select(Patient).where(Patient.id.in_({l_.patient_id for l_ in links if l_.patient_id})))} if links else {}
    base = str(request.base_url).rstrip("/").replace("http://", "https://")
    from .services import cashboxes as _cbx
    return render(request, "paysys.html", nav="settings", t=t_, PROV=pls.PROVIDERS, links=links, pats=pats, SL=pls.STATUS_LABELS,
                  boxes=_cbx.names(db), accs={a_["id"]: a_["name"] for a_ in t_["accounts"]},
                  hook_url=lambda p: f"{base}/api/public/pay/{p}/{t_['hook']}")


@app.post("/settings/payments")
async def settings_payments_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    f = await request.form()
    pls.save_common(db, int(f.get("validity_h")) if str(f.get("validity_h") or "").isdigit() else 24, str(f.get("text") or ""),
                    int(f.get("default")) if str(f.get("default") or "").isdigit() else None)
    audit(db, "paysys.save", user=user)
    return RedirectResponse(_with_msg("/settings/payments", "Збережено"), 303)


@app.post("/settings/payments/account")
async def settings_payments_account(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    f = await request.form()
    aid = int(f.get("id")) if str(f.get("id") or "").isdigit() else None
    try:
        a_ = pls.save_account(db, aid, str(f.get("provider") or ""), str(f.get("name") or ""), {k: f.get(k) for k in f.keys()},
                              str(f.get("cashbox") or ""), bool(f.get("active")))
    except ValueError as e:
        return RedirectResponse(_with_msg("/settings/payments", str(e), True), 303)
    audit(db, "paysys.account_save", user=user, account=a_["id"], provider=a_["provider"], name=a_["name"])
    if f.get("is_default"):
        pls.set_default(db, a_["id"])
    if f.get("test"):
        ok, msg = await asyncio.to_thread(pls.test, db, a_["id"])
        return RedirectResponse(_with_msg("/settings/payments", ("✅ " if ok else "❌ ") + msg, not ok), 303)
    return RedirectResponse(_with_msg("/settings/payments", f"Підключення «{a_['name']}» збережено"), 303)


@app.post("/settings/payments/account/{aid:int}/default")
def settings_payments_account_default(aid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    pls.set_default(db, aid)
    audit(db, "paysys.default", user=user, account=aid)
    return RedirectResponse(_with_msg("/settings/payments", "Підключення за замовчуванням змінено"), 303)


@app.post("/settings/payments/account/{aid:int}/delete")
def settings_payments_account_delete(aid: int, del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    err = _delpw(db, user, del_password, "paysys", None, aid)
    if err:
        return RedirectResponse(_with_err("/settings/payments", err), 303)
    pls.delete_account(db, aid)
    audit(db, "paysys.account_delete", user=user, account=aid)
    return RedirectResponse(_with_msg("/settings/payments", "Підключення видалено"), 303)


@app.post("/paylinks")
async def paylink_create(request: Request, user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    from .integrations.messaging import send_to_patient
    f = await request.form()
    back = str(f.get("back") or "/settings/payments")
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    pid = int(f.get("patient_id")) if str(f.get("patient_id") or "").isdigit() else None
    aid = int(f.get("appointment_id")) if str(f.get("appointment_id") or "").isdigit() else None
    if pid:
        _patient_or_403(db, user, pid)
    base = str(request.base_url).rstrip("/").replace("http://", "https://")
    try:
        acc = int(f.get("account_id")) if str(f.get("account_id") or "").isdigit() else None
        pl = await asyncio.to_thread(pls.create, db, _num(str(f.get("amount") or "0")) or 0, str(f.get("purpose") or ""), base, pid, aid, user.id,
                                     None, acc)
    except pls.PayError as e:
        return RedirectResponse(_with_msg(back, str(e), True), 303)
    audit(db, "paylink.create", "paylink", pl.id, user=user, amount=pl.amount, patient=pid, provider=pl.provider)
    msg = f"Посилання на оплату №{pl.id} на {pl.amount:.2f} ₴ створено"
    if f.get("send") and pid:
        p_ = db.get(Patient, pid)
        res = await asyncio.to_thread(send_to_patient, p_, pls.message(db, pl))
        pl.info = {**(pl.info or {}), "sent": {"ok": res.ok, "channel": res.channel, "at": datetime.now().strftime("%d.%m.%Y %H:%M")}}
        db.commit()
        msg += f" і надіслано ({res.channel})" if res.ok else f", але не надіслано: {res.error}"
    return RedirectResponse(_with_msg(back, msg), 303)


@app.post("/paylinks/{lid:int}/check")
async def paylink_check(lid: int, back: str = Form("/settings/payments"), user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .services import paylinks as pls
    from .models import PayLink
    pl = db.get(PayLink, lid) or (_ for _ in ()).throw(HTTPException(404))
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    try:
        st_ = await asyncio.to_thread(pls.check_status, db, pl)
    except (httpx.HTTPError, ValueError) as e:
        return RedirectResponse(_with_msg(back, f"Не вдалося перевірити: {e}", True), 303)
    return RedirectResponse(_with_msg(back, f"Посилання №{lid}: {pls.STATUS_LABELS.get(st_, st_)}"), 303)


@app.post("/paylinks/{lid:int}/send")
async def paylink_send(lid: int, ch: str = Form(...), back: str = Form("/settings/payments"), user: User = Depends(require("sales")),
                       db: Session = Depends(db_dep)):
    """Надіслати посилання на оплату пацієнту вибраним каналом: Telegram / Viber / SMS."""
    from .services import paylinks as pls
    from .models import PayLink
    from .integrations.messaging import send_to_patient
    pl = db.get(PayLink, lid) or (_ for _ in ()).throw(HTTPException(404))
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    if ch not in ("telegram", "viber", "sms"):
        raise HTTPException(400)
    p_ = db.get(Patient, pl.patient_id) if pl.patient_id else None
    if not p_:
        return RedirectResponse(_with_msg(back, "У посилання немає пацієнта", True), 303)
    res = await asyncio.to_thread(send_to_patient, p_, pls.message(db, pl), [ch])
    lbl = {"telegram": "Telegram", "viber": "Viber", "sms": "SMS"}[ch]
    if res.ok:
        pl.info = {**(pl.info or {}), "sent": {"ok": True, "channel": res.channel, "at": datetime.now().strftime("%d.%m.%Y %H:%M")}}
        db.commit()
    audit(db, "paylink.send", "paylink", lid, user=user, channel=ch, ok=res.ok)
    return RedirectResponse(_with_msg(back, f"Посилання надіслано в {lbl}" if res.ok else f"{lbl}: не надіслано — {res.error}", not res.ok), 303)


@app.post("/paylinks/{lid:int}/cancel")
def paylink_cancel(lid: int, back: str = Form("/settings/payments"), user: User = Depends(require("sales")), db: Session = Depends(db_dep)):
    from .models import PayLink
    pl = db.get(PayLink, lid) or (_ for _ in ()).throw(HTTPException(404))
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    if pl.status == "created":
        pl.status = "cancelled"
        db.commit()
        audit(db, "paylink.cancel", "paylink", lid, user=user)
    return RedirectResponse(_with_msg(back, f"Посилання №{lid} скасовано в CRM"), 303)


@app.post("/api/public/pay/{provider}/{hook}")
async def paylink_hook(request: Request, provider: str, hook: str, db: Session = Depends(db_dep)):
    """Вебхук платіжної системи про оплату посилання."""
    from .services import paylinks as pls
    import hmac as _hm
    if provider not in pls.PROVIDERS or not _hm.compare_digest(hook, pls.public(db)["hook"]):
        raise HTTPException(404)
    raw = await request.body()
    form = {}
    if provider != "mono" and raw and not raw.lstrip().startswith(b"{"):
        from urllib.parse import parse_qsl
        form = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    res = await asyncio.to_thread(pls.webhook, db, provider, raw, form)
    return JSONResponse(res)


# ---------------- Налаштування → IP-телефонія ----------------
@app.get("/settings/telephony", response_class=HTMLResponse)
def settings_telephony(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import telephony as tel, appeals as ap_
    from .models import Call
    t_ = tel.public(db)
    calls = db.scalars(select(Call).where(Call.at >= datetime.now() - timedelta(days=31)).order_by(Call.at.desc()).limit(500)).all()
    _pids = {c_.patient_id for c_ in calls if c_.patient_id}
    pnames = {p_.id: p_.full_name for p_ in db.scalars(select(Patient).where(Patient.id.in_(_pids or [0])))}
    base = str(request.base_url).rstrip("/").replace("http://", "https://")
    from .services import invoice_ai as _ia
    return render(request, "telephony.html", nav="settings", t=t_, PROV=tel.PROVIDERS, calls=calls, sources=ap_.dirs(db)["sources"], hook_log=tel.hook_log(db),
                  users_all=db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all(), has_ai_key=bool(_ia.api_key(db)),
                  POPUP_FIELDS=tel.POPUP_FIELDS, found=tel.discovered(db), line_info=lambda n: tel.line_info(t_, n),
                  hook_url=lambda p: f"{base}/api/public/telephony/{p}/{t_['hook']}", R_BODY=tel.RINGOSTAT_BODY, R_EVENTS=tel.RINGOSTAT_EVENTS, POPUP_EXTRA=tel.POPUP_EXTRA, pnames=pnames, crit=tel.crit_rows(t_.get("ai_criteria") or ""), crit_tail=tel.crit_tail(t_.get("ai_criteria") or ""))


@app.post("/settings/telephony")
async def settings_telephony_save(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import telephony as tel
    f = await request.form()
    prov = str(f.get("provider") or "")
    try:
        tel.save(db, prov, {k: f.get(k) for k in f.keys()}, str(f.get("source") or ""), bool(f.get("create_appeal")), bool(f.get("missed_only")))
        _crit = str(f.get("ai_criteria") or "")
        if f.get("crit_cfg"):
            _rows = [{"title": t_.strip(), "desc": d_.strip(), "pts": int(_num(p_) or 0)} for t_, d_, p_ in
                     zip(f.getlist("crit_t"), f.getlist("crit_d"), f.getlist("crit_p")) if t_.strip()]
            if _rows:
                _crit = tel.crit_text(_rows, str(f.get("crit_tail") or "").strip())
        tel.save_extra(db, f.getlist("popup_users"), bool(f.get("ai_on")), _crit, f.get("ai_alert_below"), bool(f.get("ai_alert")),
                       str(f.get("ai_engine") or "crm"), f.getlist("popup_fields") if f.get("popup_cfg") else None,
                       f.getlist("ai_alert_to") if f.get("ai_alert_cfg") else None,
                       str(f.get("ai_task_to") or "") if f.get("ai_task_cfg") else None, bool(f.get("ai_task_tg")) if f.get("ai_task_cfg") else None,
                       [{"key": k_, "label": l_, "text": x_} for k_, l_, x_ in zip(f.getlist("pc_key"), f.getlist("pc_label"), f.getlist("pc_text"))]
                       if f.get("popup_cfg") else None)
    except ValueError as e:
        return RedirectResponse(_with_msg("/settings/telephony", str(e), True), 303)
    audit(db, "telephony.save", user=user, provider=prov)
    if f.get("test"):
        ok, msg = await asyncio.to_thread(tel.test, db)
        return RedirectResponse(_with_msg("/settings/telephony", ("✅ " if ok else "❌ ") + msg, not ok), 303)
    return RedirectResponse(_with_msg("/settings/telephony", "Збережено"), 303)


@app.post("/settings/telephony/staff")
async def settings_telephony_staff(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    """Співробітники телефонії (ID / імʼя в Ringostat → користувач CRM) і лінії (номер → відповідальний)."""
    from .services import telephony as tel
    f = await request.form()
    staff = [{"user_id": u, "ext": e, "alias": a} for u, e, a in zip(f.getlist("s_user"), f.getlist("s_ext"), f.getlist("s_alias"))]
    lines = [{"number": n, "name": nm, "user_id": u} for n, nm, u in zip(f.getlist("l_number"), f.getlist("l_name"), f.getlist("l_user"))]
    tel.save_staff(db, staff, lines)
    n = tel.relink(db)
    audit(db, "telephony.staff", user=user, staff=len(staff), lines=len(lines), relinked=n)
    return RedirectResponse(_with_msg("/settings/telephony#staff", "Співробітників і лінії збережено" + (f" · перепривʼязано дзвінків: {n}" if n else "")), 303)


@app.post("/api/public/telephony/{provider}/{hook}")
async def telephony_hook(request: Request, provider: str, hook: str, db: Session = Depends(db_dep)):
    """Вебхук дзвінків провайдера IP-телефонії → журнал дзвінків і звернення."""
    from .services import telephony as tel
    import hmac as _hm
    t_ = tel.public(db)
    if provider not in tel.PROVIDERS or not _hm.compare_digest(hook, t_["hook"]):
        raise HTTPException(404)
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        from urllib.parse import parse_qsl
        data = dict(parse_qsl(raw.decode("utf-8", "replace")))
    if not isinstance(data, dict):
        data = {}
    tel.log_hook(db, provider, request.query_params.get("event", ""), data)
    c = tel.ingest(db, provider, data, request.query_params.get("event", ""))
    return {"ok": True, "call": c.id, "appeal": c.appeal_id, "status": "success"}


def _bday_soon(b) -> str | None:
    """День народження в найближчі ±7 днів → «12.11»."""
    if not b:
        return None
    try:
        d_ = b.replace(year=date.today().year)
    except ValueError:
        return None
    return b.strftime("%d.%m") if abs((d_ - date.today()).days) <= 7 else None


@app.get("/telephony/live")
def telephony_live(user: User = Depends(current_user), db: Session = Depends(db_dep)):
    """Вхідні дзвінки, що зараз ідуть — для спливаючого вікна «Вхідний дзвінок»."""
    from .services import telephony as tel, calendar as _cal
    from .models import Appointment as _Ap
    from sqlalchemy import func as _f
    if not tel.popup_for(db, user):
        return {"calls": []}
    show = set(tel.public(db).get("popup_fields") or [])
    out = []
    d0 = datetime.combine(date.today(), datetime.min.time())
    for c_ in tel.live(db):
        p_ = db.get(Patient, c_.patient_id) if c_.patient_id else None
        if p_ and not patient_visible(user, p_):
            p_ = None
        ap_ = None
        if p_:
            x = db.scalars(select(_Ap).where(_Ap.patient_id == p_.id, _Ap.start >= d0, _Ap.status.notin_(("cancelled", "no_show")))
                           .order_by(_Ap.start).limit(1)).first()
            if x:
                d_ = db.get(Doctor, x.doctor_id) if x.doctor_id else None
                ap_ = {"id": x.id, "day": x.start.date().isoformat(), "when": x.start.strftime("%d.%m %H:%M"), "today": x.start.date() == date.today(),
                       "doctor": d_.name if d_ else "", "services": ", ".join(s_.get("name", "") for s_ in x.services or [])[:80]}
        info = {}
        if p_:
            from .services import patient_tags as _ptg
            from .models import Visit as _V, Task as _T
            lv = _ptg.tags(db)
            n_ = _ptg.visit_days(db, [p_.id]).get(p_.id, 0)
            tag = _cal.tag_for(n_ + 1, lv)
            last = db.scalars(select(_Ap).where(_Ap.patient_id == p_.id, _Ap.start < d0, _Ap.status.in_(("done", "paid"))).order_by(_Ap.start.desc()).limit(1)).first()
            lv_txt = None
            if last:
                dl_ = db.get(Doctor, last.doctor_id) if last.doctor_id else None
                lv_txt = f"{last.start:%d.%m.%Y} · " + (", ".join(s_.get("name", "") for s_ in last.services or [])[:60] or "візит") + (f" · {dl_.name}" if dl_ else "")
            else:
                vv = db.scalars(select(_V).where(_V.patient_id == p_.id, _V.status.in_(("attended", "done"))).order_by(_V.starts_at.desc()).limit(1)).first()
                if vv:
                    lv_txt = f"{vv.starts_at:%d.%m.%Y} · {(vv.service_name or 'візит')[:60]}" + (f" · {vv.master_name}" if vv.master_name else "")
            tasks_n = db.scalar(select(_f.count()).select_from(_T).where(_T.patient_id == p_.id, _T.status == "open")) or 0
            plan = next((pl for pl in p_.plans if pl.status == "active"), None) if hasattr(p_, "plans") else None
            info = {"tag": tag, "tag_color": _ptg.colors(lv).get(tag, ""), "visits": n_, "last": lv_txt, "tasks": tasks_n,
                    "plan": (plan.title or "Бюті-план") if plan else None,
                    "note": (p_.cb_note or "")[:140] or None, "avatar": f"/files/{p_.avatar_file_id}" if p_.avatar_file_id else None,
                    "bonus": round(float(p_.cb_bonus or 0), 2) if p_.cb_bonus else None,
                    "birthday": _bday_soon(p_.birthday)}
        out.append({"id": c_.id, "phone": c_.phone or "", "dir": c_.direction, "status": c_.status,
                    "name": p_.full_name if p_ else None, "pid": p_.id if p_ else None, "employee": c_.employee or "", "appt": ap_, **info})
        row = out[-1]
        _ex = dict(tel.POPUP_EXTRA)
        row["custom"] = [{"label": r_.get("label") or _ex.get(r_["key"], ""), "value": v_, "text": r_["key"] == "text"}
                         for r_ in (tel.public(db).get("popup_custom") or [])
                         for v_ in [tel.popup_extra_value(p_, r_["key"], r_.get("text") or "")] if v_]
        for k in ("avatar", "tag", "employee", "appt", "last", "visits", "tasks", "bonus", "plan", "note", "birthday"):
            if k not in show:
                row[k] = None
        if "tag" not in show:
            row["tag_color"] = None
    return {"calls": out, "show": sorted(show), "provider": {"ringostat": "Ringostat", "binotel": "Binotel", "stream": "Stream Telecom"}.get(tel.public(db).get("provider") or "", "")}


@app.post("/telephony/calls/{cid:int}/patient")
async def telephony_call_patient(cid: int, request: Request, user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    """З вікна дзвінка: створити пацієнта з номером дзвінка (Прізвище Імʼя По батькові)."""
    from .models import Call
    c_ = db.get(Call, cid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.json()
    nm = " ".join(str(f.get(k) or "").strip() for k in ("last", "first", "middle") if str(f.get(k) or "").strip())
    if not str(f.get("first") or "").strip() and not nm:
        return JSONResponse({"error": "Вкажіть імʼя"}, status_code=400)
    from .services.appeals import find_patient
    p_ = find_patient(db, c_.phone)
    if not p_:
        p_ = Patient(full_name=nm[:200], phone=c_.phone, source="Дзвінок")
        db.add(p_)
        db.flush()
    c_.patient_id = p_.id
    db.commit()
    audit(db, "patient.create_from_call", "patient", p_.id, user=user, call=cid)
    return JSONResponse({"ok": True, "pid": p_.id, "name": p_.full_name})


@app.post("/telephony/calls/{cid:int}/evaluate")
async def telephony_call_evaluate(cid: int, user: User = Depends(require("appeals")), db: Session = Depends(db_dep)):
    from .services import telephony as tel
    from .models import Call
    c_ = db.get(Call, cid) or (_ for _ in ()).throw(HTTPException(404))
    if tel.public(db).get("ai_engine") == "agent":           # розбирає агент на VPS — повертаємо дзвінок у його чергу
        tel.requeue(db, c_)
        audit(db, "call.ai_requeue", "call", c_.id, user=user)
        return JSONResponse({"ok": True, "score": None, "comment": "Передано агенту на повторний розбір"})
    try:
        await asyncio.to_thread(tel.evaluate, db, c_)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "score": c_.ai_score, "comment": c_.ai_comment})


# ---------------- Налаштування → Checkbox (фіскалізація) ----------------
@app.get("/settings/checkbox", response_class=HTMLResponse)
def settings_checkbox(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis, sales as sl
    from .models import Sale
    last = db.scalars(select(Sale).where(Sale.fiscal.is_not(None)).order_by(Sale.id.desc()).limit(10)).all()
    from .services import cashboxes as _cbx, clinic as _cl
    boxes = [{"name": x["name"], "cash": x.get("type") == "cash"} for x in _cbx.items(db)
             if x.get("active") and not x.get("deleted") and x.get("type") in ("cash", "card", "other")]
    return render(request, "checkbox_settings.html", nav="settings", c=_fis.cfg(db), rros=_fis.rros(db), methods=sl.methods(db),
                  boxes=boxes, locs=_cl.get(db).get("locations") or [], term=_fis.terminals(_fis.cfg(db)),
                  last=last, open_=request.query_params.get("open", ""))


@app.post("/settings/checkbox/receipt")
async def settings_checkbox_receipt(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis, sales as sl
    f = await request.form()
    c = _fis.cfg(db)
    c["auto"] = bool(f.get("auto"))
    c["default_rro"] = int(f.get("default_rro")) if str(f.get("default_rro") or "").isdigit() else None
    c["method_modes"] = {m: str(f.get(f"mode_{i}") or "skip") for i, m in enumerate(sl.methods(db))
                         if str(f.get(f"mode_{i}") or "skip") in ("cash", "card", "skip")}
    _fis.save_cfg(db, c)
    audit(db, "fiscal.cfg", user=user, auto=c["auto"], rro=c["default_rro"], modes=c["method_modes"])
    return RedirectResponse(_with_msg("/settings/checkbox", "Налаштування фіскального чека збережено"), 303)


@app.post("/settings/checkbox/send")
async def settings_checkbox_send(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    f = await request.form()
    c = _fis.cfg(db)
    c["send"] = {"on": bool(f.get("on")), "channels": [x for x in ("telegram", "viber", "sms") if f.get(f"ch_{x}")], "certs": bool(f.get("certs")),
                 "text": str(f.get("text") or "")[:500] or _fis.DEFAULT["send"]["text"]}
    _fis.save_cfg(db, c)
    audit(db, "fiscal.send_cfg", user=user, on=c["send"]["on"], channels=c["send"]["channels"])
    return RedirectResponse(_with_msg("/settings/checkbox", "Відправлення чеків збережено"), 303)


@app.post("/settings/checkbox/extra")
async def settings_checkbox_extra(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    f = await request.form()
    c = _fis.cfg(db)
    if "_paylink" in f:
        c["paylink"] = bool(f.get("paylink"))
    if "_widget" in f:
        c["widget"] = bool(f.get("widget"))
    _fis.save_cfg(db, c)
    return RedirectResponse(_with_msg("/settings/checkbox", "Збережено"), 303)


@app.post("/settings/checkbox/rro")
async def settings_checkbox_rro(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    f = await request.form()
    rid = int(f.get("id")) if str(f.get("id") or "").isdigit() else None
    if not str(f.get("name") or "").strip():
        return RedirectResponse(_with_msg("/settings/checkbox?open=rro", "Вкажіть назву каси", True), 303)
    methods = {}
    for k in f.keys():
        if k.startswith("m_on::"):
            m_ = k[6:]
            methods[m_] = str(f.get("m_lbl::" + m_) or "cash")
    data = {k: f.get(k) for k in ("name", "fop", "login", "license_key", "password", "pin", "test")} | {"on": bool(f.get("on"))}
    if f.get("_methods"):
        data["methods"] = methods
        data["branches"] = f.getlist("branches")
    rid = _fis.save_rro(db, rid, data)
    audit(db, "fiscal.rro_save", user=user, rro=rid, test=bool(f.get("test")), methods=sorted(methods))
    if f.get("check"):
        ok, msg = await asyncio.to_thread(_fis.connect, db, rid)
        return RedirectResponse(_with_msg("/settings/checkbox?open=rro", ("✅ " if ok else "❌ ") + msg, not ok), 303)
    return RedirectResponse(_with_msg("/settings/checkbox?open=rro", "Касу збережено"), 303)


@app.post("/settings/checkbox/terminals")
async def settings_checkbox_terminals(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Термінали Checkbox PayLink: адреса утиліти PayLink і термінали (ID пристрою ↔ спосіб оплати каси)."""
    from .services import fiscal as _fis
    f = await request.form()
    c = _fis.cfg(db)
    t = _fis.terminals(c)
    act, tid = str(f.get("act") or "save"), str(f.get("tid") or "")
    if act == "conn":
        t["host"] = str(f.get("host") or "127.0.0.1").strip()[:120]
        t["port"] = int(f.get("port")) if str(f.get("port") or "").isdigit() else 9020
    elif act in ("on", "off", "del"):
        for x in t["items"]:
            if str(x["id"]) == tid:
                x["on"] = act == "on"
        if act == "del":
            t["items"] = [x for x in t["items"] if str(x["id"]) != tid]
    else:
        name, dev = str(f.get("name") or "").strip()[:80], str(f.get("device_id") or "").strip()[:80]
        if not name or not dev:
            return RedirectResponse(_with_msg("/settings/checkbox?open=term", "Вкажіть назву й ID термінала з PayLink", True), 303)
        x = next((x for x in t["items"] if str(x["id"]) == tid), None)
        if not x:
            x = {"id": max([int(y["id"]) for y in t["items"]] + [0]) + 1, "on": True}
            t["items"].append(x)
        x.update(name=name, device_id=dev, method=str(f.get("method") or "")[:60])
    c["terminals"] = t
    c["paylink"] = any(x.get("on") for x in t["items"])
    _fis.save_cfg(db, c)
    audit(db, "fiscal.terminals", user=user, act=act, terminal=tid)
    return RedirectResponse(_with_msg("/settings/checkbox?open=term", {"del": "Термінал видалено", "on": "Термінал підключено", "off": "Термінал відключено"}.get(act, "Збережено")), 303)


@app.post("/settings/checkbox/rro/{rid:int}/off")
def settings_checkbox_rro_off(rid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    _fis.disconnect(db, rid)
    audit(db, "fiscal.rro_off", user=user, rro=rid)
    return RedirectResponse(_with_msg("/settings/checkbox?open=rro", "Касу відключено"), 303)


@app.get("/settings/checkbox/rro/{rid:int}/{what}")
async def settings_checkbox_rro_data(rid: int, what: str, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    if what not in ("shifts", "taxes"):
        raise HTTPException(404)
    try:
        rows = await asyncio.to_thread(_fis.shifts if what == "shifts" else _fis.taxes, db, rid)
    except (_fis.FiscalError, httpx.HTTPError) as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=400)
    return JSONResponse({"rows": rows})


@app.post("/settings/checkbox/rro/{rid:int}/delete")
def settings_checkbox_rro_delete(rid: int, del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis
    err = _delpw(db, user, del_password, "rro", None, rid)
    if err:
        return RedirectResponse(_with_err("/settings/checkbox?open=rro", err), 303)
    _fis.delete_rro(db, rid)
    audit(db, "fiscal.rro_delete", user=user, rro=rid)
    return RedirectResponse(_with_msg("/settings/checkbox?open=rro", "Касу видалено"), 303)


@app.post("/settings/checkbox/rro/{rid:int}/close-shift")
async def settings_checkbox_close_shift(rid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import fiscal as _fis

    def _close():
        k = _fis.Kasa(_fis._rro(db, rid))
        if not k.shift():
            return "Зміна вже закрита"
        k.close_shift()
        return "Зміну закрито (Z-звіт)"
    try:
        msg = await asyncio.to_thread(_close)
    except (_fis.FiscalError, httpx.HTTPError) as e:
        return RedirectResponse(_with_msg("/settings/checkbox?open=rro", f"Checkbox: {e}", True), 303)
    audit(db, "fiscal.close_shift", user=user, rro=rid)
    return RedirectResponse(_with_msg("/settings/checkbox?open=rro", msg), 303)


# ---------------- Налаштування → Канали ----------------
@app.post("/settings/channels/sending")
def settings_channels_sending(on: str = Form(""), user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .integrations import messaging as _m
    if user.role != "owner":
        raise HTTPException(403, "Вмикати надсилання пацієнтам може лише власник")
    _m.set_sending(db, on == "1")
    audit(db, "channels.sending", user=user, op="on" if on == "1" else "off")
    return RedirectResponse(_with_msg("/settings/channels", "Надсилання пацієнтам увімкнено" if on == "1" else "Надсилання пацієнтам вимкнено — жодне повідомлення не піде"), 303)


@app.get("/settings/channels", response_class=HTMLResponse)
def settings_channels(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import channels as ch
    from .integrations import messaging as _m_
    from .services import meta_inbox as _mi
    _base = str(request.base_url).rstrip("/").replace("http://", "https://")
    return render(request, "channels.html", nav="settings", chs=ch.channels(db), T=ch.TYPES, open_=request.query_params.get("open", ""),
                  sending_on=_m_.sending_enabled(), meta_hook=_base + "/api/public/meta/webhook", meta_vt=_mi.verify_token(db) if _mi.meta_channel(db) else "",
                  replies_on=_mi.replies_enabled(db), meta_log=_mi.hook_events(db),
                  dlr_url=str(request.base_url).rstrip("/").replace("http://", "https://") + "/api/public/turbosms/dlr")


@app.post("/settings/channels/{cid:int}")
async def settings_channel_save(request: Request, cid: int, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import channels as ch
    f = await request.form()
    params = {}
    for k in ("sender", "allowed_from", "allowed_to", "login", "username", "ttl"):
        if k in f:
            params[k] = str(f.get(k) or "").strip()[:80]
    if "ttl" in params:
        params["ttl"] = int(params["ttl"]) if params["ttl"].isdigit() else 3600
    if "fallback_sms" in f or f.get("_has_fallback"):
        params["fallback_sms"] = bool(f.get("fallback_sms"))
    secrets = {k: str(f.get(k) or "").strip() for k in ("token", "webhook_secret", "password", "page_token", "app_secret") if f.get(k)}
    secrets.update({f"clear_{k}": "1" for k in ("token", "webhook_secret", "password", "page_token", "app_secret") if f.get(f"clear_{k}")})
    try:
        ch.update(db, cid, str(f.get("name") or "").strip(), bool(f.get("on")), params, secrets)
    except ValueError as e:
        return RedirectResponse(_with_msg("/settings/channels", str(e), True), 303)
    audit(db, "channels.save", user=user, channel=cid, on=bool(f.get("on")), params=params, secrets=sorted(secrets))
    if f.get("test"):
        ok, msg = await asyncio.to_thread(ch.test, db, cid)
        if ok and next((c for c in ch.channels(db) if c["id"] == cid and c["type"] == "meta"), None):
            from .services import meta_inbox as _mi
            sub, smsg = await asyncio.to_thread(_mi.subscription, db)
            if smsg:
                msg += f". Вебхук: {smsg}"
                ok = ok and sub is not False
        return RedirectResponse(_with_msg(f"/settings/channels?open={cid}", ("✅ " if ok else "❌ ") + msg, not ok), 303)
    return RedirectResponse(_with_msg("/settings/channels", "Канал збережено"), 303)


@app.post("/settings/channels/add")
def settings_channel_add(type_: str = Form(..., alias="type"), name: str = Form(""), user: User = Depends(require("users")),
                         db: Session = Depends(db_dep)):
    from .services import channels as ch
    if type_ not in ch.TYPES:
        raise HTTPException(400)
    c = ch.add(db, type_, name.strip())
    audit(db, "channels.add", user=user, channel=c["id"], type=type_)
    return RedirectResponse(f"/settings/channels?open={c['id']}", 303)


@app.post("/settings/channels/{cid:int}/delete")
def settings_channel_delete(cid: int, del_password: str = Form(""), user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import channels as ch
    err = _delpw(db, user, del_password, "channel", None, cid)
    if err:
        return RedirectResponse(_with_err("/settings/channels", err), 303)
    ch.remove(db, cid)
    audit(db, "channels.delete", user=user, channel=cid)
    return RedirectResponse(_with_msg("/settings/channels", "Канал видалено"), 303)


@app.post("/api/public/turbosms/dlr")
async def turbosms_dlr(request: Request, db: Session = Depends(db_dep)):
    """Вебхук TurboSMS: статуси доставки SMS / Viber → журнал повідомлень і розсилки."""
    from .services import channels as ch
    from .integrations import messaging as _m
    from .models import MessageLog, CampaignRecipient
    raw = await request.body()
    if not ch.verify_dlr(db, raw, request.headers.get("X-Signature") or request.headers.get("Signature")):
        raise HTTPException(403, "bad signature")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = dict((await request.form()).items())
    rows = data if isinstance(data, list) else (data.get("messages") or data.get("response_result") or [data])
    n = 0
    for x in rows if isinstance(rows, list) else []:
        mid, st_ = str(x.get("message_id") or ""), _m.STATUS_MAP.get(str(x.get("status") or "").lower())
        if not mid or not st_:
            continue
        for m in db.scalars(select(MessageLog).where(MessageLog.message_id == mid)):
            m.status = st_
        for r in db.scalars(select(CampaignRecipient).where(CampaignRecipient.message_id == mid)):
            r.status = st_
        n += 1
    db.commit()
    return {"ok": True, "updated": n}


# ---------------- Налаштування → Основні (загальні, звіти, локації) ----------------
@app.get("/settings/clinic", response_class=HTMLResponse)
def settings_clinic(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import clinic as cl
    from .models import CbFinOp
    data = cl.get(db)
    arts = sorted({a for (a,) in db.execute(select(CbFinOp.article).where(CbFinOp.kind == "income", CbFinOp.article.is_not(None)).distinct()) if a}
                  | set(_fa().names(db, "income", False)) | set(data["reports"]["exclude_income"]))
    return render(request, "clinic.html", nav="settings", c=data, cl=cl, income_articles=arts,
                  open_=request.query_params.get("open", ""))


def _clinic_back(open_: str = "", msg: str = "Збережено") -> RedirectResponse:
    return RedirectResponse(f"/settings/clinic?msg={_q(msg)}" + (f"&open={open_}" if open_ else ""), 303)


@app.post("/settings/clinic/general")
def settings_clinic_general(country: str = Form(""), timezone_: str = Form("Europe/Kyiv", alias="timezone"), language: str = Form(""),
                            currency: str = Form("UAH"), vat: str = Form(""), user: User = Depends(require("users")),
                            db: Session = Depends(db_dep)):
    from zoneinfo import ZoneInfo
    from .services import clinic as cl
    try:
        ZoneInfo(timezone_)
    except Exception:  # noqa: BLE001
        timezone_ = "Europe/Kyiv"
    data = cl.get(db)
    data["general"] = {"country": country[:60], "timezone": timezone_, "language": language[:40], "currency": currency[:6], "vat": bool(vat)}
    cl.save(db, data)
    audit(db, "settings.general", user=user, **data["general"])
    return _clinic_back()


@app.post("/settings/clinic/reports")
async def settings_clinic_reports(request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import clinic as cl
    f = await request.form()
    items = [str(x).strip()[:120] for x in f.getlist("exclude_income") if str(x).strip()]
    extra = str(f.get("extra") or "").strip()[:120]
    if extra:
        items.append(extra)
    data = cl.get(db)
    data["reports"]["exclude_income"] = sorted(set(items))
    cl.save(db, data)
    audit(db, "settings.reports", user=user, exclude_income=data["reports"]["exclude_income"])
    return _clinic_back()


@app.post("/settings/clinic/location")
def settings_clinic_location(lid: str = Form(""), name: str = Form(...), address: str = Form(""), phone: str = Form(""),
                             active: str = Form(""), user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    from .services import clinic as cl
    data = cl.get(db)
    loc = cl.location(data, int(lid)) if lid.isdigit() else None
    if not loc:
        nid = max([int(x.get("id", 0)) for x in data["locations"]] + [0]) + 1
        loc = {"id": nid, "hours": {str(i): {"on": i < 5, "from": "10:00", "to": "20:00"} for i in range(7)}, "resources": [], "places": []}
        data["locations"].append(loc)
    loc.update(name=name.strip()[:120] or "Локація", address=address.strip()[:200], phone=phone.strip()[:40], active=bool(active))
    cl.save(db, data)
    audit(db, "settings.location", user=user, location=loc["id"], name=loc["name"])
    return _clinic_back()


@app.post("/settings/clinic/location/{lid:int}/hours")
async def settings_clinic_hours(lid: int, request: Request, user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    import re as _re
    from .services import clinic as cl
    f = await request.form()
    data = cl.get(db)
    loc = cl.location(data, lid)
    if loc:
        hrs = {}
        for i in range(7):
            fr, to = str(f.get(f"from_{i}") or "10:00"), str(f.get(f"to_{i}") or "20:00")
            ok = _re.fullmatch(r"\d{2}:\d{2}", fr) and _re.fullmatch(r"\d{2}:\d{2}", to) and fr < to
            hrs[str(i)] = {"on": bool(f.get(f"on_{i}")) and bool(ok), "from": fr if ok else "10:00", "to": to if ok else "20:00"}
        loc["hours"] = hrs
        cl.save(db, data)
        audit(db, "settings.hours", user=user, location=lid)
    return _clinic_back(f"hours{lid}")


@app.post("/settings/clinic/location/{lid:int}/{what}")
def settings_clinic_list(lid: int, what: str, action: str = Form(...), idx: str = Form(""), name: str = Form(""),
                         del_password: str = Form(""), user: User = Depends(require("users")), db: Session = Depends(db_dep)):
    """Ресурси (апарати) і робочі місця (кабінети): add · rename · toggle · up · down · delete (з паролем видалення)."""
    from .services import clinic as cl
    if what not in ("resources", "places"):
        raise HTTPException(404)
    data = cl.get(db)
    loc = cl.location(data, lid)
    if not loc:
        return _clinic_back()
    items = loc.setdefault(what, [])
    i = int(idx) if idx.isdigit() and int(idx) < len(items) else None
    opn = f"{what}{lid}"
    if action == "add" and name.strip():
        items.append({"name": name.strip()[:120], "active": True})
    elif i is not None and action == "rename" and name.strip():
        items[i]["name"] = name.strip()[:120]
    elif i is not None and action == "toggle":
        items[i]["active"] = not items[i].get("active", True)
    elif i is not None and action in ("up", "down"):
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
    elif i is not None and action == "delete":
        err = _delpw(db, user, del_password, f"settings_{what}", "location", lid)
        if err:
            return RedirectResponse(_with_err(f"/settings/clinic?open={opn}", err), 303)
        items.pop(i)
    cl.save(db, data)
    audit(db, f"settings.{what}", user=user, location=lid, op=action, name=name or None)
    return _clinic_back(opn)


@app.get("/settings/keys", response_class=HTMLResponse)
def settings_keys(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Одне місце для ключів інтеграцій: monobank (по ФОП), Checkbox, CleverBox. Самі ключі ніколи не показуються."""
    from .integrations import monobank, checkbox as chb
    from .models import BankTxn
    fops = [a["name"] for a in chb.accounts(db)]
    mono = {m["fop"]: m for m in monobank.public(db)}
    for n in mono:
        if n not in fops:
            fops.append(n)
    cnt = {f: db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.fop == f)) or 0 for f in fops}
    jid = request.query_params.get("job")
    from .services import cashboxes as _cbx
    from .integrations import monobank as _mb
    return render(request, "keys.html", nav="keys", fops=fops, mono=mono, cnt=cnt, box_names=_cbx.names(db), mono_map=_mb.cashbox_map(db), acc_key=_mb.account_key,
                  cb=[{"name": a["name"], "configured": a["configured"]} for a in chb.accounts(db)], cb_status=chb.statuses(db),
                  cleverbox=bool(settings.cleverbox_token), job=jobs.JOBS.get(jid) if jid else None,
                  ai_key=bool(__import__("app.services.invoice_ai", fromlist=["x"]).api_key(db)),
                  ai_engine=__import__("app.services.invoice_ai", fromlist=["x"]).engine(db),
                  ai_jobs=db.scalars(select(__import__("app.models", fromlist=["x"]).InvoiceJob).order_by(__import__("app.models", fromlist=["x"]).InvoiceJob.id.desc()).limit(8)).all(),
                  shop=__import__("app.integrations.shop_express", fromlist=["x"]).creds(db),
                  shop_rep=__import__("app.integrations.shop_express", fromlist=["x"]).last_report(db))


def _shop_job(job):
    from .integrations import shop_express as sx
    with SessionLocal() as db:
        job.log("Завантажую каталог molodist.store…")
        rep = sx.sync(db)
        return {k: rep[k] for k in ("rows", "matched", "urls", "photos")}


@app.post("/settings/keys/shop")
def settings_keys_shop(site: str = Form(""), login: str = Form(""), password: str = Form(""), action: str = Form("save"),
                       user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Shop-Express: API-користувач магазину → перевірка входу → синхронізація посилань і фото."""
    from .integrations import shop_express as sx
    if action == "clear":
        sx.clear(db)
        audit(db, "keys.shop", user=user, op="clear")
        return RedirectResponse("/settings/keys#shop", 303)
    if action == "save":
        sx.save_creds(db, site, login, password)
        c = sx.creds(db)
        try:
            sx.ShopExpress(c["site"], c["login"], c["password"])
        except Exception as e:  # noqa: BLE001
            audit(db, "keys.shop", user=user, op="save", ok=False)
            return RedirectResponse(f"/settings/keys?shoperr={_q(str(e)[:200])}#shop", 303)
        audit(db, "keys.shop", user=user, op="save", ok=True)
    job = jobs.start("Синхронізація molodist.store", _shop_job)
    return RedirectResponse(f"/settings/keys?job={job.id}#shop", 303)


@app.post("/settings/keys/ai")
def settings_keys_ai(key: str = Form(""), action: str = Form("save"), engine: str = Form(""), user: User = Depends(require("finance")),
                     db: Session = Depends(db_dep)):
    from .services import invoice_ai
    if action == "engine":
        invoice_ai.set_engine(db, engine)
        audit(db, "keys.ai_engine", user=user, engine=engine)
        return RedirectResponse("/settings/keys?saved=1#ai", 303)
    invoice_ai.save_key(db, "" if action == "clear" else key)
    audit(db, "keys.ai", user=user, op=action)
    return RedirectResponse("/settings/keys?saved=1#ai", 303)


@app.post("/stock/mono/connect")
def stock_mono_connect(fop: str = Form(...), token: str = Form(""), back: str = Form(""),
                       user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .integrations import monobank
    err = monobank.connect(db, fop.strip()[:100], token)
    audit(db, "mono.connect", user=user, fop=fop, ok=err is None)            # токен у журнал не пишемо
    if back == "keys":
        return RedirectResponse(f"/settings/keys?mono={'err' if err else 'ok'}&fop={_q(fop)}#mono", 303)
    return RedirectResponse(f"/stock?tab=suppliers&mono={'err' if err else 'ok'}#mono", 303)


# ---------------- Рахунки постачальників на оплату (Telegram → погодження → чернетка monobank) ----------------
@app.get("/payreq", response_class=HTMLResponse)
def payreq_list(request: Request, st: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pay_requests as prq
    from .models import PayRequest, Supplier
    q = select(PayRequest).order_by(PayRequest.id.desc())
    if st in prq.STATUS:
        q = q.where(PayRequest.status == st)
    rows = db.scalars(q.limit(300)).all()
    cnt = {k: db.scalar(select(func.count()).select_from(PayRequest).where(PayRequest.status == k)) or 0 for k in prq.STATUS}
    from .integrations import monobank
    from .services import chat_expenses as _ce
    return render(request, "payreq.html", nav="finance", exp_on=_ce.enabled(db), exp_chats=_ce.chats(db), rows=rows, st=st, STATUS=prq.STATUS, cnt=cnt, chat=prq.chat_id(db),
                  sups={x.id: x.name for x in db.scalars(select(Supplier))}, fops=prq.fops(db), cfg=prq.cfg(db),
                  accs={f: prq.sender_accounts(db, f) for f in prq.fops(db)}, owners=prq.owner_tg_ids(db),
                  corp={m["fop"]: m.get("corp") for m in monobank.public(db)})


@app.get("/payreq/{pid:int}", response_class=HTMLResponse)
def payreq_view(request: Request, pid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pay_requests as prq
    from .models import PayRequest, Supplier
    pr = db.get(PayRequest, pid) or (_ for _ in ()).throw(HTTPException(404))
    return render(request, "payreq_view.html", nav="finance", pr=pr, STATUS=prq.STATUS, problems=prq.problems(pr), fops=prq.fops(db),
                  accs={f: prq.sender_accounts(db, f) for f in prq.fops(db)}, sup=db.get(Supplier, pr.supplier_id) if pr.supplier_id else None,
                  suppliers=db.scalars(select(Supplier).order_by(Supplier.name)).all(),
                  job=__import__("app.services.invoice_ai", fromlist=["x"]).job_state(db, "receipt" if pr.kind == "receipt" else "payreq", pr.id)
                  if pr.status == "recognizing" else None)


@app.get("/payreq/{pid:int}/file")
def payreq_file(pid: int, dl: int = 0, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayRequest
    pr = db.get(PayRequest, pid)
    if not pr or not pr.file_path or not Path(pr.file_path).exists():
        raise HTTPException(404)
    nm = (pr.file_name or "rahunok").lower()
    mt = "application/pdf" if nm.endswith(".pdf") else ({"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
        nm.rsplit(".", 1)[-1]) or pr.mime or None)
    return FileResponse(pr.file_path, filename=pr.file_name or "rahunok", media_type=mt,
                        content_disposition_type="attachment" if dl else "inline")     # inline — показ у вікні рахунку


@app.post("/payreq/upload")
async def payreq_upload(file: UploadFile = File(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pay_requests as prq
    data = await file.read()
    if not data:
        return RedirectResponse(_with_err("/payreq", "Порожній файл"), 303)
    pr = await asyncio.to_thread(prq.create, db, data, file.filename or "rahunok", file.content_type, "crm", None, None, user.name)
    audit(db, "payreq.create", "payreq", pr.id, user=user, amount=pr.amount)
    return RedirectResponse(f"/payreq/{pr.id}", 303)


@app.post("/payreq/{pid:int}")
async def payreq_save(pid: int, request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pay_requests as prq
    from .models import PayRequest
    pr = db.get(PayRequest, pid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    act = str(f.get("action") or "save")
    if act == "manual" and pr.status == "recognizing":           # не чекати агента — заповнити вручну
        pr.status, pr.error = "new", None
        db.commit()
        return RedirectResponse(f"/payreq/{pid}", 303)
    if pr.status in ("new", "error", "rejected") or pr.kind == "receipt":
        pr.receiver_name = str(f.get("receiver_name") or "").strip()[:300] or None
        pr.receiver_iban = prq.clean_iban(f.get("receiver_iban"))[:40] or None
        pr.receiver_edrpou = re.sub(r"\D", "", str(f.get("receiver_edrpou") or ""))[:20] or None
        pr.amount = _num(str(f.get("amount") or ""))
        pr.vat = _num(str(f.get("vat") or "")) or 0
        pr.invoice_no = str(f.get("invoice_no") or "").strip()[:80] or None
        pr.invoice_date = _day(str(f.get("invoice_date") or ""))
        fop_ = str(f.get("fop") or "")
        if fop_ in prq.fops(db):
            pr.fop = fop_
        pr.sender_iban = str(f.get("sender_iban") or "").strip() or prq.sender_iban(db, pr.fop)
        pr.purpose = str(f.get("purpose") or "").strip()[:420] or prq.default_purpose(pr)
        sid_ = str(f.get("supplier_id") or "")
        pr.supplier_id = int(sid_) if sid_.isdigit() else pr.supplier_id
        if pr.status == "error" and not prq.problems(pr):
            pr.status, pr.error = "new", None
        if pr.kind == "receipt":
            if pr.status == "error":
                pr.status, pr.error = "receipt", None
            if not pr.supplier_id:
                from .services.suppliers import find as _sfind
                hit = _sfind(db, pr.receiver_iban, pr.receiver_edrpou)
                pr.supplier_id = hit[0].id if hit else None
        db.commit()
        if pr.kind == "receipt" and not pr.link_id:
            tgt = prq.link_receipt(db, pr)
            if tgt:
                prq.tg_update(db, tgt)
                prq.tg_update(db, pr)
    if act == "approve":
        if user.role != "owner":
            return RedirectResponse(_with_err(f"/payreq/{pid}", "Погоджує лише власник"), 303)
        await asyncio.to_thread(prq.approve, db, pr, user.id)
        audit(db, "payreq.approve", "payreq", pid, user=user, amount=pr.amount, status=pr.status)
    elif act == "reject":
        prq.reject(db, pr, user.id)
        audit(db, "payreq.reject", "payreq", pid, user=user)
    elif act == "refresh":
        await asyncio.to_thread(prq.refresh, db, pr)
    if pr.tg_chat and pr.tg_card and state.get("bot"):                 # оновити картку в Telegram
        try:
            from .integrations.telegram_bot import pay_keyboard
            await state["bot"].edit_message_text(prq.card_text(db, pr), chat_id=pr.tg_chat, message_id=pr.tg_card,
                                                 reply_markup=pay_keyboard(pr), disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass
    msg_ = {"approve": "Чернетку створено в monobank — підпишіть у застосунку" if pr.status == "draft" else (pr.error or "Помилка"),
            "reject": "Відхилено", "refresh": f"Стан: {prq.STATUS.get(pr.status)}"}.get(act, "Збережено")
    return RedirectResponse(_with_msg(f"/payreq/{pid}", msg_, err=(act == "approve" and pr.status != "draft")), 303)


@app.post("/payreq/settings")
async def payreq_settings(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pay_requests as prq
    f = await request.form()
    c = prq.cfg(db)
    c["sender"] = {fp: str(f.get(f"sender_{i}") or "") for i, fp in enumerate(prq.fops(db)) if f.get(f"sender_{i}")}
    prq.save_cfg(db, c)
    if "chat" in f:
        prq.set_chat(db, str(f.get("chat") or "").strip() or None)
    from .services import chat_expenses as _ce
    _ce.set_enabled(db, bool(f.get("exp_on")))
    audit(db, "payreq.settings", user=user)
    return RedirectResponse(_with_msg("/payreq", "Налаштування збережено"), 303)


@app.post("/stock/mono/corp")
def stock_mono_corp(fop: str = Form(...), token: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Перевірити доступ до Corporate API monobank (платежі на підпис) — токеном виписки або окремим корпоративним."""
    from .integrations import monobank
    res = monobank.corp_probe(db, fop.strip()[:100], token)
    audit(db, "mono.corp_probe", user=user, fop=fop, ok=res.get("ok"), code=res.get("code"))      # токен у журнал не пишемо
    return RedirectResponse(f"/settings/keys?s=mono#mono", 303)


@app.post("/stock/mono/watch")
async def stock_mono_watch(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .integrations import monobank
    f = await request.form()
    monobank.set_watch(db, f.get("fop", ""), f.getlist("acc"))
    m = monobank.cashbox_map(db)
    for k, v in f.items():
        if k.startswith("cb_"):
            m[k[3:]] = v
    monobank.save_cashbox_map(db, m)
    return RedirectResponse("/settings/keys?saved=1#mono" if f.get("back") == "keys" else "/stock?tab=suppliers#mono", 303)


@app.post("/stock/mono/disconnect")
def stock_mono_disconnect(fop: str = Form(...), back: str = Form(""), user: User = Depends(require("finance")),
                          db: Session = Depends(db_dep)):
    from .integrations import monobank
    monobank.disconnect(db, fop)
    audit(db, "mono.disconnect", user=user, fop=fop)
    return RedirectResponse("/settings/keys#mono" if back == "keys" else "/stock?tab=suppliers#mono", 303)


@app.post("/stock/mono/sync")
def stock_mono_sync(days: int = Form(2), back: str = Form(""), user: User = Depends(require("finance")),
                    db: Session = Depends(db_dep)):
    job = jobs.start("Виписка monobank", _mono_job, max(1, min(days, 31)))
    if back == "keys":
        return RedirectResponse(f"/settings/keys?job={job.id}#mono", 303)
    return RedirectResponse(f"/stock?tab=suppliers&job={job.id}#mono", 303)


def _mono_history_job(job, d_from: str, d_to: str):
    from .integrations import monobank
    with SessionLocal() as db:
        job.log("Історія виписок monobank: вікнами по 31 добі, пауза 61 с між запитами (ліміт monobank)…")
        return monobank.history(db, date.fromisoformat(d_from), date.fromisoformat(d_to), log=job.log)


@app.post("/stock/mono/history")
def stock_mono_history(d_from: str = Form("2022-01-01"), d_to: str = Form("2026-10-31"), user: User = Depends(require("finance")),
                       db: Session = Depends(db_dep)):
    try:
        a_, b_ = date.fromisoformat(d_from), date.fromisoformat(d_to)
    except ValueError:
        return RedirectResponse(_with_msg("/settings/keys?s=mono#mono", "Невірні дати", True), 303)
    job = jobs.start("Історія виписок monobank", _mono_history_job, a_.isoformat(), min(b_, date.today()).isoformat())
    audit(db, "mono.history", user=user, d_from=d_from, d_to=d_to, job=job.id)
    db.commit()
    return RedirectResponse(f"/settings/keys?s=mono&job={job.id}#mono", 303)


@app.post("/stock/mono/recheck")
def stock_mono_recheck(user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Зняти автоматичні рознесення, де постачальника впізнано лише за назвою (могли промахнутись)."""
    from .models import PaymentAlloc
    from .services import payments as pay
    n_ = 0
    for w in pay.weak_auto(db):
        docs_ = {a.doc_id for a in db.scalars(select(PaymentAlloc).where(PaymentAlloc.txn_id == w.id))}
        pay.unallocate_txn(db, w)
        pay.reject(w, {pay.group_key(x) for x in (db.get(StockDoc, i) for i in docs_) if x})
        w.supplier_id = None
        db.commit()
        for d_ in docs_:
            audit(db, "stock.pay_unassign", "stock_doc", d_, user=user, amount=round(-w.amount, 2), fop=w.fop, txn=w.id,
                  why="упізнано лише за назвою")
        n_ += 1
    pay.rematch_all(db)                               # частково рознесені доносять залишок
    audit(db, "mono.recheck_weak", user=user, n=n_)
    return RedirectResponse(f"/stock?tab=suppliers&msg={_q(f'Знято сумнівних автоматичних рознесень: {n_}. Вони чекають ручного рішення нижче.')}#mono", 303)


@app.post("/stock/mono/txn/{tid}")
def stock_mono_txn(tid: str, action: str = Form(...), group: str = Form(""), supplier_id: str = Form(""), back: str = Form(""),
                   user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Ручне рознесення платежу: assign (на накладну) · unassign · ignore · auto (спробувати ще раз)."""
    from .models import BankTxn, Supplier
    from .services import payments as pay
    t = db.get(BankTxn, tid) or (_ for _ in ()).throw(HTTPException(404))
    if supplier_id.isdigit():
        t.supplier_id = int(supplier_id)
    if action == "assign" and group:
        g = pay.find_group(db, group)
        if g:
            done = pay.allocate(db, g, -t.amount, txn=t, fop=t.fop, user_id=user.id)
            sup_ = next((x for x in db.scalars(select(Supplier)) if x.name.lower() == (g.supplier or "").lower()), None)
            if sup_:
                t.supplier_id = sup_.id
                pay.learn_entity(db, t, sup_)                    # наступного разу — автоматично за IBAN
            t.status, t.note = ("matched" if done >= -t.amount - pay.TOL else "partial"), f"→ {g.label}"
            for d_ in g.docs:
                audit(db, "stock.pay_mono", "stock_doc", d_.id, user=user, amount=round(done, 2), fop=t.fop, txn=tid)
    elif action == "unassign":
        from .models import PaymentAlloc
        docs_ = {a.doc_id for a in db.scalars(select(PaymentAlloc).where(PaymentAlloc.txn_id == t.id))}
        pay.unallocate_txn(db, t)
        for d_ in docs_:
            audit(db, "stock.pay_unassign", "stock_doc", d_, user=user, amount=round(-t.amount, 2), fop=t.fop, txn=tid)
        pay.reject(t, {pay.group_key(x) for x in (db.get(StockDoc, i) for i in docs_) if x})   # на ці накладні — більше ніколи
        db.commit()
        pay.rematch_all(db)                           # інші платежі доносять залишок (за тими ж правилами вікна)
    elif action == "unbook":
        pay.unallocate_txn(db, t)
    elif action == "ignore":
        t.status = "ignored"
    elif action == "auto":
        pay.match_txn(db, t)
    db.commit()
    audit(db, "mono.txn", user=user, txn=tid, op=action)
    if back.startswith("/stock/docs/"):
        return RedirectResponse(back, 303)
    return RedirectResponse("/stock?tab=suppliers#mono", 303)


@app.post("/stock/suppliers/import")
async def stock_invoices_import(file: UploadFile = File(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Excel «Склад → Поставки» з CleverBox → оборот і борг по постачальниках."""
    import tempfile
    from .services import stock as st
    suffix = ".xls" if (file.filename or "").lower().endswith(".xls") else ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
    try:
        kind = await asyncio.to_thread(catalog.detect_export, tmp.name)
        if kind == "products":                       # переплутали форму — товари теж приймаємо
            rep = await asyncio.to_thread(catalog.import_products, db, tmp.name)
            rep = {"error": f"Це був файл «Товари» — імпортовано в Продукти ({rep.get('rows', 0)} товарів)."}
        elif kind != "supply":
            raise ValueError(catalog.WRONG_FILE[kind])
        else:
            rep = await asyncio.to_thread(st.import_supplier_invoices, db, tmp.name)
    except ValueError as e:
        rep = {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        rep = {"error": f"Не вдалося прочитати файл: {type(e).__name__}"}
    finally:
        os.unlink(tmp.name)
    audit(db, "stock.invoices_import", user=user, **rep)
    LAST_IMPORT[f"inv-{user.id}"] = rep
    return RedirectResponse("/stock?tab=suppliers", 303)


@app.get("/stock/invoices/{iid:int}", response_class=HTMLResponse)
def stock_invoice_view(iid: int, request: Request, job: str = "", user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Поставка CleverBox: що в ній було — позиції з API / Excel, позиції «Реалізації», документ складу CRM з тим самим №."""
    from .models import SupplierInvoice, ConsignLot
    from .integrations import cb_supply
    x = db.get(SupplierInvoice, iid)
    if not x:
        raise HTTPException(404)
    lots = db.scalars(select(ConsignLot).where(ConsignLot.invoice == x.cb_number).order_by(ConsignLot.id)).all()
    docs = db.scalars(select(StockDoc).where(StockDoc.ext_no == x.cb_number)).all()
    j = jobs.JOBS.get(job) if job else None
    return render(request, "stock_invoice.html", nav="stock", x=x, lots=lots, docs=docs, job=j, api_method=cb_supply.method(db))


def _inv_items_job(job, iid: int):
    from .models import SupplierInvoice
    from .integrations import cb_supply
    with SessionLocal() as db:
        x = db.get(SupplierInvoice, iid)
        if not cb_supply.method(db):
            job.log("Шукаю в API CleverBox метод поставок (до хвилини — ліміт 1 запит/сек)…")
            r = cb_supply.probe(db, log_=job.log)
            if not r["chosen"]:
                return {"found": False, "msg": "API CleverBox не віддає позиції поставок — завантажте Excel поставки"}
        items = cb_supply.items_for(db, x)
        if not items:
            return {"found": False, "msg": f"У відповіді API не знайшов поставку №{x.cb_number}"}
        x.items, x.items_src, x.items_at = items, "api", datetime.utcnow()
        db.commit()
        return {"found": True, "n": len(items)}


@app.post("/stock/invoices/{iid:int}/fetch")
def stock_invoice_fetch(iid: int, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    j = jobs.start(f"Позиції поставки {iid}", _inv_items_job, iid)
    return RedirectResponse(f"/stock/invoices/{iid}?job={j.id}", 303)


@app.post("/stock/invoices/{iid:int}/items")
async def stock_invoice_items_upload(iid: int, file: UploadFile = File(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    import tempfile
    from .models import SupplierInvoice
    from .services import consign_act as ca
    x = db.get(SupplierInvoice, iid)
    if not x:
        raise HTTPException(404)
    raw = await file.read()
    suffix = os.path.splitext(file.filename or "")[1].lower() or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(raw)
    tmp.close()
    try:
        parsed = ca.parse(ca.read_rows(tmp.name, raw))
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(_with_msg(f"/stock/invoices/{iid}", f"Не вдалося прочитати файл: {str(e)[:150]}", err=True), 303)
    finally:
        os.unlink(tmp.name)
    if not parsed["goods"]:
        return RedirectResponse(_with_msg(f"/stock/invoices/{iid}", "У файлі не знайшов рядків товару (Товар / Кількість / Ціна / Сума)", err=True), 303)
    x.items = [{"name": g["name"], "qty": g["qty"], "price": g["price"] if g["price"] is not None else (round(g["sum"] / g["qty"], 2) if g["sum"] and g["qty"] else None),
                "sum": g["sum"], "sku": g.get("sku")} for g in parsed["goods"]]
    x.items_src, x.items_at = "excel", datetime.utcnow()
    db.commit()
    return RedirectResponse(_with_msg(f"/stock/invoices/{iid}", f"Позицій завантажено: {len(x.items)}"), 303)


@app.post("/stock/invoices/{iid:int}/paid")
def stock_invoice_paid(iid: int, back: str = Form("/stock?tab=suppliers"), note: str = Form(""),
                       user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Власник відмічає поставку оплаченою (якщо в CleverBox оплату не провели)."""
    from .models import SupplierInvoice
    x = db.get(SupplierInvoice, iid)
    if x:
        x.paid = x.amount
        x.paid_note = (note.strip() or f"оплачено, відмітив {user.name} {date.today():%d.%m.%Y}")[:300]
        db.commit()
        audit(db, "stock.invoice_paid", user=user, invoice=x.cb_number, amount=x.amount)
    return RedirectResponse(back if back.startswith("/stock") else "/stock?tab=suppliers", 303)


@app.post("/stock/invoices/{iid:int}/free")
def stock_invoice_free(iid: int, back: str = Form("/stock?tab=suppliers"), note: str = Form(""),
                       user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Безоплатно: подарунок / повернення / інвентаризація — борг поставки закривається без оплати (знижка на всю суму боргу)."""
    from .models import SupplierInvoice
    x = db.get(SupplierInvoice, iid)
    if x and x.debt > 0:
        d = x.debt
        x.adjust = round((x.adjust or 0) + d, 2)
        x.adjust_note = (note.strip() or "подарунок / без оплати")[:250] + f" · {user.name} {date.today():%d.%m.%Y}"
        db.commit()
        audit(db, "stock.invoice_free", user=user, invoice=x.cb_number, amount=d, note=x.adjust_note)
    return RedirectResponse(back if back.startswith("/stock") else "/stock?tab=suppliers", 303)


@app.post("/stock/norms")
def stock_norm_add(service_id: int = Form(...), product: str = Form(...), qty: str = Form("1"), warehouse: str = Form(""),
                   techcard: str = Form(""), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Норма на послугу; якщо передано техкартку — на всі послуги цієї техкартки (без дублів)."""
    from .models import ProcedureNorm, CbService
    pr = _pick_product(db, product)
    if not pr:
        return RedirectResponse(f"/stock?tab=norms&msg=Товар+не+знайдено#s{service_id}", 303)
    ids = [service_id]
    if techcard:
        ids = [x for (x,) in db.execute(select(CbService.id).where(CbService.techcard == techcard))] or ids
    have = {sid for (sid,) in db.execute(select(ProcedureNorm.service_id).where(
        ProcedureNorm.product_id == pr.id, ProcedureNorm.service_id.in_(ids)))}
    for sid in ids:
        if sid not in have:
            db.add(ProcedureNorm(service_id=sid, product_id=pr.id, qty=_num(qty) or 1,
                                 warehouse=warehouse if warehouse in catalog.WAREHOUSES else None))
    db.commit()
    audit(db, "stock.norm_add", user=user, service=service_id, techcard=techcard or None, services=len(ids), product=pr.id)
    return RedirectResponse(f"/stock?tab=norms#s{service_id}", 303)


# ---------------- Центр моніторингу цін ----------------
@app.get("/prices", response_class=HTMLResponse)
def prices_page(request: Request, tab: str = "products", wh: str = "", zone: str = "", q: str = "",
                user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import price_monitor as pm
    from .models import PriceAlert
    pm.scan(db, notify=True)
    d = pm.cfg(db)
    prods, svcs = pm.products(db, d), pm.services(db, d)
    ql = q.strip().lower()
    rows = prods if tab == "products" else svcs
    if tab == "products" and wh in pm.WAREHOUSES:
        rows = [r for r in rows if r["wh"] == wh]
    if zone.isdigit():
        rows = [r for r in rows if r["zone"] == int(zone)] if zone != "9" else [r for r in rows if r["zone"] > 0]
    if ql:
        rows = [r for r in rows if ql in (r["name"] or "").lower() or ql in (r.get("brand") or "").lower() or ql in (r.get("category") or "").lower()]
    rows = sorted(rows, key=lambda r: (-r["zone"], -(r["diff"] or 0), r["name"]))
    alerts_ = db.scalars(select(PriceAlert).order_by(PriceAlert.created_at.desc()).limit(300)).all()
    stat = lambda xs: [sum(1 for r in xs if r["zone"] == z) for z in range(5)]  # noqa: E731
    return render(request, "prices.html", nav="stock", tab=tab, rows=rows, d=d, pm=pm, wh=wh, zone=zone, q=q, alerts=alerts_,
                  st_p=stat(prods), st_s=stat(svcs), n_p=len(prods), n_s=len(svcs), unseen=pm.unseen(db))


@app.post("/prices/settings")
async def prices_settings(request: Request, user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .services import price_monitor as pm
    f = await request.form()
    d = pm.cfg(db)
    def zs(prefix):
        vals = [_num(str(f.get(f"{prefix}{i}") or "").replace(",", ".")) or 0 for i in range(1, 5)]
        return sorted(round(float(v), 2) for v in vals)
    for k in pm.WAREHOUSES:
        d["zones"][k] = zs(f"z_{k}_")
    d["svc_zones"] = zs("z_svc_")
    d["tg"] = bool(f.get("tg"))
    pm.save_cfg(db, d)
    audit(db, "prices.settings", user=user, zones=d["zones"], svc_zones=d["svc_zones"])
    return RedirectResponse(_with_msg("/prices?tab=settings", "Зони збережено"), 303)


@app.post("/prices/accept")
def prices_accept(kind: str = Form(...), item_id: int = Form(...), back: str = Form("/prices"), user: User = Depends(require("stock")),
                  db: Session = Depends(db_dep)):
    from .services import price_monitor as pm
    pm.accept(db, "service" if kind == "service" else "product", item_id)
    audit(db, "prices.accept", user=user, kind=kind, item=item_id)
    return RedirectResponse(_with_msg(back if back.startswith("/prices") else "/prices", "Поточну ціну прийнято як стартову"), 303)


@app.post("/prices/alerts/seen")
def prices_alerts_seen(user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    from .models import PriceAlert
    for a in db.scalars(select(PriceAlert).where(PriceAlert.seen_at.is_(None))):
        a.seen_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/prices?tab=alerts", 303)


@app.post("/stock/norms/cost")
def stock_norm_cost(service_id: int = Form(...), cost_fixed: str = Form(""), techcard: str = Form(""),
                    user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Фіксована собівартість послуги (для окладів «віднімати собівартість» з фіксованою); техкартка — на всі її послуги."""
    from .models import CbService
    v = _num(cost_fixed.replace(",", ".")) if cost_fixed.strip() else None
    ids = [service_id]
    if techcard:
        ids = [x for (x,) in db.execute(select(CbService.id).where(CbService.techcard == techcard))] or ids
    for sv in db.scalars(select(CbService).where(CbService.id.in_(ids))):
        sv.cost_fixed = round(v, 2) if v is not None else None
    db.commit()
    audit(db, "stock.service_cost", user=user, service=service_id, techcard=techcard or None, cost_fixed=v, n=len(ids))
    return RedirectResponse(f"/stock?tab=norms&msg=Собівартість+збережено#s{service_id}", 303)


@app.post("/stock/norms/{nid:int}/delete")
def stock_norm_delete(nid: int, techcard: str = Form(""), del_password: str = Form(""), user: User = Depends(require("stock")),
                      db: Session = Depends(db_dep)):
    from .models import ProcedureNorm, CbService
    n = db.get(ProcedureNorm, nid)
    sid = n.service_id if n else ""
    err = _delpw(db, user, del_password, "norm", "norm", nid)
    if err:
        return RedirectResponse(_with_err(f"/stock?tab=norms#s{sid}", err), 303)
    if n:
        if techcard:      # прибрати матеріал з усієї техкартки
            ids = [x for (x,) in db.execute(select(CbService.id).where(CbService.techcard == techcard))]
            for m in db.scalars(select(ProcedureNorm).where(ProcedureNorm.product_id == n.product_id,
                                                            ProcedureNorm.service_id.in_(ids or [sid]))):
                db.delete(m)
        else:
            db.delete(n)
        db.commit()
    return RedirectResponse(f"/stock?tab=norms#s{sid}", 303)


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
    staff_ph = {d_.name: d_.photo_url for d_ in db.scalars(select(Doctor)) if d_.photo_url}
    return render(request, "finance.html", fs=fs, fc=fc, total=total, d_from=d_from, d_to=d_to, m_max=m_max, staff_ph=staff_ph, today=today)


# ---------------- Сертифікати й абонементи ----------------
@app.get("/gifts", response_class=HTMLResponse)
def gifts_page(request: Request, tab: str = "certs", st: str = "", q: str = "", seller: str = "",
               user: User = Depends(require("patients_all")), db: Session = Depends(db_dep)):
    from .models import GiftCertificate, Subscription
    from .services import certificates as cs
    tab = tab if tab in ("certs", "subs", "types") else "certs"
    today = date.today()
    if tab == "types":
        from .services import gift_types as _gt
        qp_ = request.query_params
        sold_: dict = defaultdict(int)
        for (tn_,) in db.execute(select(GiftCertificate.type_name).where(GiftCertificate.id >= _gt.CRM_ID_FROM)):
            sold_[tn_ or ""] += 1
        return render(request, "gifts.html", nav="gifts", tab=tab, types=_gt.items(db), gcfg=_gt.cfg(db), sold=dict(sold_), packages=cs.packages(db),
                      pkg_items=cs.package_items(db), PKG_KINDS=cs.PKG_KINDS,
                      pkg=cs.package_get(db, int(qp_.get("pkg"))) if (qp_.get("pkg") or "").isdigit() else ({} if qp_.get("pkg") == "new" else None),
                      pkg_rep=LAST_IMPORT.pop(f"gifts-{user.id}", None),
                      cur=_gt.get(db, int(qp_.get("edit"))) if (qp_.get("edit") or "").isdigit() else None,
                      items=[], total=0, states=cs.STATES, counts={}, st="", q="", seller="", sellers=[], today=today, other_n=0, last=None,
                      remind_days=_gt.cfg(db).get("remind_days", 14))
    model, states = (GiftCertificate, cs.STATES) if tab == "certs" else (Subscription, cs.SUB_STATES)
    items = db.scalars(select(model).order_by(model.sold_at.desc())).all()
    # неоплачені «без клієнта» з CleverBox — це заготовки-шаблони для продажу, а не сертифікати пацієнтів: у списку не показуємо
    items = [x for x in items if not (x.state(today) == "unpaid" and not x.patient_id and not x.client_name)]
    counts = defaultdict(lambda: {"n": 0, "sum": 0.0})
    for x in items:
        k = x.state(today)
        counts[k]["n"] += 1
        counts[k]["sum"] += (x.balance or 0) if tab == "certs" else x.value_left
    sellers = sorted({x.seller for x in items if x.seller})
    if st in states:
        items = [x for x in items if x.state(today) == st]
    elif not q and not seller:
        items = [x for x in items if x.state(today) in ("active", "expiring", "unpaid")] or items[:100]
    if seller:
        items = [x for x in items if x.seller == seller]
    if q:
        words = set(norm(q).split())
        items = [x for x in items if words <= set(norm(" ".join(filter(None, (
            x.client_name, x.code, str(x.id), x.type_name, x.seller)))).split())]
    if st == "expiring" or not st:
        items.sort(key=lambda x: (x.state(today) not in ("expiring",), x.valid_to or date.max))
    other = Subscription if tab == "certs" else GiftCertificate
    return render(request, "gifts.html", nav="gifts", tab=tab, items=items[:500], total=len(items), states=states,
                  counts=dict(counts), st=st, q=q, seller=seller, sellers=sellers, today=today,
                  other_n=db.scalar(select(func.count()).select_from(other)) or 0, last=LAST_IMPORT.pop(f"gifts-{user.id}", None),
                  packages=cs.package_items(db) if tab == "subs" else [],
                  remind_days=__import__("app.services.gift_types", fromlist=["cfg"]).cfg(db).get("remind_days", 14))


@app.post("/gifts/types")
async def gifts_types_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import gift_types as _gt
    f = await request.form()
    try:
        t = _gt.save(db, int(f["tid"]) if str(f.get("tid") or "").isdigit() else None,
                     {"name": f.get("name"), "nominal": _num(str(f.get("nominal") or "")), "price": _num(str(f.get("price") or "")),
                      "days": int(_num(str(f.get("days") or "")) or 60), "desc": f.get("desc"), "services": f.get("services"),
                      "only_buyer": bool(f.get("only_buyer")), "active": bool(f.get("active")), "usage": f.get("usage"),
                      "cb_id": int(f["cb_id"]) if str(f.get("cb_id") or "").isdigit() else None})
    except (ValueError, TypeError) as e:
        return RedirectResponse(_with_err("/gifts?tab=types", str(e)), 303)
    audit(db, "gifts.type_save", user=user, name=t["name"])
    return RedirectResponse(_with_msg("/gifts?tab=types", f"Шаблон «{t['name']}» збережено"), 303)


@app.post("/gifts/types/{tid:int}/toggle")
def gifts_types_toggle(tid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import gift_types as _gt
    _gt.toggle(db, tid)
    return RedirectResponse("/gifts?tab=types", 303)


@app.post("/gifts/settings")
async def gifts_settings_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import gift_types as _gt
    f = await request.form()
    _gt.save_cfg(db, {"remind_days": int(_num(str(f.get("remind_days") or "")) or 14), "notify_tg": bool(f.get("notify_tg")),
                      "code_prefix": f.get("code_prefix"), "desc": f.get("desc")})
    audit(db, "gifts.settings", user=user)
    return RedirectResponse(_with_msg("/gifts?tab=types", "Налаштування збережено"), 303)


@app.post("/gifts/packages/{pid:int}")
async def gifts_package_save(pid: int, request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Шаблон абонемента: кількість візитів, термін, ціна, активний для продажу."""
    from .services import certificates as cs
    f = await request.form()
    if f.get("action") in ("new", "full"):
        items = [{"service_id": a, "name": b, "qty": c_, "price": d_} for a, b, c_, d_ in zip(f.getlist("it_sid"), f.getlist("it_name"), f.getlist("it_qty"), f.getlist("it_price"))]
        x = cs.package_full_save(db, pid or None, {"name": f.get("name"), "kind": f.get("kind"), "short_name": f.get("short_name"), "days": f.get("days"),
                                                    "price": f.get("price"), "all_clients": bool(f.get("all_clients")), "active": bool(f.get("active", "1") if "active" in f else True),
                                                    "visits": f.get("visits"), "items": items, "services": f.get("services")})
        audit(db, "gifts.package", user=user, id=x["id"], op=f.get("action"))
        return RedirectResponse(_with_msg("/gifts?tab=types", f"Шаблон «{x['name']}» збережено") + "#pkg", 303)
    if f.get("action") == "delete":
        cs.package_delete(db, pid)
        audit(db, "gifts.package", user=user, id=pid, op="delete")
        return RedirectResponse("/gifts?tab=types#pkg", 303)
    if f.get("action") == "all_on":
        for x in cs.packages(db):
            cs.package_save(db, x["id"], {"active": True})
    elif f.get("action") == "all_off":
        for x in cs.packages(db):
            cs.package_save(db, x["id"], {"active": False})
    else:
        cs.package_save(db, pid, {"visits": f.get("visits"), "days": f.get("days"), "price": f.get("price"), "active": bool(f.get("active"))})
    audit(db, "gifts.package", user=user, id=pid, op=f.get("action") or "save")
    return RedirectResponse("/gifts?tab=types#pkg", 303)


@app.post("/gifts/import")
async def gifts_import(file: UploadFile = File(...), user: User = Depends(require("import")), db: Session = Depends(db_dep)):
    """Excel CleverBox «Сертифікати» або «Абонементи» — тип визначається за колонками."""
    from .services import certificates as cs
    tab = "certs"
    try:
        sheets = importer.read_sheets(await file.read(), file.filename or "data.xls")
        head, rows = (sheets[0][1][0], sheets[0][1][1:]) if sheets and sheets[0][1] else ([], [])
        if cs.is_certificates_export(head):
            rep = await asyncio.to_thread(cs.import_certificates, db, rows, head)
        elif cs.is_packages_export(head):
            rep, tab = await asyncio.to_thread(cs.import_packages, db, rows, head), "types"
        elif cs.is_subscriptions_export(head):
            rep, tab = await asyncio.to_thread(cs.import_subscriptions, db, rows, head), "subs"
        else:
            rep = {"error": "Це не вивантаження CleverBox «Сертифікати», «Абонементи» чи «Пакети». Колонки: "
                            + ", ".join(str(h) for h in head[:12])}
    except Exception as e:  # noqa: BLE001
        rep = {"error": f"Не вдалося прочитати файл: {type(e).__name__}"}
    rep["sheet"] = file.filename
    audit(db, "gifts.import", user=user, **{k: v for k, v in rep.items() if k in ("type", "rows", "updated", "error", "sheet")})
    LAST_IMPORT[f"gifts-{user.id}"] = rep
    return RedirectResponse(f"/gifts?tab={tab}", 303)


# ---------------- Звіти ----------------
def _report_period(period: str, df: str | None, dt_: str | None) -> tuple[date, date, str]:
    today = date.today()
    m0 = today.replace(day=1)
    if df or dt_:
        a = _day(df) or m0
        b = _day(dt_) or today
        return (a, b, "custom") if a <= b else (b, a, "custom")
    if period == "today":
        return today, today, period
    if period == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1), period
    if period == "prev_month":
        b = m0 - timedelta(days=1)
        return b.replace(day=1), b, period
    if period == "quarter":
        q0 = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        return q0, today, period
    if period == "year":
        return date(today.year, 1, 1), today, period
    if period == "30d":
        return today - timedelta(days=29), today, period
    if period == "12m":
        y, m = (today.year - 1, today.month + 1) if today.month < 12 else (today.year, 1)
        return date(y, m, 1), today, period
    return m0, today, "month"


def _expiry_page(request: Request, db: Session):
    from .services import expiry as ex, reports as rp
    wh = request.query_params.get("wh", "")
    rep = ex.report(db)
    cfg = ex.config(db)
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.role, User.name)).all()
    return render(request, "expiry.html", nav="reports", REPORTS=rp.REPORTS, key="expiry", rep=rep, wh=wh, cfg=cfg,
                  users=users, LABELS=ex.LABELS, WEEKDAYS=ex.WEEKDAYS, admin_chat=bool(settings.telegram_admin_chat_id),
                  bot=bool(settings.telegram_bot_token), recips=ex.recipients(db, cfg), ROLES=ROLES)


@app.post("/reports/expiry/writeoff")
def expiry_writeoff(wh: str = Form(...), user: User = Depends(require("stock")), db: Session = Depends(db_dep)):
    """Чернетка списання всіх прострочених партій складу — перевірити й провести."""
    from .services import stock as st, expiry as ex
    from .models import StockDocLine
    rows = [x for x in ex.rows(db) if x["wh"] == wh and x["days"] is not None and x["days"] < 0]
    if not rows or not st.ledger_active(db):
        return RedirectResponse(f"/reports?r=expiry&wh={wh}", 303)
    reason = next((r for r in st.WRITEOFF_REASONS if "простроч" in r.lower()), st.WRITEOFF_REASONS[0])
    doc = st.new_doc(db, "writeoff", user_id=user.id, wh_from=wh, reason=reason, note="Прострочені партії (зі звіту термінів)")
    for x in rows:
        doc.lines.append(StockDocLine(product_id=x["product"].id, qty=x["qty"], expiry_date=x["expiry"]))
    db.commit()
    audit(db, "expiry.writeoff_draft", "stock_doc", doc.id, user=user, wh=wh, lines=len(rows))
    return RedirectResponse(f"/stock/docs/{doc.id}?msg=Чернетка+списання+прострочених+партій+—+перевірте+і+проведіть", 303)


@app.post("/reports/expiry/notify")
async def expiry_notify(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import expiry as ex
    f = await request.form()
    cfg = {"users": [int(x) for x in f.getlist("users") if str(x).isdigit()], "admin_chat": bool(f.get("admin_chat")),
           "weekday": int(f.get("weekday") or 0), "days": int(f.get("days") or 90), "alerts": bool(f.get("alerts"))}
    ex.save_config(db, cfg)
    audit(db, "expiry.notify", user=user, **cfg)
    if f.get("test"):
        n = ex.send(db, ex.summary_text(db, cfg["days"]) or "🗓 Терміни придатності: прострочених і близьких партій немає ✅", cfg)
        return RedirectResponse(f"/reports?r=expiry&sent={n}#notify", 303)
    return RedirectResponse("/reports?r=expiry&saved=1#notify", 303)


def _fop_page(request: Request, db: Session):
    from .services import fop as fop_svc
    from .services import reports as rp
    from .integrations.checkbox import accounts
    from .models import FopMonth
    years = sorted({y for (y,) in db.execute(select(FopMonth.year).distinct())} | {date.today().year}, reverse=True)
    try:
        year = int(request.query_params.get("year") or date.today().year)
    except ValueError:
        year = date.today().year
    st_ = db.get(SyncState, "checkbox_last")
    jid = request.query_params.get("job")
    f_ = fop_svc.build(db, year)
    qp = request.query_params
    for i, x in enumerate(f_["fops"]):               # ручний період у картці кожного ФОП: ?p0_from=2025-01&p0_to=2026-03
        a = fop_svc.parse_ym(qp.get(f"p{i}_from")) or (year, 1)
        b = fop_svc.parse_ym(qp.get(f"p{i}_to")) or (year, 12)
        x["period"] = fop_svc.period(db, x["name"], a, b)
        x["period"]["custom"] = bool(qp.get(f"p{i}_from") or qp.get(f"p{i}_to"))
    return render(request, "fop.html", nav="reports", REPORTS=rp.REPORTS, key="fop", f=f_,
                  years=years, year=year, cb_status=__import__("app.integrations.checkbox", fromlist=["x"]).statuses(db),
                  accs=[{"name": a["name"], "configured": a["configured"], "login": a["login"], "source": a["source"]}
                        for a in accounts(db)],
                  cb_last=st_.value if st_ else None, job=jobs.JOBS.get(jid) if jid else None,
                  last=LAST_IMPORT.pop(f"fop-{request.state.user.id}", None))


def _fop_job(job, start: str | None = None):
    from .integrations import checkbox as chb
    with SessionLocal() as db:
        s0 = date.fromisoformat(start) if start else None
        job.log("Завантажую Z-звіти з Checkbox…")
        return chb.sync(db, s0, date.today())


@app.post("/reports/fop/sync")
def fop_sync(start: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    job = jobs.start("Оборот ФОП з Checkbox", _fop_job, start or None)
    audit(db, "fop.sync", user=user, start=start or None, job=job.id)
    return RedirectResponse(f"/reports?r=fop&job={job.id}", 303)


@app.post("/reports/fop/connect")
def fop_connect(name: str = Form(...), login: str = Form(...), password: str = Form(""),
                user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Підключити кабінет Checkbox ФОП: логін/пароль касира (пароль — зашифровано в базі)."""
    from .integrations import checkbox as chb
    if not name.strip() or not login.strip():
        return RedirectResponse("/reports?r=fop&cmsg=Вкажіть+назву+ФОП+і+логін+касира#connect", 303)
    err = chb.connect(db, name, login, password)
    audit(db, "fop.connect", user=user, fop=name.strip(), ok=err is None)       # без пароля
    if err:
        return RedirectResponse(f"/reports?r=fop&cerr={name.strip()}#connect", 303)
    job = jobs.start("Оборот ФОП з Checkbox", _fop_job, f"{date.today().year}-01-01")
    return RedirectResponse(f"/reports?r=fop&job={job.id}&cok={name.strip()}#connect", 303)


@app.post("/reports/fop/disconnect")
def fop_disconnect(name: str = Form(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .integrations import checkbox as chb
    chb.disconnect(db, name)
    audit(db, "fop.disconnect", user=user, fop=name)
    return RedirectResponse("/reports?r=fop#connect", 303)


@app.post("/reports/fop/import")
async def fop_import(file: UploadFile = File(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Google-таблиця «Ліміти ФОП» (Файл → Завантажити → .xlsx) — аркуші «<ФОП> <рік>»."""
    from .services import fop as fop_svc
    try:
        sheets = importer.read_sheets(await file.read(), file.filename or "fop.xlsx")
        if not fop_svc.is_fop_workbook(sheets):
            raise ValueError("Не знайдено аркушів «<ФОП> <рік>» з рядками «Готівка» / «Безготівка»")
        rep = fop_svc.import_workbook(db, sheets)
    except ValueError as e:
        rep = {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        rep = {"error": f"Не вдалося прочитати файл: {type(e).__name__}"}
    audit(db, "fop.import", user=user, rows=rep.get("rows"), error=rep.get("error"))
    LAST_IMPORT[f"fop-{user.id}"] = rep
    return RedirectResponse("/reports?r=fop", 303)


@app.post("/reports/fop/limit")
def fop_limit(year: int = Form(...), amount: str = Form(...), user: User = Depends(require("finance")),
              db: Session = Depends(db_dep)):
    from .services import fop as fop_svc
    v = _num(amount)
    if v:
        fop_svc.set_limit(db, year, v)
        audit(db, "fop.limit", user=user, year=year, amount=v)
    return RedirectResponse(f"/reports?r=fop&year={year}", 303)


def _cashflow_page(request: Request, db: Session, period: str, df: str | None):
    from .services import cashflow as cf
    from .services import reports as rp
    years = sorted({d.year for (d,) in db.execute(select(CashDay.day))} | {date.today().year})
    try:
        year = int(request.query_params.get("year") or date.today().year)
    except ValueError:
        year = date.today().year
    import json as _json
    probe_raw = db.get(SyncState, "cleverbox_finance_probe")
    try:
        probe = _json.loads(probe_raw.value) if probe_raw and probe_raw.value else None
    except ValueError:
        probe = None
    meth = db.get(SyncState, "cleverbox_finance_method")
    last = db.get(SyncState, "cleverbox_finance_last")
    jid = request.query_params.get("job")
    return render(request, "cashflow.html", nav="reports", REPORTS=rp.REPORTS, key="cashflow", cf=cf.build(db, year),
                  probe=probe, cb_method=meth.value if meth else None, cb_last=last.value if last else None,
                  job=jobs.JOBS.get(jid) if jid else None,
                  years=years, year=year, last=LAST_IMPORT.pop(f"cash-{request.state.user.id}", None))


@app.post("/reports/cashflow/import")
async def cashflow_import(file: UploadFile = File(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Excel CleverBox «Прибуток» (Profit_*.xls) за будь-який період — дні перезаписуються."""
    from .services import cashflow as cf
    try:
        sheets = importer.read_sheets(await file.read(), file.filename or "profit.xls")
        head, rows = (sheets[0][1][0], sheets[0][1][1:]) if sheets and sheets[0][1] else ([], [])
        if not cf.is_profit_export(head):
            raise ValueError("Це не звіт CleverBox «Прибуток» (потрібні колонки Дата / Надходження / Витрата)")
        rep = await asyncio.to_thread(cf.import_profit, db, rows, head)
        cf.auto_fill(db)
    except ValueError as e:
        rep = {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        rep = {"error": f"Не вдалося прочитати файл: {type(e).__name__}"}
    audit(db, "cashflow.import", user=user, **{k: v for k, v in rep.items() if k in ("rows", "updated", "period", "error")})
    LAST_IMPORT[f"cash-{user.id}"] = rep
    return RedirectResponse("/reports?r=cashflow", 303)


def _cf_job(job, kind: str, start: str | None = None):
    from .integrations import cb_finance
    with SessionLocal() as db:
        if kind == "probe":
            job.log("Перевіряю фінансові методи API CleverBox…")
            return cb_finance.probe(db)
        s0 = date.fromisoformat(start) if start else date(date.today().year, 1, 1)
        job.log(f"Завантажую рух коштів з {s0:%d.%m.%Y}…")
        rep = cb_finance.sync_cash(db, s0, date.today())
        from .services import cashflow as cf
        cf.auto_fill(db)
        return rep


@app.post("/reports/cashflow/cleverbox")
def cashflow_cleverbox(action: str = Form("probe"), start: str = Form(""), user: User = Depends(require("finance")),
                       db: Session = Depends(db_dep)):
    """Кнопки на сторінці Cashflow: перевірити API CleverBox / дозавантажити історію."""
    if action == "reset":
        for k in ("cleverbox_finance_method", "cleverbox_finance_probe_at"):
            st_ = db.get(SyncState, k)
            if st_:
                db.delete(st_)
        db.commit()
        action = "probe"
    job = jobs.start("Cashflow з CleverBox" if action != "probe" else "Пошук фінансових методів CleverBox",
                     _cf_job, action, start or None)
    audit(db, "cashflow.cleverbox", user=user, op=action, job=job.id)
    return RedirectResponse(f"/reports?r=cashflow&job={job.id}", 303)


@app.post("/reports/cashflow/opening")
def cashflow_opening(d: str = Form(...), amount: str = Form(...), user: User = Depends(require("finance")),
                     db: Session = Depends(db_dep)):
    from .services import cashflow as cf
    day, val = _day(d), _num(amount)
    if day and val is not None:
        cf.set_opening(db, day, val)
        audit(db, "cashflow.opening", user=user, date=str(day), amount=val)
    return RedirectResponse("/reports?r=cashflow", 303)


REPORT_PERIODS = {"today": "Сьогодні", "yesterday": "Вчора", "month": "Цей місяць", "prev_month": "Минулий місяць", "30d": "30 днів", "quarter": "Квартал",
                  "year": "З початку року", "12m": "12 місяців"}


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, r: str = "revenue", period: str = "month", df: str | None = None, dt_: str | None = None,
                 fmt: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import reports as rp
    if "r" not in request.query_params:                  # «Звіти» — сторінка з блоками
        from .services import report_hub
        _bl = report_hub.blocks()
        if user.role != "owner":                       # звіти Deye і лізингу — лише власнику
            _bl = [dict(b, items=[it for it in b["items"] if it["url"] not in ("/deye", "/finance/leasing")]) for b in _bl]
        return render(request, "reports_hub.html", nav="reports", blocks=_bl)
    key = r if r in rp.REPORTS else "revenue"
    if key == "cashflow":
        return _cashflow_page(request, db, period, df)
    if key == "pnl":
        from .services import pnl as _pnl
        _ys = _pnl.years(db)
        try:
            _y = int(request.query_params.get("year") or date.today().year)
        except ValueError:
            _y = date.today().year
        _r = _pnl.build(db, _y)
        if fmt == "xlsx":
            audit(db, "reports.export", user=user, report="pnl", year=_y)
            return Response(_pnl.to_xlsx(_r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="pnl-{_y}.xlsx"'})
        return render(request, "rep_pnl.html", nav="reports", REPORTS=rp.REPORTS, key="pnl", r=_r, years=_ys, today=date.today(),
                      MONTHS=_pnl.MONTHS, MONTHS_FULL=_pnl.MONTHS_FULL)
    if key == "patient_money":
        from .services import patient_money as _pm, pnl as _pnl
        _ys = _pm.years(db)
        _yq = request.query_params.get("year")
        try:
            _y = int(_yq) if _yq is not None else date.today().year
        except ValueError:
            _y = date.today().year
        _y = _y if _y in _ys else (0 if _yq == "0" else (_ys[-1] if _ys else date.today().year))
        _mq = request.query_params.get("month") or ""
        _m = int(_mq) if _mq.isdigit() and 1 <= int(_mq) <= 12 and _y else None
        _r = _pm.build(db, _y or None, _m, (request.query_params.get("q") or "").strip()[:80], request.query_params.get("abc") or "")
        if fmt == "xlsx":
            audit(db, "reports.export", user=user, report="patient_money", year=_y, month=_m)
            return Response(_pm.to_xlsx(_r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="patient-money-{_y or "all"}{("-%02d" % _m) if _m else ""}.xlsx"'})
        return render(request, "rep_patient_money.html", nav="reports", REPORTS=rp.REPORTS, key="patient_money", r=_r, years=_ys,
                      today=date.today(), MONTHS=_pnl.MONTHS)
    if key == "pnlm":
        from .services import pnl_monthly as _pm2, pnl_sheet as _psh
        _psh.ensure_seed(db)
        _sy = sorted(_psh.load(db))
        _ys = sorted(set(_pm2.years(db)) | set(_sy))
        try:
            _y = int(request.query_params.get("year") or date.today().year)
        except ValueError:
            _y = date.today().year
        _y = _y if _y in _ys else (_ys[-1] if _ys else date.today().year)
        _sheet = _psh.year_view(db, _y) if _y <= _psh.LAST_SHEET_YEAR else None     # з 2027 — лише метод CRM
        _src = (request.query_params.get("src") or ("hybrid" if _sheet else "crm")) if _sheet else "crm"
        _M = __import__("app.services.pnl", fromlist=["x"]).MONTHS
        if _src == "sheet" and fmt != "xlsx":
            return render(request, "rep_pnl_sheet.html", nav="reports", REPORTS=rp.REPORTS, key="pnlm", sh=_sheet, year=_y, years=_ys,
                          sheet_years=_sy, MONTHS=_M, src="sheet", dirs=_psh.directions_from_hybrid(_psh.hybrid(db, _y, None)) if _sheet else None)
        try:
            _r = _pm2.build(db, _y)
            _r["sheet"] = _sheet
            if _src == "hybrid" and _sheet and fmt != "xlsx":
                _hy = _psh.hybrid(db, _y, _r)
                return render(request, "rep_pnl_sheet.html", nav="reports", REPORTS=rp.REPORTS, key="pnlm", sh=_hy, year=_y, years=_ys,
                              sheet_years=_sy, MONTHS=_M, src="hybrid", dirs=_psh.directions_from_hybrid(_hy))
            _r["dirs"] = _psh.directions_from_crm(_r)
            if fmt == "xlsx":
                audit(db, "reports.export", user=user, report="pnlm", year=_y)
                return Response(_pm2.to_xlsx(_r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                headers={"Content-Disposition": f'attachment; filename="pnl-monthly-{_y}.xlsx"'})
            resp = render(request, "rep_pnl_monthly.html", nav="reports", REPORTS=rp.REPORTS, key="pnlm", r=_r, years=_ys, today=date.today())
            resp.body  # noqa: B018 — шаблон рендериться тут, щоб помилку шаблону теж перехопити
            return resp
        except Exception as e:  # noqa: BLE001 — показати власнику, де саме впав розрахунок, замість порожньої 500
            import traceback
            log.exception("pnlm failed")
            tb = traceback.extract_tb(e.__traceback__)
            where = " ← ".join(f"{fr.name} ({fr.filename.rsplit('/', 1)[-1]}:{fr.lineno})" for fr in tb[-5:][::-1])
            return render(request, "reports_hub.html", nav="reports", blocks=[], error_box=f"P&L по місяцях за {_y}: {type(e).__name__}: {str(e)[:300]} · {where}")
    if key == "payroll_share":
        from .services import payroll_share as _ps
        _ys = _ps.years(db)
        _yq = request.query_params.get("year")
        try:
            _y = int(_yq) if _yq is not None else date.today().year
        except ValueError:
            _y = date.today().year
        _y = _y if _y in _ys else (0 if _yq == "0" else (_ys[-1] if _ys else date.today().year))
        _mq = request.query_params.get("month") or ""
        _m = int(_mq) if _mq.isdigit() and 1 <= int(_mq) <= 12 and _y else None
        _r = _ps.build(db, _y or None, _m)
        if fmt == "xlsx":
            audit(db, "reports.export", user=user, report="payroll_share", year=_y, month=_m)
            return Response(_ps.to_xlsx(_r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="payroll-share-{_y or "all"}{("-%02d" % _m) if _m else ""}.xlsx"'})
        return render(request, "rep_payroll_share.html", nav="reports", REPORTS=rp.REPORTS, key="payroll_share", r=_r, years=_ys, today=date.today())
    if key == "fees":
        from .services import bank_fees as _bf
        _ys = _bf.years(db)
        _yq = request.query_params.get("year")
        try:
            _y = int(_yq) if _yq is not None else date.today().year
        except ValueError:
            _y = date.today().year
        _y = _y if _y in _ys else (0 if _yq == "0" else (_ys[-1] if _ys else date.today().year))
        _mq = request.query_params.get("month") or ""
        _m = int(_mq) if _mq.isdigit() and 1 <= int(_mq) <= 12 and _y else None
        _r = _bf.build(db, _y or None, _m)
        if fmt == "xlsx":
            audit(db, "reports.export", user=user, report="fees", year=_y, month=_m)
            return Response(_bf.to_xlsx(_r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="bank-fees-{_y or "all"}{("-%02d" % _m) if _m else ""}.xlsx"'})
        return render(request, "rep_fees.html", nav="reports", REPORTS=rp.REPORTS, key="fees", r=_r, years=_ys, today=date.today())
    if key == "shifts":
        from .services import shift_report as _shr
        _df, _dt, period = _report_period(period, df, dt_)
        _u = request.query_params.get("u") or ""
        _r = _shr.build(db, _df, _dt, int(_u) if _u.isdigit() else None)
        if fmt == "xlsx":
            audit(db, "reports.export", user=user, report="shifts", d_from=str(_df), d_to=str(_dt))
            return Response(_shr.to_xlsx(_r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="shifts-{_df}-{_dt}.xlsx"'})
        return render(request, "rep_shifts.html", nav="reports", REPORTS=rp.REPORTS, PERIODS=REPORT_PERIODS, key="shifts", r=_r,
                      period=period, d_from=_df, d_to=_dt, dur=_shr.dur_text)
    if key == "fop":
        return _fop_page(request, db)
    if key == "expiry":
        return _expiry_page(request, db)
    if key == "cash" and not request.query_params.get("period") and not (df or dt_):
        period = "today"
    d_from, d_to, period = _report_period(period, df, dt_)
    qp_ = request.query_params
    rep = rp.build(db, key, d_from, d_to, cat=qp_.get("cat", ""), staff=qp_.get("staff", ""), zero=qp_.get("zero", ""), src=qp_.get("src", ""))
    if fmt == "xlsx":
        audit(db, "reports.export", user=user, report=key, d_from=str(d_from), d_to=str(d_to))
        return Response(rp.to_xlsx(rep, d_from, d_to),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="report-{key}-{d_from}-{d_to}.xlsx"'})
    p_from, p_to = rp.prev_period(d_from, d_to)
    if key == "cosmetics":
        from .services import cosmetics_pct as _cp
        return render(request, "rep_cosmetics.html", nav="reports", rep=rep, PERIODS=REPORT_PERIODS, key=key, period=period,
                      d_from=d_from, d_to=d_to, BASES=_cp.BASES)
    return render(request, "reports.html", rep=rep, REPORTS=rp.REPORTS, PERIODS=REPORT_PERIODS, key=key, period=period,
                  d_from=d_from, d_to=d_to, p_from=p_from, p_to=p_to,
                  c_max=max([v for _, v in rep.chart] + [1]))


@app.post("/reports/pnlm/import")
async def pnlm_import(request: Request, file: UploadFile = File(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Імпорт P&L з таблиці власника: CSV-експорт аркуша або текстовий експорт книги."""
    from .services import pnl_sheet as _psh
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        return RedirectResponse(_with_err("/reports?r=pnlm&src=sheet", "Файл завеликий (до 5 МБ)"), 303)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    try:
        res = _psh.import_text(db, text, source=file.filename or "upload")
    except ValueError as e:
        return RedirectResponse(_with_err("/reports?r=pnlm&src=sheet", str(e)), 303)
    audit(db, "reports.pnlm_import", user=user, years=list(res))
    y = max(res)
    return RedirectResponse(_with_msg(f"/reports?r=pnlm&src=sheet&year={y}", "Імпортовано: " + ", ".join(f"{k} — {v} рядків" for k, v in sorted(res.items()))), 303)


@app.post("/reports/pnlm/plan")
async def pnlm_plan_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pnl_monthly as _pm2
    f = await request.form()
    try:
        year = int(f.get("year") or date.today().year)
    except ValueError:
        year = date.today().year
    vals = {}
    for m in range(1, 13):
        row = {}
        for fld in _pm2.PLAN_FIELDS:
            v = str(f.get(f"{fld}_{m}") or "").replace(" ", "").replace(",", ".")
            if v:
                try:
                    row[fld] = float(v)
                except ValueError:
                    pass
        vals[f"{year}-{m:02d}"] = row
    _pm2.plan_save(db, year, vals)
    audit(db, "reports.pnlm_plan", user=user, year=year)
    return RedirectResponse(f"/reports?r=pnlm&year={year}", 303)


# ---------------- Платіжний календар ----------------
@app.get("/finance/calendar", response_class=HTMLResponse)
def paycal_page(request: Request, y: int = 0, m: int = 0, group: str = "", status: str = "", user: User = Depends(require("finance")),
                db: Session = Depends(db_dep)):
    from .services import paycal as _pc
    _pc.seed_plans(db)
    from .services import leasing as _ls
    _ls.ensure_setup(db)
    _pc.setup_v2(db)
    _ls.setup_feeds(db)
    _ls.round_feeds(db)
    t = date.today()
    y, m = (y or t.year), (m or t.month)
    if not (1 <= m <= 12) or not (2020 <= y <= 2100):
        y, m = t.year, t.month
    v = _pc.month_view(db, y, m, group=group if group in _pc.GROUP_T else "", status=status if status in ("planned", "approved", "paid", "skipped", "overdue") else "")
    return render(request, "fin_paycal.html", nav="finance", v=v, FIN_TABS=FIN_TABS, dev=_pc.deviation, articles=_fa().names(db, "expense"))


def _pc_back(request: Request) -> str:
    ref = request.headers.get("referer") or ""
    return ref if "/finance/calendar" in ref else "/finance/calendar"


@app.post("/finance/calendar/item")
async def paycal_item_add(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayItem
    from .services import paycal as _pc
    f = await request.form()
    name = str(f.get("name") or "").strip()[:200]
    due = _day(str(f.get("due") or ""))
    if not name or not due:
        return RedirectResponse(_with_msg(_pc_back(request), "Вкажіть назву й дату", err=True), 303)
    g = str(f.get("group") or "opex")
    it = PayItem(due=due, name=name, group=g if g in _pc.GROUP_T else "opex", article=str(f.get("article") or "").strip()[:80] or None,
                 cashbox=str(f.get("cashbox") or "").strip()[:100] or None, counterparty=str(f.get("counterparty") or "").strip()[:200] or None,
                 amount=_num(str(f.get("amount") or "")), status="planned" if f.get("needs_approval") else "approved", source="manual",
                 note=str(f.get("note") or "").strip()[:300] or None, created_by=user.id)
    db.add(it)
    db.commit()
    audit(db, "paycal.item_add", "pay_item", it.id, user=user, name=it.name, amount=it.amount, due=str(it.due))
    return RedirectResponse(_with_msg(f"/finance/calendar?y={due.year}&m={due.month}", f"Додано: {it.name}"), 303)


@app.post("/finance/calendar/item/{iid:int}/{act}")
async def paycal_item_act(iid: int, act: str, request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayItem
    from .services import paycal as _pc
    it = db.get(PayItem, iid)
    back = _pc_back(request)
    if not it:
        return RedirectResponse(_with_msg(back, "Платіж не знайдено", err=True), 303)
    f = await request.form()
    if act == "approve":
        if user.role != "owner" and it.status == "planned":
            return RedirectResponse(_with_msg(back, "Погоджує власник", err=True), 303)
        it.status, it.approved_by, it.approved_at = "approved", user.id, datetime.utcnow()
        msg = f"Погоджено: {it.name}"
    elif act == "pay":
        amt = _num(str(f.get("amount") or ""))
        if not amt or amt <= 0:
            return RedirectResponse(_with_msg(back, "Вкажіть суму оплати", err=True), 303)
        if it.status == "planned" and user.role != "owner":
            return RedirectResponse(_with_msg(back, "Платіж ще не погоджено власником", err=True), 303)
        try:
            op = _pc.pay(db, it, amt, _day(str(f.get("day") or "")) or date.today(), str(f.get("cashbox") or "").strip() or None, user.id,
                         str(f.get("comment") or "").strip())
        except ValueError as e:
            return RedirectResponse(_with_msg(back, str(e), err=True), 303)
        msg = f"Оплачено: {it.name}" + (f" · CRM-{op.id}" if op else "")
    elif act == "skip":
        it.status = "skipped"
        msg = f"Пропущено: {it.name}"
    elif act == "reopen":
        if it.finop_id:
            return RedirectResponse(_with_msg(back, "Платіж закритий фіноперацією — спершу видаліть її в журналі", err=True), 303)
        it.status, it.paid_at, it.amount_fact = ("planned" if (it.plan and it.plan.needs_approval) else "approved"), None, None
        msg = f"Повернуто в план: {it.name}"
    elif act == "move":
        d = _day(str(f.get("due") or ""))
        if not d or it.status not in ("planned", "approved"):
            return RedirectResponse(_with_msg(back, "Перенести можна лише неоплачений платіж на коректну дату", err=True), 303)
        old_due = it.due
        _pc.move(db, it, d)
        audit(db, "paycal.move", "pay_item", it.id, user=user, name=it.name, frm=str(old_due), to=str(d))
        if (d.year, d.month) != (old_due.year, old_due.month):
            return RedirectResponse(_with_msg(f"/finance/calendar?y={d.year}&m={d.month}", f"Перенесено: {it.name} → {d:%d.%m.%Y}"), 303)
        return RedirectResponse(_with_msg(back, f"Перенесено: {it.name} → {d:%d.%m}"), 303)
    elif act == "edit":
        it.amount = _num(str(f.get("amount") or ""))
        d = _day(str(f.get("due") or ""))
        if d:
            it.due = d
        it.note = str(f.get("note") or "").strip()[:300] or None
        msg = f"Змінено: {it.name}"
    elif act == "delete":
        if it.status == "paid" and it.finop_id:
            return RedirectResponse(_with_msg(back, "Оплачений платіж не видаляється", err=True), 303)
        db.delete(it)
        db.commit()
        audit(db, "paycal.item_delete", "pay_item", iid, user=user)
        return RedirectResponse(_with_msg(back, "Видалено"), 303)
    else:
        return RedirectResponse(back, 303)
    db.commit()
    audit(db, f"paycal.{act}", "pay_item", it.id, user=user, name=it.name, amount=it.amount_fact or it.amount)
    return RedirectResponse(_with_msg(back, msg), 303)


@app.post("/finance/calendar/income-plan")
async def paycal_income_plan(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import paycal as _pc
    f = await request.form()
    wk = _day(str(f.get("week") or ""))
    if not wk:
        return RedirectResponse(_pc_back(request), 303)
    wk = wk - timedelta(days=wk.weekday())
    raw = str(f.get("amount") or "").strip()
    amt = _num(raw) if raw else None
    _pc.set_income_plan(db, wk, amt)
    audit(db, "paycal.income_plan", user=user, week=str(wk), amount=amt)
    return RedirectResponse(_with_msg(_pc_back(request), f"План приходу на тиждень з {wk:%d.%m}: " + (f"{amt:,.0f} ₴".replace(",", " ") if amt is not None else "за записами")), 303)


@app.post("/finance/calendar/plan")
async def paycal_plan_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayPlan
    from .services import paycal as _pc
    f = await request.form()
    pid = str(f.get("id") or "")
    p = db.get(PayPlan, int(pid)) if pid.isdigit() else PayPlan(start=date.today().replace(day=1))
    name = str(f.get("name") or "").strip()[:200]
    if not name:
        return RedirectResponse(_with_msg("/finance/calendar", "Вкажіть назву", err=True), 303)
    g = str(f.get("group") or "opex")
    p.name, p.group = name, (g if g in _pc.GROUP_T else "opex")
    p.article = str(f.get("article") or "").strip()[:80] or None
    p.cashbox = str(f.get("cashbox") or "").strip()[:100] or None
    p.amount = _num(str(f.get("amount") or ""))
    sch = str(f.get("schedule") or "monthly")
    p.schedule = sch if sch in _pc.SCHEDULES else "monthly"
    d1, d2, wd = str(f.get("day") or ""), str(f.get("day2") or ""), str(f.get("weekday") or "")
    p.day = int(d1) if d1.isdigit() and 1 <= int(d1) <= 31 else None
    p.day2 = int(d2) if d2.isdigit() and 1 <= int(d2) <= 31 else None
    p.weekday = int(wd) if wd.isdigit() and 0 <= int(wd) <= 6 else None
    p.needs_approval = bool(f.get("needs_approval"))
    if p.schedule == "once" and not p.start:
        p.start = date.today()
    if not pid.isdigit():
        db.add(p)
    db.flush()
    # майбутні незакриті платежі з цього шаблону — перегенерувати під нові параметри
    from .models import PayItem
    for it in db.scalars(select(PayItem).where(PayItem.plan_id == p.id, PayItem.status.in_(("planned", "approved")), PayItem.due >= date.today())):
        db.delete(it)
    db.commit()
    t = date.today()
    _pc.ensure_month(db, t.year, t.month)
    audit(db, "paycal.plan_save", "pay_plan", p.id, user=user, name=p.name, amount=p.amount, schedule=p.schedule)
    return RedirectResponse(_with_msg("/finance/calendar", f"Шаблон збережено: {p.name}"), 303)


@app.post("/finance/calendar/plan/{pid:int}/{act}")
def paycal_plan_act(pid: int, act: str, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayPlan, PayItem
    p = db.get(PayPlan, pid)
    if not p:
        return RedirectResponse("/finance/calendar", 303)
    if act == "toggle":
        p.active = not p.active
        if not p.active:
            for it in db.scalars(select(PayItem).where(PayItem.plan_id == p.id, PayItem.status.in_(("planned", "approved")), PayItem.due >= date.today())):
                db.delete(it)
        msg = f"{'Увімкнено' if p.active else 'Вимкнено'}: {p.name}"
    elif act == "delete":
        for it in db.scalars(select(PayItem).where(PayItem.plan_id == p.id)):
            if it.status in ("planned", "approved", "skipped"):
                db.delete(it)
            else:
                it.plan_id = None
        db.delete(p)
        msg = "Шаблон видалено"
    else:
        return RedirectResponse("/finance/calendar", 303)
    db.commit()
    audit(db, f"paycal.plan_{act}", "pay_plan", pid, user=user)
    return RedirectResponse(_with_msg("/finance/calendar", msg), 303)


# ---------------- Звіт Deye (лише власник) ----------------
def _owner(user: User = Depends(current_user)) -> User:
    if user.role != "owner":
        raise HTTPException(403, "Цей звіт — лише для власника")
    return user


@app.get("/finance/leasing", response_class=HTMLResponse)
def leasing_page(request: Request, edit: int = 0, user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import leasing as _ls, paycal as _pc
    _pc.seed_plans(db)
    _ls.ensure_setup(db)
    _pc.setup_v2(db)
    _ls.setup_feeds(db)
    _ls.round_feeds(db)
    return render(request, "fin_leasing.html", nav="finance", v=_ls.build(db), edit=edit, FIN_TABS=FIN_TABS)


@app.post("/finance/leasing/save")
async def leasing_save(request: Request, user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import leasing as _ls
    f = dict(await request.form())
    try:
        cid = int(f.get("id") or 0)
    except ValueError:
        cid = 0
    c = _ls.save(db, cid or None, f)
    return RedirectResponse(_with_msg("/finance/leasing", f"Збережено «{c['name']}» — графік оновлено в платіжному календарі"), 303)


@app.get("/deye", response_class=HTMLResponse)
def deye_page(request: Request, p: str = "month", d: str = "", m: str = "", tab: str = "overview", jm: str = "", ed: str = "", fmt: str = "",
              user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import energy as en
    from .models import FuelPrice
    c = en.cfg(db)
    today_fp = en.capture_fuel(db)                    # ціна дня фіксується при першому відкритті, якщо job ще не встиг
    allr = en.rows(db, c)
    p = p if p in ("day", "month", "all") else "month"
    mon = m or date.today().strftime("%Y-%m")
    day = _day(d) or (allr[0]["day"] if allr else date.today())
    sel = [r for r in allr if (p == "all") or (p == "month" and r["month"] == mon) or (p == "day" and r["day"] == day)]
    prices = db.scalars(select(FuelPrice).order_by(FuelPrice.day.desc())).all()
    mp = [x.price for x in prices if f"{x.day:%Y-%m}" == date.today().strftime("%Y-%m")]
    tab = tab if tab in ("overview", "log", "input") else "overview"
    months_ = en.by_month(allr)
    jm = jm or (months_[0]["month"] if months_ else date.today().strftime("%Y-%m"))
    if fmt == "xlsx":
        from openpyxl import Workbook
        import io as _io
        wb = Workbook(); ws = wb.active; ws.title = "Deye"
        ws.append(["Дата", "Джерело", "З батарей, кВт·год", "На заряд, кВт·год", "Без світла за добу, год", "Без світла 09–21, год",
                   "WOG, ₴/л", "Тариф, ₴/кВт·год", "Генератор, ₴", "Зарядка, ₴", "Економія, ₴"])
        for r in sorted([x for x in allr if not jm or x["month"] == jm or jm == "all"], key=lambda x: x["day"]):
            ws.append([r["day"], r["source"], r["discharge"], r["charge_kwh"], r["battery_hours"], r["business_hours"], r["fuel"], r["tariff"],
                       r["gen_cost"], r["charge_cost"], r["savings"]])
        bio = _io.BytesIO(); wb.save(bio)
        return Response(bio.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="deye-{jm}.xlsx"'})
    edd = _day(ed) if ed else date.today()
    ede = db.get(__import__("app.models", fromlist=["EnergyDay"]).EnergyDay, edd)
    return render(request, "deye.html", nav="reports", c=c, p=p, tab=tab, jm=jm, edd=edd, ede=ede,
                  fuel_on=en.fuel_for(db, edd), tariff_on=en.tariff(c, edd), mon=mon, day=day, sel=sel, sm=en.summary(sel), months=en.by_month(allr),
                  pb=en.payback(db, c, allr), fuel_today=today_fp or (prices[0] if prices else None),
                  fuel_avg=round(sum(mp) / len(mp), 2) if mp else None, fuel_n=len(mp), ready=en.deye_ready(c),
                  tariff_now=en.tariff(c, date.today()), chart=list(reversed(en.by_month(allr)))[-12:] if p == "all" else
                  sorted(sel, key=lambda r: r["day"]), today=date.today(), has=bool(allr),
                  logo=next((f"/static/img/{n}" for n in ("deye.png", "deye.svg", "deye.webp")
                             if os.path.exists(os.path.join(os.path.dirname(__file__), "static", "img", n))), None))


@app.post("/deye/day")
def deye_day(day: str = Form(...), discharge_kwh: str = Form(""), charge_kwh: str = Form(""), battery_hours: str = Form(""),
             business_hours: str = Form(""), fuel_price: str = Form(""), tariff: str = Form(""), tariff_month: str = Form(""),
             user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import energy as en
    dd = _day(day)
    if not dd:
        return RedirectResponse(_with_msg("/deye?tab=input", "Вкажіть дату", True), 303)
    f = lambda v: _num(v.replace(",", ".").replace("₴", "").replace("/л", "").strip()) if v.strip() else None  # noqa: E731
    fp = f(fuel_price)
    if fp is not None and abs((en.fuel_for(db, dd) or 0) - fp) < 0.005:
        fp = None                                         # ціну не змінювали — лишаємо зафіксовану WOG
    en.save_day(db, dd, {"discharge_kwh": f(discharge_kwh), "charge_kwh": f(charge_kwh), "battery_hours": f(battery_hours),
                         "business_hours": f(business_hours), "fuel_price": fp})
    tv = f(tariff.replace("/кВт·год", ""))
    if tv is not None:
        c = en.cfg(db)
        mk = (tariff_month or f"{dd:%Y-%m}")[:7]
        if c["tariffs"].get(mk) != tv:
            c["tariffs"][mk] = tv
            en.save_cfg(db, c)
    audit(db, "deye.day", user=user, day=str(dd))
    return RedirectResponse(_with_msg(f"/deye?tab=log&jm={dd:%Y-%m}", f"День {dd:%d.%m.%Y} збережено"), 303)


@app.post("/deye/tariff")
def deye_tariff(month: str = Form(...), value: str = Form(...), user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import energy as en
    c = en.cfg(db)
    v = _num(value.replace(",", "."))
    if v is not None and len(month) >= 7:
        c["tariffs"][month[:7]] = v
        en.save_cfg(db, c)
        audit(db, "deye.tariff", user=user, month=month[:7], value=v)
    return RedirectResponse(_with_msg("/deye", f"Тариф {month[:7]}: {value} ₴/кВт·год"), 303)  # noqa


@app.post("/deye/settings")
async def deye_settings(request: Request, user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import energy as en
    f = await request.form()
    c = en.cfg(db)
    g = lambda k: str(f.get(k) or "").strip()  # noqa: E731
    if g("station_cost"):
        c["station_cost"] = _num(g("station_cost").replace(",", ".").replace(" ", "")) or c["station_cost"]
    if g("litres_per_hour"):
        c["litres_per_hour"] = _num(g("litres_per_hour").replace(",", ".")) or c["litres_per_hour"]
    for k in ("work_from", "work_to"):
        if g(k).isdigit():
            c[k] = int(g(k))
    for k in ("base", "app_id", "station_id"):
        if k in f:
            c["deye"][k] = g(k)
    for k in en.SECRETS:
        if g(k):
            en.set_secret(c, k, g(k))
    en.save_cfg(db, c)
    audit(db, "deye.settings", user=user, secrets=[k for k in en.SECRETS if g(k)])
    msg = "Налаштування збережено"
    if f.get("test") and en.deye_ready(c):
        try:
            r = await asyncio.to_thread(en.sync_deye, db, 7)
            msg = ("✅ " if r.get("ok") else "❌ ") + r.get("msg", "")
        except Exception as e:  # noqa: BLE001
            msg = f"❌ {str(e)[:200]}"
    return RedirectResponse(_with_msg("/deye?tab=input#set", msg, msg.startswith("❌")), 303)


@app.post("/deye/sync")
async def deye_sync(days: int = Form(7), since: str = Form(""), overwrite: str = Form(""), user: User = Depends(_owner),
                    db: Session = Depends(db_dep)):
    from .services import energy as en
    sd = _day(since) if since else None
    if sd and (date.today() - sd).days > 400:
        sd = date.today() - timedelta(days=400)
    try:
        r = await asyncio.to_thread(en.sync_deye, db, max(1, min(days, 60)), None, sd, overwrite == "1")
        audit(db, "deye.sync", user=user, since=str(sd) if sd else None, overwrite=overwrite == "1")
        msg = ("✅ " if r.get("ok") else "❌ ") + r.get("msg", "")
    except Exception as e:  # noqa: BLE001
        msg = f"❌ {str(e)[:200]}"
    return RedirectResponse(_with_msg("/deye", msg, msg.startswith("❌")), 303)


@app.post("/deye/report-now")
def deye_report_now(user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import energy as en
    n = en.send_daily(db)
    return RedirectResponse(_with_msg("/deye", "Звіт за вчора надіслано в Telegram" if n else
                                      "Не надіслано: немає даних за вчора або вашого Telegram ID у «Мій кабінет»", not n), 303)


@app.post("/deye/import")
async def deye_import(request: Request, user: User = Depends(_owner), db: Session = Depends(db_dep)):
    from .services import energy as en
    import json as _j
    f = await request.form()
    raw = str(f.get("json") or "")
    up = f.get("file")
    if up is not None and hasattr(up, "read"):
        raw = (await up.read()).decode("utf-8", "ignore") or raw
    try:
        n = en.import_json(db, _j.loads(raw))
    except ValueError:
        return RedirectResponse(_with_msg("/deye#imp", "Не вдалося прочитати JSON", True), 303)
    audit(db, "deye.import", user=user, days=n)
    return RedirectResponse(_with_msg("/deye?p=all", f"Перенесено днів: {n}"), 303)


# ---------------- Фінанси → Планування ціни послуг ----------------
@app.get("/finance/service-pricing", response_class=HTMLResponse)
def service_pricing_page(request: Request, cat: str = "", q: str = "", only: str = "", fmt: str = "",
                         user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import service_pricing as sp
    from .models import CbService
    c = sp.cfg(db)
    rws = sp.rows(db, c, cat, q, only)
    if fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "Ціни послуг"
        ws.append(["Послуга", "Категорія", "Хв", "Ціна", "Матеріали", "Амортизація", "Валовий", "ЗП лікаря", "ЗП/хв", "Собівартість",
                   "Накладні", "Чистий прибуток", "Заробіток/хв", "Чиста маржа %", "ROI %", "Рекомендована ціна"])
        for r in rws:
            ws.append([r["sv"].name, r["sv"].category, r["mins"], r["price"], r["mat"], r["am"], r["gross"], r["zp"], r["zp_min"], r["cost"],
                       r["ovh"], r["net"], r["earn_min"], r["net_m"], r["roi"], r["rec"]])
        import io as _io
        bio = _io.BytesIO(); wb.save(bio)
        audit(db, "service_pricing.export", user=user, n=len(rws))
        return Response(bio.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="service-prices-{date.today()}.xlsx"'})
    cats = sorted({x for (x,) in db.execute(select(CbService.category).where(CbService.category.is_not(None)).distinct())})
    tot = {k: round(sum((r[k] or 0) for r in rws), 2) for k in ("price", "mat", "am", "zp", "ovh", "net")}
    tot["net_m"] = round(100 * tot["net"] / tot["price"], 1) if tot["price"] else None
    return render(request, "service_pricing.html", nav="finance", rows=rws, c=c, cats=cats, cat=cat, q=q, only=only, MARGINS=sp.MARGINS,
                  doctors=sp.Zp(db).doctors(), versions=sp.versions(db)[:15], tot=tot,
                  low=sum(1 for r in rws if r["net_m"] is not None and r["net_m"] < float(c["target"])))


@app.post("/finance/service-pricing/cfg")
def service_pricing_cfg(ovh_min: str = Form(""), target: str = Form(""), rnd: str = Form(""), doctor: str = Form(""), back: str = Form(""),
                        user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import service_pricing as sp
    c = sp.cfg(db)
    c["ovh_min"] = _num(ovh_min.replace(",", ".")) or 0.0 if ovh_min.strip() else c["ovh_min"]
    c["target"] = _num(target.replace(",", ".")) or c["target"] if target.strip() else c["target"]
    c["round"] = int(rnd) if rnd.isdigit() else c["round"]
    c["doctor"] = doctor if doctor.isdigit() else ""
    sp.save_cfg(db, c)
    audit(db, "service_pricing.cfg", user=user, **{k: c[k] for k in ("ovh_min", "target", "round", "doctor")})
    return RedirectResponse(_with_msg("/finance/service-pricing" + (("?" + back) if back else ""), "Налаштування збережено"), 303)


@app.post("/finance/service-pricing/save")
async def service_pricing_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import service_pricing as sp
    js = await request.json()
    prices = {int(k): float(v) for k, v in (js.get("prices") or {}).items() if str(k).isdigit() and v not in (None, "")}
    n = sp.apply_prices(db, prices, user.id, user.name, str(js.get("note") or "")[:120] or None)
    audit(db, "service_pricing.save", user=user, n=n)
    return JSONResponse({"ok": True, "n": n})


@app.post("/finance/service-pricing/version")
def service_pricing_version(note: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import service_pricing as sp
    v = sp.snapshot(db, user.name, note.strip() or None)
    audit(db, "service_pricing.version", user=user, version=v["id"])
    return RedirectResponse(_with_msg("/finance/service-pricing", f"Версію №{v['id']} збережено ({v['n']} послуг)"), 303)


@app.post("/finance/service-pricing/restore/{vid:int}")
def service_pricing_restore(vid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import service_pricing as sp
    n = sp.restore(db, vid, user.id, user.name)
    audit(db, "service_pricing.restore", user=user, version=vid, n=n)
    return RedirectResponse(_with_msg("/finance/service-pricing", f"Повернуто цін: {n}"), 303)


# ---------------- Фінанси → Планування ціни реалізації ----------------
def _pricing_rows(db: Session, wh: str, brand: str, q: str, only: str):
    from .services import pricing as pr_svc
    rt = pr_svc.rates(db)
    rows = [r for r in catalog.product_rows(db, []) if r.product.active is not False]
    if wh in catalog.WAREHOUSES:
        rows = [r for r in rows if (r.product.warehouse or "vitrina") == wh]
    if brand:
        rows = [r for r in rows if (r.product.brand or "") == ("" if brand == "—" else brand)]
    if q:
        rows = [r for r in rows if catalog.product_match(r.product, q) is not None]
    out = []
    from .models import Supplier as _Sup
    sfx = {s_.name.strip().lower(): s_ for s_ in db.scalars(select(_Sup).where((_Sup.fx_usd.is_not(None)) | (_Sup.fx_eur.is_not(None))))}
    for r in rows:
        p_ = r.product
        cur = p_.cost_currency or "UAH"
        s_ = sfx.get((p_.supplier or "").strip().lower())
        own = {"USD": s_.fx_usd, "EUR": s_.fx_eur}.get(cur) if s_ else None
        pr = pr_svc.PriceRow(p_, r.cost, own or rt.get(cur, 1.0))   # курс постачальника, інакше загальний
        pr.sup_fx = {"USD": s_.fx_usd, "EUR": s_.fx_eur, "name": s_.name} if s_ else None
        out.append(pr)
    if only == "nofx":
        out = [x for x in out if not x.product.cost_fx]
    elif only == "noplan":
        out = [x for x in out if not x.product.planned_price]
    elif only == "diff":
        out = [x for x in out if x.product.planned_price and x.price_now and abs(x.product.planned_price - x.price_now) >= 1]
    elif only == "lowmargin":
        out = [x for x in out if (pr_svc.margin(x.price_now, x.cost_uah) or 100) < 40]
    out.sort(key=lambda x: ((x.product.brand or "яяя").lower(), x.product.name))
    return out, rt


# ---------------- Фінанси: розділи як у CleverBox ----------------
def _period(df: str, dt_: str, default_days: int | None = None) -> tuple[date, date]:
    today = date.today()
    d_to = _day(dt_) or today
    d_from = _day(df) or (d_to - timedelta(days=default_days) if default_days is not None else today.replace(day=1))
    return (d_from, d_to) if d_from <= d_to else (d_to, d_from)


def _fa():
    from .services import fin_articles
    return fin_articles


def _cashboxes(db: Session) -> list[str]:
    from .models import CbFinOp
    from .services import cashboxes as _cbx
    base = _cbx.names(db)                                             # лише активні з «Налаштування → Каси / рахунки»
    if base:
        return base
    cb = [a for (a,) in db.execute(select(CbFinOp.account).where(CbFinOp.account.is_not(None)).distinct()) if a]
    return sorted(set(cb)) + ["Інше"]                                 # кас ще не налаштовано — рахунки з журналу CleverBox


def _month(m: str) -> str:
    import re as _re
    return m if _re.fullmatch(r"\d{4}-\d{2}", m or "") else fo_month_key(date.today())


def fo_month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def _journal(db: Session, request: Request, d_from: date, d_to: date):
    from .services import finops as fo
    qp = request.query_params
    users = {u_.id: u_.name for u_ in db.scalars(select(User))}
    rows = fo.journal(db, d_from, d_to, users, archive=bool(qp.get("archive")))
    opts = {"accounts": sorted({r.account for r in rows if r.account}), "articles": sorted({r.article for r in rows if r.article}),
            "authors": sorted({r.author for r in rows if r.author})}
    f = {k: qp.get(k, "") for k in ("src", "kind", "account", "article", "author", "q")}
    if f["src"]:
        rows = [r for r in rows if r.src == f["src"]]
    if f["kind"]:
        rows = [r for r in rows if (r.is_storno if f["kind"] == "storno" else (r.kind == f["kind"] and not r.is_storno))]
    for k in ("account", "article", "author"):
        if f[k]:
            rows = [r for r in rows if getattr(r, k) == f[k]]
    if f["q"].strip():
        ql = f["q"].strip().lower().lstrip("#")
        rows = [r for r in rows if ql in f"{r.num} {r.who} {r.basis} {r.comment} {r.article}".lower()]
    return rows, opts, f


@app.get("/finance/ops", response_class=HTMLResponse)
def finance_ops(request: Request, df: str = "", dt_: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    from .models import CbFinOp
    d_from, d_to = _period(df, dt_)
    rows, opts, f = _journal(db, request, d_from, d_to)
    last_cb = db.scalar(select(func.max(CbFinOp.updated_at)))
    from .services import clinic as _cl
    return render(request, "fin_ops.html", nav="finance", rows=rows[:2000], n_all=len(rows),
                  tot=fo.journal_totals(rows, _cl.excluded_income(db)),
                  d_from=d_from, d_to=d_to, f=f, opts=opts, archive=bool(request.query_params.get("archive")),
                  EXP=_fa().names(db, "expense"), INC=_fa().names(db, "income"), SAL=_fa().salary_names(db),
                  KL=fo.KIND_LABELS, boxes=_cashboxes(db), last_cb=last_cb,
                  cb_imported=LAST_IMPORT.pop(f"cbfin-{user.id}", None),
                  job=jobs.JOBS.get(request.query_params.get("job") or ""),
                  doctors=db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all(),
                  today=date.today(), this_month=fo_month_key(date.today()))


@app.get("/finance/ops/export.xlsx")
def finance_ops_export(request: Request, df: str = "", dt_: str = "", user: User = Depends(require("finance")),
                       db: Session = Depends(db_dep)):
    from openpyxl import Workbook
    from .services import finops as fo
    import io
    d_from, d_to = _period(df, dt_)
    rows, _, _ = _journal(db, request, d_from, d_to)
    wb = Workbook()
    ws = wb.active
    ws.title = "Фіноперації"
    ws.append(["Дата", "Тип", "№", "Джерело", "Від кого / кому", "Підстава", "Стаття", "Рахунок", "Вид / на рахунок", "Автор", "Сума", "Коментар"])
    for r in rows:
        ws.append([r.at, "Сторно" if r.is_storno else fo.KIND_LABELS.get(r.kind, r.kind), r.num, "CRM" if r.src == "crm" else "CleverBox",
                   r.who, r.basis, r.article, r.account, r.vid, r.author, round(r.sign * r.amount if r.sign else r.amount, 2), r.comment])
        ws.cell(ws.max_row, 1).number_format = "DD.MM.YYYY HH:MM"
        ws.cell(ws.max_row, 11).number_format = "#,##0.00"
    for col, w in zip("ABCDEFGHIJKL", (17, 12, 10, 11, 28, 22, 26, 22, 22, 20, 13, 30)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="finops_{d_from}_{d_to}.xlsx"'})


@app.post("/finance/ops/import")
async def finance_ops_import(file: UploadFile = File(...), back: str = Form("/finance/ops"), user: User = Depends(require("finance")),
                             db: Session = Depends(db_dep)):
    """Великий файл (десятки тисяч операцій) — у фоні, сторінка показує хід."""
    import tempfile
    from .services import cb_finops_import as cfi
    suffix = ".xls" if (file.filename or "").lower().endswith(".xls") else ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
    if not await asyncio.to_thread(cfi.is_journal, tmp.name):
        LAST_IMPORT[f"cbfin-{user.id}"] = {"error": "Це не журнал фіноперацій CleverBox: потрібні колонки «Стаття» і «Сума»."}
        return RedirectResponse(back if back.startswith("/finance") else "/finance/ops", 303)
    uid, fname = user.id, file.filename

    def _run(job, path):
        with SessionLocal() as s_:
            rep = cfi.import_file(s_, path, job)
            u_ = s_.get(User, uid)
            audit(s_, "fin.cb_import", user=u_, file=fname, **rep)
            return rep
    job = jobs.start("Імпорт журналу фіноперацій CleverBox", _run, tmp.name)
    from urllib.parse import urlsplit, parse_qsl, urlencode
    u = urlsplit(back if back.startswith("/finance") else "/finance/ops")
    qs = [(k, v) for k, v in parse_qsl(u.query) if k not in ("msg", "err", "job")] + [("job", job.id)]
    return RedirectResponse(f"{u.path}?{urlencode(qs)}", 303)


@app.post("/finance/ops")
def finance_op_add(day: str = Form(""), kind: str = Form("expense"), category: str = Form(""), amount: str = Form(...),
                   cashbox: str = Form(""), cashbox_to: str = Form(""), counterparty: str = Form(""), doctor_id: str = Form(""),
                   salary_month: str = Form(""), comment: str = Form(""),
                   back: str = Form("/finance/ops"), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import FinOp
    amt = _num(amount)
    back = back if back.startswith("/finance") else "/finance/ops"
    if not amt or amt <= 0:
        return RedirectResponse(_with_msg(back, "Вкажіть суму більше 0", err=True), 303)
    kind = kind if kind in ("income", "expense", "transfer") else "expense"
    if kind == "transfer":
        if not cashbox or not cashbox_to or cashbox == cashbox_to:
            return RedirectResponse(_with_msg(back, "Переміщення: оберіть два різні рахунки", err=True), 303)
        category = category or "Переміщення між рахунками"
    if not category.strip():
        return RedirectResponse(_with_msg(back, "Оберіть статтю", err=True), 303)
    x = FinOp(day=_day(day) or date.today(), kind=kind, category=category.strip()[:60],
              amount=round(amt, 2), cashbox=cashbox.strip()[:100] or None, cashbox_to=(cashbox_to.strip()[:100] or None) if kind == "transfer" else None,
              counterparty=counterparty.strip()[:200] or None, comment=comment.strip()[:500] or None,
              doctor_id=int(doctor_id) if doctor_id.isdigit() else None,
              salary_month=_month(salary_month) if salary_month else None, created_by=user.id)
    if x.category in _fa().salary_names(db):
        if x.doctor_id and not x.salary_month:
            x.salary_month = fo_month_key(x.day)
    else:
        x.salary_month = None             # «за місяць» — лише для статей зарплати (форма завжди надсилає поле)
    db.add(x)
    db.commit()
    audit(db, "fin.op_add", "fin_op", x.id, user=user, kind=x.kind, category=x.category, amount=x.amount, cashbox=x.cashbox)
    return RedirectResponse(_with_msg(back, f"Записано CRM-{x.id}: {x.category} {x.amount:,.2f} ₴".replace(",", " ")), 303)


@app.post("/finance/ops/storno")
def finance_op_storno(num: str = Form(...), comment: str = Form(""), back: str = Form("/finance/ops"),
                      user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Сторно операції CRM: зворотна операція на ту саму суму й рахунок; оригінал позначається «сторновано»."""
    import re as _re
    from .models import FinOp
    back = back if back.startswith("/finance") else "/finance/ops"
    m = _re.search(r"(\d+)", num or "")
    x = db.get(FinOp, int(m[1])) if m else None
    if not x or x.deleted_at:
        return RedirectResponse(_with_msg(back, "Операцію CRM з таким № не знайдено (операції CleverBox сторнуються в CleverBox)", err=True), 303)
    if x.storno_of or db.scalar(select(FinOp.id).where(FinOp.storno_of == x.id, FinOp.deleted_at.is_(None))):
        return RedirectResponse(_with_msg(back, f"CRM-{x.id} уже сторновано або сама є сторно", err=True), 303)
    if len(comment.strip()) < 3:
        return RedirectResponse(_with_msg(back, "Вкажіть причину сторно", err=True), 303)
    kind = {"income": "expense", "expense": "income", "transfer": "transfer"}[x.kind]
    y = FinOp(day=date.today(), kind=kind, category=x.category, amount=x.amount,
              cashbox=x.cashbox_to if x.kind == "transfer" else x.cashbox, cashbox_to=x.cashbox if x.kind == "transfer" else None,
              counterparty=x.counterparty, doctor_id=x.doctor_id, salary_month=x.salary_month, storno_of=x.id,
              comment=comment.strip()[:500], created_by=user.id)
    db.add(y)
    db.commit()
    audit(db, "fin.op_storno", "fin_op", x.id, user=user, storno=y.id, amount=x.amount, comment=y.comment)
    return RedirectResponse(_with_msg(back, f"Сторно CRM-{x.id} записано як CRM-{y.id}"), 303)


@app.post("/finance/ops/{oid:int}/delete")
def finance_op_delete(oid: int, del_password: str = Form(""), back: str = Form("/finance/ops"),
                      user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import FinOp
    back = back if back.startswith("/finance") else "/finance/ops"
    err = _delpw(db, user, del_password, "fin_op", "fin_op", oid)
    if err:
        return RedirectResponse(_with_err(back, err), 303)
    x = db.get(FinOp, oid)
    if x and not x.deleted_at:
        x.deleted_at = datetime.utcnow()
        db.commit()
        audit(db, "fin.op_delete", "fin_op", oid, user=user, category=x.category, amount=x.amount)
    return RedirectResponse(_with_msg(back, "Операцію видалено"), 303)


def _with_msg(url: str, msg: str, err: bool = False) -> str:
    base, _, frag = url.partition("#")
    return f"{base}{'&' if '?' in base else '?'}{'err' if err else 'msg'}={_q(msg)}" + (f"#{frag}" if frag else "")


@app.get("/finance/day", response_class=HTMLResponse)
def finance_day(request: Request, d: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    day = _day(d) or date.today()
    cs = fo.all_checks(db, day, day)                         # чеки CleverBox + каса CRM
    prev = fo.sales_summary(fo.all_checks(db, day - timedelta(days=7), day - timedelta(days=7)))
    return render(request, "fin_day.html", nav="finance", day=day, s=fo.sales_summary(cs), rows=fo.sales_rows(db, cs),
                  prev=prev, timedelta=timedelta)


@app.get("/finance/sales", response_class=HTMLResponse)
def finance_sales(request: Request, df: str = "", dt_: str = "", q: str = "", staff: str = "",
                  user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    d_from, d_to = _period(df, dt_)
    cs = fo.all_checks(db, d_from, d_to)
    rows = fo.sales_rows(db, cs, q, staff)
    staff_names = sorted({it.get("initiator_name") for c in cs for it in (c.items or []) if it.get("initiator_name")})
    return render(request, "fin_sales.html", nav="finance", d_from=d_from, d_to=d_to, q=q, staff=staff, staff_names=staff_names,
                  s=fo.sales_summary([r["check"] for r in rows]), rows=rows[:800], n=len(rows))


@app.get("/finance/payroll", response_class=HTMLResponse)
def finance_payroll(request: Request, m: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    month = _month(m)
    rows = fo.payroll(db, month)
    all_docs = db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all()
    from .services import accrual_articles as _aa
    return render(request, "fin_payroll.html", nav="finance", month=month, rows=rows, months=_month_list(), arts=_aa.active(db),
                  total={k: round(sum(r["m"][k] for r in rows), 2) for k in ("services", "products", "accrued", "adj")},
                  no_rule=[d_ for d_ in all_docs if d_.id not in {r["doctor"].id for r in rows if r["rule"] or r.get("plan")}])


def _month_list(n: int = 24) -> list[str]:
    t = date.today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(fo_month_key(t))
        t = (t - timedelta(days=1)).replace(day=1)
    return out


@app.post("/finance/payroll/rule")
def finance_payroll_rule(doctor_id: int = Form(...), base: str = Form(""), pct_services: str = Form(""), pct_products: str = Form(""),
                         note: str = Form(""), m: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import SalaryRule
    from .services import finops as fo
    if not db.get(SyncState, "payroll_start"):
        fo.set_payroll_start(db, _month(m))
    r = db.scalar(select(SalaryRule).where(SalaryRule.doctor_id == doctor_id)) or SalaryRule(doctor_id=doctor_id)
    r.base, r.pct_services, r.pct_products = _num(base), _num(pct_services), _num(pct_products)
    r.note, r.updated_at = note.strip()[:300] or None, datetime.utcnow()
    db.add(r)
    db.commit()
    audit(db, "fin.salary_rule", "doctor", doctor_id, user=user, base=r.base, pct_services=r.pct_services, pct_products=r.pct_products)
    return RedirectResponse(f"/finance/payroll?m={_month(m)}#d{doctor_id}", 303)


@app.post("/finance/payroll/adj")
def finance_payroll_adj(doctor_id: int = Form(...), month: str = Form(...), amount: str = Form(...), sign: str = Form("+"),
                        article: str = Form(""), comment: str = Form(""), back: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import SalaryAdj
    from .services import accrual_articles as aa
    amt = _num(amount)
    mk = _month(month)
    art = aa.get(db, int(article)) if article.isdigit() else None
    if art:                                                     # знак — за видом статті (дохід + / витрата −)
        sign = "-" if art["kind"] == "expense" else "+"
    if amt:
        a = SalaryAdj(doctor_id=doctor_id, month=mk, amount=round(-abs(amt) if sign == "-" else abs(amt), 2),
                      comment=comment.strip()[:300] or None, created_by=user.id, article=art["name"] if art else None)
        db.add(a)
        db.commit()
        audit(db, "fin.salary_adj", "doctor", doctor_id, user=user, month=mk, amount=a.amount, comment=a.comment)
    if back.startswith("/finance/"):
        return RedirectResponse(back, 303)
    return RedirectResponse(f"/finance/payroll?m={mk}#d{doctor_id}", 303)


@app.post("/finance/payroll/adj/{aid:int}/delete")
def finance_payroll_adj_delete(aid: int, del_password: str = Form(""), user: User = Depends(require("finance")),
                               db: Session = Depends(db_dep)):
    from .models import SalaryAdj
    a = db.get(SalaryAdj, aid)
    back = f"/finance/payroll?m={a.month}" if a else "/finance/payroll"
    err = _delpw(db, user, del_password, "salary_adj", "doctor", a.doctor_id if a else None)
    if err:
        return RedirectResponse(_with_err(back, err), 303)
    if a:
        audit(db, "fin.salary_adj_delete", "doctor", a.doctor_id, user=user, month=a.month, amount=a.amount)
        db.delete(a)
        db.commit()
    return RedirectResponse(back, 303)


# ---------------- Нарахування зарплати документами за період ----------------
@app.get("/finance/accruals", response_class=HTMLResponse)
def finance_accruals(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayrollDoc
    from .services import payroll_docs as pd
    docs = db.scalars(select(PayrollDoc).where(PayrollDoc.deleted_at.is_(None)).order_by(PayrollDoc.d_from.desc(), PayrollDoc.id.desc())).all()
    users = {u_.id: u_.name for u_ in db.scalars(select(User))}
    a, b = pd.default_period()
    return render(request, "fin_accruals.html", nav="finance", docs=docs, users=users, df=a, dt=b, state=pd.doc_state)


@app.get("/finance/accruals/new", response_class=HTMLResponse)
def finance_accrual_new(request: Request, df: str = "", dt_: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import payroll_docs as pd
    a, b = pd.default_period()
    d_from, d_to = _day(df) or a, _day(dt_) or b
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    return render(request, "fin_accrual.html", nav="finance", doc=None, d_from=d_from, d_to=d_to, rows=pd.compute(db, d_from, d_to),
                  bal=pd.balances(db), overlap=pd.overlaps(db, d_from, d_to))


@app.post("/finance/accruals")
async def finance_accrual_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import payroll_docs as pd
    f = await request.form()
    d_from, d_to = _day(str(f.get("df") or "")), _day(str(f.get("dt_") or ""))
    if not d_from or not d_to:
        return RedirectResponse(_with_msg("/finance/accruals", "Вкажіть період", True), 303)
    amounts = {int(k[2:]): (_num(str(v).replace(",", ".")) or 0) for k, v in f.items() if k.startswith("a_") and k[2:].isdigit()}
    doc = pd.save(db, d_from, d_to, amounts, str(f.get("comment") or ""), user.id)
    if f.get("post"):
        pd.set_posted(db, doc, True)
    elif str(f.get("post_one") or "").isdigit():                 # «На баланс» навпроти одного співробітника
        pd.set_posted(db, doc, True, int(f.get("post_one")))
    audit(db, "fin.payroll_doc", user=user, doc=doc.id, d_from=str(d_from), d_to=str(d_to), total=doc.total, posted=doc.posted)
    return RedirectResponse(_with_msg(f"/finance/accruals/{doc.id}", f"Нарахування №{doc.id} збережено"), 303)


@app.get("/finance/accruals/{aid:int}", response_class=HTMLResponse)
def finance_accrual_view(request: Request, aid: int, fmt: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayrollDoc
    from .services import payroll_docs as pd
    doc = db.get(PayrollDoc, aid)
    if not doc or doc.deleted_at:
        raise HTTPException(404)
    docs = {d_.id: d_ for d_ in db.scalars(select(Doctor))}
    if fmt == "xlsx":
        from io import BytesIO
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = f"ЗП {doc.d_from:%d.%m}-{doc.d_to:%d.%m}"
        ws.append([f"Нарахування зарплати №{doc.id} · {doc.d_from:%d.%m.%Y} — {doc.d_to:%d.%m.%Y}"])
        ws.append([])
        ws.append(["Співробітник", "Оклад", "Днів", "Ставка", "Комісійні послуги", "Комісійні товари", "% від обороту", "Розраховано", "Нараховано"])
        for c_ in ws[3]:
            c_.font = Font(bold=True)
        for ln in doc.lines:
            dt_ = ln.detail or {}
            ws.append([docs[ln.doctor_id].name if ln.doctor_id in docs else ln.doctor_id, ln.plan or "", ln.days, dt_.get("fixed", 0),
                       dt_.get("comm_services", 0), dt_.get("comm_products", 0), dt_.get("turn", 0), ln.calc, ln.amount])
        ws.append(["Разом", "", sum(ln.days for ln in doc.lines), None, None, None, None, round(sum(ln.calc for ln in doc.lines), 2), doc.total])
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 30
        buf = BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="payroll-{doc.id}.xlsx"'})
    return render(request, "fin_accrual.html", nav="finance", doc=doc, d_from=doc.d_from, d_to=doc.d_to, docs=docs,
                  bal=pd.balances(db), overlap=pd.overlaps(db, doc.d_from, doc.d_to, exclude=doc.id), lp=pd.line_posted,
                  state=pd.doc_state(doc),
                  author=db.get(User, doc.created_by).name if doc.created_by and db.get(User, doc.created_by) else None)


@app.post("/finance/accruals/{aid:int}/post")
def finance_accrual_post(aid: int, on: str = Form("1"), doctor_id: str = Form(""), user: User = Depends(require("finance")),
                         db: Session = Depends(db_dep)):
    from .models import PayrollDoc
    from .services import payroll_docs as pd
    doc = db.get(PayrollDoc, aid)
    if not doc or doc.deleted_at:
        raise HTTPException(404)
    one = int(doctor_id) if doctor_id.isdigit() else None
    pd.set_posted(db, doc, on == "1", one)
    who = db.get(Doctor, one).name if one and db.get(Doctor, one) else None
    audit(db, "fin.payroll_doc_post", user=user, doc=aid, posted=on == "1", doctor=one)
    msg = ("На баланс: " + who if on == "1" else "Знято з балансу: " + who) if who else \
          ("Додано на баланс співробітників" if on == "1" else "Знято з балансу")
    return RedirectResponse(_with_msg(f"/finance/accruals/{aid}" + (f"#d{one}" if one else ""), msg), 303)


@app.post("/finance/accruals/{aid:int}/delete")
def finance_accrual_delete(aid: int, del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PayrollDoc
    doc = db.get(PayrollDoc, aid)
    if not doc:
        raise HTTPException(404)
    err = _delpw(db, user, del_password, "payroll_doc", None, aid)
    if err:
        return RedirectResponse(_with_err(f"/finance/accruals/{aid}", err), 303)
    doc.deleted_at = datetime.utcnow()
    db.commit()
    audit(db, "fin.payroll_doc_delete", user=user, doc=aid, total=doc.total)
    return RedirectResponse(_with_msg("/finance/accruals", f"Нарахування №{aid} видалено"), 303)


@app.get("/finance/settle", response_class=HTMLResponse)
def finance_settle(request: Request, m: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    from .models import FinOp
    month = _month(m)
    rows = fo.settlements(db, month)
    a, b = fo.month_bounds(month)
    pays = db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.category.in_(_fa().salary_names(db)), FinOp.doctor_id.is_not(None))
                      .order_by(FinOp.day.desc())).all()
    pays = [x for x in pays if (x.salary_month or fo_month_key(x.day)) == month]
    return render(request, "fin_settle.html", nav="finance", month=month, rows=rows, sal_cat=_fa().salary_name(db), months=_month_list(), start=fo.payroll_start(db),
                  boxes=_cashboxes(db), pays=pays, docs={d_.id: d_ for d_ in db.scalars(select(Doctor))}, today=date.today(),
                  articles=__import__("app.services.accrual_articles", fromlist=["active"]).active(db),
                  adjs=db.scalars(select(__import__("app.models", fromlist=["SalaryAdj"]).SalaryAdj).where(
                      __import__("app.models", fromlist=["SalaryAdj"]).SalaryAdj.month == month)).all(),
                  total={k: round(sum(r[k] for r in rows), 2) for k in ("opening", "accrued", "paid", "closing")})


@app.post("/finance/settle/start")
def finance_settle_start(month: str = Form(...), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    fo.set_payroll_start(db, _month(month))
    audit(db, "fin.payroll_start", user=user, month=_month(month))
    return RedirectResponse(f"/finance/settle?m={_month(month)}", 303)


@app.get("/finance/balances", response_class=HTMLResponse)
def finance_balances(request: Request, q: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import finops as fo
    rows = fo.balances(db, q)
    pos = round(sum(p.cb_balance for p in rows if p.cb_balance > 0), 2)
    neg = round(sum(p.cb_balance for p in rows if p.cb_balance < 0), 2)
    return render(request, "fin_balances.html", nav="finance", rows=rows, q=q, pos=pos, neg=neg,
                  synced=db.scalar(select(func.max(Patient.cb_synced_at))))


@app.get("/finance/bonuses", response_class=HTMLResponse)
def finance_bonuses(request: Request, df: str = "", dt_: str = "", q: str = "", user: User = Depends(require("finance")),
                    db: Session = Depends(db_dep)):
    from .services import finops as fo
    d_from, d_to = _period(df, dt_, default_days=90)
    from .services import loyalty as ly
    from .models import BonusTxn
    a_, b_ = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    ltx = db.scalars(select(BonusTxn).where(BonusTxn.at >= a_, BonusTxn.at <= b_).order_by(BonusTxn.at.desc()).limit(1000)).all()
    lp = {p_.id: p_ for p_ in db.scalars(select(Patient).where(Patient.id.in_([t.patient_id for t in ltx] or [0])))}
    return render(request, "fin_bonuses.html", nav="finance", d_from=d_from, d_to=d_to, q=q, b=fo.bonuses(db, d_from, d_to, q),
                  ltx=ltx, lp=lp, KL=ly.KIND_LABELS, lyon=ly.settings(db).get("enabled"),
                  synced=db.scalar(select(func.max(Patient.cb_synced_at))))


# ---------------- Амортизація обладнання ----------------
@app.get("/amort", response_class=HTMLResponse)
def amort_page(request: Request, year: int = 0, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am, cashboxes as cbx
    today = date.today()
    rows = am.overview(db, today)
    yr = year or today.year
    if yr != today.year:
        for r in rows:
            r["months"] = am.series(db, r["asset"], yr)
    fund = am.fund_summary(db, rows)
    boxes = cbx.items(db)
    from collections import Counter
    main_box = (Counter(r["asset"].cashbox for r in rows if r["asset"].cashbox).most_common(1) or [(None, 0)])[0][0]
    fund_box = next((b for b in boxes if b.get("name") == main_box), None)
    from .integrations import monobank as _mono
    jars = []
    for r in rows:
        if r["asset"].jar:
            j = _mono.jar(db, r["asset"].jar, refresh=not jars)
            box = next((b for b in boxes if b.get("name") == r["asset"].cashbox), None)
            jars.append({"asset": r["asset"], "jar": j, "crm": cbx.balance(db, box) if box else None,
                         "diff": round(sum(x["t"]["diff"] for x in rows if x["asset"].active and x["asset"].cashbox == r["asset"].cashbox), 2)})
    diff_main = round(sum(x["t"]["diff"] for x in rows if x["asset"].active and x["asset"].cashbox == main_box), 2)
    fund_part = am.fund_part(db, fund_box) if fund_box else None
    split = {k_: {f_: round(sum(x["t"][f_] if f_ != "live" else x["t"]["live"]["amount"] for x in rows
                               if x["asset"].active and x["asset"].kind == k_), 2) for f_ in ("accrued", "paid", "diff", "live")}
             for k_ in ("device", "consumable")}
    for k_ in split:
        split[k_]["spent"] = round(sum(t_.amount for t_ in am.txns(db, kind="expense")
                                       if t_.asset_id and next((x["asset"].kind for x in rows if x["asset"].id == t_.asset_id), None) == k_), 2)
    return render(request, "amort.html", nav="finance", rows=rows, fund=fund, year=yr, years=am.years(db), am=am,
                  boxes=[b["name"] for b in boxes if b.get("active", True)], fund_balance=fund_part["amort"] if fund_part else None, fund_part=fund_part,
                  fund_name=fund_box["name"] if fund_box else (main_box or ""), jars=jars, diff_main=diff_main, split=split, expenses=am.txns(db, kind="expense"),
                  txns=am.txns(db, kind="pay")[:30], today=today, prev=am.prev_half(today), assets_all=[r["asset"] for r in rows])


@app.get("/amort/{aid:int}", response_class=HTMLResponse)
def amort_asset_page(aid: int, request: Request, year: int = 0, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am, cashboxes as cbx
    from .models import AmortAsset
    a = db.get(AmortAsset, aid) or (_ for _ in ()).throw(HTTPException(404))
    am.recompute_open(db)
    today = date.today()
    yr = year or today.year
    t = am.totals(db, a, today)
    children = db.scalars(select(AmortAsset).where(AmortAsset.parent_id == a.id)).all()
    all_rows = am.series_all(db, a, today)
    yr = year if year else 0                                   # 0 — увесь час
    return render(request, "amort_asset.html", nav="finance", a=a, t=t, months=am.series(db, a, yr) if yr else all_rows, year=yr,
                  years=sorted({r_["year"] for r_ in all_rows} | set(am.years(db)), reverse=True), am=am, all_rows=all_rows,
                  ytot=am.by_year_totals(all_rows),
                  jar=__import__("app.integrations.monobank", fromlist=["jar"]).jar(db, a.jar) if a.jar else None,
                  cards=am.techcards_for(db, a), txns=am.txns(db, a.id), boxes=[b["name"] for b in cbx.items(db) if b.get("active", True)],
                  children=[(c, am.totals(db, c, today)) for c in children], parent=db.get(AmortAsset, a.parent_id) if a.parent_id else None,
                  today=today, UNITS=am.UNITS)


@app.post("/amort/accrue")
def amort_accrue(year: int = Form(0), month: int = Form(0), half: int = Form(0), force: str = Form(""),
                 user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    if not (year and month and half):
        year, month, half = am.prev_half(date.today())
    rows = am.accrue(db, year, month, half, force=bool(force))
    n = sum(1 for r in rows if not r["skipped"])
    audit(db, "amort.accrue", user=user, period=f"{year}-{month:02d}-{half}", n=n)
    return RedirectResponse(_with_msg("/amort", f"Нараховано за {am.label(year, month, half)}: позицій {n}"
                                      + (" · перенесені з таблиці не змінюються" if n < len(rows) else "")), 303)


@app.post("/amort/pay")
def amort_pay(asset_id: int = Form(...), day: str = Form(""), amount: str = Form(...), cashbox_from: str = Form(""), note: str = Form(""),
              back: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    from .models import AmortAsset
    a = db.get(AmortAsset, asset_id) or (_ for _ in ()).throw(HTTPException(404))
    amt = _num(amount)
    if not amt or amt <= 0:
        return RedirectResponse(_with_err(back or "/amort", "Вкажіть суму погашення"), 303)
    t = am.pay(db, a, _day(day) or date.today(), amt, cashbox_from.strip(), user.id, note.strip() or None)
    audit(db, "amort.pay", "amort_txn", t.id, user=user, asset=a.name, amount=amt, cashbox=cashbox_from, finop=t.finop_id)
    return RedirectResponse(_with_msg(back or f"/amort/{a.id}", f"Погашено {amt:,.2f} ₴ → {a.cashbox}".replace(",", " ")), 303)


@app.post("/amort/expense")
def amort_expense(asset_id: str = Form(""), day: str = Form(""), amount: str = Form(...), cashbox: str = Form(""), note: str = Form(""),
                  back: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    from .models import AmortAsset
    a = db.get(AmortAsset, int(asset_id)) if asset_id.isdigit() else None
    amt = _num(amount)
    if not amt or amt <= 0 or not note.strip():
        return RedirectResponse(_with_err(back or "/amort", "Вкажіть суму і що саме оплачено"), 303)
    t = am.expense(db, a, _day(day) or date.today(), amt, cashbox.strip(), note.strip(), user.id)
    audit(db, "amort.expense", "amort_txn", t.id, user=user, asset=a.name if a else None, amount=amt, note=note)
    return RedirectResponse(_with_msg(back or "/amort", f"Витрата з фонду {amt:,.2f} ₴ записана".replace(",", " ")), 303)


@app.post("/amort/{aid:int}/replace")
def amort_replace(aid: int, day: str = Form(""), cost: str = Form(...), resource: str = Form(...), cashbox: str = Form(""), note: str = Form(""),
                  from_fund: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    from .models import AmortAsset
    a = db.get(AmortAsset, aid) or (_ for _ in ()).throw(HTTPException(404))
    c, r = _num(cost), _num(resource)
    try:
        new = am.replace(db, a, _day(day) or date.today(), c or 0, r or 0, cashbox.strip() or None, user.id, note.strip() or None, bool(from_fund))
    except (ValueError, Exception) as e:  # noqa: BLE001
        return RedirectResponse(_with_err(f"/amort/{aid}", f"Заміна не вдалася: {str(e)[:150]}"), 303)
    audit(db, "amort.replace", "amort_asset", new.id, user=user, old=aid, cost=c, resource=r)
    return RedirectResponse(_with_msg(f"/amort/{new.id}", f"Новий «{new.name}»: {r:,.0f} за {c:,.2f} ₴ · ставка {new.rate:.4f} ₴".replace(",", " ")), 303)


@app.post("/amort/txn/{tid:int}/delete")
def amort_txn_delete(tid: int, back: str = Form(""), del_password: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    from .models import AmortTxn
    err = _delpw(db, user, del_password, "amort_txn", "amort_txn", tid)
    if err:
        return RedirectResponse(_with_err(back or "/amort", err), 303)
    t = db.get(AmortTxn, tid)
    if t:
        audit(db, "amort.txn_delete", "amort_txn", tid, user=user, kind=t.kind, amount=t.amount)
        am.delete_txn(db, t)
    return RedirectResponse(back or "/amort", 303)


# ---------------- Звіти → KPI лікарів ----------------
def _kpi_month(m: str) -> tuple[date, date, str]:
    mk = _month(m)
    y, mm = int(mk[:4]), int(mk[5:])
    a = date(y, mm, 1)
    b = date(y + (mm == 12), (mm % 12) + 1, 1) - timedelta(days=1)
    return a, b, mk


@app.get("/reports/doctors-kpi", response_class=HTMLResponse)
def doctors_kpi_page(request: Request, m: str = "", doc: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import doctor_kpi as dk
    d_from, d_to, mk = _kpi_month(m)
    only = int(doc) if doc.isdigit() else None
    partial = d_from <= date.today() < d_to                    # поточний місяць — лише до сьогодні, порівняння з тими ж днями
    if partial:
        d_to = date.today()
    r = dk.compute(db, d_from, d_to, only=only)
    prev_total = r.get("prev_total")
    series = dk.series_all(db, d_to, 6) if r["rows"] else {}
    sh_ = dk.sheet_history(db)
    for did_, sr_ in series.items():                         # місяці з таблиці власника — її цифри (звірені вручну)
        hm_ = (sh_.get(did_) or {}).get("months") or {}
        for pt_ in sr_:
            row_ = hm_.get(pt_["month"].strftime("%Y-%m"))
            if row_:
                pt_.update(total=row_.get("total") or 0, patients=int(row_.get("patients") or 0), sheet=True)
    rows_ = r["rows"]
    leaders = {}
    if rows_:
        leaders["turnover"] = max(rows_, key=lambda z: z["total"])
        hr_ = [z for z in rows_ if z["rev_per_worked_h"]]
        leaders["hour"] = max(hr_, key=lambda z: z["rev_per_worked_h"]) if hr_ else None
        ld_ = [z for z in rows_ if z["load"] is not None]
        leaders["load"] = max(ld_, key=lambda z: z["load"]) if ld_ else None
        for i_, z in enumerate(sorted(rows_, key=lambda z: -z["total"])):
            z["rank"] = i_ + 1
    months = []
    cur = date.today().replace(day=1)
    for i in range(0, 4):
        y, mm = cur.year, cur.month - i
        while mm < 1:
            y, mm = y - 1, mm + 12
        months.append({"key": f"{y:04d}-{mm:02d}", "label": date(y, mm, 1).strftime("%m.%Y") if i else "Цей місяць"})
    months.reverse()
    return render(request, "doctor_kpi.html", nav="reports", r=r, d_from=d_from, d_to=d_to, month_key=mk, month_label=d_from.strftime("%m.%Y"),
                  months=months, doctors=db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all(), doc=doc,
                  prev_total=prev_total, series=series, leaders=leaders, ltv=dk.clinic_ltv(db), cosm=dk.cosmetics(rows_),
                  arch=dk.archive(db, d_from, {z["doctor_id"] for z in rows_}) if not only else [],
                  partial=partial, avail_from=dk.avail_from(db))


@app.get("/reports/doctors-kpi/compare", response_class=HTMLResponse)
def doctors_kpi_compare(request: Request, ms: str = "", doc: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import doctor_kpi as dk
    keys = sorted({k for k in ms.split(",") if len(k) == 7 and k[4] == "-" and k[:4].isdigit() and k[5:].isdigit()})[-6:]
    if len(keys) < 2:
        return RedirectResponse(f"/reports/doctors-kpi?m={keys[0] if keys else ''}", 303)
    only = int(doc) if doc.isdigit() else None
    cmp_ = dk.compare(db, keys, only)
    return render(request, "doctor_kpi_compare.html", nav="reports", c=cmp_, keys=keys, doc=doc, M=dk.CMP_METRICS,
                  doctors=db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all(), avail_from=dk.avail_from(db))


@app.get("/reports/doctors-kpi/history", response_class=HTMLResponse)
def doctors_kpi_history(request: Request, key: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import doctor_kpi as dk
    h = dk.history(db, key, date.today())
    return render(request, "doctor_kpi_history.html", nav="reports", h=h, F=dk.HIST_FIELDS)


@app.post("/reports/doctors-kpi/premium")
def doctors_kpi_premium(m: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import doctor_kpi as dk
    d_from, d_to, mk = _kpi_month(m)
    r = dk.compute(db, d_from, d_to, with_prev=False)
    n = dk.accrue_premium(db, mk, r["rows"], user.id)
    audit(db, "kpi.premium", user=user, month=mk, n=n, total=r["total"]["premium"])
    return RedirectResponse(_with_msg(f"/reports/doctors-kpi?m={mk}", f"Премію KPI за {mk} нараховано: {n} лікар(ів), {r['total']['premium']:,.2f} ₴ — див. Фінанси → Взаєморозрахунки по з/п".replace(",", " ")), 303)


@app.get("/settings/doctor-kpi", response_class=HTMLResponse)
def settings_doctor_kpi(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import doctor_kpi as dk
    return render(request, "doctor_kpi_settings.html", nav="settings", cfg=dk.settings(db), TL=dk.TARGET_LABELS, LM=dk.LADDER_METRICS,
                  doctors=db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)).all())


@app.post("/settings/doctor-kpi")
async def settings_doctor_kpi_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import doctor_kpi as dk
    f = await request.form()
    cfg = dk.settings(db)
    for k in dk.TARGET_LABELS:
        v = _num(str(f.get(f"t_{k}") or ""))
        if v is not None:
            cfg["targets"][k] = v
    ladder = []
    for met, mn, am in zip(f.getlist("l_metric"), f.getlist("l_min"), f.getlist("l_amount")):
        if met in dk.LADDER_METRICS and _num(str(mn)) is not None and _num(str(am)):
            ladder.append({"metric": met, "min": _num(str(mn)), "amount": _num(str(am))})
    cfg["ladder"] = ladder
    for d in db.scalars(select(Doctor)):
        tx, rt = _num(str(f.get(f"d_{d.id}_taxes") or "")), _num(str(f.get(f"d_{d.id}_revenue") or ""))
        if f"d_{d.id}_taxes" in f or f"d_{d.id}_revenue" in f:
            x = cfg["doctors"].setdefault(str(d.id), {})
            x["taxes"], x["revenue_target"] = tx or 0, rt or 0
    dk.save_settings(db, cfg)
    audit(db, "kpi.settings", user=user)
    return RedirectResponse(_with_msg("/settings/doctor-kpi", "Збережено"), 303)


@app.get("/settings/amort", response_class=HTMLResponse)
def settings_amort(request: Request, edit: int = 0, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am, cashboxes as cbx
    from .models import AmortAsset
    assets = db.scalars(select(AmortAsset).order_by(AmortAsset.active.desc(), AmortAsset.kind.desc(), AmortAsset.sort, AmortAsset.id)).all()
    return render(request, "amort_settings.html", nav="settings", assets=assets, cur=db.get(AmortAsset, edit) if edit else None,
                  products=am.candidates(db), boxes=[b["name"] for b in cbx.items(db) if b.get("active", True)],
                  devices=[a for a in assets if a.kind == "device"], UNITS=am.UNITS, am=am,
                  seeded=am._flag(db, am.SEEDED_KEY), migrated=am._flag(db, am.MIGRATED_KEY), operational=am.operational(db),
                  pnames={a.product_id: (a.product.name if a.product else "") for a in assets if a.product_id})


@app.post("/settings/amort/operational")
def settings_amort_operational(amount: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    v = _num(amount)
    if v is None:
        return RedirectResponse(_with_err("/settings/amort", "Вкажіть суму"), 303)
    am.set_operational(db, v)
    audit(db, "amort.operational", "sync_state", None, user=user, amount=v)
    return RedirectResponse(_with_msg("/settings/amort", "Операційну частину фонду збережено"), 303)


@app.post("/settings/amort")
async def settings_amort_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import AmortAsset
    f = await request.form()
    aid = str(f.get("aid") or "")
    a = db.get(AmortAsset, int(aid)) if aid.isdigit() else AmortAsset()
    name = " ".join(str(f.get("name") or "").split())[:200]
    if not name:
        return RedirectResponse(_with_err("/settings/amort", "Вкажіть назву"), 303)
    a.name = name
    a.kind = "consumable" if f.get("kind") == "consumable" else "device"
    a.product_id = int(f["product_id"]) if str(f.get("product_id") or "").isdigit() else None
    a.parent_id = int(f["parent_id"]) if str(f.get("parent_id") or "").isdigit() and a.kind == "consumable" else None
    a.cost = _num(str(f.get("cost") or "")) or 0.0
    a.life_years = _num(str(f.get("life_years") or "")) if a.kind == "device" else None
    a.resource = _num(str(f.get("resource") or "")) if a.kind == "consumable" else None
    a.unit = str(f.get("unit") or "процедура") if str(f.get("unit") or "") in ("процедура", "імпульс", "хвилина") else "процедура"
    a.rate = _num(str(f.get("rate") or "")) or 0.0
    if a.kind == "consumable" and not a.rate and a.cost and a.resource:
        a.rate = round(a.cost / a.resource, 4)
    a.avg_month = _num(str(f.get("avg_month") or ""))
    a.commissioned_at = _day(str(f.get("commissioned_at") or ""))
    a.cashbox = str(f.get("cashbox") or "").strip()[:100] or None
    a.jar = str(f.get("jar") or "").strip()[:200] or None
    a.accrued_before = _num(str(f.get("accrued_before") or "")) or 0.0
    a.used_before = _num(str(f.get("used_before") or "")) or 0.0
    a.active = bool(f.get("active"))
    a.note = str(f.get("note") or "").strip()[:500] or None
    if a.product and a.product.price != a.rate and a.unit == "процедура" and a.product_id:
        a.product.price = a.rate                                # ставка живе в ціні позиції техкарти — тримаємо однаковою
    db.add(a)
    db.commit()
    audit(db, "amort.asset", "amort_asset", a.id, user=user, name=a.name, rate=a.rate)
    return RedirectResponse(_with_msg("/settings/amort", "Збережено"), 303)


@app.post("/settings/amort/setup")
def settings_amort_setup(what: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import amort as am
    if what == "seed":
        r = am.seed_from_sheet(db)
        msg = f"Перенесено з таблиці: позицій {r['assets']}, півмісяців {r['periods']}, погашень/витрат {r['txns']}" + \
              (f" · не знайдено товарів: {', '.join(r['no_product'])}" if r["no_product"] else "")
    else:
        r = am.migrate_products(db)
        msg = f"Перенесено в «Обладнання»: {r['moved']} поз. · у техкартах оновлено {r['lines']} рядків, прибрано дублів {r['dupes']}, вимкнено старих {r['deactivated']}"
    audit(db, "amort.setup", user=user, what=what, **{k: v for k, v in r.items() if not isinstance(v, list)})
    return RedirectResponse(_with_msg("/settings/amort", msg), 303)


@app.get("/finance/pricing", response_class=HTMLResponse)
def finance_pricing(request: Request, wh: str = "vitrina", brand: str = "", q: str = "", only: str = "",
                    user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pricing as pr_svc
    rows, rt = _pricing_rows(db, wh, brand, q, only)
    brands = sorted({x for (x,) in db.execute(select(Product.brand).where(Product.brand.is_not(None)).distinct())}, key=str.lower)
    from .models import Supplier
    from .services import supplier_prices as spx
    last_prices = spx.latest_by_brand(db)
    return render(request, "pricing.html", nav="finance", rows=rows[:600], total=len(rows), rt=rt, wh=wh, brand=brand, q=q,
                  only=only, brands=brands, WAREHOUSES=catalog.WAREHOUSES, CURRENCIES=pr_svc.CURRENCIES, MARGINS=pr_svc.MARGINS,
                  suppliers=db.scalars(select(Supplier).order_by(Supplier.name)).all(), last_price=last_prices.get(brand) if brand else None,
                  def_sup=spx.default_supplier(db, brand) if brand and brand != "—" else None, today=date.today(),
                  last_pl=spx.last_for_products(db, {x.product.id for x in rows[:600]}))


@app.post("/finance/pricing/price")
async def finance_price_upload(file: UploadFile = File(...), brand: str = Form(""), supplier_id: str = Form(""), currency: str = Form(""),
                               price_date: str = Form(""), user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Прайс постачальника по бренду → зіставлення з товарами → перегляд перед застосуванням."""
    from .services import supplier_prices as spx
    data = await file.read()
    back = f"/finance/pricing?brand={quote(brand)}" if brand else "/finance/pricing"
    if not data:
        return RedirectResponse(_with_err(back, "Порожній файл"), 303)
    try:
        sp = spx.create(db, data, file.filename or "prais.xlsx", brand.strip() or None, int(supplier_id) if supplier_id.isdigit() else None,
                        currency or None, _day(price_date), user.id)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(_with_err(back, f"Прайс не прочитано: {str(e)[:200]}"), 303)
    audit(db, "pricing.price_upload", "supplier_price", sp.id, user=user, brand=sp.brand, rows=sp.n_rows, matched=sp.n_matched)
    return RedirectResponse(f"/finance/pricing/price/{sp.id}", 303)


@app.get("/finance/pricing/price/{spid:int}", response_class=HTMLResponse)
def finance_price_view(request: Request, spid: int, show: str = "", user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import SupplierPrice, Supplier
    from .services import supplier_prices as spx, pricing as pr_svc
    sp = db.get(SupplierPrice, spid) or (_ for _ in ()).throw(HTTPException(404))
    rows = spx.preview(db, sp)
    if show == "nomatch":
        rows = [r for r in rows if not r["p"]]
    elif show == "changed":
        rows = [r for r in rows if r["p"] and not r["same"]]
    brand_products = db.scalars(select(Product).where(Product.brand == sp.brand, Product.active.is_not(False)).order_by(Product.name)).all() if sp.brand else []
    sup_ = db.get(Supplier, sp.supplier_id) if sp.supplier_id else None
    rt_ = dict(pr_svc.rates(db))
    if sup_ and sup_.fx_usd:
        rt_["USD"] = sup_.fx_usd
    if sup_ and sup_.fx_eur:
        rt_["EUR"] = sup_.fx_eur
    return render(request, "pricing_price.html", nav="finance", sp=sp, rows=rows, show=show, sup=sup_,
                  rt=rt_, brand_products=brand_products,
                  job=__import__("app.services.invoice_ai", fromlist=["x"]).job_state(db, "price", sp.id) if sp.status == "recognizing" else None,
                  uploader=db.get(User, sp.user_id) if sp.user_id else None)


@app.get("/finance/pricing/price/{spid:int}/file")
def finance_price_file(spid: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from pathlib import Path
    if not (can(user, "finance") or can(user, "stock")):
        raise HTTPException(403, "Недостатньо прав")
    from .models import SupplierPrice
    sp = db.get(SupplierPrice, spid) or (_ for _ in ()).throw(HTTPException(404))
    if not sp.file_path or not Path(sp.file_path).exists():
        raise HTTPException(410, "Файл недоступний")
    return FileResponse(sp.file_path, filename=sp.file_name or "prais",
                        content_disposition_type="inline" if (sp.file_name or "").lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp")) else "attachment")


@app.get("/finance/pricing/price/{spid:int}/text")
def finance_price_text(spid: int, offset: int = 0, limit: int = 40, page: int = 0, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Що CRM бачить у PDF прайсу (перші рядки таблиці / тексту) — щоб зрозуміти, чому не розібрано."""
    from pathlib import Path
    from .models import SupplierPrice
    from .services import supplier_prices as spx
    sp = db.get(SupplierPrice, spid) or (_ for _ in ()).throw(HTTPException(404))
    if not sp.file_path or not Path(sp.file_path).exists() or not (sp.file_name or "").lower().endswith(".pdf"):
        return JSONResponse({"error": "не PDF або файл недоступний"}, status_code=400)
    try:
        if page:                                          # слова сторінки з координатами (для налаштування розбору)
            import io as _io
            import pdfplumber
            with pdfplumber.open(_io.BytesIO(Path(sp.file_path).read_bytes())) as pdf:
                pg = pdf.pages[page - 1]
                ws = [[round(w["x0"]), round(w["top"]), w["text"]] for w in pg.extract_words()]
                return JSONResponse({"pages": len(pdf.pages), "tables": len(pg.extract_tables() or []), "words": ws[offset:offset + limit]})
        tab = spx.pdf_table(Path(sp.file_path).read_bytes())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:300]}, status_code=400)
    rows = [[f"{i}:{spx._s(c)}" for i, c in enumerate(r) if spx._s(c)] for r in tab]
    return JSONResponse({"rows": len(tab), "sample": rows[offset:offset + limit]})


@app.post("/finance/pricing/price/{spid:int}")
async def finance_price_apply(spid: int, request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import SupplierPrice
    from .services import supplier_prices as spx
    sp = db.get(SupplierPrice, spid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    if f.get("action") == "match":
        i, pid_ = str(f.get("i") or ""), str(f.get("pid") or "")
        if i.isdigit():
            spx.set_match(db, sp, int(i), int(pid_) if pid_.isdigit() else None)
        return RedirectResponse(f"/finance/pricing/price/{spid}?show={quote(str(f.get('show') or ''))}#r{i}", 303)
    if f.get("action") == "retry":
        spx.retry(db, sp)
        return RedirectResponse(f"/finance/pricing/price/{spid}", 303)
    if f.get("action") == "undo":
        n = spx.undo(db, sp, user.id)
        audit(db, "pricing.price_undo", "supplier_price", sp.id, user=user, reverted=n)
        return RedirectResponse(_with_msg(f"/finance/pricing/price/{spid}", f"Скасовано підстановку з прайсу: {n} товарів повернуто"), 303)
    if f.get("action") == "meta":
        cur = str(f.get("currency") or "")
        if cur in ("USD", "EUR", "UAH"):
            sp.currency = cur
        sp.price_date = _day(str(f.get("price_date") or "")) or sp.price_date
        sid_ = str(f.get("supplier_id") or "")
        sp.supplier_id = int(sid_) if sid_.isdigit() else sp.supplier_id
        db.commit()
        return RedirectResponse(f"/finance/pricing/price/{spid}", 303)
    idx = [int(x) for x in f.getlist("row") if str(x).isdigit()]
    n = spx.apply(db, sp, idx, with_rrp=bool(f.get("rrp")), user_id=user.id)
    audit(db, "pricing.price_apply", "supplier_price", sp.id, user=user, applied=n)
    b = f"/finance/pricing?brand={quote(sp.brand)}" if sp.brand else "/finance/pricing"
    return RedirectResponse(_with_msg(b, f"Закупівельні ціни з прайсу підставлено: {n}"), 303)


@app.post("/finance/rates")
async def finance_rates(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pricing as pr_svc
    f = await request.form()
    back = str(f.get("back") or "/finance/pricing")
    if f.get("nbu"):
        try:
            nb = await asyncio.to_thread(pr_svc.nbu_rates)
            pr_svc.save_rates(db, nb.get("USD"), nb.get("EUR"), None, "НБУ")
        except Exception as e:  # noqa: BLE001
            log.warning("NBU rates: %s", e)
            return RedirectResponse(back + ("&" if "?" in back else "?") + "msg=Не+вдалося+отримати+курс+НБУ", 303)
    else:
        rnd = f.get("round")
        pr_svc.save_rates(db, _num(f.get("usd") or ""), _num(f.get("eur") or ""), int(rnd) if str(rnd).isdigit() else None)
    audit(db, "pricing.rates", user=user)
    return RedirectResponse(back if back.startswith("/finance") else "/finance/pricing", 303)


@app.post("/finance/pricing")
async def finance_pricing_save(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .services import pricing as pr_svc
    f = await request.form()
    from .services import price_history as ph
    ids = [int(x) for x in f.getlist("pid") if str(x).isdigit()]
    n = 0
    now = datetime.now()
    _prods = db.scalars(select(Product).where(Product.id.in_(ids))).all()
    _before = {p_.id: ph.state(p_) for p_ in _prods}
    for p_ in _prods:
        cur = f.get(f"cur_{p_.id}")
        cfx, rrp, plan = _num(f.get(f"cfx_{p_.id}") or ""), _num(f.get(f"rrp_{p_.id}") or ""), _num(f.get(f"plan_{p_.id}") or "")
        new = (cur if cur in pr_svc.CURRENCIES else p_.cost_currency, cfx, rrp, plan)
        if new != (p_.cost_currency, p_.cost_fx, p_.rrp_fx, p_.planned_price):
            if plan != p_.planned_price:
                p_.planned_at = now if plan else None
            p_.cost_currency, p_.cost_fx, p_.rrp_fx, p_.planned_price = new
            n += 1
    db.commit()
    _sn = ph.record(db, "save", _before, _prods, user.id, note="Зміни в таблиці планування")
    audit(db, "pricing.save", user=user, changed=n)
    back = str(f.get("back") or "/finance/pricing")
    sep = "&" if "?" in back else "?"
    return RedirectResponse((back if back.startswith("/finance") else "/finance/pricing") + f"{sep}msg=Збережено:+{n}"
                            + (f"&xl={_sn.id}" if _sn else ""), 303)


@app.post("/finance/pricing/sale/{pid:int}")
async def finance_pricing_sale(pid: int, request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Ціна продажу окремого товару (ціна в програмі) = вказана / рекомендована; синк CleverBox її більше не затирає."""
    from .services import price_history as ph
    p_ = db.get(Product, pid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    v = _num(str(f.get("price") or "")) or p_.planned_price
    if not v or v <= 0:
        return JSONResponse({"error": "Вкажіть рекомендовану ціну"}, status_code=400)
    b = {p_.id: ph.state(p_)}
    old = p_.price
    p_.price, p_.price_set_at = round(v, 2), datetime.now()
    db.commit()
    ph.record(db, "sale", b, [p_], user.id, p_.brand, note=f"Ціна продажу {p_.name}: {old or 0:.2f} → {v:.2f}")
    audit(db, "pricing.sale_price", "product", p_.id, user=user, old=old, new=v)
    return JSONResponse({"ok": True, "price": p_.price})


@app.post("/finance/pricing/sale-bulk")
async def finance_pricing_sale_bulk(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Ціна продажу = рекомендована для кількох товарів одним записом в історії (можна відкотити)."""
    from .services import price_history as ph
    body = await request.json()
    want = {int(x["pid"]): float(x["price"]) for x in (body.get("items") or []) if str(x.get("pid", "")).isdigit() and float(x.get("price") or 0) > 0}
    prods = db.scalars(select(Product).where(Product.id.in_(list(want)))).all() if want else []
    before = {p_.id: ph.state(p_) for p_ in prods}
    now_ = datetime.now()
    for p_ in prods:
        p_.price, p_.price_set_at = round(want[p_.id], 2), now_
    db.commit()
    ph.record(db, "sale", before, prods, user.id, note=f"Ціна продажу = рекомендована для {len(prods)} товарів")
    audit(db, "pricing.sale_bulk", user=user, n=len(prods))
    return JSONResponse({"ok": True, "n": len(prods), "prices": {p_.id: p_.price for p_ in prods}})


@app.post("/finance/pricing/version")
async def finance_pricing_version(request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    """Зберегти поточне планування (бренд / фільтри сторінки) як версію в історії."""
    from .services import price_history as ph, supplier_prices as spx
    f = await request.form()
    wh, brand, q, only = (str(f.get(k) or "") for k in ("wh", "brand", "q", "only"))
    rows, _ = _pricing_rows(db, wh or "vitrina", brand, q, only)
    sup = spx.default_supplier(db, brand) if brand and brand != "—" else None
    sn = ph.version(db, [x.product for x in rows], user.id, brand or None, sup.id if sup else None, str(f.get("note") or "").strip() or None)
    back = f"/finance/pricing?wh={quote(wh)}&brand={quote(brand)}&q={quote(q)}&only={quote(only)}"
    return RedirectResponse(_with_msg(back, f"Версію планування збережено: {sn.n if sn else 0} товарів (№{sn.id if sn else '—'})")
                            + (f"&xl={sn.id}" if sn else ""), 303)


@app.get("/finance/pricing/history", response_class=HTMLResponse)
def finance_pricing_history(request: Request, brand: str = "", sn: int = 0, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PriceSnapshot, Supplier
    from .services import price_history as ph
    rows = ph.for_brand(db, brand or None)
    cur = db.get(PriceSnapshot, sn) if sn else None
    users = {u.id: u.name for u in db.scalars(select(User))}
    sups = {x.id: x.name for x in db.scalars(select(Supplier))}
    brands = sorted({x for (x,) in db.execute(select(PriceSnapshot.brand).where(PriceSnapshot.brand.is_not(None)).distinct())}, key=str.lower)
    return render(request, "pricing_history.html", nav="finance", rows=rows, cur=cur, brand=brand, brands=brands, users=users, sups=sups,
                  KINDS=ph.KINDS, FIELDS=ph.FIELDS)


@app.get("/finance/pricing/history/{snid:int}.xlsx")
def finance_pricing_history_xlsx(snid: int, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PriceSnapshot
    from .services import price_history as ph
    sn = db.get(PriceSnapshot, snid) or (_ for _ in ()).throw(HTTPException(404))
    name = f"planuvannia-{(sn.brand or 'vsi').replace(' ', '_')}-{sn.created_at:%Y-%m-%d}-{sn.id}.xlsx"
    try:
        data = ph.xlsx(db, sn)
    except Exception as e:  # noqa: BLE001
        log.exception("pricing history xlsx %s", snid)
        return HTMLResponse(f"Не вдалося сформувати Excel: {type(e).__name__}: {__import__("html").escape(str(e))[:300]}", status_code=500)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.post("/finance/pricing/history/{snid:int}")
async def finance_pricing_restore(snid: int, request: Request, user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    from .models import PriceSnapshot
    from .services import price_history as ph
    sn = db.get(PriceSnapshot, snid) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    which = "before" if f.get("which") == "before" else "after"
    n = ph.restore(db, sn, which, user.id)
    audit(db, "pricing.restore", "price_snapshot", sn.id, user=user, which=which, n=n)
    return RedirectResponse(_with_msg(f"/finance/pricing/history?brand={quote(sn.brand or '')}",
                                      f"{'Відкочено' if which == 'before' else 'Повернуто версію'} №{sn.id}: {n} товарів"), 303)


@app.get("/finance/pricing.xlsx")
def finance_pricing_xlsx(wh: str = "vitrina", brand: str = "", q: str = "", only: str = "",
                         user: User = Depends(require("finance")), db: Session = Depends(db_dep)):
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from fastapi.responses import StreamingResponse
    from .services import pricing as pr_svc
    rows, rt = _pricing_rows(db, wh, brand, q, only)
    wb = Workbook(); ws = wb.active; ws.title = "Планування цін"
    ws.append([f"Курс USD {rt['USD']}  ·  EUR {rt['EUR']}  ·  {rt['source']} {rt['updated'] or ''}"])
    head = ["Бренд", "Товар", "Артикул", "Склад", "Валюта", "Закупівля (вал.)", "Закупівля, грн", "РРЦ постачальника (вал.)",
            "Ціна в програмі", "Валовий прибуток, грн", "Маржа, %", "Ціна при курсі", "Маржа при курсі, %"] + \
           [f"Ціна при маржі {m}%" for m in pr_svc.MARGINS] + ["Рекомендована ціна", "Різниця з поточною", "Націнка, %", "Маржа, %"]
    ws.append(head)
    for c in ws[2]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="3B3050")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    rnd = rt["round"]
    for x in rows:
        p_ = x.product; cost = x.cost_uah; now_ = x.price_now; at = x.price_at_rate; plan = p_.planned_price
        ws.append([p_.brand, p_.name, p_.sku, catalog.WAREHOUSES.get(p_.warehouse or "vitrina"), x.cur if p_.cost_fx else "UAH",
                   p_.cost_fx, cost, p_.rrp_fx, now_, (now_ - cost) if now_ and cost is not None else None,
                   pr_svc.margin(now_, cost), pr_svc.round_price(at, rnd), pr_svc.margin(at, cost)] +
                  [pr_svc.round_price(pr_svc.price_for_margin(cost, m), rnd) for m in pr_svc.MARGINS] +
                  [plan, (plan - now_) if plan and now_ else None, pr_svc.markup(plan, cost), pr_svc.margin(plan, cost)])
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTU", [14, 46, 12, 12, 8] + [13] * 16):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "C3"
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=pricing-{date.today()}.xlsx"})


@app.get("/book/{item_id}", response_class=HTMLResponse)
def book_page(request: Request, item_id: int, day: str | None = None, user: User = Depends(require("book")),
              db: Session = Depends(db_dep)):
    from .services import booking
    item = db.get(PlanItem, item_id)
    if not item or item.kind != "procedure" or not plan_visible(user, item.plan):
        raise HTTPException(404, "Процедуру не знайдено")
    from .services import autonomy as _aut
    if _aut.offline():                                          # автономний режим: запис — у календарі CRM
        return RedirectResponse(f"/appointments/new?patient={item.plan.patient_id}" + (f"&doc={item.plan.doctor_id}" if item.plan.doctor_id else ""), 303)
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


@app.post("/patients/{pid}/groups")
def patient_groups_edit(pid: int, action: str = Form(...), name: str = Form(""), user: User = Depends(require("patient_edit")),
                        db: Session = Depends(db_dep)):
    from .services import patient_tags as _pt
    p = _patient_or_403(db, user, pid)
    cur = [n for n in (_pt.group_name(g) for g in (p.client_groups or [])) if n]
    nm = " ".join(name.split())[:60]
    allowed = {g["name"].lower(): g["name"] for g in _pt.groups(db)}
    if action == "add" and nm.lower() not in allowed:
        raise HTTPException(400, "Такої групи немає — додайте її в Налаштування → Пацієнти → Групи")
    if action == "add" and nm.lower() not in {x.lower() for x in cur}:
        cur.append(allowed[nm.lower()])
    elif action == "remove":
        cur = [x for x in cur if x.lower() != nm.lower()]
    p.client_groups = list(dict.fromkeys(cur))     # [] — свідомо порожньо, синк CleverBox не перезаписує
    db.commit()
    audit(db, f"patient.group_{action}", "patient", p.id, user=user, group=nm)
    return RedirectResponse(f"/patients/{pid}", 303)


@app.post("/patients/{pid}/mark")
def patient_mark(pid: int, mark: str = Form(""), user: User = Depends(require("patient_edit")), db: Session = Depends(db_dep)):
    """Мітка пацієнта VIP / BAD (поле cb_mark, як у CleverBox); повторне натискання — зняти."""
    p = _patient_or_403(db, user, pid)
    old = p.cb_mark
    p.cb_mark = None if mark not in ("vip", "bad") or p.cb_mark == mark else mark
    db.commit()
    audit(db, "patient.mark", "patient", p.id, user=user, old=old, new=p.cb_mark)
    return RedirectResponse(f"/patients/{pid}", 303)


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
        err = _delpw(db, user, str(f.get("del_password") or ""), "script_note", "procedure_note", n.id)
        if err:
            return RedirectResponse(_with_err("/scripts", err), 303)
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


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(str(BASE / "static" / "img" / "icon-32.png"), media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


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


# ---------------- Графік спеціалістів ----------------
def _places(db: Session) -> list[str]:
    from .services import clinic as cl
    return [p["name"] for l_ in cl.get(db)["locations"] if l_.get("active") for p in l_.get("places", []) if p.get("active", True)]


@app.get("/specialists/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, m: str = "", doc: int = 0, user: User = Depends(require("specialists")),
                  db: Session = Depends(db_dep)):
    """Графік співробітників на місяць (як у CleverBox): зміни, неробочі дні з причиною, записи по днях."""
    from .services import schedule as sch, clinic as _cl, calendar as _cal
    today = date.today()
    first = (_day(m + "-01") if m else None) or today.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    days = [first + timedelta(days=i) for i in range((nxt - first).days)]
    sh, of = sch.shifts(db, days[0], days[-1]), sch.offs(db, days[0], days[-1])
    act = [d_ for d_ in db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)) if not doc or d_.id == doc]
    show_all = request.query_params.get("all") == "1"
    docs = [d_ for d_ in act if show_all or doc or in_schedule(d_)]
    hidden = len(act) - len(docs)
    hours = {d: _cl.hours_for(db, d) for d in days}
    cnt: dict = {}
    for d, evs in _cal.events(db, user, first, len(days)).items():
        for e in evs:
            if e.kind in ("visit", "attended") and e.doctor_id:
                cnt[(e.doctor_id, d)] = cnt.get((e.doctor_id, d), 0) + 1
    work = {d_.id: sum(1 for d in days if (d_.id, d) in sh) for d_ in docs}
    MONTHS = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
    return render(request, "schedule.html", nav="specialists", days=days, docs=docs, sh=sh, of=of, hours=hours, cnt=cnt, work=work,
                  first=first, prev=(first - timedelta(days=1)).replace(day=1), nxt=nxt, month_name=f"{MONTHS[first.month - 1]} {first.year}",
                  today=today, doc=doc, places=_places(db), OFF=sch.OFF_KINDS, color=spec_svc.color, initials=spec_svc.initials,
                  WD=["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"], all_docs=docs, timedelta=timedelta, hidden=hidden, show_all=show_all)


@app.post("/specialists/schedule/bulk")
async def schedule_bulk(request: Request, user: User = Depends(require("schedule")), db: Session = Depends(db_dep)):
    from .services import schedule as sch
    js = await request.json()
    cells = [(int(c[0]), _day(str(c[1]))) for c in js.get("cells") or [] if str(c[0]).isdigit() and _day(str(c[1]))][:2000]
    if not cells:
        return JSONResponse({"error": "Оберіть клітинки графіка"}, status_code=400)
    try:
        n = sch.bulk(db, cells, "work" if js.get("action") == "work" else "off", str(js.get("start") or ""), str(js.get("end") or ""),
                     str(js.get("place") or "").strip()[:120] or None, str(js.get("kind") or ""), user.id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    audit(db, "schedule.bulk", user=user, op=js.get("action"), kind=js.get("kind"), cells=n)
    return JSONResponse({"ok": True, "n": n})


def _sched_back(back: str, msg: str = "", err: bool = False) -> RedirectResponse:
    back = back if back.startswith("/specialists/schedule") else "/specialists/schedule"
    return RedirectResponse(_with_msg(back, msg, err) if msg else back, 303)


@app.post("/specialists/schedule/cell")
def schedule_cell(doctor_id: int = Form(...), day: str = Form(...), start: str = Form(""), end: str = Form(""), place: str = Form(""),
                  note: str = Form(""), off: str = Form(""), back: str = Form(""), user: User = Depends(require("schedule")),
                  db: Session = Depends(db_dep)):
    from .services import schedule as sch
    d_ = _day(day)
    if not d_:
        return _sched_back(back, "Невірна дата", True)
    try:
        sch.set_shift(db, doctor_id, d_, None if off else start, None if off else end, place.strip()[:120] or None,
                      note.strip()[:200] or None, user.id)
    except ValueError as e:
        return _sched_back(back, str(e), True)
    audit(db, "schedule.cell", "doctor", doctor_id, user=user, day=day, start=None if off else start, end=None if off else end, place=place or None)
    return _sched_back(back)


@app.post("/specialists/schedule/fill")
async def schedule_fill(request: Request, user: User = Depends(require("schedule")), db: Session = Depends(db_dep)):
    from .services import schedule as sch
    f = await request.form()
    back = str(f.get("back") or "")
    d_from, d_to = _day(str(f.get("d_from") or "")), _day(str(f.get("d_to") or ""))
    if not d_from or not d_to or d_to < d_from or (d_to - d_from).days > 370:
        return _sched_back(back, "Вкажіть період (до року)", True)
    ids = [int(x) for x in f.getlist("doctor_id") if str(x).isdigit()]
    if not ids:
        return _sched_back(back, "Оберіть спеціаліста", True)
    wd = {int(x) for x in f.getlist("wd") if str(x).isdigit()}
    n = 0
    try:
        for did in ids:
            n += sch.fill(db, did, d_from, d_to, wd, str(f.get("start") or ""), str(f.get("end") or ""),
                          str(f.get("place") or "").strip()[:120] or None, str(f.get("mode") or "weekdays"), user.id,
                          overwrite=bool(f.get("overwrite")))
    except ValueError as e:
        return _sched_back(back, str(e), True)
    audit(db, "schedule.fill", user=user, doctors=ids, d_from=str(d_from), d_to=str(d_to), mode=f.get("mode"), shifts=n)
    return _sched_back(back, f"Заповнено змін: {n}")


@app.post("/specialists/schedule/copy")
def schedule_copy(src: str = Form(...), weeks: int = Form(1), back: str = Form(""), user: User = Depends(require("schedule")),
                  db: Session = Depends(db_dep)):
    from .services import schedule as sch
    s = _day(src)
    if not s:
        return _sched_back(back, "Невірна дата", True)
    s = s - timedelta(days=s.weekday())
    n = sum(sch.copy_week(db, s, s + timedelta(days=7 * k), user.id) for k in range(1, max(1, min(weeks, 12)) + 1))
    audit(db, "schedule.copy", user=user, src=str(s), weeks=weeks, shifts=n)
    return _sched_back(back, f"Скопійовано змін: {n}")


@app.post("/specialists/schedule/clear")
def schedule_clear(doctor_id: str = Form(""), d_from: str = Form(...), d_to: str = Form(...), del_password: str = Form(""),
                   back: str = Form(""), user: User = Depends(require("schedule")), db: Session = Depends(db_dep)):
    from .services import schedule as sch
    err = _delpw(db, user, del_password, "schedule", "doctor", int(doctor_id) if doctor_id.isdigit() else None)
    if err:
        b = back if back.startswith("/specialists/schedule") else "/specialists/schedule"
        return RedirectResponse(_with_err(b, err), 303)
    a, b_ = _day(d_from), _day(d_to)
    n = sch.clear(db, int(doctor_id) if doctor_id.isdigit() else None, a, b_) if a and b_ else 0
    audit(db, "schedule.clear", user=user, doctor=doctor_id or "all", d_from=d_from, d_to=d_to, shifts=n)
    return _sched_back(back, f"Прибрано змін: {n}")


SPEC_TABS = {"info": "Інформація", "services": "Доступні послуги", "photos": "Візити з фото", "log": "Журнал дій",
             "files": "Файли", "online": "Онлайн-запис"}
DOC_FILE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".tiff", ".tif", ".webp", ".doc", ".docx", ".pdf", ".xls", ".xlsx")
SOCIALS = [("viber", "Viber"), ("telegram", "Telegram"), ("whatsapp", "WhatsApp"), ("facebook", "Facebook"), ("instagram", "Instagram")]


AUDIT_WORDS = {"visit.note": "Додано нотатку до візиту", "login": "Увійшов до системи", "logout": "Вийшов із системи", "specialist.update": "Оновлено картку співробітника",
               "specialist.services": "Оновлено доступні послуги", "specialist.services_fill": "Відмічено виконані послуги",
               "specialist.files": "Завантажено файли", "specialist.file_delete": "Видалено файл", "specialist.calendar": "Змінено показ у календарі",
               "specialist.create": "Створено співробітника", "specialist.status_auto": "Статус оновлено з CleverBox",
               "sale.create": "Продаж", "sale.refund": "Повернення чека", "sale.cancel": "Скасовано чек", "sale.fiscalize": "Фіскалізовано чек",
               "task.create": "Створено задачу", "task.done": "Завершено задачу", "task.update": "Оновлено задачу", "appt.create": "Створено запис",
               "appt.update": "Змінено запис", "appt.status": "Змінено статус запису", "message.send": "Надіслано повідомлення",
               "medcard.create": "Запис у медкартці", "medcard.update": "Змінено запис медкартки", "fin.salary_adj": "Нарахування / утримання",
               "schedule.fill": "Заповнено графік", "schedule.set": "Змінено графік", "plan.create": "Створено бюті-план", "patient.update": "Оновлено картку пацієнта"}


def audit_text(a) -> str:
    base = AUDIT_WORDS.get(a.action) or AUDIT_WORDS.get(a.action.split(".")[0]) or a.action
    det = a.details or {}
    bits = []
    if a.entity and a.entity_id and a.entity != "doctor":
        bits.append({"sale": "Ч-", "task": "задача ", "appointment": "запис ", "patient": "пацієнт ", "stock_doc": "документ "}.get(a.entity, a.entity + " ") + str(a.entity_id))
    for k in ("name", "total", "amount", "comment", "n", "files"):
        if det.get(k) not in (None, "", 0):
            bits.append(f"{det[k]}")
    return base + (" · " + " · ".join(bits[:3]) if bits else "")


templates.env.globals["audit_text"] = audit_text


def _spec_tab_data(db: Session, user: User, d: Doctor, tab: str) -> dict:
    """Дані вкладок картки спеціаліста (лише для відкритої вкладки)."""
    from .models import DoctorFile, MedRecord
    out: dict = {}
    if tab == "services":
        chosen = {str(k): v for k, v in ((d.extra or {}).get("services") or {}).items()}
        groups: dict = {}
        for s_ in db.scalars(select(CbService).order_by(CbService.name)):
            groups.setdefault(s_.category or "Без категорії", []).append(s_)
        out["groups"] = sorted(groups.items(), key=lambda kv: kv[0].lower())
        out["chosen"] = chosen
    elif tab == "photos":
        from .security import patient_visible
        recs = db.scalars(select(MedRecord).where(MedRecord.doctor_id == d.id, MedRecord.deleted_at.is_(None))
                          .order_by(MedRecord.at.desc()).limit(300)).all()
        files = db.scalars(select(PatientFile).where(PatientFile.kind == "photo", PatientFile.record_id.in_([r_.id for r_ in recs] or [0]))).all()
        by_rec: dict = {}
        for f_ in files:
            by_rec.setdefault(f_.record_id, []).append(f_)
        pats = {p_.id: p_ for p_ in db.scalars(select(Patient).where(Patient.id.in_([r_.patient_id for r_ in recs if r_.id in by_rec] or [0])))}
        out["visits"] = [{"r": r_, "p": pats[r_.patient_id], "photos": sorted(by_rec[r_.id], key=lambda f_: f_.id)} for r_ in recs
                         if r_.id in by_rec and r_.patient_id in pats and patient_visible(user, pats[r_.patient_id])]
    elif tab == "log":
        uids = [u_.id for u_ in db.scalars(select(User).where(User.doctor_id == d.id))]
        q = select(AuditLog).where((AuditLog.entity == "doctor") & (AuditLog.entity_id == d.id))
        if uids:
            q = select(AuditLog).where(((AuditLog.entity == "doctor") & (AuditLog.entity_id == d.id)) | AuditLog.user_id.in_(uids))
        out["log"] = db.scalars(q.order_by(AuditLog.at.desc()).limit(300)).all()
        out["has_user"] = bool(uids)
    elif tab == "files":
        out["files"] = db.scalars(select(DoctorFile).where(DoctorFile.doctor_id == d.id).order_by(DoctorFile.uploaded_at.desc())).all()
    return out


@app.get("/specialists/{did:int}", response_class=HTMLResponse)
def specialist_page(did: int, request: Request, tab: str = "info", user: User = Depends(require("specialists")),
                    db: Session = Depends(db_dep)):
    d = db.get(Doctor, did)
    if not d:
        raise HTTPException(404)
    tab = tab if tab in SPEC_TABS else "info"
    card = spec_svc.cards(db, [did])[0]
    now = datetime.now()
    idx = spec_svc.DoctorIndex(spec_svc.all_doctors(db))
    upcoming = [v for v in db.scalars(select(Visit).where(Visit.status == "booked", Visit.starts_at > now,
                                                          Visit.starts_at <= now + timedelta(days=14),
                                                          Visit.master_name.is_not(None)).order_by(Visit.starts_at))
                if (m := idx.find(v.master_name)) and m.id == did][:30]
    patients = db.scalars(select(Patient).where(Patient.doctor_id == did).order_by(Patient.full_name).limit(200)).all()
    from .services import positions as _ps, staff_fields as _sf, clinic as _cl
    xf = _sf.custom(db)
    from .services import salary_plans as _spl
    auto_opts = {"clinic": [l_.get("name") for l_ in (_cl.get(db).get("locations") or []) if l_.get("name")], "salary": _spl.names(db)}
    locs = [l_.get("name") for l_ in (_cl.get(db).get("locations") or []) if l_.get("name")] or ["MOLODIST"]
    return render(request, "specialist.html", d=d, c=card, upcoming=upcoming, patients=patients, tab=tab, TABS=SPEC_TABS,
                  palette=spec_svc.PALETTE, positions=_ps.names(db), xfields=xf, auto_opts=auto_opts, xv=d.extra or {},
                  SOCIALS=SOCIALS, locs=locs, WH=catalog.WAREHOUSES, td=_spec_tab_data(db, user, d, tab),
                  chief=__import__("app.services.tg_notify", fromlist=["is_chief"]).is_chief(db, d.id),
                  d_users=db.scalars(select(User).where(User.doctor_id == d.id, User.active.is_(True))).all())


def in_schedule(d) -> bool:
    """Чи виводити співробітника в «Графік співробітників». Не задано — як «Відображати в календарі»."""
    v = (d.extra or {}).get("in_schedule")
    return bool(v) if v is not None else d.in_calendar is not False


templates.env.globals["in_schedule"] = in_schedule


@app.get("/specialists/{did:int}/fire", response_class=HTMLResponse)
def specialist_fire_page(request: Request, did: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import offboarding as ob
    d = db.get(Doctor, did) or (_ for _ in ()).throw(HTTPException(404))
    return render(request, "specialist_fire.html", nav="specialists", d=d, pv=ob.preview(db, d))


@app.post("/specialists/{did:int}/fire")
def specialist_fire(did: int, to_doctor: str = Form(""), to_user: str = Form(""), block_login: str = Form(""),
                    drop_schedule: str = Form(""), user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .services import offboarding as ob
    d = db.get(Doctor, did) or (_ for _ in ()).throw(HTTPException(404))
    if not d.active:
        return RedirectResponse(_with_msg(f"/specialists/{did}", "Співробітника вже звільнено", True), 303)
    try:
        rep = ob.fire(db, d, int(to_doctor) if to_doctor.isdigit() else None, int(to_user) if to_user.isdigit() else None,
                      block_login == "1", drop_schedule == "1", user)
    except ValueError as e:
        return RedirectResponse(_with_msg(f"/specialists/{did}/fire", str(e), True), 303)
    audit(db, "specialist.fire", "doctor", did, user=user, to_doctor=to_doctor or None, to_user=to_user or None,
          appts=rep["appts"], tasks=rep["tasks"], blocked=rep["blocked"])
    db.commit()
    msg = f"{d.name} звільнено. Передано візитів: {rep['appts']}, задач: {rep['tasks']}" + \
          (f"; прибрано змін графіка: {rep['shifts']}" if rep["shifts"] else "") + (f"; вхід у CRM заблоковано" if rep["blocked"] else "") + \
          (f". Накладки в календарі: {'; '.join(rep['conflicts'][:3])}" if rep["conflicts"] else "")
    return RedirectResponse(_with_msg(f"/specialists/{did}", msg, bool(rep["conflicts"])), 303)


@app.post("/specialists/{did:int}")
async def specialist_save(did: int, request: Request, name: str = Form(...), position: str = Form(""), phone: str = Form(""),
                    email: str = Form(""), color: str = Form(""), bio: str = Form(""), work_days: str = Form(""),
                    telegram_chat_id: str = Form(""), photo_url: str = Form(""), status: str = Form("active"),
                    in_calendar: str = Form(""), sale_initiator: str = Form("1"), user: User = Depends(require("specialists_edit")),
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
    if not want_active and d.active:                            # звільнення — лише через майстер передачі справ
        return RedirectResponse(f"/specialists/{did}/fire", 303)
    if want_active != bool(d.active):
        d.active, d.status_locked = want_active, True
        d.fired_at = None if want_active else date.today()
    d.in_calendar = in_calendar == "1"
    d.sale_initiator = sale_initiator == "1"
    f_chief = await request.form()
    if "chief_set" in f_chief:
        from .services import tg_notify as _tn
        if _tn.is_chief(db, d.id) != (f_chief.get("chief") == "1"):
            _tn.set_chief(db, d.id, f_chief.get("chief") == "1")
            audit(db, "specialist.chief", user=user, id=d.id, op="on" if f_chief.get("chief") == "1" else "off")
    from .services import staff_fields as _sf
    xf = _sf.custom(db)
    if xf:
        vals = _sf.read_form(xf, await request.form())
        for f_ in xf:                                           # дати — текстом дд.мм.рррр → ISO
            if f_["type"] == "date" and vals.get(f_["key"]):
                dd_ = _exp_day(vals[f_["key"]])
                vals[f_["key"]] = dd_.isoformat() if dd_ else ""
        d.extra = {**(d.extra or {}), **vals}
    f_all = await request.form()
    d.extra = {**(d.extra or {}),
               "social": {k: str(f_all.get(f"social_{k}") or "").strip()[:200] for k, _ in SOCIALS if str(f_all.get(f"social_{k}") or "").strip()},
               "writeoff_wh": {k[3:]: v for k, v in f_all.items() if k.startswith("wh_") and v in catalog.WAREHOUSES},
               "in_schedule": f_all.get("in_schedule") == "1"}
    db.commit()
    audit(db, "specialist.update", "doctor", d.id, user=user)
    return RedirectResponse(f"/specialists/{did}?saved=1", 303)


@app.post("/specialists/{did:int}/services")
async def specialist_services(did: int, request: Request, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    """Доступні послуги спеціаліста й час виконання: svc_{id}=1, min_{id}=хвилини; fill=1 — відмітити виконані раніше."""
    d = db.get(Doctor, did) or (_ for _ in ()).throw(HTTPException(404))
    f = await request.form()
    if f.get("fill") == "1":
        idx = spec_svc.DoctorIndex(spec_svc.all_doctors(db))
        names = {(v_.service_name or "").strip().lower() for v_ in db.scalars(select(Visit).where(Visit.master_name.is_not(None)))
                 if v_.service_name and (m_ := idx.find(v_.master_name)) and m_.id == did}
        cur = dict((d.extra or {}).get("services") or {})
        for s_ in db.scalars(select(CbService)):
            if (s_.name or "").strip().lower() in names and str(s_.id) not in cur:
                cur[str(s_.id)] = s_.time_min or 30
        d.extra = {**(d.extra or {}), "services": cur}
        db.commit()
        audit(db, "specialist.services_fill", "doctor", did, user=user, n=len(cur))
        return RedirectResponse(_with_msg(f"/specialists/{did}?tab=services", f"Відмічено послуг: {len(cur)}"), 303)
    out = {}
    for k, v in f.items():
        if k.startswith("svc_") and v == "1":
            sid = k[4:]
            out[sid] = int(_num(str(f.get(f"min_{sid}") or "")) or 0) or 30
    d.extra = {**(d.extra or {}), "services": out}
    db.commit()
    audit(db, "specialist.services", "doctor", did, user=user, n=len(out))
    return RedirectResponse(_with_msg(f"/specialists/{did}?tab=services", "Послуги збережено"), 303)


@app.post("/specialists/{did:int}/avatar")
async def specialist_avatar(did: int, file: UploadFile = File(...), user: User = Depends(require("specialists_edit")),
                            db: Session = Depends(db_dep)):
    """Аватар співробітника: jpg / png / webp до 5 МБ → показується в картці, списку й календарі."""
    from .services import ingest
    d = db.get(Doctor, did) or (_ for _ in ()).throw(HTTPException(404))
    data = await file.read()
    nm = (file.filename or "avatar").lower()
    if not data or len(data) > 5 * 1024 * 1024 or not (nm.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")) or (file.content_type or "").startswith("image/")):
        return RedirectResponse(_with_msg(f"/specialists/{did}", "Аватар: потрібне фото jpg, png або webp до 5 МБ", True), 303)
    path = ingest.store(data, "avatar_" + nm[-40:])
    d.extra = {**(d.extra or {}), "avatar_path": path, "avatar_mime": (file.content_type or "image/jpeg")[:60]}
    d.photo_url = f"/specialists/{did}/avatar?v={int(datetime.now().timestamp())}"
    db.commit()
    audit(db, "specialist.avatar", "doctor", did, user=user)
    return RedirectResponse(_with_msg(f"/specialists/{did}", "Аватар оновлено"), 303)


@app.get("/specialists/{did:int}/avatar")
def specialist_avatar_get(did: int, user: User = Depends(current_user), db: Session = Depends(db_dep)):
    d = db.get(Doctor, did)
    p = (d.extra or {}).get("avatar_path") if d else None
    if not p or not Path(p).exists():
        raise HTTPException(404)
    return FileResponse(p, media_type=(d.extra or {}).get("avatar_mime") or "image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.post("/specialists/{did:int}/avatar/delete")
def specialist_avatar_delete(did: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    d = db.get(Doctor, did) or (_ for _ in ()).throw(HTTPException(404))
    ex = dict(d.extra or {})
    ex.pop("avatar_path", None); ex.pop("avatar_mime", None)
    d.extra = ex
    if (d.photo_url or "").startswith("/specialists/"):
        d.photo_url = None
    db.commit()
    audit(db, "specialist.avatar_delete", "doctor", did, user=user)
    return RedirectResponse(_with_msg(f"/specialists/{did}", "Аватар прибрано"), 303)


@app.post("/specialists/{did:int}/files")
async def specialist_files_upload(did: int, files: list[UploadFile] = File(...), user: User = Depends(require("specialists_edit")),
                                  db: Session = Depends(db_dep)):
    from .services import ingest
    from .models import DoctorFile
    db.get(Doctor, did) or (_ for _ in ()).throw(HTTPException(404))
    n, bad = 0, []
    for f in files:
        data = await f.read()
        nm = (f.filename or "файл")[:200]
        if not data or len(data) > 8 * 1024 * 1024 or not nm.lower().endswith(DOC_FILE_EXT):
            bad.append(nm)
            continue
        db.add(DoctorFile(doctor_id=did, path=ingest.store(data, nm), name=nm, mime=(f.content_type or "")[:100] or None,
                          size=len(data), uploaded_by=user.id))
        n += 1
    db.commit()
    audit(db, "specialist.files", "doctor", did, user=user, files=n)
    msg = f"Завантажено файлів: {n}" + (f". Пропущено (формат або > 8 МБ): {', '.join(bad)[:150]}" if bad else "")
    return RedirectResponse(_with_msg(f"/specialists/{did}?tab=files", msg, bool(bad) and not n), 303)


@app.get("/specialists/files/{fid:int}")
def specialist_file_get(fid: int, user: User = Depends(require("specialists")), db: Session = Depends(db_dep)):
    from .models import DoctorFile
    f = db.get(DoctorFile, fid)
    if not f or not Path(f.path).exists():
        raise HTTPException(404, "Файл не знайдено")
    return FileResponse(f.path, media_type=f.mime or mimetypes.guess_type(f.name)[0] or "application/octet-stream", filename=f.name)


@app.post("/specialists/files/{fid:int}/delete")
def specialist_file_delete(fid: int, user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    from .models import DoctorFile
    f = db.get(DoctorFile, fid) or (_ for _ in ()).throw(HTTPException(404))
    did, nm = f.doctor_id, f.name
    db.delete(f)
    db.commit()
    audit(db, "specialist.file_delete", "doctor", did, user=user, name=nm)
    return RedirectResponse(_with_msg(f"/specialists/{did}?tab=files", "Файл видалено"), 303)


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


@app.post("/specialists/{did:int}/calendar")
def specialist_calendar_toggle(did: int, request: Request, enabled: str = Form(""),
                               user: User = Depends(require("specialists_edit")), db: Session = Depends(db_dep)):
    """Галочка «Показувати в календарі» на картці спеціаліста."""
    d = db.get(Doctor, did)
    if not d:
        raise HTTPException(404)
    d.in_calendar = enabled == "1"
    db.commit()
    audit(db, "specialist.calendar", "doctor", d.id, user=user, in_calendar=d.in_calendar)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "in_calendar": d.in_calendar}
    return RedirectResponse("/specialists", 303)


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


# ---------------- Бюті-плани: дошка ----------------
PLAN_COLUMNS = [("new", "Нові", "ще не стартували, без запису"),
                ("need", "Потребують запису", "почали, але наступна процедура не записана"),
                ("booked", "Записані", "є запис наперед"),
                ("done", "Завершені", "усі процедури виконано")]


def plan_column(ps) -> str:
    if ps.plan.status == "completed" or ps.is_complete:
        return "done"
    if ps.is_booked:
        return "booked"
    if ps.realized_sum > 0 or ps.proc_done > 0:
        return "need"
    return "new"


@app.get("/plans", response_class=HTMLResponse)
def plans_board(request: Request, doc: str = "", q: str = "", plan: int | None = None,
                user: User = Depends(current_user), db: Session = Depends(db_dep)):
    from .services import specialists as spec_svc
    from .services.kpi import all_statuses
    doc_id = int(doc) if doc.isdigit() else None
    statuses = [ps for ps in all_statuses(db, doctor_id=doctor_scope(user)) if ps.plan.status != "cancelled"]
    if doc_id:
        statuses = [ps for ps in statuses if ps.plan.doctor_id == doc_id]
    if q:
        words = set(norm(q).split())
        statuses = [ps for ps in statuses if __import__("app.services.search", fromlist=["x"]).match(list(words), ps.plan.patient.full_name, ps.plan.patient.phone or "")
                    or (ps.plan.patient.phone and q.strip() in ps.plan.patient.phone)]
    cols = {k: [] for k, *_ in PLAN_COLUMNS}
    for ps in statuses:
        cols[plan_column(ps)].append(ps)
    far = datetime(2100, 1, 1)
    cols["new"].sort(key=lambda ps: ps.plan.session_at or far, reverse=True)
    cols["need"].sort(key=lambda ps: ({"red": 0, "yellow": 1}.get(ps.flag, 2), ps.due_date or far))
    cols["booked"].sort(key=lambda ps: ps.next_booking or far)
    cols["done"].sort(key=lambda ps: ps.last_activity or datetime.min, reverse=True)
    sel = next((ps for ps in statuses if ps.plan.id == plan), None) if plan else None
    doctors = [d for d in spec_svc.all_doctors(db) if any(ps.plan.doctor_id == d.id for ps in statuses) or d.active]
    return render(request, "plans.html", cols=cols, COLS=PLAN_COLUMNS, sel=sel, doc=doc_id, q=q, total=len(statuses),
                  doctors=doctors, color=spec_svc.color, initials=spec_svc.initials, now=datetime.now(),
                  sums={k: sum(ps.planned_sum for ps in v) for k, v in cols.items()})
