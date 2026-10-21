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
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(64), index=True)
    cleverbox_id: Mapped[int | None] = mapped_column(Integer, unique=True)
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
    # розширений профіль клієнта (ручне ведення або імпорт з CRM)
    email: Mapped[str | None] = mapped_column(String(200))
    gender: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(300))
    communication_channel: Mapped[str | None] = mapped_column(String(30))
    client_groups: Mapped[list | None] = mapped_column(JSON)
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

    patient: Mapped[Patient | None] = relationship(back_populates="files")


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
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"))   # для ролі «лікар»
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    min_stock: Mapped[int | None] = mapped_column(Integer)        # мінімальний залишок (для замовлення)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


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
