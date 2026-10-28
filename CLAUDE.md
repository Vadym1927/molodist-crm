# MOLODIST Beauty Plan CRM — контекст для Claude Code

Мова спілкування з власником (Вадим) — українська. Працює швидко, любить коротко і по суті.

## Що це
CRM естетичної клініки MOLODIST (Київ) під бюті-плани, повʼязана з CleverBox (основна CRM клініки).
Основа — картка пацієнта з бюті-планом (PDF з апарата OBSERV 520x → процедури × ціна + домашній догляд)
і відстеження реалізації плану за даними CleverBox: виконано / записано / залишок, KPI, фінпрогноз.

## Де і як працює (VPS)
- Код: `/opt/molodist-crm`, запуск `sudo docker compose up -d --build` (сервіси `db` Postgres 16, `app` FastAPI).
- **Caddy НЕ використовується**: порти 80/443 зайняті наявним Nginx. `app` слухає `127.0.0.1:8010`,
  Nginx-конфіг `crm.molodist.com.ua` → `proxy_pass http://127.0.0.1:8010`, HTTPS через certbot.
- На цьому ж VPS працює OpenClaw (автоматизація клініки) і бот для пацієнтів — їх не чіпати.
- `.env` (секрети) — НЕ виводити у відповідях/логи, не комітити. Права: root, тому `sudo`.
- Один процес app: усередині планувальник (синк CleverBox кожні 2 год, бекапи 03:30, нагадування 10:00,
  розсилки) і Telegram-бот адмінів. Не запускати кілька воркерів/копій.
- Код на сервері змінювався агентом OpenClaw (вкладки в картці пацієнта, поле токена CleverBox в UI тощо) —
  перед змінами подивись `git log`/`git diff`, не затирай чужі правки.

## Структура
- `app/main.py` — веб-маршрути, планувальник; `app/api.py` — REST API v1 (ключі зі scope read/write/medical/bot)
- `app/models.py` — SQLAlchemy; автоміграція лише ДОДАЄ nullable-колонки (`_add_missing_columns`)
- `app/parsers/observ.py` — парсер PDF OBSERV (блок «Загальні витрати» — джерело цифр)
- `app/services/kpi.py` — зіставлення візитів/продажів з позиціями плану, статуси, KPI, прогноз
  (статуси вантажаться пакетно `PLAN_LOAD` + мемо в межах сесії — не повертати N+1)
- `app/integrations/cleverbox.py` — CleverBox REST API v3 (https://cbox.mobi/api/v3/rest, POST JSON,
  токен у заголовку `token`, ліміт 1 запит/сек, IP сервера має бути в дозволених у CleverBox)
- `app/services/`: clients (сегменти), tasks (автозадачі), scripts (скрипти адмінів), campaigns (розсилки),
  booking (запис у CleverBox через slots), backups, alerts (Telegram власнику), exporter/importer
- `app/integrations/messaging.py` — бот пацієнтів (токен PATIENT_BOT_TOKEN) + TurboSMS (Viber→SMS)
- `docs/openclaw-patient-bot.md` — контракт API для бота пацієнтів
- Тести: `docker compose exec app pytest -q` (потрібен будь-який PDF плану в `tests/sample_plan.pdf`)

## Як вносяться зміни
- Код живе в GitHub `Vadym1927/molodist-crm` (main). Сервер кожні 3 хв підтягує main
  (`deploy/autodeploy.sh`, systemd-таймер): бекап → оновлення → /health → відкат при збої, звіт у Telegram.
- Руками на сервері код не правити — автооновлення зупиниться. `/health` показує версію (короткий хеш коміту).

## Склад (облік у CRM)
- Правда — журнал `StockMove` (залишок = сума рухів по товару і складу); 5 складів: vitrina, inject, classic, single, equip.
- Старт — документ «Стартові залишки» з поточних залишків CleverBox/Excel (`stock.start_ledger`), дата в SyncState `stock_ledger_start`.
- Документи `StockDoc`: supply, order, transfer, writeoff, return, inventory; draft → posted (рухи) → можна скасувати.
- Після кожного синку `stock.auto_moves`: продажі CleverBox (ref sale:…) і процедури за нормами `ProcedureNorm` (ref proc:…).

## Відкриті питання
- Перевірити можливі дублі продажів товарів після переносу частинами (external_id = check_id-product_id-date).
- Поля CleverBox /clients і /staff звіряються автоматично (назви полів пишуться в SyncState
  `cleverbox_client_fields` / `cleverbox_staff_fields`, власник бачить їх у «Спеціалісти»).

## Брендбук (2026) — для будь-яких змін інтерфейсу
- Кольори: білий #FFFFFF, крем #F7F4F0, грейдж #E9E5DF, графіт #2A2430 (текст, панель), глибокий фіолетовий #3B3050 (акцент),
  лаванда #E4DEEF / #C9BFE6. Класичний #5C4B82 — лише кнопки й посилання. Пропорція 60% світлі / 30% графіт / 10% фіолетовий.
- Шрифти (self-hosted у app/static/fonts): Belleza — лише слово MOLODIST (розрідка 0.16em); Montserrat — заголовки великими;
  Open Sans — текст. Монограма — літера M у колі.
- Словник: «пацієнт» (не клієнт), «лікар» (не майстер), «медичний простір» (не салон), «ботулінотерапія» (не ботокс), на «ви».

## Правила
- Медичні нотатки лікаря (`BeautyPlan.medical_notes`) не відправляти в Telegram/SMS/скрипти/API без scope medical.
- Не використовувати російські сервіси (напр. Tilda для нових рішень) — українські/європейські альтернативи.
- Перед `docker compose up -d --build` — зробити бекап: «Дані → Зробити копію зараз» або
  `sudo docker compose exec app python -c "from app.services.backups import run_backup; print(run_backup('manual'))"`.
- Деструктивні дії з БД (DELETE, DROP, відновлення бекапу) — лише з явного дозволу власника.
