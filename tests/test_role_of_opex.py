from types import SimpleNamespace as N

from app.services.payroll_share import role_of


def test_financial_operations_is_opex():
    assert role_of(N(name="Financial Operations", position="")) == "admin"
    assert role_of(N(name="Дар‘я Директор", position=None)) == "admin"
    assert role_of(N(name="Софія Сирова", position="Лікар дерматолог")) == "doctor"
    assert role_of(N(name="Рокун Вікторія", position="")) == "doctor"
