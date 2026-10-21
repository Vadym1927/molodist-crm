"""Telegram-бот MOLODIST CRM.

• Додайте бота в робочі чати (з PDF бюті-планів і фото) та в чат адмінів.
• /id — показує ID чату (вписати в .env: TELEGRAM_ALLOWED_CHAT_IDS / TELEGRAM_ADMIN_CHAT_ID).
• PDF Observ -> бот створює/оновлює бюті-план і відповідає коротким підсумком.
• Фото з підписом «Прізвище Ім'я» (або телефон) -> фото потрапляє в картку пацієнта.
  Фото без підпису: відповісти на нього командою /to Прізвище Ім'я.
• /plan Прізвище — статус плану; /digest — надіслати нагадування зараз.
"""
from __future__ import annotations

import io
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from ..config import settings
from ..models import SessionLocal, PatientFile
from ..services import ingest, reminders
from ..services.kpi import compute_plan
from sqlalchemy import select

log = logging.getLogger("tg")
router = Router()


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
            if err:
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


def make_bot() -> tuple[Bot, Dispatcher]:
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
