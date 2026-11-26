from datetime import datetime

from app.models import SessionLocal, Product, SyncState, StockMove
from app.services import tg_notify as tn


def test_price_tags_instant_only_in_stock(monkeypatch):
    sent = []
    monkeypatch.setattr(tn, "send_text", lambda ch, t: sent.append(t) or True)
    with SessionLocal() as db:
        st = db.get(SyncState, tn.PT_KEY)
        if st:
            db.delete(st); db.commit()
        p = Product(name="Цінник крем", name_norm="цінник крем", warehouse="vitrina", price=1000)
        e = Product(name="Цінник немає", name_norm="цінник немає", warehouse="vitrina", price=500)
        q = Product(name="Цінник шприц", name_norm="цінник шприц", warehouse="single", price=5)
        db.add_all([p, e, q]); db.flush()
        db.add(StockMove(product_id=p.id, warehouse="vitrina", qty=2, kind="opening", at=datetime(2026, 1, 1)))
        db.commit()
        assert tn.price_tag_changes(db) == []                    # перший запуск — лише база
        p.price, e.price, q.price = 1200, 600, 7
        db.commit()
        xs = tn.price_tag_changes(db)
        assert [x["name"] for x in xs] == ["Цінник крем"]         # без залишку й не вітрина — не надсилаємо
        t = tn.price_tags_text(xs)
        assert "1000 → <b>1200 ₴</b>" in t and f"ids={p.id}" in t
        assert tn.price_tag_changes(db) == []                    # повторно не повідомляє
        assert tn.text_price_tags(db, None, "admin") == t        # «тест → мені» — останнє сповіщення
        assert tn.cfg(db)["roles"]["admin"]["price_tags"]["per"] == "instant"
