"""Засоби домашнього догляду: бренди, постачальники, закупівлі (сер./остання ціна), термін, фільтри, картка."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, ProductPurchase, StockLevel, ProductSale, Patient
from app.services import catalog


def test_products_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Product(name="ZO Daily Power Defense 50 мл", name_norm="zo daily", cb_product_id=501, price=4200,
                    brand="ZO Skin Health", sku="ZO-DPD50", min_stock=3)
        b = Product(name="AG Azeloroza сироватка", name_norm="ag azeloroza", cb_product_id=502, price=2200)
        db.add_all([a, b]); db.flush()
        soon = date.today() + timedelta(days=40)
        db.add_all([ProductPurchase(product_id=a.id, bought_on=date.today() - timedelta(days=60), qty=4, unit_cost=2000,
                                    supplier="Дистрибʼютор А", expiry_date=date.today() + timedelta(days=400)),
                    ProductPurchase(product_id=a.id, bought_on=date.today() - timedelta(days=5), qty=2, unit_cost=2300,
                                    supplier="Дистрибʼютор Б", expiry_date=soon),
                    StockLevel(cb_product_id=501, name=a.name, qty=2), StockLevel(cb_product_id=502, name=b.name, qty=0)])
        p = Patient(full_name="Покупниця Тест"); db.add(p); db.flush()
        db.add(ProductSale(external_id="s-501", patient_id=p.id, product_id=501, product_name=a.name, quantity=3,
                           amount=12600, sold_at=datetime.now() - timedelta(days=10)))
        db.commit()
        rows = {r.product.id: r for r in catalog.product_rows(db, [])}
        ra = rows[a.id]
        assert ra.cost_avg == round((4 * 2000 + 2 * 2300) / 6, 2) and ra.cost_last == 2300
        assert ra.expiry == soon and ra.margin == round(100 * (4200 - ra.cost_avg) / ra.cost_avg)
        assert ra.sold_90 == 3 and ra.days_left == 60
        aid, bid = a.id, b.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/products").text
        assert "ZO Skin Health" in page and "Без бренду" in page and "Термін ≤ 90 дн" in page
        assert "ZO Daily" in c.get("/products?f=expiring").text
        assert "AG Azeloroza" in c.get("/products?f=out").text and "ZO Daily" not in c.get("/products?f=out").text
        assert "AG Azeloroza" not in c.get("/products?brand=ZO+Skin+Health").text
        card = c.get(f"/products/{aid}").text
        assert "ZO-DPD50" in card and "Дистрибʼютор Б" in card and "Покупниця Тест" in card
        c.post(f"/products/{aid}/purchases", data={"qty": "1", "unit_cost": "2500", "supplier": "Дистрибʼютор Б"})
        c.post("/products/bulk", data={"ids": [str(bid)], "field": "brand", "value": "AG Skin"})
        c.post(f"/products/{bid}", data={"sku": "AG-AZ", "supplier": "Дистрибʼютор А", "expiry_date": "2027-05-01",
                                          "price_manual": "", "brand": "AG Skin"})
        assert "Дистрибʼютор Б" in c.get("/products?tab=suppliers").text
    with SessionLocal() as db:
        assert db.get(Product, bid).brand == "AG Skin" and db.get(Product, bid).sku == "AG-AZ"
        assert db.query(ProductPurchase).filter_by(product_id=aid).count() == 3


def test_import_cleverbox_excel(tmp_path):
    """Імпорт Excel-вивантаження товарів CleverBox: склади, бренди, постачальники, ціни, залишки."""
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["ID", "Назва", "Скор. назва ", "Артикул", "Штрихкод", "Од. списання ", "Коефiцiєнт", "Од. приходу", "Категорія ",
               "Напрямок ", "Бренд", "Постачальник", "Крит. залишок", "Для продажу", "Для витрати", "Для магазину",
               "Категорії магазину", "Ціна закупівлі", "Ціна продажу", "Кількість", "Створено "])
    ws.append(["7001", "Advanced Night Restore 50мл", "", "32042/2", "0", "шт.", "1.00", "шт.", "Вітрина", "Вітрина", "MediK8",
               "Medik8", "", "1", "", "1", "", "2 320.00", "3700.00", "2", "14.12.2022"])
    ws.append(["7002", "Belotero Balance 1 мл", "", "B-1", "", "шт.", "1", "шт.", "Ін'єкційна Косметологія", "", "Belotero",
               "MERZ", "3", "", "1", "", "", "4 100.00", "", "5", ""])
    ws.append(["7003", "Рукавички нітрилові M", "", "", "", "уп.", "1", "уп.", "Одноразка", "Одноразка", "", "РіоБіз(Одноразка)",
               "", "", "1", "", "", "250", "", "12", ""])
    ws.append(["7004", "Пілінг Casmara", "", "", "", "мл.", "1", "мл.", "Класична Косметологія", "", "medik8", "Casmara",
               "", "", "1", "", "", "3.5", "", "551.5", ""])
    f = tmp_path / "products.xlsx"; wb.save(f)
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post("/products/import", files={"file": ("products.xlsx", f.read_bytes(),
                                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                   follow_redirects=False)
        assert r.status_code == 303
        page = c.get("/products?wh=all").text
        assert "Імпортовано 4 рядків" in page
        assert "Belotero Balance" in c.get("/products?wh=inject").text
        assert "Belotero Balance" not in c.get("/products?wh=vitrina").text
        assert "Рукавички" in c.get("/products?wh=single").text and "Пілінг Casmara" in c.get("/products?wh=classic").text
    with SessionLocal() as db:
        p = db.query(Product).filter_by(cb_product_id=7001).one()
        assert (p.warehouse, p.brand, p.supplier, p.sku, p.cost, p.price) == ("vitrina", "MediK8", "Medik8", "32042/2", 2320, 3700)
        assert db.query(Product).filter_by(cb_product_id=7004).one().brand == "MediK8"     # регістр бренду об'єднано
        assert db.query(Product).filter_by(cb_product_id=7002).one().min_stock == 3
        assert db.query(StockLevel).filter_by(cb_product_id=7004).one().qty == 551.5


def test_import_turnover_report(tmp_path):
    """Звіт «Рух товарів» за період → закупівлі; коефіцієнт одиниць; поточна собівартість CleverBox як остання ціна."""
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Товар", "артикул", "штрихкод", "коеф.", "Одиниця обліку", "Одиниця постачання", "Початковий залишок",
               "Сума залишку", "надходження к-ть одиниць обліку", "надходження к-ть одиниць продажу", "Прихід, сума",
               "витрата к-ть одиниць обліку", "витрата к-ть одиниць проджажу", "Витрата, сума",
               "залишок к-ть одиниць обліку", "залишок к-ть одиниць проджажу", "Сума залишку"])
    ws.append(["Гель Тест Кабінет 500мл", "G-500", "0", "500.00", "Мілілітр", "шт.", "", "0", "1000", "2", "1730.00",
               "561", "1.12", "-970.53", "439", "0.88", ""])
    ws.append(["Крем Тест Старий", "", "0", "1.00", "шт.", "шт.", "", "0", "2", "2", "2849.00", "0", "0", "0", "2", "2", ""])
    ws.append(["Без надходження", "", "0", "1.00", "шт.", "шт.", "", "0", "0", "0", "0", "1", "1", "-10", "0", "0", ""])
    f = tmp_path / "turn.xlsx"; wb.save(f)
    with SessionLocal() as db:
        db.add_all([Product(name="Гель Тест Кабінет 500мл", name_norm="гель тест кабінет 500мл", sku="G-500", warehouse="classic"),
                    Product(name="Крем Тест Старий", name_norm="крем тест старий", warehouse="vitrina", cost=1600, price=3000)])
        db.commit()
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for _ in range(2):                     # повторний імпорт того самого періоду не дублює
            c.post("/products/import-turnover", data={"d_from": "2022-01-01", "d_to": "2022-12-31"},
                   files={"file": ("turn.xlsx", f.read_bytes(), "application/octet-stream")})
    with SessionLocal() as db:
        gel = db.query(Product).filter_by(sku="G-500").one()
        cream = db.query(Product).filter_by(name="Крем Тест Старий").one()
        assert db.query(ProductPurchase).filter_by(product_id=gel.id).count() == 1 and gel.coef == 500
        rows = {r.product.id: r for r in catalog.product_rows(db, [])}
        assert rows[gel.id].cost_avg == 865.0                       # 1730 / 1000 мл × 500 = за флакон
        assert rows[cream.id].cost_avg == 1424.5 and rows[cream.id].cost_last == 1600   # остання — поточна з CleverBox
        assert rows[cream.id].last_source == "CleverBox"


def test_wrong_file_in_import_form(tmp_path):
    """Файл не в ту форму — зрозуміла підказка, куди його завантажити."""
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Товар", "артикул", "коеф.", "надходження к-ть одиниць обліку", "Прихід, сума"])
    ws.append(["Щось", "", "1", "2", "100"])
    f = tmp_path / "t.xlsx"; wb.save(f)
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/stock/suppliers/import", files={"file": ("t.xlsx", f.read_bytes(), "application/octet-stream")})
        assert "Рух товарів" in c.get("/stock?tab=suppliers").text
