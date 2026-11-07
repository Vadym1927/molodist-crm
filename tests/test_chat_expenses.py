"""Видатки з Telegram-групи: розбір суми, каси, статті."""
from app.models import SessionLocal, FinOp
from app.services import chat_expenses as ce, cashboxes, fin_articles


def test_parse_and_create(monkeypatch):
    monkeypatch.setattr(cashboxes, "names", lambda db, active_only=True: ["Готівка каса", "Термінал ФОП Бондар", "Термінал ФОП Колган"])
    monkeypatch.setattr(fin_articles, "names", lambda db, kind, active_only=True: ["Доставка Нова Пошта", "Податки ЄСВ ФОП Бондар",
                                                                                  "Господарські витрати", "Канцтовари"])
    with SessionLocal() as db:
        d = ce.parse(db, "155 грн\nдоставка НП\nтермінал Бондар")
        assert d["amount"] == 155 and d["cashbox"] == "Термінал ФОП Бондар" and d["category"] == "Доставка Нова Пошта"
        assert "доставка НП" in d["comment"]
        d2 = ce.parse(db, "1 250,50 канцтовари")
        assert d2["amount"] == 1250.5 and d2["category"] == "Канцтовари" and d2["cashbox"] is None
        assert ce.parse(db, "скидайте я руками внесу") is None and ce.parse(db, "/rahunky") is None and ce.parse(db, "300") is None
        x = ce.create(db, d, "Рецепція", None, "tg:1:2")
        assert x.kind == "expense" and x.amount == 155 and "Рецепція" in x.comment
        ce.cancel(db, x.id)
        assert db.get(FinOp, x.id).deleted_at is not None
        ce.pend_put(db, "k1", {"amount": 5}); assert ce.pend_get(db, "k1")["amount"] == 5; ce.pend_del(db, "k1"); assert ce.pend_get(db, "k1") is None


def test_transfer_and_chat_binding(monkeypatch):
    monkeypatch.setattr(cashboxes, "names", lambda db, active_only=True: ["Готівка каса", "Термінал ФОП Бондар", "Термінал ФОП Колган"])
    monkeypatch.setattr(fin_articles, "names", lambda db, kind, active_only=True: ["Канцтовари"])
    with SessionLocal() as db:
        d = ce.parse(db, "2000 переміщення з готівки на термінал Колган")
        assert d["kind"] == "transfer" and d["cashbox"] == "Готівка каса" and d["cashbox_to"] == "Термінал ФОП Колган" and ce.ready(d)
        d2 = ce.parse(db, "5 000 з готівка каса на термінал Бондар")
        assert d2["kind"] == "transfer" and d2["amount"] == 5000 and d2["cashbox_to"] == "Термінал ФОП Бондар"
        d3 = ce.parse(db, "3000 інкасація готівка")
        assert d3["kind"] == "transfer" and d3["cashbox"] == "Готівка каса" and not d3["cashbox_to"] and not ce.ready(d3)
        x = ce.create(db, d, "Адмін", None, "tg:9:9")
        assert x.kind == "transfer" and x.cashbox_to == "Термінал ФОП Колган" and "Переміщення внесено" in ce.text_done(x)
        assert ce.parse(db, "350 готівка канцтовари")["kind"] == "expense"
        ce.set_chat(db, "-100777")
        assert ce.is_chat(db, "-100777") and not ce.is_chat(db, "-1005")
        ce.set_chat(db, "-100777", False)
        assert not ce.is_chat(db, "-100777")


REAL = ["Готівка ФОП Колган", "Термінал ФОП Колган", "Термінал ФОП Бондар", "Готівка ФОП Бондар", "Операційний Фонд",
        "Амортизаційний + Операційний Фонд", "Готівка Cейф", "Банка OBSERV 520x"]


def test_real_admin_messages(monkeypatch):
    monkeypatch.setattr(cashboxes, "names", lambda db, active_only=True: REAL)
    monkeypatch.setattr(fin_articles, "names", lambda db, kind, active_only=True: ["Доставка Нова Пошта", "Канцтовари"])
    with SessionLocal() as db:
        d = ce.parse(db, "8000 грн\nготівка з каси ФОП Бондар в сейф")
        assert d["kind"] == "transfer" and d["amount"] == 8000 and d["cashbox"] == "Готівка ФОП Бондар" and d["cashbox_to"] == "Готівка Cейф"
        e = ce.parse(db, "155 грн\nдоставка НП\nтермінал Бондар")
        assert e["kind"] == "expense" and e["cashbox"] == "Термінал ФОП Бондар" and e["category"] == "Доставка Нова Пошта"
