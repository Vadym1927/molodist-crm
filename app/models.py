from __future__ import annotations

from datetime import datetime, date
from sqlalchemy import (create_engine, String, Integer, Float, Text, DateTime, Date, ForeignKey,
                        Boolean, JSON, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Doctor(Base):
    __tablename__ = "doctors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    cleverbox_id: Mapped[int | None] = mapped_column(Integer, index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)          # працює (False = звільнений)
    # картка спеціаліста: частина синхронізується з CleverBox (/staff), решта ведеться вручну
    position: Mapped[str | None] = mapped_column(String(200))           # посада / спеціалізація
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(200))
    photo_url: Mapped[str | None] = mapped_column(String(500))
    color: Mapped[str | None] = mapped_column(String(16))               # колір у календарі
    bio: Mapped[str | None] = mapped_column(Text)                       # досвід, сертифікати, процедури
    work_days: Mapped[str | None] = mapped_column(String(100))          # графік, напр. «Пн–Пт 10–19»
    source: Mapped[str | None] = mapped_column(String(20))              # cleverbox | manual | plan
    in_calendar: Mapped[bool | None] = mapped_column(Boolean, default=True)
    extra: Mapped[dict | None] = mapped_column(JSON)                      # власні поля картки («Поля співробітників»)
    sale_initiator: Mapped[bool | None] = mapped_column(Boolean)          # показувати як ініціатора продажу в касі (None = так)
    status_locked: Mapped[bool | None] = mapped_column(Boolean, default=False)   # статус змінено вручну — синк не чіпає
    cb_seen_at: Mapped[datetime | None] = mapped_column(DateTime)       # востаннє був у списку /staff
    fired_at: Mapped[date | None] = mapped_column(Date)


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(64), index=True)
    cleverbox_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    avatar_file_id: Mapped[int | None] = mapped_column(Integer)          # фото пацієнта (PatientFile) — аватар у картці візиту
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # статистика клієнта з CleverBox (оновлюється синком)
    birthday: Mapped[date | None] = mapped_column(Date)
    cb_visits: Mapped[int | None] = mapped_column(Integer)          # завершених візитів
    cb_total: Mapped[float | None] = mapped_column(Float)           # LTV — сума надходжень
    cb_average: Mapped[float | None] = mapped_column(Float)         # середній чек
    cb_last_visit: Mapped[datetime | None] = mapped_column(DateTime)
    cb_status: Mapped[str | None] = mapped_column(String(100))
    cb_mark: Mapped[str | None] = mapped_column(String(20))         # vip | bad
    source: Mapped[str | None] = mapped_column(String(100))         # джерело залучення
    cb_registered_at: Mapped[datetime | None] = mapped_column(DateTime)   # дата додавання клієнта в CleverBox
    cb_extra: Mapped[dict | None] = mapped_column(JSON)              # усі інші поля картки CleverBox (як є)
    cb_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    cb_first_visit: Mapped[datetime | None] = mapped_column(DateTime)
    cb_note: Mapped[str | None] = mapped_column(Text)                # «Замітка» з CleverBox
    cb_anamnesis: Mapped[str | None] = mapped_column(Text)           # «Медичний анамнез» — лише для ролей з medical
    instagram: Mapped[str | None] = mapped_column(String(100))
    viber: Mapped[str | None] = mapped_column(String(64))
    telegram_contact: Mapped[str | None] = mapped_column(String(100))
    preferred_doctor: Mapped[str | None] = mapped_column(String(200))
    drink: Mapped[str | None] = mapped_column(String(200))           # напій, якому надає перевагу
    directions: Mapped[list | None] = mapped_column(JSON)            # напрямки: ЛЕ, Косметолог, Трихолог…
    no_repeat_call: Mapped[bool | None] = mapped_column(Boolean)     # не дзвонити при повторному візиті
    delivery: Mapped[str | None] = mapped_column(String(300))        # дані для відправки НП
    # розширений профіль клієнта (ручне ведення або імпорт з CRM)
    email: Mapped[str | None] = mapped_column(String(200))
    gender: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(300))
    communication_channel: Mapped[str | None] = mapped_column(String(30))
    client_groups: Mapped[list | None] = mapped_column(JSON)
    discount_pct: Mapped[float | None] = mapped_column(Float)          # персональна знижка, %
    disc_products_pct: Mapped[float | None] = mapped_column(Float)     # персональна знижка на товари, % (порожньо — discount_pct)
    disc_services_pct: Mapped[float | None] = mapped_column(Float)     # персональна знижка на послуги, %
    med_allergies: Mapped[str | None] = mapped_column(Text)           # алергії (лише medical)
    med_conditions: Mapped[str | None] = mapped_column(Text)          # хронічні захворювання, препарати, вагітність…
    med_contra: Mapped[str | None] = mapped_column(Text)              # протипоказання
    phototype: Mapped[str | None] = mapped_column(String(10))         # фототип за Фіцпатриком
    cb_balance: Mapped[float | None] = mapped_column(Float)
    cb_bonus: Mapped[float | None] = mapped_column(Float)
    # звʼязок з ботом для пацієнтів (OpenClaw) і згода на розсилки
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100))
    telegram_linked_at: Mapped[datetime | None] = mapped_column(DateTime)
    marketing_opt_out: Mapped[bool | None] = mapped_column(Boolean, default=False)   # відписка від розсилок
    opt_out_at: Mapped[datetime | None] = mapped_column(DateTime)

    doctor: Mapped[Doctor | None] = relationship()
    plans: Mapped[list["BeautyPlan"]] = relationship(back_populates="patient", order_by="BeautyPlan.session_at.desc()")
    visits: Mapped[list["Visit"]] = relationship(back_populates="patient", order_by="Visit.starts_at")
    sales: Mapped[list["ProductSale"]] = relationship(back_populates="patient", order_by="ProductSale.sold_at")
    files: Mapped[list["PatientFile"]] = relationship(back_populates="patient", order_by="PatientFile.created_at.desc()")


class BeautyPlan(Base):
    __tablename__ = "beauty_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"))
    title: Mapped[str | None] = mapped_column(String(200))
    session_at: Mapped[datetime | None] = mapped_column(DateTime)
    complaints: Mapped[str | None] = mapped_column(Text)
    medical_notes: Mapped[str | None] = mapped_column(Text)     # коментарі лікаря (не йдуть у ТГ)
    routine_comment: Mapped[str | None] = mapped_column(Text)
    total_planned: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|completed|cancelled
    source_file: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    patient: Mapped[Patient] = relationship(back_populates="plans")
    doctor: Mapped[Doctor | None] = relationship()
    items: Mapped[list["PlanItem"]] = relationship(back_populates="plan", cascade="all, delete-orphan",
                                                    order_by="PlanItem.id")


class PlanItem(Base):
    __tablename__ = "plan_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("beauty_plans.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))     # procedure|product
    name: Mapped[str] = mapped_column(String(300))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    volume: Mapped[str | None] = mapped_column(String(30))
    usage: Mapped[list | None] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(String(500))
    interval_days: Mapped[int | None] = mapped_column(Integer)   # інтервал між процедурами
    supply_days: Mapped[int | None] = mapped_column(Integer)     # на скільки днів вистачає засобу
    # ручне ведення (якщо CleverBox не зіставив): к-сть виконано/куплено
    manual_done: Mapped[int] = mapped_column(Integer, default=0)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))   # каталог (для засобів)

    plan: Mapped[BeautyPlan] = relationship(back_populates="items")


class Alias(Base):
    """Зіставлення назви з бюті-плану з послугою/товаром CleverBox."""
    __tablename__ = "aliases"
    __table_args__ = (UniqueConstraint("kind", "plan_name_norm"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))           # procedure|product
    plan_name_norm: Mapped[str] = mapped_column(String(300))
    cb_ids: Mapped[list] = mapped_column(JSON, default=list)   # id послуг/товарів CleverBox
    cb_names: Mapped[list] = mapped_column(JSON, default=list)


class CbService(Base):
    __tablename__ = "cb_services"
    id: Mapped[int] = mapped_column(primary_key=True)        # = CleverBox service id
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(200))
    price: Mapped[float | None] = mapped_column(Float)
    time_min: Mapped[int | None] = mapped_column(Integer)
    check_name: Mapped[str | None] = mapped_column(String(300))     # з Excel «Послуги» CleverBox
    sms_name: Mapped[str | None] = mapped_column(String(300))
    direction: Mapped[str | None] = mapped_column(String(200))      # «Направленість»: Ін'єкційна, Лазерна епіляція…
    techcard: Mapped[str | None] = mapped_column(String(200))       # техкартка — спільні норми списання
    cost_fixed: Mapped[float | None] = mapped_column(Float)          # фіксована собівартість послуги (для окладів), ₴
    techcard_id: Mapped[int | None] = mapped_column(Integer, index=True)   # техкарта CRM (TechCard)
    duration: Mapped[int | None] = mapped_column(Integer)            # тривалість, хв (редагується в CRM)
    active: Mapped[bool | None] = mapped_column(Boolean)              # False — прихована з каси й запису
    source: Mapped[str | None] = mapped_column(String(10))            # crm — створена в CRM
    # картка послуги (як у CleverBox) + планувальник ціни
    cb_price: Mapped[float | None] = mapped_column(Float)              # ціна в CleverBox (з останнього синку)
    price_locked: Mapped[bool | None] = mapped_column(Boolean)         # ціну задано в CRM — синк CleverBox її не перезаписує
    price_set_on: Mapped[date | None] = mapped_column(Date)            # дата формування ціни продажу
    repeat_days: Mapped[int | None] = mapped_column(Integer)           # повторний візит через N днів
    prepay: Mapped[float | None] = mapped_column(Float)                # фіксована передоплата, ₴
    color: Mapped[str | None] = mapped_column(String(10))              # колір у календарі
    online: Mapped[bool | None] = mapped_column(Boolean)               # доступна для онлайн-запису
    online_name: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)              # опис для онлайн-запису
    specialist_note: Mapped[str | None] = mapped_column(Text)          # інформація для спеціаліста
    doctor_ids: Mapped[list | None] = mapped_column(JSON)              # хто надає послугу
    allow_price_change: Mapped[bool | None] = mapped_column(Boolean)   # дозволити зміну ціни при оплаті
    zp_fixed: Mapped[float | None] = mapped_column(Float)              # ЗП лікаря за послугу вручну (інакше — за окладом)


class EnergyDay(Base):
    """Звіт Deye (лише власник): день роботи станції — з Deye Cloud або вручну."""
    __tablename__ = "energy_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    discharge_kwh: Mapped[float | None] = mapped_column(Float)         # віддано з батарей
    charge_kwh: Mapped[float | None] = mapped_column(Float)            # на заряд
    battery_hours: Mapped[float | None] = mapped_column(Float)         # час на інверторі (без мережі)
    business_hours: Mapped[float | None] = mapped_column(Float)        # з них у робочий час 09:00–21:00
    fuel_price: Mapped[float | None] = mapped_column(Float)            # WOG А-95 на цей день, ₴/л (вручну для минулих дат)
    source: Mapped[str | None] = mapped_column(String(12))             # manual | deye | import
    raw: Mapped[dict | None] = mapped_column(JSON)                     # що віддав Deye (для перевірки)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)


class FuelPrice(Base):
    """Ціна пального на день (WOG А-95 з api.wog.ua — фіксується щодня)."""
    __tablename__ = "fuel_prices"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    price: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str | None] = mapped_column(String(40))


class ServiceLog(Base):
    """Історія послуги: зміни ціни, назви, техкарти, тривалості (CRM і синк CleverBox)."""
    __tablename__ = "service_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    field: Mapped[str] = mapped_column(String(30))                     # price | name | techcard | duration | created | ...
    old: Mapped[str | None] = mapped_column(String(300))
    new: Mapped[str | None] = mapped_column(String(300))
    user_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(String(12))             # crm | cleverbox


class Visit(Base):
    """Візит/надана послуга з CleverBox. status: booked|done|cancelled."""
    __tablename__ = "visits"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))           # cb_visit | cb_service_sale | manual
    external_id: Mapped[str] = mapped_column(String(64))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    service_id: Mapped[int | None] = mapped_column(Integer)
    service_name: Mapped[str | None] = mapped_column(String(300))
    master_name: Mapped[str | None] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)      # кінець запису (з CleverBox), якщо відомий
    status: Mapped[str] = mapped_column(String(20))
    status_label: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[float | None] = mapped_column(Float)
    plan_item_id: Mapped[int | None] = mapped_column(ForeignKey("plan_items.id"))
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    patient: Mapped[Patient] = relationship(back_populates="visits")


class ProductSale(Base):
    __tablename__ = "product_sales"
    __table_args__ = (UniqueConstraint("external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(Integer)
    product_name: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Float, default=0)
    sold_at: Mapped[datetime] = mapped_column(DateTime)
    plan_item_id: Mapped[int | None] = mapped_column(ForeignKey("plan_items.id"))

    patient: Mapped[Patient] = relationship(back_populates="sales")


class PatientFile(Base):
    __tablename__ = "patient_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))      # plan_pdf | photo
    path: Mapped[str] = mapped_column(String(500))
    caption: Mapped[str | None] = mapped_column(Text)
    tg_chat: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    stage: Mapped[str | None] = mapped_column(String(10))            # фото: before | after | progress
    zone: Mapped[str | None] = mapped_column(String(80))             # обличчя, шия, зона під очима…
    taken_at: Mapped[date | None] = mapped_column(Date)
    record_id: Mapped[int | None] = mapped_column(Integer, index=True)   # запис медкартки
    cb_file_id: Mapped[int | None] = mapped_column(Integer, index=True)  # файл CleverBox (перенос фото — без дублів)
    category: Mapped[str | None] = mapped_column(String(12))             # consent | face | body | doc (порожньо — визначається автоматично)

    patient: Mapped[Patient | None] = relationship(back_populates="files")


class Appointment(Base):
    """Запис у CRM (власний календар). За потреби дублюється в CleverBox (cb_record_id) — тоді візит CleverBox не дублюється."""
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    services: Mapped[list | None] = mapped_column(JSON)                 # [{id, name, price, minutes, category}]
    place: Mapped[str | None] = mapped_column(String(120))              # кабінет
    resource: Mapped[str | None] = mapped_column(String(120))           # апарат
    status: Mapped[str] = mapped_column(String(15), default="booked", index=True)   # booked|confirmed|arrived|done|no_show|cancelled
    comment: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20), default="crm")      # crm | online | bot | appeal
    booking_source: Mapped[str | None] = mapped_column(String(60))     # джерело запису: телефон, Instagram, повторний…
    cb_record_id: Mapped[int | None] = mapped_column(Integer, index=True)
    sale_id: Mapped[int | None] = mapped_column(Integer)
    appeal_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    # історія з CleverBox (перенесений закритий візит): чек, оплата, автор і статус у CleverBox
    cb_check_id: Mapped[int | None] = mapped_column(Integer, index=True)
    cb_paid: Mapped[float | None] = mapped_column(Float)
    cb_discount: Mapped[float | None] = mapped_column(Float)
    cb_author: Mapped[str | None] = mapped_column(String(120))
    cb_status: Mapped[str | None] = mapped_column(String(80))

    patient: Mapped[Patient] = relationship()

    @property
    def historic(self) -> bool:
        """Візит перенесено з історії CleverBox: витратні вже списані там — у CRM лише для перегляду."""
        return bool(self.cb_status)

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def total(self) -> float:
        return round(sum(float(s.get("price") or 0) for s in self.services or []), 2)


class Sale(Base):
    """Продаж (чек) у CRM: послуги, товари, сертифікати; знижки, бонуси, кілька способів оплати."""
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    appointment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    appt_ids: Mapped[list | None] = mapped_column(JSON)                 # усі візити, розраховані цим чеком (різні лікарі)
    status: Mapped[str] = mapped_column(String(12), default="paid", index=True)    # paid | awaiting | refunded | cancelled
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    bonus_spent: Mapped[float] = mapped_column(Float, default=0)
    bonus_accrued: Mapped[float] = mapped_column(Float, default=0)
    rules: Mapped[list | None] = mapped_column(JSON)
    comment: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[int | None] = mapped_column(Integer)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime)
    refund_reason: Mapped[str | None] = mapped_column(String(300))
    fiscal: Mapped[dict | None] = mapped_column(JSON)                  # Checkbox: id, номер, посилання
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)   # прихований з каси (архів чеків)

    items: Mapped[list["SaleItem"]] = relationship(back_populates="sale", cascade="all, delete-orphan", order_by="SaleItem.id")
    payments: Mapped[list["SalePayment"]] = relationship(back_populates="sale", cascade="all, delete-orphan", order_by="SalePayment.id")
    patient: Mapped[Patient | None] = relationship()


class SaleItem(Base):
    __tablename__ = "sale_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12))                      # service | product | certificate | other
    ref_id: Mapped[int | None] = mapped_column(Integer)                 # CbService.id / Product.id
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(200))
    qty: Mapped[float] = mapped_column(Float, default=1)
    price: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)   # хто надав / продав
    warehouse: Mapped[str | None] = mapped_column(String(20))
    expiry_date: Mapped[date | None] = mapped_column(Date)
    sale: Mapped[Sale] = relationship(back_populates="items")


class SalePayment(Base):
    __tablename__ = "sale_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(60))                    # Готівка | Картка (термінал) | ФОП … рахунок | Бонуси | Сертифікат
    amount: Mapped[float] = mapped_column(Float)
    cert_id: Mapped[int | None] = mapped_column(Integer)
    sub_id: Mapped[int | None] = mapped_column(Integer)                  # абонемент, з якого списано
    sale: Mapped[Sale] = relationship(back_populates="payments")


class DiscountRule(Base):
    """Знижка: % або сума; на все / послуги / товари / категорії; період, дні тижня, мінімальна сума; авто чи вручну."""
    __tablename__ = "discount_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(10), default="percent")       # percent | amount
    value: Mapped[float] = mapped_column(Float, default=0)
    applies_to: Mapped[str] = mapped_column(String(12), default="all")      # all | services | products
    categories: Mapped[list | None] = mapped_column(JSON)                  # категорії послуг / товарів (порожньо — усі)
    date_from: Mapped[date | None] = mapped_column(Date)
    date_to: Mapped[date | None] = mapped_column(Date)
    weekdays: Mapped[list | None] = mapped_column(JSON)                    # [0..6], порожньо — щодня
    min_sum: Mapped[float | None] = mapped_column(Float)
    segment: Mapped[str | None] = mapped_column(String(40))                # birthday | new | all
    auto: Mapped[bool] = mapped_column(Boolean, default=False)             # застосовується сама
    combine: Mapped[bool] = mapped_column(Boolean, default=False)          # сумується з іншими
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # як у CleverBox: розділ (тег), обмеження, години, кому і на що
    tag: Mapped[str | None] = mapped_column(String(60))                    # ЛЕ | STRATO | Постійні акції | Співробітник …
    per_client_limit: Mapped[int | None] = mapped_column(Integer)          # скільки разів один пацієнт
    uses_limit: Mapped[int | None] = mapped_column(Integer)                # загальна к-сть використань (порожньо — безліміт)
    time_windows: Mapped[dict | None] = mapped_column(JSON)                # {"0": ["09:00","21:00"], …} — дні тижня з годинами
    client_groups: Mapped[list | None] = mapped_column(JSON)               # групи пацієнтів (порожньо — усі)
    patient_statuses: Mapped[list | None] = mapped_column(JSON)            # статуси пацієнтів (Новий, Постійний …)
    services: Mapped[list | None] = mapped_column(JSON)                    # назви послуг (порожньо — за «На що»/категоріями)
    products: Mapped[list | None] = mapped_column(JSON)                    # назви товарів
    on_certs: Mapped[bool | None] = mapped_column(Boolean)                 # діє на продаж сертифікатів
    on_subs: Mapped[bool | None] = mapped_column(Boolean)                  # діє на продаж абонементів


class BonusTxn(Base):
    """Рух бонусів пацієнта: нарахування (+), списання (−), згоряння, ручні, стартовий залишок з CleverBox."""
    __tablename__ = "bonus_txns"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    amount: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(12))                          # accrual | spend | manual | expire | import | birthday | welcome
    expires_at: Mapped[date | None] = mapped_column(Date)
    ref: Mapped[str | None] = mapped_column(String(60))                    # sale:123
    comment: Mapped[str | None] = mapped_column(String(300))
    user_id: Mapped[int | None] = mapped_column(Integer)


class Appeal(Base):
    """Звернення (заявка): дзвінок, повідомлення, форма сайту, бот. Довідники — у «Налаштування → Звернення»."""
    __tablename__ = "appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(64), index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    source: Mapped[str | None] = mapped_column(String(80), index=True)
    cause: Mapped[str | None] = mapped_column(String(120))            # причина звернення
    interest: Mapped[str | None] = mapped_column(String(200))         # послуга, що цікавить
    marks: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(60), default="Новий", index=True)
    deny_reason: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[float | None] = mapped_column(Float)               # потенційна сума
    comment: Mapped[str | None] = mapped_column(Text)
    responsible_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    next_contact: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    utm: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class PayLink(Base):
    """Посилання на оплату (еквайринг: monobank / LiqPay / WayForPay) — пацієнту в месенджер."""
    __tablename__ = "pay_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    provider: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Float)
    purpose: Mapped[str] = mapped_column(String(300))
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    appointment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(20), default="created", index=True)   # created | paid | failed | expired | cancelled
    ext_id: Mapped[str | None] = mapped_column(String(120), index=True)
    url: Mapped[str | None] = mapped_column(String(600))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_amount: Mapped[float | None] = mapped_column(Float)
    info: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[int | None] = mapped_column(Integer)
    sale_id: Mapped[int | None] = mapped_column(Integer, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, index=True)     # підключення (ФОП) з «Платіжні системи»


class Call(Base):
    """Дзвінок з IP-телефонії (Ringostat / Binotel / Stream Telecom) — з вебхука провайдера."""
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    provider: Mapped[str] = mapped_column(String(20))
    ext_id: Mapped[str | None] = mapped_column(String(120), index=True)
    direction: Mapped[str] = mapped_column(String(10), default="in")            # in | out
    phone: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str | None] = mapped_column(String(30))                      # answered | missed | …
    duration: Mapped[int | None] = mapped_column(Integer)
    employee: Mapped[str | None] = mapped_column(String(120))
    record_url: Mapped[str | None] = mapped_column(String(500))
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    appeal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)                # співробітник CRM (за імʼям з телефонії)
    transcript: Mapped[str | None] = mapped_column(Text)                         # текст діалогу (Ringostat AI / VTT)
    summary: Mapped[str | None] = mapped_column(Text)                            # короткий зміст
    ai_meta: Mapped[dict | None] = mapped_column(JSON)                           # настрій, рекомендації, сирі поля AI
    ai_score: Mapped[float | None] = mapped_column(Float)                        # оцінка діалогу 0–100 (Claude за критеріями)
    ai_comment: Mapped[str | None] = mapped_column(Text)
    ai_at: Mapped[datetime | None] = mapped_column(DateTime)
    line: Mapped[str | None] = mapped_column(String(64), index=True)           # номер лінії, на яку дзвонили (наш номер)
    agent_ext: Mapped[str | None] = mapped_column(String(64))                  # ID співробітника в телефонії (Ringostat staffid)
    utm_source: Mapped[str | None] = mapped_column(String(120), index=True)     # звідки дзвінок (колтрекінг Ringostat)
    utm_medium: Mapped[str | None] = mapped_column(String(120))
    utm_campaign: Mapped[str | None] = mapped_column(String(300))
    utm_term: Mapped[str | None] = mapped_column(String(300))
    utm_content: Mapped[str | None] = mapped_column(String(300))
    gclid: Mapped[str | None] = mapped_column(String(300))
    landing: Mapped[str | None] = mapped_column(String(500))
    attr: Mapped[str | None] = mapped_column(String(12))                       # звідки мітки: ringostat | ga4 (за часом кліку) | ga4? (неоднозначно)


class AppealNote(Base):
    """Коментар / подія в зверненні (історія)."""
    __tablename__ = "appeal_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user_id: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="note")     # note | status | system


class MedRecord(Base):
    """Запис медичної картки: консультація / процедура / контроль. Лише для ролей з правом medical (власник, лікар)."""
    __tablename__ = "med_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="procedure")   # consultation | procedure | control
    title: Mapped[str | None] = mapped_column(String(200))             # процедура / привід
    complaints: Mapped[str | None] = mapped_column(Text)               # скарги
    anamnesis: Mapped[str | None] = mapped_column(Text)                # анамнез
    objective: Mapped[str | None] = mapped_column(Text)                # об'єктивно / огляд, фототип, стан шкіри
    diagnosis: Mapped[str | None] = mapped_column(Text)
    protocol: Mapped[str | None] = mapped_column(Text)                 # що зроблено: препарат, серія, доза, зони, параметри
    drugs: Mapped[list | None] = mapped_column(JSON)                   # [{name, batch, expiry, dose, zone}]
    recommendations: Mapped[str | None] = mapped_column(Text)
    next_visit: Mapped[date | None] = mapped_column(Date)
    consent: Mapped[bool | None] = mapped_column(Boolean)              # інформована згода підписана
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class MedTemplate(Base):
    """Шаблон запису медкартки (протокол процедури): підставляє текст у поля."""
    __tablename__ = "med_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(20), default="procedure")
    fields: Mapped[dict | None] = mapped_column(JSON)                  # {complaints, objective, protocol, recommendations…}
    created_by: Mapped[int | None] = mapped_column(Integer)


class ReminderLog(Base):
    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("kind", "entity_key", "period_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    entity_key: Mapped[str] = mapped_column(String(100))
    period_key: Mapped[str] = mapped_column(String(30))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncState(Base):
    __tablename__ = "sync_state"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        import os
        os.makedirs("data", exist_ok=True)
        if ":memory:" in url:       # тести: одна спільна БД для всіх потоків
            from sqlalchemy.pool import StaticPool
            return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


engine = _engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """Легка автоміграція: нові nullable-колонки в уже існуючих таблицях (оновлення без втрати даних)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for t in Base.metadata.sorted_tables:
            if not insp.has_table(t.name):
                continue
            have = {c["name"] for c in insp.get_columns(t.name)}
            for col in t.columns:
                if col.name not in have:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE {t.name} ADD COLUMN {col.name} {ddl}'))


# ---------------- Користувачі, доступ, API ----------------

ROLES = {"owner": "Власник", "admin": "Адміністратор", "doctor": "Лікар"}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="admin")      # owner | admin | doctor
    password_hash: Mapped[str] = mapped_column(String(300))
    delete_pw_hash: Mapped[str | None] = mapped_column(String(300))   # окремий пароль для видалення (задає власник)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"))   # для ролі «лікар»
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    photo_path: Mapped[str | None] = mapped_column(String(500))          # фото профілю (панель профілю, аватар)
    photo_mime: Mapped[str | None] = mapped_column(String(60))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)         # «Зараз онлайн»
    notif_seen_at: Mapped[datetime | None] = mapped_column(DateTime)     # дзвіночок: коли востаннє переглянув сповіщення

    doctor: Mapped[Doctor | None] = relationship()

    @property
    def role_label(self):
        return ROLES.get(self.role, self.role)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16), index=True)        # видима частина ключа
    key_hash: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[list] = mapped_column(JSON, default=list)           # read | write | medical
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used: Mapped[datetime | None] = mapped_column(DateTime)


class Webhook(Base):
    __tablename__ = "webhooks"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list] = mapped_column(JSON, default=list)           # plan.created, plan.updated, reminders.daily, sync.done
    secret: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id"))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSON)

    user: Mapped[User | None] = relationship()
    api_key: Mapped[ApiKey | None] = relationship()



# ---------------- Каталог, склад, фінанси, задачі ----------------

class Product(Base):
    """Каталог засобів домашнього догляду (з CleverBox /products/list + з бюті-планів)."""
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    name_norm: Mapped[str] = mapped_column(String(300), index=True)
    cb_product_id: Mapped[int | None] = mapped_column(Integer, index=True)
    category: Mapped[str | None] = mapped_column(String(200))
    volume: Mapped[str | None] = mapped_column(String(30))
    price: Mapped[float | None] = mapped_column(Float)
    cost: Mapped[float | None] = mapped_column(Float)
    supply_days: Mapped[int | None] = mapped_column(Integer)      # тривалість за замовчуванням для нових планів
    url: Mapped[str | None] = mapped_column(String(500))
    photo_url: Mapped[str | None] = mapped_column(String(500))            # фото з molodist.store (Shop-Express)
    shop_id: Mapped[str | None] = mapped_column(String(64), index=True)   # id товару в Shop-Express
    shop_price: Mapped[float | None] = mapped_column(Float)                # ціна на сайті
    shop_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    min_stock: Mapped[int | None] = mapped_column(Integer)        # мінімальний залишок (для замовлення)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    brand: Mapped[str | None] = mapped_column(String(120), index=True)
    supplier: Mapped[str | None] = mapped_column(String(200), index=True)   # основний постачальник
    sku: Mapped[str | None] = mapped_column(String(100), index=True)        # артикул
    expiry_date: Mapped[date | None] = mapped_column(Date)                  # термін придатності (найближчий, вручну)
    price_manual: Mapped[float | None] = mapped_column(Float)               # ціна продажу, якщо немає в CleverBox
    warehouse: Mapped[str | None] = mapped_column(String(20), index=True)   # склад: vitrina|inject|classic|single|equip
    unit: Mapped[str | None] = mapped_column(String(20))                    # од. списання: шт., мл. …
    barcode: Mapped[str | None] = mapped_column(String(64))
    for_sale: Mapped[bool | None] = mapped_column(Boolean)                  # для продажу (інакше — витратний матеріал)
    for_expense: Mapped[bool | None] = mapped_column(Boolean)               # для витрати (розхідники у візитах і техкартах)
    short_name: Mapped[str | None] = mapped_column(String(128))             # коротка назва в чеку (фіскальному)
    purchase_unit: Mapped[str | None] = mapped_column(String(20))           # од. приходу (флакон), якщо відрізняється від од. обліку
    tags: Mapped[list | None] = mapped_column(JSON)                         # мітки
    direction: Mapped[str | None] = mapped_column(String(120))              # напрямок (ширше за категорію)
    tax_code: Mapped[str | None] = mapped_column(String(20))                # податок у Checkbox (код / літера)
    uktzed: Mapped[str | None] = mapped_column(String(20))                  # код УКТЗЕД
    tare_g: Mapped[float | None] = mapped_column(Float)                     # вага тари, г
    coef: Mapped[float | None] = mapped_column(Float)                       # одиниць обліку в одиниці приходу (500 мл у флаконі)
    # планування ціни реалізації
    cost_currency: Mapped[str | None] = mapped_column(String(3))           # USD | EUR | UAH
    cost_fx: Mapped[float | None] = mapped_column(Float)                    # ціна закупівлі у валюті
    rrp_fx: Mapped[float | None] = mapped_column(Float)                     # рекомендована ціна постачальника у валюті
    planned_price: Mapped[float | None] = mapped_column(Float)              # рекомендована ціна продажу (наше рішення)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime)
    price_set_at: Mapped[datetime | None] = mapped_column(DateTime)        # ціну продажу задано в CRM — синк CleverBox її не перезаписує
    stock_cost: Mapped[float | None] = mapped_column(Float)                 # собівартість залишку CleverBox за од. приходу
    stock_cost_at: Mapped[date | None] = mapped_column(Date)


class ProductPurchase(Base):
    """Закупівля партії засобу: від неї — середня й остання ціна закупівлі та термін придатності."""
    __tablename__ = "product_purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    bought_on: Mapped[date] = mapped_column(Date, index=True)          # дата закупівлі
    supplier: Mapped[str | None] = mapped_column(String(200))
    qty: Mapped[float] = mapped_column(Float, default=1)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    doc_no: Mapped[str | None] = mapped_column(String(100))                 # № накладної
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped["Product"] = relationship()


class StockLevel(Base):
    """Знімок залишків з CleverBox /products/stock (перезаписується при синку)."""
    __tablename__ = "stock_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    cb_product_id: Mapped[int | None] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(300))
    storage: Mapped[str | None] = mapped_column(String(200))
    qty: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str | None] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    contact: Mapped[str | None] = mapped_column(String(200))       # контактна особа / менеджер
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(200))
    terms: Mapped[str | None] = mapped_column(String(300))         # умови: доставка, мін. замовлення, оплата
    legal_name: Mapped[str | None] = mapped_column(String(300))    # отримувач платежу (ТОВ / ФОП …)
    edrpou: Mapped[str | None] = mapped_column(String(20), index=True)   # ЄДРПОУ / РНОКПП
    iban: Mapped[str | None] = mapped_column(String(40), index=True)
    bank: Mapped[str | None] = mapped_column(String(200))
    pay_purpose: Mapped[str | None] = mapped_column(String(300))   # шаблон призначення платежу
    address: Mapped[str | None] = mapped_column(String(300))
    contract: Mapped[str | None] = mapped_column(String(200))      # № і дата договору
    vat: Mapped[bool | None] = mapped_column(Boolean)              # платник ПДВ
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    fx_usd: Mapped[float | None] = mapped_column(Float)            # курс постачальника USD, ₴ (для «Планування ціни»)
    fx_eur: Mapped[float | None] = mapped_column(Float)            # курс постачальника EUR, ₴
    fx_at: Mapped[datetime | None] = mapped_column(DateTime)
    consignment: Mapped[bool | None] = mapped_column(Boolean)      # товар під реалізацію: платимо за продане (звіти реалізації)


class StockPayRow(Base):
    """Календар оплат складу: оплата постачальнику за цикл (замовлення → погодження → оплата з ФОП).
    Планова сума — із замовлення (або оцінка за критичними залишками, поки замовлення немає)."""
    __tablename__ = "stock_pay_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(200), index=True)
    grp: Mapped[str | None] = mapped_column(String(12))                     # inject | cosmetics | single
    fop: Mapped[str | None] = mapped_column(String(40))                      # ФОП Колган | ФОП Бондар | Готівка | None — очікує вибору
    fact_amount: Mapped[float | None] = mapped_column(Float)                 # сума з рахунку постачальника
    plan_date: Mapped[date | None] = mapped_column(Date)
    approved: Mapped[bool | None] = mapped_column(Boolean)
    approved_by: Mapped[int | None] = mapped_column(Integer)
    order_doc_id: Mapped[int | None] = mapped_column(Integer)
    paid_at: Mapped[date | None] = mapped_column(Date)
    paid_amount: Mapped[float | None] = mapped_column(Float)
    paid_ref: Mapped[str | None] = mapped_column(String(80))                 # bank:<id> | finop:<id> | manual
    paid_fop: Mapped[str | None] = mapped_column(String(100))
    closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConsignLot(Base):
    """Реалізація: рядок поставки під реалізацію — товар, к-сть, ціна банки; sold — скільки вже включено у зафіксовані звіти."""
    __tablename__ = "consign_lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(200), index=True)
    invoice: Mapped[str | None] = mapped_column(String(40), index=True)      # № поставки CleverBox / CRM
    inv_date: Mapped[date] = mapped_column(Date, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(60))
    qty: Mapped[float] = mapped_column(Float, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    sold: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str | None] = mapped_column(String(12))                   # seed | doc | manual
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConsignReport(Base):
    """Звіт реалізації постачальнику: продажі за період, розписані по найстаріших поставках (FIFO)."""
    __tablename__ = "consign_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(200), index=True)
    d_from: Mapped[date] = mapped_column(Date)
    d_to: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="fixed")
    lines: Mapped[list | None] = mapped_column(JSON)                       # [{pid, name, qty, sum, alloc:[{lot, invoice, date, qty, cost, sum}], short}]
    total: Mapped[float] = mapped_column(Float, default=0)
    paid: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConsignPay(Base):
    """Оплата постачальнику за реалізацію однією сумою → рознесено по звітах і поставках (найстаріші першими)."""
    __tablename__ = "consign_pays"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier: Mapped[str] = mapped_column(String(200), index=True)
    day: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    alloc: Mapped[list | None] = mapped_column(JSON)                       # [{report, invoice, amount}]
    finop_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SupplierPrice(Base):
    """Прайс постачальника по бренду (Excel / CSV): файл, дата прайсу, коли завантажено; рядки й зіставлення з товарами CRM.
    «Застосувати» підставляє закупівлю у валюті (і РРЦ) в «Планування ціни реалізації»."""
    __tablename__ = "supplier_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(Integer, index=True)
    brand: Mapped[str | None] = mapped_column(String(120), index=True)
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(300))
    price_date: Mapped[date | None] = mapped_column(Date)                  # дата прайсу (від постачальника)
    currency: Mapped[str | None] = mapped_column(String(3))                # USD | EUR | UAH
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    rows: Mapped[list | None] = mapped_column(JSON)                         # [{name, sku, barcode, cost, rrp, pid, how}]
    n_rows: Mapped[int | None] = mapped_column(Integer)
    n_matched: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    n_applied: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(12))                 # None — готовий; recognizing — PDF/фото в агента; error
    error: Mapped[str | None] = mapped_column(String(500))
    cur_forced: Mapped[bool | None] = mapped_column(Boolean)               # валюту вибрано вручну при завантаженні


class PriceSnapshot(Base):
    """Історія «Планування ціни реалізації»: стан товарів до й після зміни (прайс, збереження, ціна продажу) або збережена версія.
    Можна повернутись: «відкотити» (стан до) або «повернути цю версію» (стан після)."""
    __tablename__ = "price_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12), default="version")      # version | price | save | sale | restore
    brand: Mapped[str | None] = mapped_column(String(120), index=True)
    supplier_id: Mapped[int | None] = mapped_column(Integer, index=True)
    price_id: Mapped[int | None] = mapped_column(Integer)                  # SupplierPrice, якщо зміна з прайсу
    note: Mapped[str | None] = mapped_column(String(300))
    items: Mapped[list | None] = mapped_column(JSON)                       # [{pid, name, before:{…}, after:{…}}]
    n: Mapped[int | None] = mapped_column(Integer)
    rates: Mapped[dict | None] = mapped_column(JSON)                       # курс на момент збереження {USD, EUR, round}


class SupplierEntity(Base):
    """Юридична особа постачальника (ТОВ / ФОП / ПП…): в одного постачальника їх може бути кілька — свої реквізити,
    договір, ПДВ. Основна дублюється в поля Supplier.legal_name/edrpou/iban… (для сумісності)."""
    __tablename__ = "supplier_entities"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str | None] = mapped_column(String(20))            # ТОВ | ФОП | ПП | інше
    legal_name: Mapped[str | None] = mapped_column(String(300))
    edrpou: Mapped[str | None] = mapped_column(String(20), index=True)
    iban: Mapped[str | None] = mapped_column(String(40), index=True)
    bank: Mapped[str | None] = mapped_column(String(200))
    mfo: Mapped[str | None] = mapped_column(String(10))
    pay_purpose: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(String(300))
    director: Mapped[str | None] = mapped_column(String(200))       # підписант / керівник
    contract: Mapped[str | None] = mapped_column(String(200))
    vat: Mapped[bool | None] = mapped_column(Boolean)
    tax_group: Mapped[str | None] = mapped_column(String(40))       # для ФОП: група єдиного податку
    email: Mapped[str | None] = mapped_column(String(200))           # для рахунків / актів
    is_main: Mapped[bool | None] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SupplierInvoice(Base):
    """Поставка з CleverBox (шапка документа): для обороту й боргу по постачальниках. На залишки не впливає."""
    __tablename__ = "supplier_invoices"
    __table_args__ = (UniqueConstraint("cb_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cb_number: Mapped[str] = mapped_column(String(40))
    inv_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    supplier: Mapped[str | None] = mapped_column(String(200), index=True)
    warehouse: Mapped[str | None] = mapped_column(String(20))
    warehouse_name: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Float, default=0)
    paid: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str | None] = mapped_column(String(40))
    author: Mapped[str | None] = mapped_column(String(200))
    comment: Mapped[str | None] = mapped_column(String(500))
    paid_note: Mapped[str | None] = mapped_column(String(300))       # відмітка в CRM: «оплачено готівкою 12.10» тощо
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    adjust: Mapped[float | None] = mapped_column(Float)              # знижка / повернення постачальнику — зменшує борг (CRM)
    adjust_note: Mapped[str | None] = mapped_column(String(300))
    crm_paid: Mapped[float | None] = mapped_column(Float)            # оплати реалізації, рознесені в CRM
    items: Mapped[list | None] = mapped_column(JSON)                 # позиції поставки [{name, qty, price, sum}] (API CleverBox / Excel)
    items_src: Mapped[str | None] = mapped_column(String(20))        # api | excel
    items_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def debt(self) -> float:
        return round(max(0.0, (self.amount or 0) - (self.paid or 0) - (self.adjust or 0) - (self.crm_paid or 0)), 2)


class StockDoc(Base):
    """Складський документ: supply (поставка) | order (замовлення постачальнику) | transfer (переміщення) |
    writeoff (списання) | return (повернення) | inventory (інвентаризація) | opening (стартові залишки).
    status: draft → posted (рухи створено) | cancelled; для order: draft → sent → received | cancelled."""
    __tablename__ = "stock_docs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    number: Mapped[str] = mapped_column(String(30), index=True)
    doc_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    supplier: Mapped[str | None] = mapped_column(String(200))
    wh_from: Mapped[str | None] = mapped_column(String(20))
    wh_to: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(100))         # списання: причина; повернення: supplier|patient
    ext_no: Mapped[str | None] = mapped_column(String(100))         # № накладної постачальника
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"))
    source_id: Mapped[int | None] = mapped_column(Integer)          # поставка, створена з замовлення
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_by: Mapped[str | None] = mapped_column(String(100))        # яким ФОП оплачено
    pay_group: Mapped[str | None] = mapped_column(String(60), index=True)   # одна накладна → кілька складів
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)   # видалено в архів (за паролем)
    archived_by: Mapped[int | None] = mapped_column(Integer)
    archived_from: Mapped[str | None] = mapped_column(String(20))   # статус до видалення
    archive_note: Mapped[str | None] = mapped_column(String(300))

    lines: Mapped[list["StockDocLine"]] = relationship(back_populates="doc", cascade="all, delete-orphan",
                                                       order_by="StockDocLine.id")


class StockDocLine(Base):
    __tablename__ = "stock_doc_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("stock_docs.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=0)            # інвентаризація: фактично пораховано
    unit_cost: Mapped[float | None] = mapped_column(Float)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    sys_qty: Mapped[float | None] = mapped_column(Float)            # інвентаризація: облікова к-сть на момент проведення

    doc: Mapped[StockDoc] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()


class StockMove(Base):
    """Рух товару (журнал). Залишок = сума qty по товару і складу. ref — захист від дублів автосписань."""
    __tablename__ = "stock_moves"
    __table_args__ = (UniqueConstraint("ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse: Mapped[str] = mapped_column(String(20), index=True)
    qty: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(20), index=True)       # supply|sale|procedure|writeoff|transfer|return|inventory|opening
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("stock_docs.id"), index=True)
    unit_cost: Mapped[float | None] = mapped_column(Float)
    ref: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(String(300))
    patient_id: Mapped[int | None] = mapped_column(Integer)
    expiry_date: Mapped[date | None] = mapped_column(Date, index=True)   # партія: термін придатності (None — без терміну)


class ProcedureNorm(Base):
    """Норма списання матеріалів на одну послугу CleverBox."""
    __tablename__ = "procedure_norms"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(Integer, index=True)    # CbService.id
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1)
    warehouse: Mapped[str | None] = mapped_column(String(20))       # звідки списувати (за замовчуванням — склад товару)

    product: Mapped["Product"] = relationship()


class GiftCertificate(Base):
    """Подарунковий сертифікат CleverBox (вивантаження «Сертифікати»)."""
    __tablename__ = "gift_certificates"
    id: Mapped[int] = mapped_column(primary_key=True)                  # = ID CleverBox
    code: Mapped[str | None] = mapped_column(String(40), index=True)
    type_name: Mapped[str | None] = mapped_column(String(200))
    cb_client_id: Mapped[int | None] = mapped_column(Integer, index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    client_name: Mapped[str | None] = mapped_column(String(200))
    seller: Mapped[str | None] = mapped_column(String(200))
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    price: Mapped[float] = mapped_column(Float, default=0)
    paid: Mapped[float] = mapped_column(Float, default=0)
    nominal: Mapped[float] = mapped_column(Float, default=0)
    balance: Mapped[float] = mapped_column(Float, default=0)
    valid_to: Mapped[date | None] = mapped_column(Date, index=True)
    comment: Mapped[str | None] = mapped_column(String(500))
    cb_status: Mapped[str | None] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    single_use: Mapped[bool | None] = mapped_column(Boolean)              # одноразовий: після першого використання залишок згорає

    patient: Mapped[Patient | None] = relationship()

    def state(self, today: date | None = None) -> str:
        """active | expiring | expired (згорів із залишком) | used | unpaid"""
        today = today or date.today()
        if (self.cb_status or "").lower().startswith("не оплач"):
            return "unpaid"
        if (self.balance or 0) <= 0:
            return "used"
        if self.valid_to and self.valid_to < today:
            return "expired"
        if self.valid_to and (self.valid_to - today).days <= 14:
            return "expiring"
        return "active"


class Subscription(Base):
    """Абонемент CleverBox (вивантаження «Абонементи»): N візитів / послуг до дати."""
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)                  # = ID CleverBox
    code: Mapped[str | None] = mapped_column(String(40))
    type_name: Mapped[str | None] = mapped_column(String(300))
    services: Mapped[str | None] = mapped_column(Text)                 # на які послуги діє
    cb_client_id: Mapped[int | None] = mapped_column(Integer, index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    client_name: Mapped[str | None] = mapped_column(String(200))
    seller: Mapped[str | None] = mapped_column(String(200))
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    price: Mapped[float] = mapped_column(Float, default=0)
    paid: Mapped[float] = mapped_column(Float, default=0)
    visits_total: Mapped[float | None] = mapped_column(Float)
    visits_used: Mapped[float | None] = mapped_column(Float)
    balance: Mapped[float | None] = mapped_column(Float)              # грошовий залишок, якщо абонемент у гривнях
    valid_to: Mapped[date | None] = mapped_column(Date, index=True)
    comment: Mapped[str | None] = mapped_column(String(500))
    cb_status: Mapped[str | None] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    patient: Mapped[Patient | None] = relationship()

    @property
    def visits_left(self) -> float | None:
        if self.visits_total is None:
            return None
        return max((self.visits_total or 0) - (self.visits_used or 0), 0)

    @property
    def unit_price(self) -> float:
        return round(self.paid / self.visits_total, 2) if self.visits_total else 0.0

    @property
    def value_left(self) -> float:
        """Неспожита вартість: залишок візитів × ціна візиту (або грошовий залишок)."""
        if self.visits_left is not None:
            return round(self.visits_left * self.unit_price, 2)
        return self.balance or 0.0

    def state(self, today: date | None = None) -> str:
        today = today or date.today()
        st = (self.cb_status or "").lower()
        if st.startswith("не оплач"):
            return "unpaid"
        left = self.visits_left if self.visits_left is not None else (self.balance or 0)
        if left <= 0 or "закрит" in st or "використ" in st:
            return "used"
        if self.valid_to and self.valid_to < today:
            return "expired"
        if self.valid_to and (self.valid_to - today).days <= 14:
            return "expiring"
        return "active"


class CashDay(Base):
    """Рух коштів за день: звіт CleverBox «Прибуток» (source=cleverbox) або оцінка з чеків без витрат (source=checks)."""
    __tablename__ = "cash_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    cash_in: Mapped[float] = mapped_column(Float, default=0)
    card_in: Mapped[float] = mapped_column(Float, default=0)
    income: Mapped[float] = mapped_column(Float, default=0)
    expense: Mapped[float] = mapped_column(Float, default=0)        # додатне число
    source: Mapped[str] = mapped_column(String(20), default="cleverbox")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def net(self) -> float:
        return round((self.income or 0) - (self.expense or 0), 2)


class FinOp(Base):
    """Ручна фінансова операція в CRM: витрата / надходження (оренда, зарплата, податки…).
    Надходження з чеків CleverBox і оплати постачальникам з monobank сюди не пишуться — показуються поруч автоматично."""
    __tablename__ = "fin_ops"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(10))                     # income | expense | transfer
    category: Mapped[str] = mapped_column(String(60))
    cashbox_to: Mapped[str | None] = mapped_column(String(100))       # переміщення: на який рахунок
    counterparty: Mapped[str | None] = mapped_column(String(200))     # від кого / кому
    storno_of: Mapped[int | None] = mapped_column(Integer, index=True)   # сторно операції №
    ref: Mapped[str | None] = mapped_column(String(80), index=True)      # джерело: banktxn:<id> — оплата поставки з виписки monobank
    amount: Mapped[float] = mapped_column(Float)                      # додатне число
    cashbox: Mapped[str | None] = mapped_column(String(100))          # готівка / ФОП Бондар / ФОП Колган / …
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)   # зарплата: кому
    salary_month: Mapped[str | None] = mapped_column(String(7))       # зарплата: за який місяць (РРРР-ММ)
    comment: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class CbFinOp(Base):
    """Операція з журналу фіноперацій CleverBox (API або імпорт Excel «Журнал фіноперацій»)."""
    __tablename__ = "cb_fin_ops"
    id: Mapped[int] = mapped_column(primary_key=True)                 # № операції CleverBox
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(10))                     # income | expense | transfer | storno
    counterparty: Mapped[str | None] = mapped_column(String(300))
    basis: Mapped[str | None] = mapped_column(String(200))            # підстава: «Продаж #12067», «Поставка #20835»
    article: Mapped[str | None] = mapped_column(String(200))          # стаття
    filial: Mapped[str | None] = mapped_column(String(200))
    account: Mapped[str | None] = mapped_column(String(200))          # рахунок
    vid: Mapped[str | None] = mapped_column(String(200))              # вид / рахунок призначення
    author: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Float, default=0)
    comment: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(10), default="import")  # api | import
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StaffShift(Base):
    """Зміна спеціаліста в графіку: день, години, кабінет (робоче місце з «Налаштування → Локації»)."""
    __tablename__ = "staff_shifts"
    __table_args__ = (UniqueConstraint("doctor_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    start: Mapped[str] = mapped_column(String(5))                    # «10:00»
    end: Mapped[str] = mapped_column(String(5))                      # «20:00»
    place: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(200))
    updated_by: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def minutes(self) -> int:
        h1, m1 = map(int, self.start.split(":"))
        h2, m2 = map(int, self.end.split(":"))
        return max(0, h2 * 60 + m2 - h1 * 60 - m1)


class SalaryRule(Base):
    """Правило нарахування зарплати спеціаліста: оклад + % від послуг + % від товарів (з чеків CleverBox)."""
    __tablename__ = "salary_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), unique=True)
    base: Mapped[float | None] = mapped_column(Float)                 # оклад за місяць
    pct_services: Mapped[float | None] = mapped_column(Float)
    pct_products: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalaryAdj(Base):
    """Коригування нарахування за місяць: премія (+) або штраф / утримання (−)."""
    __tablename__ = "salary_adjs"
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(String(300))
    article: Mapped[str | None] = mapped_column(String(80))             # стаття для нарахувань: Премія, Штраф, Лікарняні…
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FopMonth(Base):
    """Оборот ФОП за місяць: готівка / безготівка. source: checkbox (Z-звіти ПРРО) | manual (таблиця / вручну)."""
    __tablename__ = "fop_months"
    __table_args__ = (UniqueConstraint("fop", "year", "month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    fop: Mapped[str] = mapped_column(String(100), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer)
    cash: Mapped[float] = mapped_column(Float, default=0)
    cashless: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def total(self) -> float:
        return round((self.cash or 0) + (self.cashless or 0), 2)


class DayOff(Base):
    """Неробочий день співробітника з причиною: вікенд, відпустка, лікарняний, день народження…"""
    __tablename__ = "day_offs"
    __table_args__ = (UniqueConstraint("doctor_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    updated_by: Mapped[int | None] = mapped_column(Integer)


class PayrollDoc(Base):
    """Документ «Нарахування зарплати» за період (як у CleverBox): рядки по співробітниках за їхніми окладами.
    posted — суми додано на баланс співробітників (взаєморозрахунки)."""
    __tablename__ = "payroll_docs"
    id: Mapped[int] = mapped_column(primary_key=True)
    d_from: Mapped[date] = mapped_column(Date, index=True)
    d_to: Mapped[date] = mapped_column(Date, index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    total: Mapped[float] = mapped_column(Float, default=0)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    lines: Mapped[list["PayrollLine"]] = relationship(back_populates="doc", cascade="all, delete-orphan", order_by="PayrollLine.id")


class PayrollLine(Base):
    __tablename__ = "payroll_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("payroll_docs.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    plan: Mapped[str | None] = mapped_column(String(120))
    days: Mapped[int] = mapped_column(Integer, default=0)
    calc: Mapped[float] = mapped_column(Float, default=0)          # розраховано алгоритмом
    amount: Mapped[float] = mapped_column(Float, default=0)        # нараховано (можна виправити вручну)
    detail: Mapped[dict | None] = mapped_column(JSON)              # ставка, комісійні послуги/товари, оборот, години, виручка
    posted: Mapped[bool | None] = mapped_column(Boolean)           # на балансі співробітника (окремо по кожному рядку)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)

    doc: Mapped[PayrollDoc] = relationship(back_populates="lines")


class TechCard(Base):
    """Техкарта (як у CleverBox): матеріали на 1 послугу за категоріями, кожна категорія — свій склад списання."""
    __tablename__ = "tech_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str | None] = mapped_column(String(200))
    max_cost: Mapped[float | None] = mapped_column(Float)              # ліміт собівартості у візиті
    mode: Mapped[str] = mapped_column(String(10), default="extended")  # extended — склад за категорією; simple — склад спеціаліста
    cats: Mapped[list | None] = mapped_column(JSON)                    # [{name, wh}]
    comment: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    cb_id: Mapped[int | None] = mapped_column(Integer, index=True)     # id техкарти в CleverBox (імпорт)

    lines: Mapped[list["TechCardLine"]] = relationship(back_populates="card", cascade="all, delete-orphan", order_by="TechCardLine.id")


class TechCardLine(Base):
    __tablename__ = "tech_card_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("tech_cards.id"), index=True)
    cat: Mapped[str] = mapped_column(String(80))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1)

    card: Mapped[TechCard] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()


class AmortAsset(Base):
    """Амортизація: апарат (ставка за процедуру) або розхідник з ресурсом (імпульси, хвилини — ставка за одиницю).
    product_id — позиція «Амортизація…» / «маніпула» / «хвилини мотору», що стоїть у техкартах: списання цієї позиції у візиті = нарахування."""
    __tablename__ = "amort_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(12), default="device")        # device | consumable
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("amort_assets.id"))   # розхідник → апарат
    cost: Mapped[float] = mapped_column(Float, default=0)                  # вартість, ₴
    life_years: Mapped[float | None] = mapped_column(Float)                # апарат: термін служби
    resource: Mapped[float | None] = mapped_column(Float)                  # розхідник: ресурс (імпульсів / хвилин)
    unit: Mapped[str] = mapped_column(String(20), default="процедура")     # процедура | імпульс | хвилина
    rate: Mapped[float] = mapped_column(Float, default=0)                  # ₴ за одиницю
    avg_month: Mapped[float | None] = mapped_column(Float)                 # середня к-сть на місяць (план)
    commissioned_at: Mapped[date | None] = mapped_column(Date)
    cashbox: Mapped[str | None] = mapped_column(String(100))               # рахунок фонду, куди погашаємо
    jar: Mapped[str | None] = mapped_column(String(200))                   # банка monobank (посилання send.monobank.ua/jar/…)
    accrued_before: Mapped[float] = mapped_column(Float, default=0)        # нараховано до першого періоду в CRM
    used_before: Mapped[float] = mapped_column(Float, default=0)           # використано ресурсу до першого періоду
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped["Product | None"] = relationship()


class AmortPeriod(Base):
    """Нарахування за півмісяць: 1–15 (half=1) і 16–кінець (half=2). source: sheet (перенесено) | crm (з візитів) | manual."""
    __tablename__ = "amort_periods"
    __table_args__ = (UniqueConstraint("asset_id", "year", "month", "half"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("amort_assets.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    half: Mapped[int] = mapped_column(Integer)
    procs: Mapped[float] = mapped_column(Float, default=0)                 # кількість процедур
    qty: Mapped[float] = mapped_column(Float, default=0)                   # одиниць (процедур × ставка-одиниць / імпульсів / хвилин)
    amount: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(10), default="crm")
    accrued_at: Mapped[datetime | None] = mapped_column(DateTime)          # коли зафіксовано (16-го / 1-го)
    note: Mapped[str | None] = mapped_column(String(300))


class AmortTxn(Base):
    """Рух фонду амортизації: pay — погашення (переміщення грошей на рахунок фонду), expense — витрата з фонду (ремонт, кредит…).
    finop_id — операція в журналі фіноперацій (порожньо — історія з таблиці, без руху грошей)."""
    __tablename__ = "amort_txns"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("amort_assets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))                          # pay | expense
    day: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)
    cashbox: Mapped[str | None] = mapped_column(String(100))               # pay: звідки; expense: з якого рахунку
    note: Mapped[str | None] = mapped_column(String(300))
    finop_id: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(10), default="crm")         # crm | sheet
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VisitMaterial(Base):
    """Витратні матеріали конкретного візиту CRM: копія техкарти на кожну послугу, кількість можна змінити,
    позицію — додати чи прибрати. Після оплати візиту — списання зі складу документами (по складу кожної категорії)."""
    __tablename__ = "visit_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    appt_id: Mapped[int] = mapped_column(Integer, index=True)
    svc_idx: Mapped[int] = mapped_column(Integer, default=0)            # № послуги у візиті
    service_id: Mapped[int | None] = mapped_column(Integer)
    cat: Mapped[str] = mapped_column(String(80))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    warehouse: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped["Product"] = relationship()


class PriceAlert(Base):
    """Центр моніторингу цін: сповіщення про перехід у вищу зону подорожчання (товар складу або собівартість послуги)."""
    __tablename__ = "price_alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), index=True)        # product | service
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(300))
    zone: Mapped[int] = mapped_column(Integer)                      # 1..4
    base: Mapped[float] = mapped_column(Float)
    current: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class VisitNote(Base):
    """Нотатка до візиту в календарі (вкладка «Нотатка» вікна візиту). key: appt:<id> | cb:<Visit.id>."""
    __tablename__ = "visit_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40), index=True)
    patient_id: Mapped[int | None] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TimeBlock(Base):
    """Заблокований час у календарі спеціаліста: зйомка, навчання, обід, техобслуговування."""
    __tablename__ = "time_blocks"
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    title: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DoctorFile(Base):
    """Файли співробітника: договір, дипломи, сертифікати, медкнижка."""
    __tablename__ = "doctor_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProductFile(Base):
    """Фото й документи товару (сертифікати відповідності, інструкції); перенос з файл-менеджера CleverBox — cb_file_id."""
    __tablename__ = "product_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="doc")        # photo | doc
    mime: Mapped[str | None] = mapped_column(String(100))
    cb_file_id: Mapped[int | None] = mapped_column(Integer, index=True)
    uploaded_by: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StockDocFile(Base):
    """Скан / фото накладної, рахунку, ТТН до складського документа."""
    __tablename__ = "stock_doc_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("stock_docs.id"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str | None] = mapped_column(String(100))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BankTxn(Base):
    """Операція з виписки monobank (лише вихідні платежі постачальникам цікавлять склад)."""
    __tablename__ = "bank_txns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)    # id операції monobank
    fop: Mapped[str | None] = mapped_column(String(100), index=True)
    account: Mapped[str | None] = mapped_column(String(64))         # id рахунку monobank
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount: Mapped[float] = mapped_column(Float)                     # ₴, від'ємна — списання
    counter_name: Mapped[str | None] = mapped_column(String(300))
    counter_iban: Mapped[str | None] = mapped_column(String(40), index=True)
    counter_edrpou: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(String(500))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)   # new | matched | partial | ignored
    note: Mapped[str | None] = mapped_column(String(300))
    rejected: Mapped[str | None] = mapped_column(String(500))      # групи накладних, з яких платіж зняли вручну («не цей платіж»)


class PayRequest(Base):
    """Рахунок постачальника на оплату: з Telegram-чату або CRM → погодження власником → чернетка платежу в monobank (Corporate API)."""
    __tablename__ = "pay_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)   # recognizing | new | approved | draft | paid | declined | rejected | error
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(200))
    mime: Mapped[str | None] = mapped_column(String(100))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), index=True)
    receiver_name: Mapped[str | None] = mapped_column(String(300))
    receiver_iban: Mapped[str | None] = mapped_column(String(40))
    receiver_edrpou: Mapped[str | None] = mapped_column(String(20))
    payer_name: Mapped[str | None] = mapped_column(String(300))           # платник з рахунку
    fop: Mapped[str | None] = mapped_column(String(100))                  # ФОП CRM, з якого платимо
    sender_iban: Mapped[str | None] = mapped_column(String(40))
    amount: Mapped[float | None] = mapped_column(Float)
    vat: Mapped[float | None] = mapped_column(Float)
    invoice_no: Mapped[str | None] = mapped_column(String(80))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    purpose: Mapped[str | None] = mapped_column(String(420))
    corp_id: Mapped[str | None] = mapped_column(String(80))               # id платежу в monobank
    corp_state: Mapped[str | None] = mapped_column(String(30))            # DRAFT | DECLINED | IN_STATEMENT
    error: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str | None] = mapped_column(String(20))                # telegram | crm
    tg_chat: Mapped[str | None] = mapped_column(String(40))
    tg_msg: Mapped[int | None] = mapped_column(Integer)                   # повідомлення з рахунком
    tg_card: Mapped[int | None] = mapped_column(Integer)                  # картка бота з кнопками
    sender_name: Mapped[str | None] = mapped_column(String(200))          # хто надіслав у чат
    raw: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[int | None] = mapped_column(Integer)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    kind: Mapped[str | None] = mapped_column(String(12))            # None — рахунок; receipt — квитанція про оплату
    link_id: Mapped[int | None] = mapped_column(Integer)            # квитанція ↔ рахунок (взаємно)


class PaymentAlloc(Base):
    """Рознесення оплати: на документ і рядок (товар). Одна оплата може покривати кілька складів."""
    __tablename__ = "payment_allocs"
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[str | None] = mapped_column(ForeignKey("bank_txns.id"), index=True)   # None — відмічено вручну
    doc_id: Mapped[int] = mapped_column(ForeignKey("stock_docs.id"), index=True)
    line_id: Mapped[int | None] = mapped_column(ForeignKey("stock_doc_lines.id"))
    amount: Mapped[float] = mapped_column(Float)
    fop: Mapped[str | None] = mapped_column(String(100))
    by_user: Mapped[int | None] = mapped_column(Integer)      # хто розніс вручну; None + txn — автоматично (monobank)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def mode(self) -> str:
        """auto — monobank сам; mono_manual — платіж monobank рознесено вручну; manual — відмічено вручну."""
        return ("mono_manual" if self.by_user else "auto") if self.txn_id else "manual"


class InvoiceJob(Base):
    """Накладна в черзі на розпізнавання зовнішнім агентом (OpenClaw на VPS) через API v1 /invoices."""
    __tablename__ = "invoice_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str | None] = mapped_column(String(80))
    path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)   # pending | taken | done | error
    taken_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    kind: Mapped[str | None] = mapped_column(String(20))       # None — накладна; payreq — рахунок на оплату (PayRequest)
    ref_id: Mapped[int | None] = mapped_column(Integer)        # id PayRequest для kind=payreq


class Check(Base):
    """Чек CleverBox (/sales/checks) — джерело фактичної виручки."""
    __tablename__ = "checks"
    id: Mapped[int] = mapped_column(primary_key=True)             # = check_id CleverBox
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    cb_client_id: Mapped[int | None] = mapped_column(Integer, index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    author: Mapped[str | None] = mapped_column(String(200))
    items: Mapped[list | None] = mapped_column(JSON)
    payments: Mapped[list | None] = mapped_column(JSON)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="manual")   # manual | not_booked | care_ending | stock | follow_up
    source_key: Mapped[str | None] = mapped_column(String(100), unique=True)  # для автозадач (без дублів)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("beauty_plans.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))   # None = черга адміністраторів
    assignee_role: Mapped[str | None] = mapped_column(String(20))            # admin | owner — якщо без конкретної людини
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    priority: Mapped[str] = mapped_column(String(10), default="normal")      # low | normal | high
    status: Mapped[str] = mapped_column(String(20), default="open")          # open | done | cancelled
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)

    patient: Mapped[Patient | None] = relationship()
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])


# ---------------- Скрипти, розсилки, повідомлення ----------------

class ScriptTemplate(Base):
    """Шаблон скрипта для адміна. kind: not_booked | care_ending | follow_up | manual; channel: call | message."""
    __tablename__ = "script_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProcedureNote(Base):
    """Пояснення «навіщо дотримуватись інтервалу» для процедури (підставляється в скрипти)."""
    __tablename__ = "procedure_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(200))        # слова через «|»: «IPL|фотоомолодж»
    why: Mapped[str] = mapped_column(Text)                   # чому важливо не пропускати
    benefit: Mapped[str | None] = mapped_column(Text)        # що дасть курс


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    segment: Mapped[str] = mapped_column(String(30))
    filters: Mapped[dict | None] = mapped_column(JSON)       # doctor_id, source, min_ltv, max_ltv
    channels: Mapped[list] = mapped_column(JSON, default=list)   # порядок: telegram, viber, sms
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")   # draft|scheduled|sending|done|cancelled
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    attribution_days: Mapped[int] = mapped_column(Integer, default=14)

    recipients: Mapped[list["CampaignRecipient"]] = relationship(back_populates="campaign",
                                                                cascade="all, delete-orphan")


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    channel: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(100))
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|sent|delivered|read|failed|skipped
    skip_reason: Mapped[str | None] = mapped_column(String(100))
    message_id: Mapped[str | None] = mapped_column(String(100), index=True)
    error: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    status_at: Mapped[datetime | None] = mapped_column(DateTime)

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")
    patient: Mapped[Patient] = relationship()


class ChatMessage(Base):
    """Переписка з пацієнтами з Messenger / Instagram Direct (вебхук Meta) — центр спілкування."""
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    channel: Mapped[str] = mapped_column(String(20), index=True)          # facebook | instagram
    thread: Mapped[str] = mapped_column(String(80), index=True)           # PSID / IGSID співрозмовника
    direction: Mapped[str] = mapped_column(String(4), default="in")       # in | out
    text: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list | None] = mapped_column(JSON)
    mid: Mapped[str | None] = mapped_column(String(200), index=True)      # id повідомлення Meta (від дублів)
    name: Mapped[str | None] = mapped_column(String(200))                 # імʼя / @username співрозмовника
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), index=True)
    appeal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer)                  # хто відповів (out)
    status: Mapped[str | None] = mapped_column(String(20))                # out: sent | failed | paused
    error: Mapped[str | None] = mapped_column(String(300))
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class MessageLog(Base):
    """Кожне повідомлення пацієнту (з задачі, з картки, з розсилки) — для історії і ліміту частоти."""
    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    channel: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(String(300))
    marketing: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User | None] = relationship()


class CashShift(Base):
    """Касова зміна (як у CleverBox): відкривається на обрані каси із залишками на початок, закривається із залишками на кінець."""
    __tablename__ = "cash_shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    opened_by: Mapped[int | None] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    closed_by: Mapped[int | None] = mapped_column(Integer)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    location_id: Mapped[int | None] = mapped_column(Integer)
    boxes: Mapped[list | None] = mapped_column(JSON)          # [{id, name, type, open, close}]
    summary: Mapped[dict | None] = mapped_column(JSON)        # на закритті: чеків, сума, по способах оплати
    note: Mapped[str | None] = mapped_column(Text)


class PayPlan(Base):
    """Платіжний календар: регулярний платіж (шаблон) — оренда 13-го, ЄП 20-го, ЗП 15-го і 30-го, лізинг щомісяця.
    schedule: monthly (day) | twice (day, day2) | weekly (weekday 0–6) | quarterly (day, місяці 1/4/7/10) | once (start)."""
    __tablename__ = "pay_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    group: Mapped[str] = mapped_column(String(20), default="opex", index=True)      # opex | marketing | fot | tax | debt | purchase | capex
    article: Mapped[str | None] = mapped_column(String(80))                         # стаття фіноперацій (для авто-зіставлення з оплатою)
    cashbox: Mapped[str | None] = mapped_column(String(100))                        # з якого рахунку платимо
    counterparty: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[float | None] = mapped_column(Float)                             # планова сума (None — змінна)
    schedule: Mapped[str] = mapped_column(String(12), default="monthly")
    day: Mapped[int | None] = mapped_column(Integer)
    day2: Mapped[int | None] = mapped_column(Integer)
    weekday: Mapped[int | None] = mapped_column(Integer)
    start: Mapped[date | None] = mapped_column(Date)
    end: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False)            # без «Погоджено» адмін не платить
    note: Mapped[str | None] = mapped_column(String(300))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PayItem(Base):
    """Платіж на конкретну дату: з шаблону, разовий, з рахунку постачальника (payreq) або з нарахування ЗП."""
    __tablename__ = "pay_items"
    __table_args__ = (UniqueConstraint("plan_id", "due", name="uq_pay_item_plan_due"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("pay_plans.id"), index=True)
    due: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(200))
    group: Mapped[str] = mapped_column(String(20), default="opex", index=True)
    article: Mapped[str | None] = mapped_column(String(80))
    cashbox: Mapped[str | None] = mapped_column(String(100))
    counterparty: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[float | None] = mapped_column(Float)                             # план
    status: Mapped[str] = mapped_column(String(12), default="planned", index=True)  # planned | approved | paid | skipped
    approved_by: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    finop_id: Mapped[int | None] = mapped_column(Integer, index=True)               # оплата — фіноперація CRM
    paid_at: Mapped[date | None] = mapped_column(Date)
    amount_fact: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(12), default="plan")                 # plan | manual | payreq | salary
    ref: Mapped[str | None] = mapped_column(String(60), index=True)                 # payreq:<id>
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    plan: Mapped[PayPlan | None] = relationship()


class AdsStat(Base):
    """Статистика рекламного кабінету (Google Ads): день × рівень (account | campaign | adgroup | keyword | term | device | geo | hour)."""
    __tablename__ = "ads_stats"
    __table_args__ = (UniqueConstraint("source", "level", "day", "key", name="uq_ads_stat"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(12), default="google", index=True)
    level: Mapped[str] = mapped_column(String(12), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    key: Mapped[str] = mapped_column(String(200))                     # id кампанії / групи / ключа, текст запиту…
    name: Mapped[str | None] = mapped_column(String(400))
    parent: Mapped[str | None] = mapped_column(String(400))           # кампанія (для груп, ключів, запитів)
    status: Mapped[str | None] = mapped_column(String(30))
    clicks: Mapped[float] = mapped_column(Float, default=0)
    impressions: Mapped[float] = mapped_column(Float, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)             # у валюті кабінету, без ПДВ
    conversions: Mapped[float] = mapped_column(Float, default=0)
    conv_value: Mapped[float] = mapped_column(Float, default=0)
    extra: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdsTopup(Base):
    """Поповнення рекламного кабінету: вручну або з виписки банку. credited — скільки зайшло на баланс (без ПДВ)."""
    __tablename__ = "ads_topups"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(12), default="google", index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)           # сплачено (з ПДВ)
    credited: Mapped[float] = mapped_column(Float, default=0)         # зараховано на баланс
    origin: Mapped[str] = mapped_column(String(12), default="manual")  # manual | bank
    ref: Mapped[str | None] = mapped_column(String(80), index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GaEvent(Base):
    """Подія GA4 по хвилинах (клік по номеру, форма…) з джерелом і кампанією сеансу — для привʼязки дзвінків до реклами."""
    __tablename__ = "ga_events"
    __table_args__ = (UniqueConstraint("at", "event", "source", "medium", "campaign", "keyword", name="uq_ga_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)         # хвилина події (час ресурсу GA4)
    event: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(120), default="")
    medium: Mapped[str] = mapped_column(String(120), default="")
    campaign: Mapped[str] = mapped_column(String(300), default="")
    keyword: Mapped[str] = mapped_column(String(300), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)

