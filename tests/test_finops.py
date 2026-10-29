"""Фінанси: фіноперації, продажі за день / період, нарахування й взаєморозрахунки з/п, баланс і бонуси."""
from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, Doctor, Patient, FinOp
from app.services import finops as fo


def test_finance_sections():
    settings.admin_password = "owner-pass-1"
    today = date.today()
    mk = fo.month_key(today)
    with SessionLocal() as db:
        d = Doctor(name="Зарплатна Лікарка", active=True)
        p = Patient(full_name="Бонусна Пацієнтка", phone="+380500000111", cb_balance=1500, cb_bonus=250)
        db.add_all([d, p]); db.commit()
        db.add(Check(id=990001, date=datetime.now(), amount=3000, discount=100, patient_id=p.id, author="Адмін Тест",
                     items=[{"item_type": "service", "item_name": "Чистка обличчя", "payd_amount": 2000, "initiator_name": "Лікарка Зарплатна"},
                            {"item_type": "product", "item_name": "Крем SPF", "payd_amount": 1000, "initiator_name": "Лікарка Зарплатна"}],
                     payments=[{"payment_name": "Картка", "amount": 2800}, {"payment_name": "Бонуси", "amount": 200}]))
        db.commit(); did, pid = d.id, p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for u in ("/finance", "/finance/ops", "/finance/day", "/finance/sales", "/finance/payroll", "/finance/settle",
                  "/finance/balances", "/finance/bonuses", "/finance/pricing"):
            r = c.get(u)
            assert r.status_code == 200 and "Взаєморозрахунки по з/п" in r.text, u
        assert "Чистка обличчя" in c.get("/finance/day").text and "Бонусна Пацієнтка" in c.get("/finance/sales?q=бонусна").text
        ops = c.get("/finance/ops").text
        assert "Журнал фіноперацій" in ops and "2 800,00 ₴" in ops and "не гроші" in ops and "monobank" not in ops.split("<tbody>")[1]
        c.post("/finance/payroll/rule", data={"doctor_id": did, "pct_services": "40", "pct_products": "10", "base": "5000", "m": mk})
        c.post("/finance/payroll/adj", data={"doctor_id": did, "month": mk, "amount": "300", "sign": "+", "comment": "план"})
        assert "6 200,00 ₴" in c.get(f"/finance/payroll?m={mk}").text            # 5000 + 40%·2000 + 10%·1000 + 300
        c.post("/finance/ops", data={"kind": "expense", "category": "Зарплата", "doctor_id": str(did), "salary_month": mk,
                                     "amount": "2000", "cashbox": "Готівка", "day": str(today), "back": f"/finance/settle?m={mk}"})
        st = c.get(f"/finance/settle?m={mk}").text
        assert "4 200,00 ₴" in st                                                 # 6200 − 2000
        assert "1 500,00 ₴" in c.get("/finance/balances").text and "200,00 ₴" in c.get("/finance/bonuses").text
    with SessionLocal() as db:
        rows = fo.settlements(db, mk)
        r = next(x for x in rows if x["doctor"].id == did)
        assert r["accrued"] == 6200 and r["paid"] == 2000 and r["closing"] == 4200
        assert db.query(FinOp).filter_by(doctor_id=did, category="Зарплата").count() == 1


def test_journal_transfer_storno_and_cb_import(tmp_path):
    from openpyxl import Workbook
    from app.models import CbFinOp
    settings.admin_password = "owner-pass-1"
    wb = Workbook(); ws = wb.active
    ws.append(["Журнал фіноперацій"])
    ws.append(["Дата", "Тип", "Від кого/ кому", "Стаття", "Філія", "Рахунок", "Вид", "Автор", "Сума"])
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    ws.append([now, "Витрата #32263", "PRX-T33", "Закупка розхідні матеріали", "Molodist_", "Операційний Фонд", "", "Operations Financial", "6 240,00 ₴"])
    ws.append([now, "Дохід #32261/12067", "Котик Вероніка підстава: Продаж #12067", "Оплата послуг, товарів", "Molodist_", "Готівка ФОП Колган", "Готівка ФОП Колган", "Дзеціна Вікторія", "2500"])
    p = tmp_path / "fin.xlsx"; wb.save(p)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        import time
        from app.services import jobs
        for _ in range(2):                                                      # двічі — без дублів
            with open(p, "rb") as fh:
                r = c.post("/finance/ops/import", files={"file": ("fin.xlsx", fh, "application/octet-stream")}, follow_redirects=False)
            jid = r.headers["location"].split("job=")[1]
            for _ in range(100):
                if jobs.JOBS[jid].status != "running":
                    break
                time.sleep(0.05)
            assert jobs.JOBS[jid].status == "done", jobs.JOBS[jid].error
        assert "імпортовано" in c.get(f"/finance/ops?job={jid}").text
        with SessionLocal() as db:
            x = db.get(CbFinOp, 32263); y = db.get(CbFinOp, 32261)
            assert x.kind == "expense" and x.amount == 6240 and y.kind == "income" and y.basis.strip() == "Продаж #12067"
            assert db.query(CbFinOp).count() == 2
        page = c.get("/finance/ops").text
        assert "#32263" in page and "PRX-T33" in page and "Операційний Фонд" in page
        r = c.post("/finance/ops", data={"kind": "transfer", "amount": "1000", "cashbox": "Готівка", "cashbox_to": "Інше"},
                   follow_redirects=False)
        assert "msg=" in r.headers["location"]
        with SessionLocal() as db:
            t = db.query(FinOp).filter_by(kind="transfer").one(); tid = t.id
        assert "Переміщення" in c.get("/finance/ops?kind=transfer").text
        bad = c.post("/finance/ops/storno", data={"num": f"CRM-{tid}", "comment": ""}, follow_redirects=False)
        assert "err=" in bad.headers["location"]
        c.post("/finance/ops/storno", data={"num": f"CRM-{tid}", "comment": "помилка"})
        assert "сторновано" in c.get("/finance/ops").text
        assert c.get("/finance/ops/export.xlsx").status_code == 200
        assert "PRX-T33" not in c.get("/finance/ops?src=crm").text
