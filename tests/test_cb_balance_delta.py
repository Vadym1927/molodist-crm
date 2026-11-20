from app.models import Patient, apply_cb_balance


def test_cb_sync_keeps_crm_topup():
    p = Patient(full_name="Тест Баланс")
    apply_cb_balance(p, 0)                 # перший синк: у CleverBox 0
    assert p.cb_balance == 0 and p.cb_balance_src == 0
    p.cb_balance = 3500                    # поповнення в касі CRM
    apply_cb_balance(p, 0)                 # наступний синк — CleverBox без змін
    assert p.cb_balance == 3500
    apply_cb_balance(p, 1000)              # поповнили ще й у CleverBox
    assert p.cb_balance == 4500 and p.cb_balance_src == 1000
    apply_cb_balance(p, 400)               # у CleverBox витратили 600
    assert p.cb_balance == 3900


def test_cb_first_seen_keeps_existing():
    p = Patient(full_name="Тест Баланс 2", cb_balance=700)
    apply_cb_balance(p, 500)               # попереднє значення CleverBox невідоме — не чіпаємо
    assert p.cb_balance == 700 and p.cb_balance_src == 500
