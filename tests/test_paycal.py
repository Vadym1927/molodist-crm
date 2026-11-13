"""Платіжний календар: шаблони → платежі місяця, рахунки постачальників, авто-зіставлення з фінопераціями, оплата, дайджест, сторінка."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import SessionLocal, FinOp, PayItem, PayPlan, PayRequest
from app.services import paycal as pc


def test_plan_dates():
    p = PayPlan(name="x", schedule="monthly", day=31)
    assert pc.plan_dates(p, 2026, 2) == [date(2026, 2, 28)]
    p = PayPlan(name="x", schedule="twice", day=15, day2=30)
    assert pc.plan_dates(p, 2026, 11) == [date(2026, 11, 15), date(2026, 11, 30)]
    p = PayPlan(name="x", schedule="weekly", weekday=0)
    assert len(pc.plan_dates(p, 2026, 11)) == 5 and all(d.weekday() == 0 for d in pc.plan_dates(p, 2026, 11))
    p = PayPlan(name="x", schedule="quarterly", day=20)
    assert pc.plan_dates(p, 2026, 10) == [date(2026, 10, 20)] and pc.plan_dates(p, 2026, 11) == []
    p = PayPlan(name="x", schedule="once", start=date(2026, 11, 5))
    assert pc.plan_dates(p, 2026, 11) == [date(2026, 11, 5)] and pc.plan_dates(p, 2026, 12) == []
    p = PayPlan(name="x", schedule="monthly", day=10, start=date(2026, 11, 15))
    assert pc.plan_dates(p, 2026, 11) == [] and pc.plan_dates(p, 2026, 12) == [date(2026, 12, 10)]


def test_calendar_flow():
    settings.admin_password = "owner-pass-1"
    today = date.today()
    y, m = today.year, today.month
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            db.add(PayPlan(name="Оренда тест", group="opex", article="Оренда тест", cashbox="Готівка", amount=30060, schedule="monthly", day=13, start=date(y, 1, 1)))
            db.add(PayPlan(name="ЗП тест", group="fot", article="Персонал Зарплата", amount=None, schedule="twice", day=15, day2=30, needs_approval=True, start=date(y, 1, 1)))
            db.add(PayRequest(status="approved", receiver_name="Постачальник Тест", amount=4321, invoice_no="A-1", invoice_date=today, fop="ФОП Бондар"))
            db.commit()
            n = pc.ensure_month(db, y, m)
            assert n >= 4
            items = db.scalars(select(PayItem)).all()
            rent = next(i for i in items if i.name == "Оренда тест")
            assert rent.status == "approved" and rent.due == date(y, m, 13) and rent.amount == 30060
            zp = [i for i in items if i.name == "ЗП тест"]
            assert len(zp) == 2 and all(i.status == "planned" for i in zp)
            pr_item = next(i for i in items if i.ref and i.ref.startswith("payreq:"))
            assert pr_item.group == "purchase" and pr_item.amount == 4321 and pr_item.status == "approved"
            assert pc.ensure_month(db, y, m) == 0                                   # без дублів
            # оплата з журналу закриває платіж автоматично
            db.add(FinOp(day=date(y, m, 14), kind="expense", category="Оренда тест", amount=30060, cashbox="Готівка"))
            db.commit()
            v = pc.month_view(db, y, m, today=date(y, m, 20))
            rent = db.get(PayItem, rent.id)
            assert rent.status == "paid" and rent.amount_fact == 30060 and rent.finop_id and pc.deviation(rent) == 0
            assert v["paid"] >= 30060 and v["by_group"]["opex"]["paid"] >= 30060 and "forecast" in v and v["cash"] is not None
            # кнопка «Оплачено» створює фіноперацію
            op = pc.pay(db, zp[0], 12000, date(y, m, 15), "Готівка", None)
            assert op.kind == "expense" and op.category == "Персонал Зарплата" and op.ref == f"paycal:{zp[0].id}"
            assert db.get(PayItem, zp[0].id).status == "paid"
            # дайджест
            txt = pc.digest_text(db, today=date(y, m, 30))
            assert txt and "ЗП тест" in txt
        t = c.get(f"/finance/calendar?y={y}&m={m}").text
        assert "Платіжний календар" in t and "Оренда тест" in t and "Регулярні" in t
        r = c.post("/finance/calendar/item", data={"name": "Разовий тест", "group": "capex", "amount": "1 500", "due": today.isoformat()}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            it = db.scalar(select(PayItem).where(PayItem.name == "Разовий тест"))
            assert it.amount == 1500 and it.status == "approved" and it.source == "manual"
            iid = it.id
        r = c.post(f"/finance/calendar/item/{iid}/pay", data={"amount": "1400", "day": today.isoformat(), "cashbox": "Готівка"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            it = db.get(PayItem, iid)
            assert it.status == "paid" and it.amount_fact == 1400 and pc.deviation(it) == -100
        r = c.post("/finance/calendar/plan", data={"name": "Новий шаблон", "group": "tax", "schedule": "monthly", "day": "20", "amount": "1729,4"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            p = db.scalar(select(PayPlan).where(PayPlan.name == "Новий шаблон"))
            assert p.amount == 1729.4 and p.day == 20
            assert db.scalar(select(PayItem).where(PayItem.plan_id == p.id)) is not None or date(y, m, 20) < p.start
        assert 'href="/finance/calendar"' in c.get("/reports").text


def test_move_and_weekly_income():
    from app.models import Sale, SaleItem, SalePayment
    settings.admin_password = "owner-pass-1"
    today = date.today()
    wk = today - timedelta(days=today.weekday())
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            it = PayItem(due=today, name="Переносний тест", group="opex", amount=500, status="approved", source="manual")
            db.add(it)
            s = Sale(total=7000, subtotal=7000, status="paid", at=datetime.combine(wk, datetime.min.time()).replace(hour=12))
            s.items.append(SaleItem(kind="service", name="x", qty=1, price=7000, total=7000)); s.payments.append(SalePayment(method="Готівка", amount=7000))
            db.add(s); db.commit()
            iid = it.id
        new = today + timedelta(days=7)
        r = c.post(f"/finance/calendar/item/{iid}/move", data={"due": new.isoformat()}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            assert db.get(PayItem, iid).due == new
            v = pc.month_view(db, today.year, today.month)
            w = next(x for x in v["weeks"] if x["current"])
            assert w["inc_fact"] >= 7000 and w["inc_manual"] is None
        r = c.post("/finance/calendar/income-plan", data={"week": today.isoformat(), "amount": "100 000"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            v = pc.month_view(db, today.year, today.month)
            w = next(x for x in v["weeks"] if x["current"])
            assert w["inc_manual"] == 100000 and w["inc_plan"] == 100000 and w["inc_pct"] is not None
            pc.set_income_plan(db, wk, None)
            assert pc.income_plans(db).get(wk.isoformat()) is None
        t = c.get("/finance/calendar").text
        assert "Прихід по тижнях" in t and 'draggable="true"' in t or "Прихід по тижнях" in t


def test_money_now(monkeypatch):
    from app.integrations import monobank
    monkeypatch.setattr(monobank, "saved", lambda db: [{"fop": "Тест", "watch": ["a1"], "accounts": [
        {"id": "a1", "iban": "UA000000000000000000000001111", "currency": 980}, {"id": "a2", "iban": "UA9", "currency": 840}]}])
    monkeypatch.setattr(monobank, "cashbox_map", lambda db: {"00001111": "Опер рахунок"})
    boxes = [{"name": "Готівка ФОП Тест", "type": "cash", "balance": 1500.0}, {"name": "Готівка ФОП Інший", "type": "cash", "balance": 500.0},
             {"name": "Готівка Сейф", "type": "cash", "balance": 7000.0}, {"name": "Опер рахунок", "type": "bank", "balance": 9000.0},
             {"name": "Термінал ФОП Тест", "type": "card", "balance": 300.0}]
    with SessionLocal() as db:
        r = pc.money_now(db, boxes, mono_balances={"a1": {"balance": 10000.0, "at": "09.11 10:00"}})
    assert [x["name"] for x in r["mono"]] == ["Опер рахунок"] and r["mono"][0]["diff"] == 1000.0
    assert r["cash_total"] == 2000.0 and {c["fop"] for c in r["cash"]} == {"Тест", "Інший"}
    assert r["safe_total"] == 7000.0 and r["total"] == 19000.0
    with SessionLocal() as db:
        monkeypatch.setattr(monobank, "saved", lambda db: [])
        r = pc.money_now(db, boxes + [{"name": "Готівка Cейф", "type": "cash", "balance": 1.0}], mono_balances={})
    assert r["mono_crm"] and r["mono_total"] == 9000.0 and r["safe_total"] == 7001.0


def test_dynamic_salary_and_setup_v2():
    from app.models import Doctor, PayrollDoc, PayrollLine, SyncState
    from app.services import leasing as ls
    with SessionLocal() as db:
        d1 = Doctor(name="Лікар Дин", position="Лікар-косметолог")
        d2 = Doctor(name="Адмін Дин", position="Адміністратор")
        db.add_all([d1, d2])
        db.flush()
        doc = PayrollDoc(d_from=date(2026, 11, 1), d_to=date(2026, 11, 15))
        doc.lines = [PayrollLine(doctor_id=d1.id, amount=12000, posted=True), PayrollLine(doctor_id=d2.id, amount=5000, posted=True)]
        db.add(doc)
        p = PayPlan(name="Зарплата спеціалістів", group="fot", article="Персонал Зарплата", schedule="twice", day=1, day2=16,
                    needs_approval=True, start=date(2026, 11, 1))
        db.add(p)
        db.commit()
        pc.ensure_month(db, 2026, 11)
        pc.month_view(db, 2026, 11, today=date(2026, 11, 17))
        parents = db.scalars(select(PayItem).where(PayItem.plan_id == p.id)).all()
        assert all(i.ref == "split-parent" for i in parents)
        mine = sorted(db.scalars(select(PayItem).where(PayItem.ref == f"sal:{p.id}:{d1.id}")).all(), key=lambda i: i.due)
        assert mine[0].status == "skipped" and mine[1].amount == 12000 and mine[1].name == "Зарплата · Лікар Дин"
        assert not db.scalars(select(PayItem).where(PayItem.ref == f"sal:{p.id}:{d2.id}")).all()      # адмін — у своєму платежі
        pc.pay(db, mine[1], 12000, date(2026, 11, 17), "Готівка", None)
        from app.services import payroll_docs
        assert payroll_docs.balances(db)[d1.id] == 0 and payroll_docs.balances(db)[d2.id] == 5000
        # закупівлі
        db.add(FinOp(day=date(2026, 11, 5), kind="expense", category="Закупівля косметики", amount=7000))
        db.commit()
        assert pc.purchases(db, date(2026, 11, 1), date(2026, 11, 30))["total"] >= 7000
        # налаштування v2 + планшет 6 500 / міс
        st = db.get(SyncState, pc.SETUP2_KEY)
        if st:
            db.delete(st)
            db.commit()
        if not ls.contracts(db):
            ls.ensure_setup(db)
        assert pc.setup_v2(db, today=date(2026, 11, 9))
        names = {x.name for x in db.scalars(select(PayPlan))}
        assert {"Оренда робочих місць", "Checkbox ФОП Колган", "Ringostat", "Домени", "Військовий збір ФОП Бондар"} <= names
        tab = next(r for r in ls.build(db, today=date(2026, 11, 9))["rows"] if r["group"] == "tablet")
        assert [s["amount"] for s in tab["sched"] if s["status"] != "paid"] == [6500, 6500, 6500, 6500, 4000]
        assert tab["sched"][0]["due"] == date(2026, 11, 15)


def test_edit_name_and_copy():
    settings.admin_password = "owner-pass-1"
    today = date.today()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            it = PayItem(due=today, name="Оренда тест", group="opex", article="Оренда", cashbox="Готівка", amount=1000, status="approved", source="plan", note="н")
            paid = PayItem(due=today, name="Оплачений тест", group="opex", amount=500, amount_fact=520, status="paid", source="manual")
            db.add_all([it, paid]); db.commit()
            iid, pid = it.id, paid.id
        assert c.post(f"/finance/calendar/item/{iid}/edit", data={"name": "  Оренда  кабінету ", "amount": "1200", "due": today.isoformat(), "note": ""},
                      follow_redirects=False).status_code == 303
        nxt = today + timedelta(days=40)
        assert c.post(f"/finance/calendar/item/{iid}/copy", data={"name": "Оренда кабінету (грудень)", "amount": "1300", "due": nxt.isoformat(), "note": "копія"},
                      follow_redirects=False).status_code == 303
        c.post(f"/finance/calendar/item/{pid}/copy", data={"due": today.isoformat()})                       # копія оплаченого — як новий план
        c.post(f"/finance/calendar/item/{pid}/edit", data={"name": "Зламати", "amount": "1", "due": today.isoformat()})   # оплачений не редагується
        with SessionLocal() as db:
            assert db.get(PayItem, iid).name == "Оренда кабінету" and db.get(PayItem, iid).amount == 1200
            cp = db.query(PayItem).filter(PayItem.name == "Оренда кабінету (грудень)").one()
            assert cp.due == nxt and cp.amount == 1300 and cp.article == "Оренда" and cp.cashbox == "Готівка" and cp.status == "approved" and cp.source == "manual" and cp.plan_id is None
            cp2 = db.query(PayItem).filter(PayItem.name == "Оплачений тест", PayItem.id != pid).one()
            assert cp2.status == "approved" and cp2.amount == 500 and cp2.finop_id is None
            assert db.get(PayItem, pid).name == "Оплачений тест"
        t = c.get("/finance/calendar").text
        assert "⧉" in t and 'id="edit-name"' in t
