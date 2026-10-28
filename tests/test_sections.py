"""Розділи: Клієнти, Календар, Задачі, Продукти, Склад, Фінанси (на моках CleverBox)."""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Task, Doctor
from app.integrations import cleverbox as cbm

PDF = Path(__file__).parent / "sample_plan.pdf"
pytestmark = pytest.mark.skipif(not PDF.exists(), reason="потрібен tests/sample_plan.pdf")


def login(c, lg, pw):
    assert c.post("/login", data={"login": lg, "password": pw}, follow_redirects=False).status_code == 303


def test_sections(monkeypatch, tmp_path):
    from tests.test_cleverbox_sync import handler
    settings.admin_password, settings.files_dir = "owner-pass-1", str(tmp_path)
    settings.cleverbox_token, settings.cleverbox_min_interval_sec = "test", 0
    orig = cbm.CleverBox.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.http = httpx.Client(transport=httpx.MockTransport(handler), headers={"token": "test"})
    monkeypatch.setattr(cbm.CleverBox, "__init__", init)

    with TestClient(app) as c:
        login(c, settings.owner_login, "owner-pass-1")
        c.post("/upload", files={"files": ("plan.pdf", PDF.read_bytes(), "application/pdf")})
        assert c.post("/actions/sync", follow_redirects=False).status_code == 303

        cl = c.get("/clients").text
        assert "Даниленко" in cl and "Нова Клієнтка" in cl and "Без плану (потенціал)" in cl
        assert "Ручне введення пацієнта" in cl and "Тестова картка" in cl
        created = c.post("/patients", data={"full_name": "Ручна Пацієнтка", "phone": "0671234567",
                                              "notes": "створено вручну"}, follow_redirects=False)
        assert created.status_code == 303 and "/patients/" in created.headers["location"]
        test_card = c.post("/patients/test", follow_redirects=False)
        assert test_card.status_code == 303
        assert "Тестова картка пацієнта" in c.get(test_card.headers["location"]).text
        assert "Нова Клієнтка" in c.get("/clients?seg=new").text
        assert "Даниленко" in c.get("/clients?seg=plan").text and "Нова Клієнтка" not in c.get("/clients?seg=plan").text

        from datetime import date, timedelta
        cal = c.get(f"/calendar?view=week&start={(date.today() + timedelta(days=5)).isoformat()}").text   # тиждень із записами
        assert "Даниленко" in cal and "Нова Клієнтка" in cal and "class=\"tag\">план" in cal

        # склад: залишок Azelorosa 2, інші засоби з плану не куплені і їх немає на складі -> дефіцит
        st = c.get("/stock?only=deficit").text
        assert "Дозамовити" in st and "Azelorosa" not in st.split("<tbody>")[1].split("</tbody>")[0]

        # автозадачі після синку: дефіцит складу -> задачі власнику, без дублів при повторі
        with SessionLocal() as db:
            n_stock = db.query(Task).filter(Task.kind == "stock", Task.status == "open").count()
        assert n_stock == 1                    # одна зведена задача
        c.post("/tasks-generate")
        with SessionLocal() as db:
            assert db.query(Task).filter(Task.kind == "stock", Task.status == "open").count() == n_stock
        assert "Дозамовити" in c.get("/tasks").text

        # ручна задача з картки клієнта і її закриття
        c.post("/tasks", data={"title": "Передзвонити щодо IPL", "patient_id": "1", "back": "/patients/1"})
        with SessionLocal() as db:
            t = db.query(Task).filter(Task.title == "Передзвонити щодо IPL").one()
            tid = t.id
        assert "Передзвонити щодо IPL" in c.get("/patients/1").text
        c.post(f"/tasks/{tid}", data={"status": "done", "result": "Записана"})
        with SessionLocal() as db:
            assert db.get(Task, tid).status == "done"

        pr = c.get("/products").text
        assert "Azelorosa" in pr and "Термін придатності" in pr
        assert "IPL" in c.get("/products?tab=procedures").text
        c.post("/products/2", data={"supply_days": "45", "min_stock": "3", "url": "", "apply": "1"})

        fin = c.get("/finance?df=2026-10-01&dt_=2026-10-31").text
        assert "4 500 ₴" in fin and "Дарʼя Колган" in fin and "Картка" in fin

        # ролі
        with SessionLocal() as db:
            doc_id = db.query(Doctor).first().id
        c.post("/access/users", data={"login": "adm2", "name": "Адмін", "role": "admin", "password": "password-123", "active": "1"})
        c.post("/access/users", data={"login": "doc9", "name": "Лікар", "role": "doctor", "doctor_id": str(doc_id),
                                      "password": "password-123", "active": "1"})
        c.get("/logout"); login(c, "adm2", "password-123")
        assert c.get("/finance").status_code == 403 and c.get("/stock").status_code == 200
        assert "Фінанси" not in c.get("/").text.split("<main>")[0]
        c.get("/logout"); login(c, "doc9", "password-123")
        assert c.get("/products").status_code == 403 and c.get("/stock").status_code == 403
        cal = c.get(f"/calendar?view=week&start={(date.today() + timedelta(days=5)).isoformat()}").text
        assert "Даниленко" in cal and "Нова Клієнтка" not in cal
        assert "Нова Клієнтка" not in c.get("/clients").text


def test_booking_from_plan(monkeypatch):
    """Запис з бюті-плану в CleverBox: слоти → збереження → локальний запис → задача закрита."""
    import httpx
    from datetime import date, timedelta
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, CbService, Doctor, PlanItem, Task, Visit
    from app.integrations import cleverbox as cbm
    pdf = Path(__file__).parent / "sample_plan.pdf"
    if not pdf.exists():
        pytest.skip("потрібен tests/sample_plan.pdf")
    day = date.today() + timedelta(days=3)
    saved = {}

    def handler(req: httpx.Request):
        import json
        m = req.url.path.split("/api/v3/rest/")[1]
        body = json.loads(req.content or b"{}")
        if m == "filials":
            return httpx.Response(200, json={"ok": True, "data": [{"id": 114, "title": "MOLODIST", "is_hidden": 0}]})
        if m == "slots/get":
            return httpx.Response(200, json={"ok": True, "slots": {day.strftime("%d.%m.%Y"): [
                {"on": True, "time": "10:00", "masters": ["7", "5"]}, {"on": False, "time": "10:15", "masters": []}]},
                "masters": [{"id": "5", "name": "Дарʼя Колган"}, {"id": "7", "name": "Інший"}]})
        if m == "slots/save":
            saved.update(body)
            return httpx.Response(200, json={"ok": True, "record_id": 555, "data": {"master": "Дарʼя Колган", "id": 555}})
        return httpx.Response(200, json={"ok": True, "data": []})

    settings.cleverbox_token, settings.cleverbox_min_interval_sec, settings.admin_password = "t", 0, "owner-pass-1"
    orig = cbm.CleverBox.__init__
    monkeypatch.setattr(cbm.CleverBox, "__init__", lambda self, *a, **k: (orig(self, *a, **k),
                        setattr(self, "http", httpx.Client(transport=httpx.MockTransport(handler))))[0])
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/upload", files={"files": ("plan.pdf", pdf.read_bytes(), "application/pdf")})
        with SessionLocal() as db:
            db.add(CbService(id=12, name="IPL терапія обличчя", time_min=40))
            db.query(Doctor).first().cleverbox_id = 5
            item = db.query(PlanItem).filter(PlanItem.name.like("IPL%")).one()
            db.add(Task(title="Записати", kind="not_booked", plan_id=item.plan_id, status="open"))
            db.commit()
            item.plan.patient.phone = "0671112233"
            db.commit()
            iid, pid = item.id, item.plan.patient_id
        page = c.get(f"/book/{iid}?day={day.isoformat()}").text
        assert "IPL терапія обличчя" in page and 'value="10:00|5"' in page
        assert page.index('value="10:00|5"') < page.index('value="10:00|7"')      # лікар плану першим
        assert "10:15" not in page
        r = c.post(f"/book/{iid}", data={"day": day.isoformat(), "slot": "10:00|5"}, follow_redirects=False)
        assert r.status_code == 303, r.text[-600:]
        assert saved["services"][0] == {"id": 12, "master_id": 5, "time": "10:00", "long": 40}
        assert saved["salon_id"] == 114 and saved["client_id"] == 0 and saved["phone"] == "0671112233"
        with SessionLocal() as db:
            v = db.query(Visit).filter(Visit.external_id == "555-12").one()
            assert v.status == "booked" and v.plan_item_id == iid
            assert db.query(Task).filter(Task.kind == "not_booked").one().status == "done"
        assert "запис" in c.get(f"/patients/{pid}").text



