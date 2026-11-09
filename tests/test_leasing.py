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


def test_history_when_closed():
    with SessionLocal() as db:
        if not ls.contracts(db):
            st = db.get(SyncState, ls.SEED_KEY)
            if st:
                db.delete(st)
                db.commit()
            ls.ensure_setup(db)
        c = next(x for x in ls.contracts(db) if x["name"] == "Батарея Deye · 1")
        for it in db.scalars(select(PayItem).where(PayItem.plan_id == c["plan_id"])):
            if it.status in ("planned", "approved"):
                it.status, it.amount_fact, it.paid_at = "paid", it.amount, it.due
        db.commit()
        v = ls.build(db)
        assert any(r["name"] == "Батарея Deye · 1" for r in v["history"])
        assert all(r["name"] != "Батарея Deye · 1" for g in v["groups"] for r in g["rows"])
        assert sum(p["amount"] for p in v["pay_hist"] if p["name"] == "Батарея Deye · 1") == 12000


def test_custom_group_kind_and_observ_feeds():
    from app.models import FinOp
    from app.services import paycal as pc
    with SessionLocal() as db:
        if not ls.contracts(db):
            ls.ensure_setup(db)
        c = ls.save(db, None, {"name": "Лазер тест", "group_new": "Лазери", "kind": "credit", "total": "120000", "paid_before": "0",
                               "monthly": "10000", "next": "2026-12-05", "bank": "ПриватБанк", "terms": "12 міс · 1,9%"})
        assert c["group_title"] == "Лазери" and c["kind"] == "credit"
        v = ls.build(db, today=date(2026, 11, 9))
        g = next(x for x in v["groups"] if x["title"] == "Лазери")
        assert g["left"] == 120000 and len(g["rows"][0]["sched"]) == 12
        st = db.get(SyncState, ls.FEEDS_KEY)
        if st:
            db.delete(st)
            db.commit()
        assert ls.setup_feeds(db)
        plans = db.scalars(select(PayPlan).where(PayPlan.name.like("OBSERV → банка%"))).all()
        assert sorted((p.day, p.amount) for p in plans) == [(8, 8000.0), (10, 4000.0), (14, 4000.0)] and all(p.start == date(2027, 3, 1) for p in plans)
        pc.ensure_month(db, 2027, 3)
        it = db.scalar(select(PayItem).where(PayItem.plan_id == plans[0].id))
        op = pc.pay(db, it, it.amount, it.due, "Готівка", None)
        assert op.kind == "transfer" and op.cashbox_to == "Банка OBSERV 520x"
        obs = next(r for r in ls.build(db, today=date(2027, 3, 20))["rows"] if r["kind"] == "fund")
        assert obs["feed_month"] == 16000 and obs["feed_paid"] == it.amount and obs["feed_end"]
