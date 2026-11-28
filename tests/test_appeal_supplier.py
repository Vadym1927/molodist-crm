"""Звернення від постачальника → контакт у картці постачальника, номер і діалог більше не створюють звернень."""
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appeal, ChatMessage, Supplier
from app.services import appeals as ap, meta_inbox as mi


def test_appeal_linked_to_supplier():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        s = Supplier(name="Регінера Тест"); db.add(s)
        a = Appeal(name="Менеджер Олена", phone="+380 50 777 66 55", source="Дзвінок", status="Новий")
        b = Appeal(name="Менеджер Олена", phone="0507776655", source="Дзвінок", status="Новий")
        db.add_all([a, b]); db.commit()
        db.add(ChatMessage(channel="instagram", thread="IG-SUP-1", direction="in", text="Привіт, прайс", appeal_id=a.id)); db.commit()
        aid, bid, sid = a.id, b.id, s.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        card = c.get(f"/appeals/{aid}").text
        assert "Який постачальник" in card and "Регінера Тест" in card
        oth = ap.other_status(ap.dirs(SessionLocal()))
        c.post(f"/appeals/{aid}/status", data={"status": oth, "deny_reason": "Постачальник", "supplier_id": str(sid)})
        assert "Регінера Тест" in c.get(f"/appeals/{aid}").text
        assert "Менеджер Олена" in c.get(f"/stock/suppliers/{sid}/card").text
    with SessionLocal() as db:
        s = db.get(Supplier, sid)
        assert s.contacts and s.contacts[0]["thread"] == "IG-SUP-1" and s.phone
        assert ap.is_ignored(db, "0507776655")["supplier_id"] == sid and ap.thread_ignored(db, "IG-SUP-1")
        assert db.get(Appeal, bid).closed_at                     # друге відкрите звернення того ж номера закрито
        assert mi._appeal_for(db, "instagram", "IG-SUP-1", None, "Олена", "ще питання") is None


def test_appeal_new_supplier_by_name():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Appeal(name="Курʼєр Нова", phone="0631234500", status="Новий"); db.add(a); db.commit(); aid = a.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        oth = ap.other_status(ap.dirs(SessionLocal()))
        c.post(f"/appeals/{aid}/status", data={"status": oth, "deny_reason": "Постачальник", "supplier_id": "new", "new_supplier": "Новий Дистриб'ютор"})
    with SessionLocal() as db:
        s = db.query(Supplier).filter_by(name="Новий Дистриб'ютор").one()
        assert s.contacts[0]["phone"] == "0631234500"
