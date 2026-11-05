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
