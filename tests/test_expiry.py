"""Терміни придатності: звіт по складах, задача в CRM, сповіщення в Telegram (без реальних запитів)."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, SyncState, Task, User
from app.services import expiry as ex, stock as st
from app.services.tasks import generate_auto


def test_expiry_report_tasks_and_notify(monkeypatch):
    settings.admin_password = "owner-pass-1"
    sent = []
    monkeypatch.setattr("app.services.alerts._api", lambda m, **kw: sent.append(kw["data"]) or True)
    old_chat = settings.telegram_admin_chat_id
    settings.telegram_admin_chat_id = "-100500"
    today = date.today()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/reports?r=expiry").text
        assert "Терміни придатності" in page and "Сповіщення" in page
    with SessionLocal() as db:
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
        p = Product(name="Терміновий крем", name_norm="терміновий крем", warehouse="vitrina")
        q = Product(name="Терміновий філер", name_norm="терміновий філер", warehouse="inject")
        db.add_all([p, q]); db.commit()
        st.start_ledger(db)
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="Термін Тест")
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=2, unit_cost=100, expiry_date=today - timedelta(days=3)))
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=5, unit_cost=100, expiry_date=today + timedelta(days=20)))
        db.commit(); st.post(db, doc)
        d2 = st.new_doc(db, "supply", wh_to="inject", supplier="Термін Тест")
        d2.lines.append(st.StockDocLine(product_id=q.id, qty=1, unit_cost=3000, expiry_date=today + timedelta(days=150)))
        db.commit(); st.post(db, d2)
        rep = ex.report(db)
        vit = next(w for w in rep["warehouses"] if w["key"] == "vitrina")
        assert vit["b"]["expired"]["n"] == 1 and vit["b"]["d30"]["n"] == 1 and vit["b"]["expired"]["value"] == 200
        assert next(w for w in rep["warehouses"] if w["key"] == "inject")["b"]["d180"]["n"] == 1
        generate_auto(db)
        t = db.query(Task).filter(Task.kind == "expiry", Task.source_key.like("exp:vitrina:%")).one()
        assert "1 прострочених" in t.title and t.priority == "high"
        owner = User(login="exp-doctor", name="Головний Лікар", role="doctor", password_hash="x", telegram_chat_id="777")
        db.add(owner); db.commit()
        ex.save_config(db, {"users": [owner.id], "admin_chat": True, "weekday": today.weekday(), "days": 90, "alerts": True})
        r = ex.daily(db)
        assert r["alerts"] == 2 and r["summary"] == 2                     # 2 отримувачі: чат адмінів + власник
        assert {m["chat_id"] for m in sent} == {"-100500", "777"} and any("Вітрина" in m["text"] for m in sent)
        sent.clear()
        assert ex.daily(db) == {"alerts": 0, "summary": 0} and not sent  # повторно — тиша
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete()
        oid = owner.id
    settings.telegram_admin_chat_id = old_chat
