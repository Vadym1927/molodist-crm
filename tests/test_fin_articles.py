from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, FinOp
from app.services import fin_articles as fa


def test_articles_seed_and_salary():
    with SessionLocal() as db:
        assert "Реклама Google Ads" in fa.names(db, "expense") and "Оплата послуг, товарів" in fa.names(db, "income")
        assert fa.salary_name(db) == "Персонал Зарплата"
        x = fa.save(db, None, "Тестова стаття", "expense", True, False)
        db.add(FinOp(day=date.today(), kind="expense", category="Тестова стаття", amount=10)); db.commit()
        fa.save(db, x["id"], "Тестова стаття 2", "expense", True, False)
        assert db.query(FinOp).filter(FinOp.category == "Тестова стаття 2").count() == 1
        assert not fa.delete(db, x["id"]) and "Тестова стаття 2" not in fa.names(db, "expense")
        y = fa.save(db, None, "Порожня", "income", True, False)
        assert fa.delete(db, y["id"])


def test_articles_page():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert 'href="/settings/articles"' in c.get("/settings").text
        t = c.get("/settings/articles").text
        assert "Податки ЄСВ ФОП Колган" in t and "ID: 906" in t
        assert "Google Ads" in c.get("/finance/ops").text
