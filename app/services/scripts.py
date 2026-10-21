"""Скрипти для адміністраторів: готовий текст дзвінка і повідомлення під бюті-план конкретної пацієнтки.

Шаблони і пояснення по процедурах редагує власник (розділ «Скрипти»); тут — дефолти для першого запуску.
Підстановки в шаблонах — у фігурних дужках, напр. {імʼя}. Медичні нотатки лікаря в скрипти НЕ потрапляють.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ScriptTemplate, ProcedureNote, Task, Patient
from .kpi import PlanStatus, compute_plan

PLACEHOLDERS = {
    "імʼя": "Імʼя пацієнтки (без прізвища)",
    "лікар": "Лікар з бюті-плану",
    "адмін": "Імʼя адміністратора, що телефонує",
    "клініка": "Назва клініки",
    "план": "Назва плану, напр. Beauty Plan Anti Age",
    "дата_плану": "Дата консультації",
    "процедура": "Наступна процедура з плану",
    "виконано": "Скільки процедур курсу вже зроблено",
    "всього": "Скільки процедур у курсі",
    "залишилось": "Скільки процедур лишилось",
    "інтервал": "Рекомендований інтервал, днів",
    "остання_дата": "Дата останньої процедури",
    "чому_важливо": "Пояснення з довідника процедур",
    "результат": "Що дасть курс (з довідника процедур)",
    "засіб": "Засіб домашнього догляду, що закінчується",
    "посилання": "Посилання на засіб у molodist.store",
    "прогрес": "Відсоток реалізації плану",
    "темп": "Фраза під прогрес: старт / гарний старт / більше половини",
}

DEFAULT_TEMPLATES = [
    ("not_booked", "call", "Дзвінок: запис на наступну процедуру",
     "Добрий день, {імʼя}! Це {адмін}, клініка {клініка}.\n"
     "Телефоную щодо вашого бюті-плану, який склала {лікар} {дата_плану}.\n"
     "Ви вже пройшли {виконано} з {всього} процедур — {темп}\n"
     "Наступна за планом — {процедура}. {чому_важливо}\n"
     "Підберемо зручний час? Можу запропонувати найближчі вільні вікна.\n"
     "— Якщо «подумаю»: розумію. Скажу лише, що {результат} Давайте я попередньо забронюю час, а за день до "
     "візиту нагадаю — якщо плани зміняться, просто перенесемо.\n"
     "— Якщо «дорого»: можемо розглянути оплату курсу частинами або пакетом — так вигідніше."),
    ("not_booked", "message", "Повідомлення: запис на наступну процедуру",
     "{імʼя}, добрий день! Це клініка {клініка} 🌿\n"
     "За вашим бюті-планом наступна процедура — {процедура} (виконано {виконано} з {всього}). "
     "{чому_важливо}\n"
     "Підказати вільний час на цьому тижні?"),
    ("care_ending", "call", "Дзвінок: повторний домашній догляд",
     "Добрий день, {імʼя}! Це {адмін}, клініка {клініка}.\n"
     "За нашими розрахунками, {засіб} у вас скоро закінчиться. Як вам засіб, чи все підходить?\n"
     "Щоб не було перерви в догляді, можемо відкласти для вас новий — заберете на наступному візиті "
     "або замовите з доставкою: {посилання}"),
    ("care_ending", "message", "Повідомлення: повторний домашній догляд",
     "{імʼя}, добрий день! Нагадуємо: {засіб} з вашого домашнього догляду скоро закінчиться.\n"
     "Щоб результат процедур тримався, важливо не робити перерв. Відкласти для вас новий? "
     "Або замовлення з доставкою: {посилання}"),
    ("follow_up", "call", "Дзвінок: звʼязатися з пацієнткою",
     "Добрий день, {імʼя}! Це {адмін}, клініка {клініка}. Як ви себе почуваєте після процедур? "
     "Хотіла уточнити, чи все гаразд і чи є питання до лікаря {лікар}."),
    ("follow_up", "message", "Повідомлення: звʼязатися з пацієнткою",
     "{імʼя}, добрий день! Це клініка {клініка}. Як ви? Якщо є питання щодо догляду чи плану — пишіть, допоможемо."),
]

DEFAULT_NOTES = [
    ("IPL|фотоомолодж|фототерап",
     "IPL працює курсом: кожна наступна процедура підсилює попередню, а інтервал 3–4 тижні — саме той час, "
     "коли шкіра встигає відновитися і ще «памʼятає» попередній імпульс. Якщо перерва затягується, частину "
     "ефекту доводиться набирати знову.",
     "рівний тон, менше почервонінь і пігментації, а результат курсу тримається місяцями."),
    ("гігієн|чистк",
     "Регулярне очищення раз на 4–6 тижнів не дає порам знову закупорюватися і готує шкіру до наступних процедур плану.",
     "чиста, рівна шкіра, на якій краще працюють і процедури, і домашній догляд."),
    ("біоревіт|мезо|rejuran|реджуран|полінукл|plenhyage|jalupro|nithya|vitaran|вітаран",
     "Біоревіталізація дає накопичувальний ефект: процедури з інтервалом 2–3 тижні запускають відновлення шкіри "
     "поетапно. Пропуск інтервалу знижує результат курсу.",
     "зволожена, щільна шкіра з помітно кращою текстурою."),
    ("пілінг|prx|peel",
     "Пілінги роблять курсом з інтервалом 10–14 днів: шкіра оновлюється поступово і безпечно, без пересушування.",
     "оновлена, сяйлива шкіра і рівніший тон."),
    ("ботул|ботокс|dysport|xeomin|relatox",
     "Ефект ботулінотерапії триває 3–5 місяців; повторна корекція вчасно не дає мімічним зморшкам знову закріпитися.",
     "свіже, відпочите обличчя без глибоких мімічних зморшок."),
    ("rf|ліфтинг|smas|hifu|мікрострум",
     "Апаратний ліфтинг працює курсом: колаген утворюється поступово, тож важливо пройти всі сеанси в рекомендованому ритмі.",
     "підтягнутий овал і щільніша шкіра."),
]


def seed_defaults(db: Session):
    if not db.scalar(select(ScriptTemplate).limit(1)):
        for kind, ch, title, text in DEFAULT_TEMPLATES:
            db.add(ScriptTemplate(kind=kind, channel=ch, title=title, text=text))
    if not db.scalar(select(ProcedureNote).limit(1)):
        for kw, why, benefit in DEFAULT_NOTES:
            db.add(ProcedureNote(keyword=kw, why=why, benefit=benefit))
    db.commit()


def note_for(db: Session, procedure: str | None) -> ProcedureNote | None:
    if not procedure:
        return None
    low = procedure.lower()
    best, best_len = None, 0
    for n in db.scalars(select(ProcedureNote)):
        for k in n.keyword.split("|"):
            k = k.strip().lower()
            # ключ — з початку слова («ліфтинг» не спрацює всередині «плазмоліфтинг»); найдовший збіг — найточніший
            if k and len(k) > best_len and re.search(r"(?<![\wʼ'])" + re.escape(k), low):
                best, best_len = n, len(k)
    return best


def first_name(full: str) -> str:
    """«Олена Даниленко» / «Даниленко Олена» → «Олена» (за довідником жіночих/чоловічих імен — евристика)."""
    parts = full.split()
    if len(parts) < 2:
        return full
    # прізвища часто закінчуються на -енко, -ук, -юк, -ська, -цька, -ова, -ева, -іна, -ич
    sur = re.compile(r"(енко|ук|юк|ськ[аий]|цьк[аий]|ов[аи]?|ев[аи]?|єв[аи]?|ін[аи]?|ич|ак|як|чук|ко)$", re.I)
    if sur.search(parts[0]) and not sur.search(parts[1]):
        return parts[1]
    return parts[0]


def context(db: Session, patient: Patient, ps: PlanStatus | None = None, task: Task | None = None,
            admin_name: str | None = None, item_id: int | None = None) -> dict:
    ctx = {k: "" for k in PLACEHOLDERS}
    ctx.update(імʼя=first_name(patient.full_name), клініка="MOLODIST", адмін=(admin_name or "адміністратор").split()[0],
               лікар=patient.doctor.name if patient.doctor else "ваш лікар")
    if ps is None and patient.plans:
        ps = compute_plan(db, patient.plans[0])
    if ps:
        pl = ps.plan
        nxt = ps.next_procedure
        ctx.update(план=pl.title or "бюті-план", дата_плану=f"{pl.session_at:%d.%m}" if pl.session_at else "",
                   лікар=pl.doctor.name if pl.doctor else ctx["лікар"], прогрес=f"{ps.realization_pct:g}%",
                   виконано=str(ps.proc_done), всього=str(ps.proc_planned),
                   залишилось=str(max(ps.proc_planned - ps.proc_done, 0)))
        ratio = ps.proc_done / ps.proc_planned if ps.proc_planned else 0
        ctx["темп"] = ("ви на самому старті, і саме зараз важливо задати ритм курсу." if ps.proc_done == 0 else
                       "гарний старт, тепер головне — не збавляти темп." if ratio < 0.5 else
                       "ви вже більше ніж на половині шляху, чудовий темп!")
        if nxt:
            note = note_for(db, nxt.item.name)
            last = max(filter(None, [i.last_done for i in ps.items if i.item.kind == "procedure"]), default=None)
            ctx.update(процедура=nxt.item.name, інтервал=str(nxt.item.interval_days or ""),
                       остання_дата=f"{last:%d.%m}" if last else "",
                       чому_важливо=note.why if note else "Процедури плану працюють курсом — важливо зберегти рекомендований інтервал.",
                       результат=(note.benefit if note and note.benefit else "курс дає накопичувальний результат.")
                       .rstrip(".") + ".")
        # засіб: з задачі «повторний догляд» або перший, що закінчується
        care = None
        if item_id:
            care = next((st for st in ps.items if st.item.id == item_id), None)
        if not care:
            care = next((st for st in ps.items if st.item.kind == "product" and st.last_done), None) or \
                   next((st for st in ps.items if st.item.kind == "product"), None)
        if care:
            ctx.update(засіб=care.item.name, посилання=care.item.url or "molodist.store")
    return ctx


def render(text: str, ctx: dict) -> str:
    out = re.sub(r"\{([^{}]+)\}", lambda m: str(ctx.get(m.group(1).strip(), m.group(0))), text)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


@dataclass
class Script:
    title: str
    channel: str
    text: str


def for_task(db: Session, task: Task, admin_name: str | None = None) -> list[Script]:
    if not task.patient_id:
        return []
    patient = db.get(Patient, task.patient_id)
    kind = task.kind if task.kind in ("not_booked", "care_ending") else "follow_up"
    item_id = None
    if task.kind == "care_ending" and task.source_key:
        m = re.match(r"care:(\d+):", task.source_key)
        item_id = int(m.group(1)) if m else None
    ps = None
    if task.plan_id:
        from ..models import BeautyPlan
        plan = db.get(BeautyPlan, task.plan_id)
        ps = compute_plan(db, plan) if plan else None
    ctx = context(db, patient, ps, task, admin_name, item_id)
    tpls = db.scalars(select(ScriptTemplate).where(ScriptTemplate.kind == kind, ScriptTemplate.active.is_(True))
                      .order_by(ScriptTemplate.channel)).all()
    return [Script(t.title, t.channel, render(t.text, ctx)) for t in tpls]


def for_patient(db: Session, patient: Patient, kind: str = "not_booked", admin_name: str | None = None) -> list[Script]:
    ctx = context(db, patient, None, None, admin_name)
    tpls = db.scalars(select(ScriptTemplate).where(ScriptTemplate.kind == kind, ScriptTemplate.active.is_(True))
                      .order_by(ScriptTemplate.channel)).all()
    return [Script(t.title, t.channel, render(t.text, ctx)) for t in tpls]
