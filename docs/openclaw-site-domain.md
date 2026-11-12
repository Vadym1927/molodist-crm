# Інструкція агенту OpenClaw: підключити домен сайту new.molodist.com.ua

## Роль і межі
Ти — агент автоматизації MOLODIST на VPS. Завдання: відкрити сторінки сайту, які вже віддає CRM, на домені `new.molodist.com.ua`.
Код CRM, контейнери Docker, `.env`, бот пацієнтів, конфіг `crm.molodist.com.ua` — **не чіпати**.

## Що вже є
- A-запис `new.molodist.com.ua` → IP цього VPS додав власник.
- CRM (контейнер `app`) слухає `127.0.0.1:8010` і сама розпізнає домен `new.molodist.com.ua` за заголовком `Host`:
  на ньому відкривається сайт, CRM там недоступна. Нічого в CRM міняти не треба.

## Кроки
1. **Перевір DNS:** `dig +short new.molodist.com.ua` має повернути IP цього сервера (`curl -s ifconfig.me`). Якщо ні — зупинись і напиши власнику.
2. **Подивись наявний конфіг CRM** (не змінюй його): `sudo grep -rl "crm.molodist.com.ua" /etc/nginx/` — візьми з нього стиль (sites-available / conf.d).
3. **Створи новий файл** (у тій самій теці, де конфіг crm), напр. `/etc/nginx/sites-available/new.molodist.com.ua`:
   ```nginx
   server {
       listen 80;
       listen [::]:80;
       server_name new.molodist.com.ua;

       client_max_body_size 10m;

       location / {
           proxy_pass http://127.0.0.1:8010;
           proxy_http_version 1.1;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_read_timeout 60s;
       }
   }
   ```
   Якщо використовується sites-enabled: `sudo ln -s /etc/nginx/sites-available/new.molodist.com.ua /etc/nginx/sites-enabled/`.
4. **Перевір і перезавантаж:** `sudo nginx -t` — лише якщо OK, тоді `sudo systemctl reload nginx` (reload, не restart).
   Якщо `nginx -t` дає помилку — видали свій новий файл/посилання і напиши власнику текст помилки.
5. **Сертифікат:** `sudo certbot --nginx -d new.molodist.com.ua --redirect --non-interactive --agree-tos -m <email з наявних сертифікатів certbot>`
   (email подивись у `sudo certbot certificates` / `/etc/letsencrypt/renewal/`; якщо не знайшов — запусти без `--non-interactive` і спитай власника).
   Перевір автооновлення: `sudo certbot renew --dry-run`.

## Перевірка
- `curl -sI https://new.molodist.com.ua/laserepilation` → `200`.
- `curl -s https://new.molodist.com.ua/ | grep -c data-mlds` → `1` або більше (це сторінка сайту, не CRM).
- `curl -sI https://new.molodist.com.ua/login` → не сторінка входу CRM (404 — нормально).
- `curl -sI http://new.molodist.com.ua/` → редірект 301 на https.
- `curl -sI https://crm.molodist.com.ua/health` → як і раніше `200` (CRM не зачеплено).

## Чого не робити
- Не змінювати конфіги `crm.molodist.com.ua`, інших доменів, бота пацієнтів і OpenClaw.
- Не відкривати порт 8010 назовні, не перезапускати Docker-контейнери CRM.
- Не писати токени, ключі й вміст `.env` у чат, звіти чи логи.

## Звіт власнику (коротко)
- шлях створеного конфігу Nginx; результат `nginx -t`; результат certbot і дата закінчення сертифіката;
- результати 5 перевірок вище.
