from datetime import datetime

from app.models import Patient, SessionLocal, ShopReview, SiteOrder


def test_coins_for_published_review_once():
    from app.services import coins as cn, shop_front as sf, shop_reviews as rv
    with SessionLocal() as db:
        p = Patient(full_name="Відгукова Пацієнтка", phone="380671239977")
        db.add(p); db.flush()
        o = SiteOrder(source="crm", ext_id="RVC1", number="RVC1", created_at=datetime.now(), status="Отримано", patient_id=p.id)
        db.add(o); db.flush()
        r = ShopReview(order_id=o.id, product_id=1, patient_id=p.id, rating=3, created_at=datetime.utcnow(), status="new")
        db.add(r); db.flush()
        c = sf.cfg(db)
        assert not rv.award(db, r, c)                      # не опубліковано — без коїнів
        rv.moderate(db, r.id, "ok", None, None)
        assert rv.award(db, r, c) and not rv.award(db, r, c)   # раз на відгук, за будь-яку оцінку
        assert cn.balance(db, p.id) == 25
