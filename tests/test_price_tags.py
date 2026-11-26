from datetime import date

from app.models import SessionLocal, Product, SyncState
from app.services import tg_notify as tn


def test_price_tags_notify_changes_once_per_day():
    with SessionLocal() as db:
        st = db.get(SyncState, tn.PT_KEY)
        if st:
            db.delete(st); db.commit()
        p = Product(name="Цінник крем", name_norm="цінник крем", warehouse="vitrina", price=1000)
        q = Product(name="Цінник шприц", name_norm="цінник шприц", warehouse="single", price=5)
        db.add_all([p, q]); db.commit()
        assert tn.price_tag_changes(db, date(2026, 11, 26)) == []          # перший запуск — лише база
        p.price, q.price = 1200, 7
        db.commit()
        assert tn.text_price_tags(db, None, "admin", date(2026, 11, 26)) is None    # сьогодні вже зафіксовано
        t = tn.text_price_tags(db, None, "admin", date(2026, 11, 27))
        assert "Цінник крем" in t and "1000 → <b>1200 ₴</b>" in t and f"ids={p.id}" in t and "шприц" not in t
        assert tn.text_price_tags(db, None, "owner", date(2026, 11, 27)) == t       # той самий список для всіх ролей дня
        assert tn.text_price_tags(db, None, "admin", date(2026, 11, 28)) is None    # змін більше немає
        assert "price_tags" in tn.cfg(db)["roles"]["admin"] and tn.cfg(db)["roles"]["admin"]["price_tags"]["time"] == "09:30"
