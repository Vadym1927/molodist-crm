"""Автознижка −25% на одиниці вітрини з терміном < 12 міс: каса CRM (обрана партія) і сайт (лише к-сть короткої партії)."""
from datetime import date, datetime, timedelta

from app.models import SessionLocal, Product, SyncState, Patient, Appointment
from app.services import stock as st, sales as sl, shop_front as sf, short_expiry as sx
from app.services.patient_match import by_visit_day


def test_short_expiry_pos_and_site():
    today = date.today()
    with SessionLocal() as db:
        db.query(SyncState).filter(SyncState.key.in_(("stock_ledger_start", sx.KEY))).delete(); db.commit()
        p = Product(name="Короткий крем SX", name_norm="короткий крем sx", warehouse="vitrina", price=1000, site_on=True,
                    site_info={"slug": "short-sx", "title": "Короткий крем SX", "presence": 1})
        db.add(p); db.commit()
        st.start_ledger(db)
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="SX")
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=1, unit_cost=300, expiry_date=today + timedelta(days=100)))
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=1, unit_cost=300, expiry_date=today + timedelta(days=500)))
        db.commit(); st.post(db, doc)
        short_e, long_e = today + timedelta(days=100), today + timedelta(days=500)
        assert sx.short_stock(db)[p.id]["qty"] == 1
        # каса: коротка партія — −25%, довга — без знижки; к-сть понад залишок партії знижку не отримує
        it = {"kind": "product", "ref_id": p.id, "name": p.name, "qty": 1, "price": 1000, "expiry": short_e}
        q = sl.quote(db, [it], None)
        assert q["discount"] == 250 and any("Короткий термін" in r for r in q["rules"])
        assert sl.quote(db, [{**it, "expiry": long_e}], None)["discount"] == 0
        assert sl.quote(db, [{**it, "qty": 2}], None)["discount"] == 250
        assert sl.quote(db, [it], None, no_discounts=True)["discount"] == 0
        # сайт: ціна зі знижкою, у кошику знижка лише на 1 шт
        data = sf.catalog(db, fresh=True)
        v = next(v for i in data["items"] for v in i["variants"] if v["id"] == p.id)
        assert v["price"] == 750 and v["old"] == 1000 and v["short"] == 1
        cart = sf.price_cart(db, [{"id": p.id, "qty": 2}])
        assert cart["total"] == 1750 and cart["items"][0]["short"] == 1
        assert sf.price_cart(db, [{"id": p.id, "qty": 1}])["total"] == 750
        # продали коротку одиницю — знижка зникає
        sale = sl.create(db, {"items": [it], "payments": [{"method": "Готівка", "amount": 750}]})
        db.commit()
        assert sale.discount == 250 and p.id not in sx.short_stock(db)
        assert sf.catalog(db, fresh=True)["items"] and next(v for i in sf.catalog(db, fresh=True)["items"] for v in i["variants"] if v["id"] == p.id)["price"] == 1000
        sx.save(db, {"on": "", "months": 12, "pct": 25})
        assert sx.cfg(db)["on"] is False
        db.query(SyncState).filter(SyncState.key.in_(("stock_ledger_start", sx.KEY))).delete(); db.commit()


def test_plan_patient_by_visit_day():
    with SessionLocal() as db:
        p = Patient(full_name="Мирослава", phone="380951112233")
        db.add(p); db.flush()
        db.add(Appointment(patient_id=p.id, start=datetime.now().replace(hour=12, minute=0), end=datetime.now().replace(hour=13, minute=0)))
        db.commit()
        assert by_visit_day(db, "Мирослава Кирилюкова", date.today()).id == p.id
        assert by_visit_day(db, "Мирослава Кирилюкова", date.today() - timedelta(days=3)) is None


def test_tg_plan_without_phone_does_not_create_card():
    from pathlib import Path
    from app.parsers.observ import parse_observ_pdf
    from app.services import ingest
    pdf = Path(__file__).parent / "sample_plan.pdf"
    if not pdf.exists():
        return
    parsed = parse_observ_pdf(pdf)
    if parsed.patient_phone:
        return
    with SessionLocal() as db:
        from app.services.plans import find_patient
        if find_patient(db, parsed.patient_name, None):    # пацієнтка вже є в базі тестів — сценарій не перевірити
            return
        n0 = db.query(Patient).count()
        plan, _, err = ingest.ingest_pdf(db, pdf.read_bytes(), "plan.pdf", "-100777")
        assert plan is None and isinstance(err, ingest.Attached) and "не створено" in err and err.file_id
        assert db.query(Patient).count() == n0


def test_sale_category_only_discounted():
    from app.services import shop_front as sf
    with SessionLocal() as db:
        data = sf.catalog(db, fresh=True)
        sale = data["by_path"].get("/shop/sale/")
        if sale:
            assert all(data["by_slug"][s]["old"] for s in sale["slugs"])
