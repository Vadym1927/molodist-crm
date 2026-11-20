from datetime import date

from app.models import Product, ProductPurchase, SessionLocal
from app.services import catalog


def test_cost_last_mixed_units():
    with SessionLocal() as db:
        p = Product(name="Дрінк 30 стік тест", name_norm="дрінк 30 стік тест", coef=30)
        db.add(p); db.flush()
        db.add_all([ProductPurchase(product_id=p.id, bought_on=date(2026, 8, 1), qty=60, unit_cost=73.0),        # за стік
                    ProductPurchase(product_id=p.id, bought_on=date(2026, 9, 30), qty=1, unit_cost=2201.63)])   # за упаковку
        db.commit()
        class R: pass
        r = R(); r.product, r.stock, r.purchases, r.cost_avg, r.cost_last, r.expiry = p, None, 0, None, None, None
        r.last_bought = r.last_supplier = None
        catalog.apply_purchases(db, {p.id: r})
        assert r.cost_last == 2201.63 and 2150 < r.cost_avg < 2250
