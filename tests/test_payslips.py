"""Розрахункові листи: нараховано, премії, податки, утримання, до виплати, виплачено на руки."""
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, FinOp, PayrollDoc, PayrollLine, SalaryAdj, SalaryRule
from app.services import payslips as ps, finops as fo


def test_payslip_month():
    settings.admin_password = "owner-pass-1"
    mk = fo.month_key(date.today())
    with SessionLocal() as db:
        ps.set_manual_only(db, False)                               # авто-режим (ставка + комісійні з документів)
        d = Doctor(name="Розрахунок Тест", active=True, position="Адміністратор")
        db.add(d); db.flush()
        db.add(SalaryRule(doctor_id=d.id, base=99000))             # авторозрахунок у листи не потрапляє
        a0 = date.today().replace(day=1)
        for x, y in ((1, 15), (16, 28)):
            db.add(PayrollDoc(d_from=a0.replace(day=x), d_to=a0.replace(day=y), posted=True, total=10000, lines=[
                PayrollLine(doctor_id=d.id, calc=10000, amount=10000, days=5, posted=True, detail={"fixed": 10000.0})]))
        db.add(PayrollDoc(d_from=a0, d_to=a0.replace(day=15), posted=False, total=5000, lines=[
            PayrollLine(doctor_id=d.id, calc=5000, amount=5000, posted=False, detail={"fixed": 5000.0})]))   # не на балансі
        db.add_all([SalaryAdj(doctor_id=d.id, month=mk, amount=3000, article="Премія", comment="план"),
                    SalaryAdj(doctor_id=d.id, month=mk, amount=-2500, article="Податки"),
                    SalaryAdj(doctor_id=d.id, month=mk, amount=-500, article="Штраф", comment="запізнення")])
        from app.services.fin_articles import salary_name
        db.add(FinOp(day=date.today(), kind="expense", category=salary_name(db), amount=15000, doctor_id=d.id, salary_month=mk, cashbox="Готівка"))
        db.commit()
        fo.set_payroll_start(db, mk)
        v = ps.build(db, mk)
        r = next(x for x in v["rows"] if x["doctor"].id == d.id)
        assert (r["base"], r["bonus"], r["gross"], r["tax"], r["hold"], r["net"], r["paid"], r["closing"]) == (20000, 3000, 23000, -2500, -500, 20000, 15000, 5000)
        m, a, b = (next(x for x in ps.build(db, mk, h)["rows"] if x["doctor"].id == d.id) for h in (0, 1, 2))
        assert a["base"] == 10000 and b["base"] == 10000
        for k in ("gross", "tax", "hold", "net", "paid"):
            assert round(a[k] + b[k], 2) == m[k], k
        assert a["opening"] == m["opening"] and b["opening"] == a["closing"] and b["closing"] == m["closing"]
        assert v["total"]["gross"] >= 23000 and ps.to_xlsx(v)[:2] == b"PK"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get(f"/finance/payslips?m={mk}").text
        assert "Розрахункові листи" in h and "Розрахунок Тест" in h and "запізнення" in h and "Виплачено на руки" in h
        x = c.get(f"/finance/payslips?m={mk}&xlsx=1")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        did = next(x["doctor"].id for x in v["rows"] if x["doctor"].name == "Розрахунок Тест")
        s = c.get(f"/finance/payslips/slip?m={mk}&h=1&doctor={did}")
        assert s.status_code == 200 and "Розрахунковий лист" in s.text and "Розрахунок Тест" in s.text and "15." in s.text
        assert c.get(f"/finance/payslips/slip?m={mk}").status_code == 200
        assert "16–кінець" in c.get(f"/finance/payslips?m={mk}&h=2").text

        j = c.get(f"/finance/payslips/tg?m={mk}&h=0&doctor={did}").json()
        assert j["chat"] is False and "Розрахунковий лист" in j["text"] and "20 000,00" in j["text"] and "запізнення" in j["text"]
        assert c.post("/finance/payslips/tg", data={"m": mk, "h": 0, "doctor": did}).status_code == 400
        with SessionLocal() as db:
            db.get(Doctor, did).telegram_chat_id = "777"
            db.commit()
        from app.services import tg_notify
        sent, orig = [], tg_notify.send_text
        tg_notify.send_text = lambda chat, text: sent.append((chat, text)) or True
        assert c.post("/finance/payslips/tg", data={"m": mk, "h": 0, "doctor": did}).json()["ok"] and sent[0][0] == "777"
        tg_notify.send_text = orig


def test_payslip_manual_only():
    from app.models import SalaryAdj
    mk = fo.month_key(date.today())
    with SessionLocal() as db:
        d = Doctor(name="Ручна Тест", active=True); db.add(d); db.flush()
        db.add(SalaryRule(doctor_id=d.id, base=50000))
        db.add_all([SalaryAdj(doctor_id=d.id, month=mk, amount=38940.22, article="Зарплата"),
                    SalaryAdj(doctor_id=d.id, month=mk, amount=-500, article="Податки")]); db.commit()
        ps.set_manual_only(db, True)
        r = next(x for x in ps.build(db, mk)["rows"] if x["doctor"].id == d.id)
        assert r["base"] == 0 and r["comm"] == 0 and r["gross"] == 38940.22 and r["net"] == 38440.22
        ps.set_manual_only(db, False)
