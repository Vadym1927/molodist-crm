import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not (Path(__file__).parent / "sample_plan.pdf").exists(),
                                reason="покладіть будь-який PDF бюті-плану Observ у tests/sample_plan.pdf")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.models import init_db, SessionLocal, Visit, ProductSale  # noqa: E402
from app.parsers.observ import parse_observ_pdf  # noqa: E402
from app.services.plans import import_plan  # noqa: E402
from app.services.kpi import compute_plan, kpi_summary, forecast  # noqa: E402

PDF = Path(__file__).parent / "sample_plan.pdf"


def test_parse():
    p = parse_observ_pdf(PDF)
    assert p.patient_name == "Олена Даниленко"
    assert p.doctor_name == "Дарʼя Колган"
    assert p.total == 26450
    assert [(i.name, i.qty, i.unit_price) for i in p.procedures] == [
        ("Гігієна обличчя", 1, 2800), ("IPL -терапія обличчя", 4, 3900)]
    assert len(p.products) == 5 and sum(i.total for i in p.items) == 26450
    azel = next(i for i in p.products if "Azelorosa" in i.name)
    assert azel.usage == ["evening"] and "azelorosa" in azel.url


def test_realization_and_forecast():
    init_db()
    db = SessionLocal()
    plan = import_plan(db, parse_observ_pdf(PDF), "Даниленко Олена - BeautyPlan Anti Age.pdf")
    pat = plan.patient
    s0 = plan.session_at
    now = s0 + timedelta(days=20)
    # гігієна виконана, 1 IPL виконано, 1 IPL заброньовано, купили очищувальний гель
    db.add_all([
        Visit(source="cb_service_sale", external_id="1", patient_id=pat.id, service_name="Гігієна обличчя комбінована",
              starts_at=s0 + timedelta(days=3), status="done"),
        Visit(source="cb_service_sale", external_id="2", patient_id=pat.id, service_name="IPL терапія обличчя",
              starts_at=s0 + timedelta(days=10), status="done"),
        Visit(source="cb_visit", external_id="3", patient_id=pat.id, service_name="IPL терапія обличчя",
              starts_at=now + timedelta(days=15), status="booked"),
        ProductSale(external_id="p1", patient_id=pat.id,
                    product_name="Зволожуючий очищувальний гель AG SKIN Regular Moisturizing Cleansing Gel 245 мл",
                    quantity=1, amount=1350, sold_at=s0),
    ])
    db.commit()
    db.refresh(pat)
    ps = compute_plan(db, plan, now=now)
    assert (ps.proc_planned, ps.proc_done, ps.proc_booked) == (5, 2, 1)
    assert ps.prod_bought == 1
    assert ps.realized_sum == 2800 + 3900 + 1350
    assert ps.realization_pct == round(100 * 8050 / 26450, 1)
    assert ps.is_booked and ps.flag == "green"
    total, by_doc = kpi_summary([ps])
    assert by_doc[0].label == "Дарʼя Колган" and total.booked_pct == 100.0
    fc = forecast([ps], months=4, now=now)
    assert sum(r["confirmed"] for r in fc) == 3900
    assert sum(r["expected"] for r in fc) == 2 * 3900 + (1750 + 1800 + 1700 + 1450)
    supply = {i.name[:20]: i.supply_days for i in plan.items if i.kind == "product"}
    assert all(14 <= v <= 365 for v in supply.values())


def test_reminders():
    from app.services import reminders
    db = SessionLocal()
    plan = import_plan(db, parse_observ_pdf(PDF), "x.pdf")
    plan.session_at = datetime.now() - timedelta(days=60)
    pat = plan.patient
    db.add(ProductSale(external_id="s", patient_id=pat.id, product_name="AG SKIN Azelorosa Rosy Cream Gel 50 мл",
                       quantity=1, amount=1700, sold_at=datetime.now() - timedelta(days=58)))
    next(i for i in plan.items if "Azelorosa" in i.name).supply_days = 65   # закінчиться через ~7 днів
    db.commit(); db.refresh(pat)
    rems = reminders.pending(db)
    kinds = sorted(r.kind for r in rems)
    assert kinds == ["care_ending", "not_booked"]
    assert all("Пролактин" not in r.text and "Достінекс" not in r.text for r in rems)   # мед. дані не йдуть у ТГ
    msgs = reminders.digest_messages(rems)
    assert msgs and all(len(m) < 4096 for m in msgs)
    reminders.mark_sent(db, rems)
    assert reminders.pending(db) == []        # повторно в той самий тиждень не шлемо
