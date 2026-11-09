from datetime import date

from sqlalchemy import select

from app.models import SessionLocal, PayItem, PayPlan, SyncState
from app.services import leasing as ls


def test_schedule_last_payment():
    s = ls.schedule(79049, 55328, 7904, date(2026, 12, 8))
    assert [d for d, _ in s] == [date(2026, 12, 8), date(2027, 1, 8), date(2027, 2, 8)]
    assert [a for _, a in s] == [7904, 7904, 7913]
    assert sum(a for _, a in ls.schedule(42750, 12825, 4275, date(2026, 11, 14))) == 29925


def test_setup_and_build():
    with SessionLocal() as db:
        for k in (ls.KEY, ls.SEED_KEY):
            st = db.get(SyncState, k)
            if st:
                db.delete(st)
        db.commit()
        assert ls.ensure_setup(db)
        v = ls.build(db, today=date(2026, 11, 9))
        inv = next(g for g in v["groups"] if g["key"] == "inverter")
        assert inv["left"] == 81646 and inv["total"] == 201799
        assert v["this_month"] == 8275                      # 14.11: 4000 + 4275
        assert next(g for g in v["groups"] if g["key"] == "observ")["left"] == 611050
        c = next(x for x in ls.contracts(db) if x["name"] == "Батарея Deye · 3")
        items = db.scalars(select(PayItem).where(PayItem.plan_id == c["plan_id"])).all()
        assert len(items) == 7 and not db.get(PayPlan, c["plan_id"]).active
        it = min(items, key=lambda i: i.due)
        it.status, it.amount_fact = "paid", 4275
        db.commit()
        c2 = ls.save(db, c["id"], {**{k: str(c[k]) for k in ("name", "group", "kind", "total", "paid_before", "monthly")}, "next": "2026-12-14"})
        open_ = [i for i in db.scalars(select(PayItem).where(PayItem.plan_id == c2["plan_id"])) if i.status in ("planned", "approved")]
        assert len(open_) == 6 and min(i.due for i in open_) == date(2026, 12, 14)
        assert ls.build(db)["rows"][3]["left"] == 25650
