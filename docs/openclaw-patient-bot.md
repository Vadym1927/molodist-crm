# Бот турботи @molodist_assistant_bot (OpenClaw) ↔ MOLODIST CRM

CRM і бот працюють у парі:
- **бот → CRM**: повідомляє, хто з пацієнтів у боті (привʼязка за телефоном), відписки, і може показати пацієнту «мій бюті-план»;
- **CRM → пацієнт**: розсилки й повідомлення з задач адміни шлють **токеном цього ж бота** (`PATIENT_BOT_TOKEN` у `.env` CRM).
  Надсилання через Bot API не заважає OpenClaw отримувати оновлення (polling/webhook лишаються як є).

## 1. Ключ
CRM → «Доступи й API» → API-ключі → дозвіл **bot** (лише він). Ключ передається в заголовку `X-API-Key`.
Такий ключ не може читати базу пацієнтів — лише методи нижче.

## 2. Методи (база `https://<домен CRM>/api/v1`)

### Привʼязка: після «Поділитися контактом» у боті
```
POST /bot/link
{"chat_id": "123456789", "phone": "+380671112233", "username": "olena_d",
 "name": "Олена", "create_if_missing": false}
→ {"linked": true, "created": false, "patient_id": 42, "name": "Олена Даниленко"}
→ {"linked": false, "reason": "пацієнта з таким телефоном немає в CRM"}
```
`create_if_missing: true` + `name` — створити нову картку (джерело «Telegram-бот»), якщо номера немає в базі.

### Відписка / підписка на розсилки (кнопка «Не надсилати пропозиції»)
```
POST /bot/unsubscribe   {"chat_id": "123456789"}
POST /bot/subscribe     {"chat_id": "123456789"}
```
Сервісні повідомлення (нагадування про запис з задач) відписка не блокує — лише маркетингові розсилки.

### Пацієнт вийшов з бота / заблокував
```
POST /bot/unlink        {"chat_id": "123456789"}
```
(CRM і сама відвʼязує, якщо Telegram відповів «bot was blocked».)

### «Мій бюті-план» — для кнопки в боті
```
GET /bot/patient/123456789
→ {"name": "Олена", "subscribed": true, "next_visit": "2026-10-22T11:00",
   "plan": {"title": "Beauty Plan Anti Age", "date": "2026-10-10", "doctor": "Дарʼя Колган",
            "procedures": [{"name": "IPL-терапія обличчя", "done": 1, "total": 4, "booked": 1}],
            "progress_pct": 40, "next_procedure": "IPL-терапія обличчя", "next_due": "2026-11-07",
            "home_care": [{"name": "…Azelorosa…", "url": "https://molodist.store/…", "usage": ["evening"],
                           "bought": true, "ends_about": "2026-12-20"}],
            "routine_comment": "Патчі під очі Rejuran"}}
```
Медичних нотаток і цін тут немає.

## 3. Відповіді пацієнтів
Відповіді на розсилки приходять у бот (OpenClaw) — їх обробляє бот/адміни як зазвичай.
Записи після розсилки CRM рахує сама (з CleverBox) у вікні атрибуції.


---

# Версія 2 (16.11.2026): підключити наявне меню бота до CRM

**Меню й тексти бота лишаються як є** (Записатись онлайн, Локація, Підготовка, Рекомендації, Аналізи, Мої записи…, Написати
адміністратору тощо). Завдання — щоб кнопки брали дані з CRM і передавали дії в CRM, а не зберігали їх лише в боті.

**Головне правило:** нове вмикає власник у CRM («Налаштування → Бот турботи»). Бот бере `GET /bot/config` (кеш до 5 хв)
і для вимкненої функції працює як раніше (старий текст / старе сховище). Повідомлення пацієнтам від CRM додатково блокує
загальна пауза в CRM.

## Що є в боті зараз (перевірка 16.11.2026) і що змінити
Бот: `/home/sgadmin/.openclaw/workspace/projects/molodist-clinic-bot/molodist_clinic_bot.py`, сервіс `molodist-clinic-bot`, polling.
Дані — у локальних JSON (`data/profiles.json`, `analyses.json`, `consultation_documents.json`, `beauty_plans.json`, `leads.json`),
CRM API зараз **не викликається взагалі**. Порядок робіт:

1. **Привʼязка.** Після «Поділитися контактом» — `POST /bot/link` (chat_id, phone, username, name). Один раз пройти
   `data/profiles.json` і привʼязати всіх, у кого є телефон (`create_if_missing: false`). Результат (скільки привʼязано / не знайдено) —
   у звіт власнику. Без привʼязки кабінет не працює (CRM шукає пацієнта за chat_id).
2. **Заявки (leads).** Кожну заявку з бота (запис, консультація, зворотний звʼязок) — у CRM: `POST /appeals`
   `{"name", "phone", "source": "Telegram-бот", "interest", "comment"}`. Telegram-група й Google Sheets — як зараз, паралельно.
3. **Файли.** Один раз перенести `analyses.json` → `POST /bot/patient/{chat_id}/files?category=lab`, `consultation_documents.json` →
   `?category=doc` (згоди — `consent`, фото — `face`/`body`). Дублікати CRM відкине. Далі нові файли — одразу в CRM.
4. **Бюті-плани.** Джерело — CRM (`GET /bot/patient/{chat_id}`). `beauty_plans.json` більше не наповнювати; якщо там є плани,
   яких немає в CRM, — список (імʼя, дата) у звіт власнику, не переносити самостійно.
5. **Кнопки кабінету й інші** — за таблицею нижче, кожна лише коли її функцію ввімкнено в CRM (`/bot/config`).
6. **Подарунковий сертифікат.** Наявний сценарій (сума → формат → доставка НП) лишається; в кінці замість ручної оплати —
   `POST /bot/patient/{chat_id}/order` з `note` = формат і доставка, пацієнту — `pay_url`.

## Відповідність кнопок бота і методів CRM (база `https://crm.molodist.com.ua/api/v1`, заголовок `X-API-Key`)

| Кнопка в боті | Функція (`features`) | Метод CRM |
|---|---|---|
| 💜 Мої записи, знижки, бонуси та бʼюті-план → 🗓 Мої записи | cabinet | `GET /bot/patient/{chat_id}/records` |
| → 💰 Мій баланс | cabinet | `GET /bot/patient/{chat_id}/wallet` → `balance` (лише гроші на рахунку, не бонуси) |
| → 🏷 Мої знижки та абонементи | cabinet | той самий `wallet` → `discount_pct`, `bonuses`, `subscriptions[]`, `certificates[]` |
| → ✨ Мій бʼюті-план | cabinet | `GET /bot/patient/{chat_id}` (процедури done/total, прогрес, наступна, домашній догляд) |
| → 📁 Мої документи | files | `GET /bot/patient/{chat_id}/files` → `categories[]` (Обличчя, Тіло, Згоди, Документи, Аналізи з кількістю) і `files[]`; `?category=lab` — лише аналізи; файл — `GET .../files/{id}` |
| 📨 Аналізи (надіслати) | files | `POST /bot/patient/{chat_id}/files?category=lab` (multipart `file`) |
| 💬 Написати адміністратору | messages | `POST /bot/patient/{chat_id}/message {"to": "admin", "text": "..."}` |
| 💌 Відправити відгук головному лікарю | messages | `POST /bot/patient/{chat_id}/message {"to": "chief", "kind": "review", "text": "..."}` |
| 🎁 Подарунковий сертифікат | certs | `GET /bot/offers` → `certs[]`, `rules.cert_rules`; `POST /bot/patient/{chat_id}/order` |
| 🔥 Акції | — (завжди) | `GET /bot/offers` → `rules.promos[]`, `rules.loyalty_note` («Програми лояльності між собою не сумуються») |
| 📚 Корисні матеріали | — (завжди) | `GET /bot/guides` → `guides[]` (назва, опис, посилання на PDF) |
| 👨‍⚕️ Про лікарів | — (завжди) | `GET /bot/doctors` (Колган, Сирова, Чугунова — як на сайті) |
| 📅 Записатись онлайн | — | посилання на сайт: `https://new.molodist.com.ua/zapys` (поки сторінка не опублікована — як зараз) |
| Нагадування про візит (CRM шле сама) | reminders | callback-кнопки → `POST /bot/appointment/{id}/answer` (нижче) |

Кнопки «Локація», «Підготовка до процедури», «Рекомендації після процедури», «Обстеження…», «Акне», «Знижки від партнерів»,
«Замовити косметику», «Суміжні спеціалісти» — без змін.

Що вже лежить у боті (документи, аналізи, які пацієнти надсилали раніше): **один раз** перенести в CRM через
`POST /bot/patient/{chat_id}/files?category=<face|body|consent|doc|lab>` — дублікати CRM відкине сама. Після цього
джерело правди — CRM.

## Деталі методів

### Конфіг
```
GET /bot/config
→ {"features": {"cabinet": false, "files": false, "messages": false, "certs": false, "reminders": false, "login": false},
   "callback_format": "mcrm:<confirm|reschedule|cancel>:<appointment_id>", "actions": {...}}
```

### Мої записи
```
GET /bot/patient/{chat_id}/records
→ {"upcoming": [{"id": 812, "when": "вівторок, 17 листопада о 11:00", "service": "IPL Все обличчя", "doctor": "Софія Сирова",
                 "status_text": "Записаний", "actions": ["confirm", "reschedule", "cancel"]}],
   "past": [...]}
```
Під майбутнім записом — inline-кнопки з `actions` (ті самі, що в нагадуванні, callback `mcrm:<дія>:<id>`).

### Баланс, знижки, абонементи
```
GET /bot/patient/{chat_id}/wallet
→ {"balance": 350.0, "discount_pct": 10, "bonuses": 120.0,
   "subscriptions": [{"name": "...", "visits_left": 4, "visits_total": 6, "money_left": null, "valid_to": "2027-03-01"}],
   "certificates": [{"code": "S-1024", "name": "...", "balance": 2000.0, "valid_to": "2026-12-31"}]}
```

### Мої документи / аналізи
```
GET  /bot/patient/{chat_id}/files[?category=lab]
GET  /bot/patient/{chat_id}/files/{id}                 → сам файл
POST /bot/patient/{chat_id}/files?category=lab&caption=...   multipart, поле file (PDF / фото, до 20 МБ)
→ {"ok": true, "duplicate": false, "file_id": 123}
```
`duplicate: true` — «Цей файл уже є у вашій картці». Новий файл → задача адміністратору в CRM. Вміст аналізів бот не читає й не переказує.

### Написати адміністратору / відгук головному лікарю
```
POST /bot/patient/{chat_id}/message  {"to": "admin" | "chief", "kind": "message" | "review", "text": "..."}
→ {"ok": true, "task_id": 55}
```

### Сертифікат / абонемент
```
GET  /bot/offers
POST /bot/patient/{chat_id}/order  {"kind": "cert", "id": 2, "to": "Олена", "to_phone": "+380671112233"}
                                   {"kind": "sub", "id": 7}
→ {"ok": true, "title": "...", "price": 2000, "pay_url": "https://pay.monobank.ua/..."}
```
Сертифікат: імʼя й телефон отримувача обовʼязкові (спитати двома кроками), показати `rules.cert_rules`. Абонемент: показати вигоду
«{per} ₴ за процедуру, вигода {save} ₴ (−{pct}%)». Після оплати CRM сама проводить продаж і фіскалізацію (чек monobank).
Немає `pay_url` — «Адміністратор звʼяжеться з вами для оплати».

### Нагадування з кнопками
CRM шле напередодні візиту **токеном цього бота** з inline-кнопками
`mcrm:confirm:<id>` / `mcrm:reschedule:<id>` / `mcrm:cancel:<id>`. Callback приходить у бот — бот обробляє:
1. confirm → `POST /bot/appointment/{id}/answer {"chat_id": "...", "action": "confirm"}` → показати `reply`, прибрати кнопки.
2. cancel → спитати причину (кнопки «Захворів(ла)», «Змінились плани», «Інше» + текст) → `{"action": "cancel", "reason": "..."}`.
3. reschedule → «На коли вам зручно?» (текст) → `{"action": "reschedule", "wish": "..."}`.
Відповідь: `{"ok": true, "status": "confirmed", "reply": "текст для пацієнта"}`; `ok: false` — показати `reply`. Завжди `answerCallbackQuery`.

### Код для входу на сайті (features.login) — готується, метод буде додано.

## Правила
- Ключ лише зі scope **bot**, у секретах агента, не в логах. До 2 запитів/с.
- Дані — лише власнику chat_id. Без медичних подробиць понад те, що віддає CRM. Медичних нотаток CRM боту не віддає.
- Розсилки робить CRM, бот сам масово не пише. Без російських сервісів.
- Помилки: 404 — не привʼязано (запропонувати «Поділитися контактом»), 400 — показати `detail` простими словами,
  5xx — «Сервіс тимчасово недоступний», до 3 повторів.
- Не змінювати поведінку кнопок, поки відповідна функція вимкнена в CRM.
- Перевірка: CRM «Налаштування → Бот турботи» (лічильник, останні дії бота), картка пацієнта (Telegram-бот ✓, задачі, файли за категоріями, історія візиту).
