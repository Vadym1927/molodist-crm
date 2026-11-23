from datetime import date, timedelta

from app.models import AdsTopup, FinOp, SessionLocal
from app.services import google_ads as ga


def test_finops_google_ads_counted_as_topups_once():
    with SessionLocal() as db:
        d = date.today()
        a = FinOp(day=d, kind="expense", category="Реклама Google Ads", amount=1200.0, cashbox="Термінал ФОП Колган")
        b = FinOp(day=d, kind="expense", category="Інше", comment="Google Ads /Платіжний календар: Поповнення Google Ads (щоденне)", amount=600.0)
        x = FinOp(day=d, kind="expense", category="Сервіси та підписки Google", amount=272.67)
        st = FinOp(day=d, kind="expense", category="Реклама Google Ads", amount=999.0)
        db.add_all([a, b, x, st]); db.flush()
        db.add(FinOp(day=d, kind="income", category="Реклама Google Ads", amount=999.0, storno_of=st.id))
        db.commit()
        ga.finops_topups(db)
        ga.finops_topups(db)                              # повторно — без дублів
        refs = {t.ref: t for t in db.query(AdsTopup).filter(AdsTopup.ref.like("finop:%"))}
        assert f"finop:{a.id}" in refs and f"finop:{b.id}" in refs
        assert f"finop:{x.id}" not in refs and f"finop:{st.id}" not in refs
        assert abs(refs[f"finop:{a.id}"].credited - 1000.0) < 0.01          # без ПДВ 20%
