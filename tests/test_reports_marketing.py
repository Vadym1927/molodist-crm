from datetime import date, datetime, timedelta

from app.models import (SessionLocal, AdsStat, Appeal, Appointment, Campaign, CampaignRecipient, FinOp, Patient, Sale)
from app.services import reports as rp


def test_marketing_reports():
    t = date.today()
    now = datetime.now()
    with SessionLocal() as db:
        db.add(AdsStat(source="meta", level="ad", day=t, key="MK1", name="Банер", clicks=10, impressions=100, cost=10, extra={"rate": 40}))
        db.add(AdsStat(source="google", level="campaign", day=t, key="G1", name="Пошук", clicks=5, impressions=50, cost=100))
        p1, p2, p3 = Patient(full_name="Маркетинг Один", phone="380501110001"), Patient(full_name="Маркетинг Два", phone="380501110002"), \
            Patient(full_name="Маркетинг Три")
        db.add_all([p1, p2, p3])
        db.flush()
        u = datetime.utcnow()
        db.add(Appeal(name="Один", phone=p1.phone, patient_id=p1.id, source="Instagram", status="Записаний", created_at=u - timedelta(hours=3), closed_at=u - timedelta(hours=1)))
        db.add(Appeal(name="Два", phone=p2.phone, patient_id=p2.id, source="Instagram", status="Відмова", deny_reason="Дорого", created_at=u - timedelta(hours=3), closed_at=u))
        db.add(Appeal(name="Три", source="Сайт", status="Новий", created_at=u - timedelta(hours=2)))
        db.add(Appointment(patient_id=p1.id, start=now - timedelta(minutes=30), end=now, status="done", services=[]))
        db.add(Sale(patient_id=p1.id, at=now - timedelta(minutes=5), status="paid", subtotal=2000, total=2000))
        db.add(Appointment(patient_id=p3.id, start=now - timedelta(minutes=40), end=now, status="done", services=[], booking_source="Рекомендація"))
        db.add(Sale(patient_id=p3.id, at=now - timedelta(minutes=3), status="paid", subtotal=500, total=500))
        db.add(FinOp(day=t, kind="expense", category="Маркетинг СММ", amount=300, cashbox="Готівка"))
        db.add(FinOp(day=t, kind="expense", category="Маркетинг", amount=999, cashbox="Готівка", comment="Поповнення Google Ads"))
        c = Campaign(name="Акція тест", segment="all", channels=["sms"], text="x", status="done", sent_at=now - timedelta(hours=2), attribution_days=14)
        c.recipients.append(CampaignRecipient(patient_id=p1.id, channel="sms", status="delivered", sent_at=now - timedelta(hours=2)))
        c.recipients.append(CampaignRecipient(patient_id=p2.id, channel="sms", status="failed"))
        db.add(c)
        db.commit()

        ch = rp.build(db, "channels", t, t)
        by = {x["name"]: x for x in ch.rows}
        assert by["Meta Ads"]["cost"] == 400 and by["Google Ads"]["cost"] == 120
        ig = by["Звернення · Instagram"]
        assert ig["leads"] == 2 and ig["came"] == 1 and ig["new"] == 1 and ig["revenue"] == 2000
        assert by["Запис напряму · Рекомендація"]["new"] == 1 and by["Запис напряму · Рекомендація"]["revenue"] == 500
        assert "інший маркетинг 300" in ch.tiles[0][4]

        ac = rp.build(db, "appeals_conv", t, t)
        row = next(x for x in ac.rows if x["name"] == "Instagram")
        assert row["n"] == 2 and row["ok"] == 1 and row["fail"] == 1 and row["conv"] == 50 and row["payers"] == 1
        assert any(x["name"] == "Сайт" and x["work"] == 1 for x in ac.rows)
        assert rp.build(db, "appeals_conv", t, t, cat="deny").rows[0]["name"] == "Дорого"
        assert rp.build(db, "appeals_conv", t, t, cat="staff").rows

        ml = rp.build(db, "mailings", t, t)
        row = next(x for x in ml.rows if x["name"] == "Акція тест")
        assert row["total"] == 2 and row["sent"] == 1 and row["failed"] == 1 and row["payers"] == 1 and row["revenue"] == 2000

        cc = rp.build(db, "cac", t, t)
        last = cc.rows[-1]
        assert last["name"] == f"{t.year}-{t.month:02d}" and last["ads"] == 520 and last["other"] == 300 and last["new"] >= 2 and last["r30"] >= 2500
