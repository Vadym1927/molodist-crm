from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbService, Product, TechCard


def test_import_cb_techcards():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add_all([CbService(id=97001, name="Імпорт Ботокс 10", price=3000), CbService(id=97002, name="Імпорт IPL", price=2000),
                    Product(name="Імпорт Бахіли", name_norm="імпорт бахіли", cb_product_id=97059, warehouse="single", cost=1.14)])
        old = TechCard(name="Стара техкарта", lines=[])
        db.add(old); db.commit(); old_id = old.id
    data = {"cards": [[9001, "Botox 10 імп", "Ботулінотерапія", 0, [["Одноразка", "single"], ["Препарат", "inject"]],
                       [[1, 97332, 10], [0, 97059, 2]], [97001, 97999], ""],
                      [9002, "IPL імп", "IPL", 500, [["Одноразка", "single"], ["Обладнання", "equip"]], [[1, 97596, 1]], [97002], "коментар"]],
            "products": {"97332": ["Ботокс імп", "Од", 49.0, "inject"], "97596": ["IPL амортизація імп", "шт.", 100, "equip"]}}
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Імпорт з CleverBox" in c.get("/techcards").text and c.get("/techcards/import-cb").status_code == 200
        r = c.post("/techcards/import-cb", json=data).json()
        assert r["created"] == 2 and r["lines"] == 3 and r["products_created"] == 2 and r["services_linked"] == 2 and r["services_missing"] == [97999]
        r2 = c.post("/techcards/import-cb", json=data).json()                # повторно — оновлення, без дублів
        assert r2["created"] == 0 and r2["updated"] == 2 and r2["products_created"] == 0
    with SessionLocal() as db:
        k = db.query(TechCard).filter(TechCard.cb_id == 9001).one()
        assert len(k.lines) == 2 and {ln.cat for ln in k.lines} == {"Одноразка", "Препарат"}
        assert db.get(CbService, 97001).techcard_id == k.id
        ipl = db.query(TechCard).filter(TechCard.cb_id == 9002).one()
        assert ipl.max_cost == 500 and ipl.cats[1]["wh"] == "equip"
        p = db.query(Product).filter(Product.cb_product_id == 97596).one()
        assert p.warehouse == "equip" and p.cost == 100 and p.for_sale is False
        assert db.get(TechCard, old_id).active is False
