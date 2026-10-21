"""Розділ «Задачі»: ручні задачі + автозадачі з бюті-планів і складу, автозакриття, видимість за ролями."""
from __future__ import annotations

from datetime import datetime, date, timedelta

from sqlalchemy import select, or_, and_, false
from sqlalchemy.orm import Session

from ..models import Task, User, Patient
from .kpi import all_statuses
from .catalog import product_rows

KINDS = {"manual": "Ручна", "not_booked": "Записати на процедуру", "care_ending": "Повторний догляд",
         "stock": "Дозамовити товар", "follow_up": "Звʼязатися з клієнтом"}
PRIORITY = {"high": "високий", "normal": "звичайний", "low": "низький"}


def visible_query(user: User):
    q = select(Task)
    if user.role == "owner":
        return q
    if user.role == "admin":
        return q.where(or_(Task.assignee_id == user.id, Task.assignee_role == "admin",
                           and_(Task.assignee_id.is_(None), Task.assignee_role.is_(None))))
    # лікар: свої задачі + задачі по його пацієнтах
    pats = select(Patient.id).where(Patient.doctor_id == user.doctor_id) if user.doctor_id else select(Patient.id).where(false())
    return q.where(or_(Task.assignee_id == user.id, Task.patient_id.in_(pats)))


def generate_auto(db: Session, now: datetime | None = None) -> dict:
    """Створює автозадачі (без дублів) і закриває ті, що вже неактуальні."""
    now = now or datetime.now()
    week = f"{now.isocalendar().year}W{now.isocalendar().week:02d}"
    created = closed = 0
    statuses = all_statuses(db)
    open_auto = {t.source_key: t for t in db.scalars(select(Task).where(Task.status == "open", Task.source_key.is_not(None)))}
    still_actual: set[str] = set()

    def ensure(key_prefix: str, key: str, **kw):
        nonlocal created
        # уже є відкрита задача з тим самим префіксом (напр. той самий план) — нову не створюємо
        if any(k.startswith(key_prefix) for k in open_auto):
            still_actual.update(k for k in open_auto if k.startswith(key_prefix))
            return
        if db.scalar(select(Task).where(Task.source_key == key)):
            return                                   # цього тижня вже була (виконана/скасована)
        t = Task(source_key=key, **kw)
        db.add(t)
        open_auto[key] = t
        still_actual.add(key)
        created += 1

    for ps in statuses:
        plan, pat = ps.plan, ps.plan.patient
        if plan.status != "active":
            continue
        nxt = ps.next_procedure
        if nxt and not ps.is_booked and ps.due_date and ps.due_date <= now:
            ensure(f"nb:{plan.id}:", f"nb:{plan.id}:{week}", kind="not_booked", patient_id=pat.id, plan_id=plan.id,
                   title=f"Записати {pat.full_name} на «{nxt.item.name}»",
                   description=f"Бюті-план від {plan.session_at:%d.%m.%Y}" if plan.session_at else None,
                   assignee_role="admin", due_date=now.date(),
                   priority="high" if ps.flag == "red" else "normal")
        for st in ps.items:
            it = st.item
            if it.kind != "product" or not st.last_done or not it.supply_days or st.done < it.qty:
                continue
            ends = st.last_done + timedelta(days=it.supply_days)
            if ends - timedelta(days=10) <= now <= ends + timedelta(days=30):
                ensure(f"care:{it.id}:{st.last_done:%Y%m%d}", f"care:{it.id}:{st.last_done:%Y%m%d}",
                       kind="care_ending", patient_id=pat.id, plan_id=plan.id,
                       title=f"Запропонувати {pat.full_name} повторно «{it.name[:80]}»",
                       description=f"Закінчується ≈ {ends:%d.%m.%Y}", assignee_role="admin",
                       due_date=max(now.date(), (ends - timedelta(days=7)).date()))

    deficits = [r for r in product_rows(db, statuses, now) if r.deficit]
    if deficits:                                   # одна зведена задача на тиждень, а не по кожному товару
        lines = [f"• {r.product.name[:70]} — {r.deficit} шт (залишок {r.stock:g}, потреба {r.need_30})" for r in deficits]
        ensure("stock:", f"stock:{week}", kind="stock",
               title=f"Дозамовити товари для бюті-планів: {len(deficits)} поз.",
               description="\n".join(lines[:25]), assignee_role="owner", due_date=now.date())

    # автозакриття: умова зникла (пацієнт записався, купив, склад поповнено)
    for key, t in open_auto.items():
        if key not in still_actual and t.id:
            t.status, t.done_at = "done", now
            t.result = "Закрито автоматично: умова більше не актуальна (запис/покупка/поповнення)"
            closed += 1
    db.commit()
    return {"created": created, "closed": closed}


def counts_for(db: Session, user: User) -> dict:
    today = date.today()
    q = visible_query(user).where(Task.status == "open")
    tasks = db.scalars(q).all()
    return {"open": len(tasks), "overdue": sum(1 for t in tasks if t.due_date and t.due_date < today),
            "mine": sum(1 for t in tasks if t.assignee_id == user.id)}
