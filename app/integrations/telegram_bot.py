"""Telegram-бот MOLODIST CRM.

• Додайте бота в робочі чати (з PDF бюті-планів і фото) та в чат адмінів.
• /id — показує ID чату (вписати в .env: TELEGRAM_ALLOWED_CHAT_IDS / TELEGRAM_ADMIN_CHAT_ID).
• PDF Observ -> бот створює/оновлює бюті-план і відповідає коротким підсумком.
• Фото з підписом «Прізвище Ім'я» (або телефон) -> фото потрапляє в картку пацієнта.
  Фото без підпису: відповісти на нього командою /to Прізвище Ім'я.
• /plan Прізвище — статус плану; /digest — надіслати нагадування зараз.
"""
from __future__ import annotations

import asyncio
import io
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from ..config import settings
from ..models import SessionLocal, PatientFile
from ..services import ingest, reminders
from ..services.kpi import compute_plan
from sqlalchemy import select

log = logging.getLogger("tg")
router = Router()
pay_router = Router()          # група «Рахунки на оплату» — обробляється раніше за бюті-плани / фото


# ---------------- рахунки постачальників на оплату ----------------
def _is_pay_chat(msg: Message) -> bool:
    from ..services import pay_requests as prq
    with SessionLocal() as db:
        cid = prq.chat_id(db)
    return bool(cid) and str(msg.chat.id) == cid


def pay_keyboard(pr) -> InlineKeyboardMarkup | None:
    if pr.status not in ("new", "error"):
        return None
    from ..services import pay_requests as prq
    rows = [[InlineKeyboardButton(text=f"✅ Погодити · ФОП {pr.fop}" if pr.fop else "✅ Погодити", callback_data=f"pay:ok:{pr.id}"),
             InlineKeyboardButton(text="❌ Відхилити", callback_data=f"pay:no:{pr.id}")]]
    with SessionLocal() as db:
        others = [f for f in prq.fops(db) if f != pr.fop]
    if others:
        rows.append([InlineKeyboardButton(text=f"🔁 Платити з ФОП {f}", callback_data=f"pay:fop:{pr.id}:{f[:20]}") for f in others[:2]])
    rows.append([InlineKeyboardButton(text="✏️ Відкрити в CRM", url=f"{settings.public_url.rstrip('/')}/payreq/{pr.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _pay_file(msg: Message, bot: Bot, file_id: str, name: str, mime: str | None):
    import asyncio
    from ..services import pay_requests as prq
    data = await _download(bot, file_id)
    who = (msg.from_user.full_name if msg.from_user else "") or ""

    def run():
        with SessionLocal() as db:
            pr = prq.create(db, data, name, mime, source="telegram", tg_chat=str(msg.chat.id), tg_msg=msg.message_id, sender=who)
            return pr.id, prq.card_text(db, pr), pr
    pid, text, pr = await asyncio.to_thread(run)
    card = await msg.reply(text, reply_markup=pay_keyboard(pr), disable_web_page_preview=True)
    with SessionLocal() as db:
        from ..models import PayRequest
        x = db.get(PayRequest, pid)
        if x:
            x.tg_card = card.message_id
            db.commit()


@pay_router.message(Command("rahunky"))
async def cmd_bind_pay(msg: Message):
    """Призначити цей чат групою рахунків на оплату (лише власник)."""
    from ..services import pay_requests as prq
    with SessionLocal() as db:
        if str(msg.from_user.id if msg.from_user else "") not in prq.owner_tg_ids(db):
            await msg.reply("Призначити чат рахунків може лише власник (його Telegram ID — у «Мій кабінет» CRM).")
            return
        prq.set_chat(db, str(msg.chat.id))
    await msg.reply("✅ Цей чат — «Рахунки на оплату». Надсилайте сюди рахунки постачальників (PDF або фото): "
                    "я розпізнаю їх, а після погодження власником створю чернетку платежу в monobank на підпис.")


@pay_router.message(F.document, _is_pay_chat)
async def pay_document(msg: Message, bot: Bot):
    d = msg.document
    if not ((d.mime_type or "").startswith("image/") or (d.file_name or "").lower().endswith(".pdf") or d.mime_type == "application/pdf"):
        return
    await _pay_file(msg, bot, d.file_id, d.file_name or "rahunok.pdf", d.mime_type)


@pay_router.message(F.photo, _is_pay_chat)
async def pay_photo(msg: Message, bot: Bot):
    ph = msg.photo[-1]
    await _pay_file(msg, bot, ph.file_id, f"rahunok_{ph.file_unique_id}.jpg", "image/jpeg")


@pay_router.message(Command("zayavky"))
async def cmd_bind_leads(msg: Message):
    """Призначити цей чат чатом заявок із сайту (лише власник). /zayavky off — відвʼязати (заявки знову власнику)."""
    from ..services import pay_requests as prq, site_leads as sl
    with SessionLocal() as db:
        if str(msg.from_user.id if msg.from_user else "") not in prq.owner_tg_ids(db):
            await msg.reply("Призначити чат заявок може лише власник (його Telegram ID — у «Мій кабінет» CRM).")
            return
        off = "off" in (msg.text or "").lower()
        sl.set_chat(db, None if off else str(msg.chat.id))
    if off:
        await msg.reply("Заявки з сайту більше не надходять у цей чат (знову — власнику).")
        return
    await msg.reply("✅ Цей чат — «Заявки з сайту». Сюди надходитимуть нові заявки: імʼя, телефон, сторінка й посилання на звернення в CRM.")


def _exp_text(msg: Message) -> bool:
    """Текст у групі бухгалтерії з сумою й назвою — видаток з каси (див. services/chat_expenses)."""
    if not msg.text or msg.text.startswith("/"):
        return False
    from ..services import chat_expenses as ce
    with SessionLocal() as db:
        return ce.is_chat(db, msg.chat.id) and ce.enabled(db) and ce.parse(db, msg.text) is not None


@pay_router.message(Command("rozhody"))
async def cmd_bind_exp(msg: Message):
    """Призначити цей чат чатом видатків і переміщень (лише власник). /rozhody off — відвʼязати."""
    from ..services import pay_requests as prq, chat_expenses as ce
    with SessionLocal() as db:
        if str(msg.from_user.id if msg.from_user else "") not in prq.owner_tg_ids(db):
            await msg.reply("Призначити чат видатків може лише власник (його Telegram ID — у «Мій кабінет» CRM).")
            return
        off = "off" in (msg.text or "").lower()
        ce.set_chat(db, str(msg.chat.id), not off)
    if off:
        await msg.reply("Цей чат більше не вносить видатки в CRM.")
        return
    await msg.reply("✅ Цей чат — «Видатки й переміщення». Пишіть одним повідомленням суму, касу й статтю:\n"
                    "• <code>350 готівка канцтовари папір А4</code> — видаток\n"
                    "• <code>2000 переміщення з готівки на ФОП Бондар</code> — переміщення між касами\n"
                    "Що не впізнаю — спитаю кнопками. Під кожним внесеним є «Скасувати».")


def _exp_kb(key: str, d: dict) -> InlineKeyboardMarkup:
    rows = []
    if d.get("kind") == "transfer":
        act = "c" if not d.get("cashbox") else "t"
        rows += [[InlineKeyboardButton(text=f"💳 {n}"[:60], callback_data=f"exp:{act}:{key}:{i}")]
                 for i, n in enumerate(d.get("cash_all", [])[:8]) if n != (d.get("cashbox") if act == "t" else d.get("cashbox_to"))]
        rows.append([InlineKeyboardButton(text="✖️ Не вносити", callback_data=f"exp:x:{key}:0")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if not d.get("cashbox"):
        rows += [[InlineKeyboardButton(text=f"💳 {n}"[:60], callback_data=f"exp:c:{key}:{i}")] for i, n in enumerate(d.get("cash_all", [])[:8])]
    elif not d.get("category"):
        rows += [[InlineKeyboardButton(text=f"📂 {n}"[:60], callback_data=f"exp:a:{key}:{i}")] for i, n in enumerate(d.get("cat_top", [])[:6])]
        rows.append([InlineKeyboardButton(text="✏️ Інша стаття — у CRM", url=f"{settings.public_url.rstrip('/')}/finance/ops")])
    rows.append([InlineKeyboardButton(text="✖️ Не вносити", callback_data=f"exp:x:{key}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _exp_ask(d: dict) -> str:
    from html import escape
    if d.get("kind") == "transfer":
        head = f"🔁 Переміщення <b>{d['amount']:,.2f} ₴</b>".replace(",", " ") + (f" · {escape(d['comment'])}" if d.get("comment") else "")
        if not d.get("cashbox"):
            return head + (f"\nНа: {escape(d['cashbox_to'])}" if d.get("cashbox_to") else "") + "\nЗ якої каси? Оберіть кнопкою:"
        return head + f"\nЗ: {escape(d['cashbox'])}\nНа яку касу? Оберіть кнопкою:"
    head = f"🧾 Видаток <b>{d['amount']:,.2f} ₴</b>".replace(",", " ") + (f" · {escape(d['comment'])}" if d.get("comment") else "")
    if not d.get("cashbox"):
        return head + "\nЗ якої каси? Оберіть кнопкою:"
    return head + f"\nКаса: {escape(d['cashbox'])}\nЯка стаття? Оберіть кнопкою:"


def _undo_kb(op_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Скасувати", callback_data=f"exp:u:{op_id}:0"),
                                                   InlineKeyboardButton(text="Фіноперації", url=f"{settings.public_url.rstrip('/')}/finance/ops")]])


@pay_router.message(F.text, _exp_text)
async def exp_message(msg: Message):
    from ..services import chat_expenses as ce, cashboxes
    who = (msg.from_user.full_name if msg.from_user else "") or ""
    with SessionLocal() as db:
        d = ce.parse(db, msg.text)
        u = ce.user_by_tg(db, msg.from_user.id) if msg.from_user else None
        ref = f"tg:{msg.chat.id}:{msg.message_id}"
        if ce.ready(d):
            x = ce.create(db, d, who, u.id if u else None, ref)
            await msg.reply(ce.text_done(x), reply_markup=_undo_kb(x.id))
            return
        key = f"{msg.chat.id}_{msg.message_id}"
        if not d["cat_top"] and d.get("kind") != "transfer":
            from ..services import fin_articles
            d["cat_top"] = fin_articles.names(db, "expense")[:6]
        d.update(author=who, uid=u.id if u else None, ref=ref, cash_all=d["cash_top"] or cashboxes.names(db)[:8])
        ce.pend_put(db, key, d)
    await msg.reply(_exp_ask(d), reply_markup=_exp_kb(key, d))


@pay_router.callback_query(F.data.startswith("exp:"))
async def exp_callback(cb: CallbackQuery):
    from ..services import chat_expenses as ce
    _, act, key, idx = (cb.data or "").split(":", 3)
    with SessionLocal() as db:
        if act == "u":
            x = ce.cancel(db, int(key))
            await cb.answer("Скасовано")
            try:
                await cb.message.edit_text(f"↩️ Операцію CRM-{key} скасовано" + (f" ({x.amount:,.2f} ₴ · {x.category})".replace(",", " ") if x else ""))
            except Exception:  # noqa: BLE001
                pass
            return
        d = ce.pend_get(db, key)
        if not d:
            await cb.answer("Уже оброблено", show_alert=True)
            return
        if act == "x":
            ce.pend_del(db, key)
            await cb.answer("Не вношу")
            try:
                await cb.message.edit_text("✖️ Видаток не внесено")
            except Exception:  # noqa: BLE001
                pass
            return
        i = int(idx) if idx.isdigit() else -1
        if act == "c" and 0 <= i < len(d.get("cash_all", [])):
            d["cashbox"] = d["cash_all"][i]
        elif act == "t" and 0 <= i < len(d.get("cash_all", [])):
            d["cashbox_to"] = d["cash_all"][i]
        elif act == "a" and 0 <= i < len(d.get("cat_top", [])):
            d["category"] = d["cat_top"][i]
        if ce.ready(d):
            x = ce.create(db, d, d.get("author"), d.get("uid"), d.get("ref"))
            ce.pend_del(db, key)
            text, kb = ce.text_done(x), _undo_kb(x.id)
        else:
            if not d.get("cat_top") and d.get("kind") != "transfer":
                from ..services import fin_articles
                d["cat_top"] = fin_articles.names(db, "expense")[:6]
            ce.pend_put(db, key, d)
            text, kb = _exp_ask(d), _exp_kb(key, d)
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass


@pay_router.callback_query(F.data.startswith("pay:"))
async def pay_callback(cb: CallbackQuery):
    import asyncio
    from ..services import pay_requests as prq
    from ..models import PayRequest
    parts = (cb.data or "").split(":")
    act, pid = parts[1], int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    with SessionLocal() as db:
        if str(cb.from_user.id) not in prq.owner_tg_ids(db):
            await cb.answer("Погоджує лише власник", show_alert=True)
            return
        owner = db.scalar(select(__import__("app.models", fromlist=["User"]).User).where(
            __import__("app.models", fromlist=["User"]).User.telegram_chat_id == str(cb.from_user.id)))
        uid = owner.id if owner else None
        pr = db.get(PayRequest, pid)
        if not pr:
            await cb.answer("Рахунок не знайдено", show_alert=True)
            return
        if act == "fop" and len(parts) > 3:
            f = next((x for x in prq.fops(db) if x[:20] == parts[3]), None)
            if f:
                pr.fop, pr.sender_iban, pr.status, pr.error = f, prq.sender_iban(db, f), "new", None
                db.commit()
            await cb.answer(f"Платник: ФОП {pr.fop}")
        elif act == "no":
            prq.reject(db, pr, uid)
            await cb.answer("Відхилено")
        elif act == "ok":
            await cb.answer("Створюю чернетку в monobank…")
            await asyncio.to_thread(prq.approve, db, pr, uid)
        text, kb = prq.card_text(db, pr), pay_keyboard(pr)
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        pass


@pay_router.callback_query(F.data.startswith("ads:"))
async def ads_callback(cb: CallbackQuery):
    """Погодження змін у Google Ads кнопками: ads:ok:<id> | ads:all:<id.id…> | ads:none:<id.id…>."""
    import json as _json
    from ..services import ads_changes as ac, pay_requests as prq
    from ..models import AdsChange, User
    parts = (cb.data or "").split(":")
    act = parts[1] if len(parts) > 1 else ""
    ids = [int(x) for x in (parts[2] if len(parts) > 2 else "").split(".") if x.isdigit()]
    with SessionLocal() as db:
        if str(cb.from_user.id) not in prq.owner_tg_ids(db):
            await cb.answer("Погоджує лише власник", show_alert=True)
            return
        u = db.scalar(select(User).where(User.telegram_chat_id == str(cb.from_user.id)))
        n = 0
        for i in ids:
            ch = db.get(AdsChange, i)
            if ch:
                n += ac.decide(db, ch, act in ("ok", "all"), u.id if u else None)
        await cb.answer(("Погоджено" if act in ("ok", "all") else "Відхилено") + (f": {n}" if n > 1 else ""))
        chs = ac.tg_refresh(db, str(cb.message.chat.id), cb.message.message_id)
        text, kb = ac.tg_text(chs), ac.tg_kb(chs)
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup.model_validate(kb), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        pass


async def pay_notify(bot: Bot, changed) -> None:
    """Зміна стану чернетки (оплачено / відхилено в monobank) — оновити картку в чаті."""
    from ..services import pay_requests as prq
    for pr in changed:
        if not (pr.tg_chat and pr.tg_card):
            continue
        with SessionLocal() as db:
            text = prq.card_text(db, pr)
        try:
            await bot.edit_message_text(text, chat_id=pr.tg_chat, message_id=pr.tg_card, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass


def allowed(msg: Message) -> bool:
    chats = settings.allowed_chats | ({settings.telegram_admin_chat_id} if settings.telegram_admin_chat_id else set())
    return not chats or str(msg.chat.id) in chats


async def _download(bot: Bot, file_id: str) -> bytes:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


def plan_summary(db, plan) -> str:
    ps = compute_plan(db, plan)
    lines = [f"✅ <b>{plan.patient.full_name}</b> — бюті-план {plan.session_at:%d.%m.%Y}" if plan.session_at
             else f"✅ <b>{plan.patient.full_name}</b>"]
    if plan.doctor:
        lines.append(f"Лікар: {plan.doctor.name}")
    for st in ps.items:
        mark = "💉" if st.item.kind == "procedure" else "🧴"
        lines.append(f"{mark} {st.item.name[:60]} — {st.done}/{st.item.qty}"
                     + (f" (запис: {st.next_booked:%d.%m})" if st.next_booked else ""))
    lines.append(f"\nСума плану: <b>{ps.planned_sum:,.0f} ₴</b> · реалізовано {ps.realization_pct}%".replace(",", " "))
    lines.append(f"Записана: {'так, ' + ps.next_booking.strftime('%d.%m %H:%M') if ps.is_booked else 'ні'}")
    lines.append(f'<a href="{settings.public_url.rstrip("/")}/patients/{plan.patient_id}">Відкрити картку</a>')
    return "\n".join(lines)


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"ID цього чату: <code>{msg.chat.id}</code>")


@router.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer("MOLODIST CRM. Надсилайте PDF бюті-планів з Observ та фото пацієнтів (з підписом ПІБ).\n"
                     "/plan Прізвище — статус плану\n/digest — нагадування зараз\n/id — ID чату")


@router.message(F.document)
async def on_document(msg: Message, bot: Bot):
    if not allowed(msg):
        return
    doc = msg.document
    name = doc.file_name or "file"
    data = await _download(bot, doc.file_id)
    with SessionLocal() as db:
        if name.lower().endswith(".pdf") or doc.mime_type == "application/pdf":
            plan, _, err = ingest.ingest_pdf(db, data, name, str(msg.chat.id))
            if isinstance(err, ingest.Attached):
                await msg.reply(str(err))
            elif err:
                await msg.reply(f"⚠️ Файл збережено, але не розпізнано як бюті-план: {err}")
            else:
                await msg.reply(plan_summary(db, plan), disable_web_page_preview=True)
        elif (doc.mime_type or "").startswith("image/"):
            f = ingest.ingest_photo(db, data, name, msg.caption, str(msg.chat.id))
            await _photo_reply(msg, db, f)


@router.message(F.photo)
async def on_photo(msg: Message, bot: Bot):
    if not allowed(msg):
        return
    ph = msg.photo[-1]
    data = await _download(bot, ph.file_id)
    with SessionLocal() as db:
        f = ingest.ingest_photo(db, data, f"photo_{ph.file_unique_id}.jpg", msg.caption, str(msg.chat.id))
        await _photo_reply(msg, db, f)


async def _photo_reply(msg: Message, db, f: PatientFile):
    if f.patient_id:
        await msg.reply(f"📷 Додано в картку: {f.patient.full_name}")
    else:
        await msg.reply("📷 Фото збережено, але пацієнта не визначено. Відповідайте на фото: /to Прізвище Ім'я "
                        "(або привʼяжіть у веб-CRM → «Нерозібрані файли»).")
        f.caption = (f.caption or "") + f" [tg_msg:{msg.message_id}]"
        db.commit()


@router.message(Command("to"))
async def cmd_to(msg: Message):
    if not allowed(msg) or not msg.reply_to_message:
        return await msg.reply("Відповідайте командою /to Прізвище Ім'я на повідомлення з фото.")
    query = (msg.text or "").split(maxsplit=1)[1:] or [""]
    with SessionLocal() as db:
        pat = ingest.guess_patient(db, query[0])
        if not pat:
            return await msg.reply("Не знайшов пацієнта з таким ПІБ/телефоном у CRM.")
        tag = f"[tg_msg:{msg.reply_to_message.message_id}]"
        f = db.scalar(select(PatientFile).where(PatientFile.patient_id.is_(None),
                                                PatientFile.caption.contains(tag)))
        if not f:
            return await msg.reply("Не знайшов це фото серед нерозібраних.")
        f.patient_id = pat.id
        f.caption = f.caption.replace(tag, "").strip() or None
        db.commit()
        await msg.reply(f"📷 Привʼязано до: {pat.full_name}")


@router.message(Command("plan"))
async def cmd_plan(msg: Message):
    if not allowed(msg):
        return
    q = (msg.text or "").split(maxsplit=1)
    if len(q) < 2:
        return await msg.reply("Формат: /plan Прізвище")
    from ..models import Patient
    from ..parsers.observ import norm
    with SessionLocal() as db:
        words = set(norm(q[1]).split())
        hits = [p for p in db.scalars(select(Patient)) if words <= set(norm(p.full_name).split())]
        if not hits:
            return await msg.reply("Не знайдено.")
        for p in hits[:3]:
            if p.plans:
                await msg.reply(plan_summary(db, p.plans[0]), disable_web_page_preview=True)
            else:
                await msg.reply(f"{p.full_name}: бюті-плану немає")


@router.message(Command("digest"))
async def cmd_digest(msg: Message, bot: Bot):
    if not allowed(msg):
        return
    n = await send_digest(bot, force_chat=str(msg.chat.id), only_new=False)
    if not n:
        await msg.reply("Нагадувань немає 👌")


async def send_digest(bot: Bot, force_chat: str | None = None, only_new: bool = True) -> int:
    chat = force_chat or settings.telegram_admin_chat_id
    if not chat:
        log.warning("TELEGRAM_ADMIN_CHAT_ID не задано — нагадування не надіслано")
        return 0
    with SessionLocal() as db:
        rems = reminders.pending(db) if only_new else reminders.collect(db)
        for text in reminders.digest_messages(rems):
            await bot.send_message(chat, text, disable_web_page_preview=True)
        if only_new:
            reminders.mark_sent(db, rems)
    return len(rems)


biz_router = Router()


@biz_router.business_connection()
async def biz_connection(conn) -> None:
    """Власник підключив / відключив бота в «Telegram для бізнесу» акаунта клініки."""
    from ..services import tg_business as tb
    data = conn.model_dump(mode="json", by_alias=True, exclude_none=True)
    def run():
        with SessionLocal() as db:
            return tb.on_connection(db, data)
    c = await asyncio.to_thread(run)
    log.info("Telegram Business: %s", c.get("log", [{}])[0].get("note"))


@biz_router.business_message()
async def biz_message(message: Message) -> None:
    """Повідомлення в особистих чатах акаунта клініки → центр спілкування CRM."""
    from ..services import tg_business as tb
    data = message.model_dump(mode="json", by_alias=True, exclude_none=True)
    def run():
        with SessionLocal() as db:
            tb.ingest(db, data)
    try:
        await asyncio.to_thread(run)
    except Exception as e:      # noqa: BLE001
        log.warning("tg business ingest: %s", e)


def make_bot() -> tuple[Bot, Dispatcher]:
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(pay_router)
    dp.include_router(biz_router)
    dp.include_router(router)
    return bot, dp
