from app.services import certificates as cert


def test_visits_from_package_name():
    assert cert.visits_from_name("Апаратний масаж STRATOSPHERE 50 хв Курс 6") == 6
    assert cert.visits_from_name("Масаж 50 хв") == 1
    assert cert.pkg_visits({"name": "Масаж Курс 6", "visits": 1}) == 6
    assert cert.pkg_visits({"name": "Масаж Курс 6", "visits": 8}) == 8
    assert cert.pkg_visits({"name": "Масаж", "visits": 1}) == 1
