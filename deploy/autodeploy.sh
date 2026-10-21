#!/usr/bin/env bash
# Автооновлення MOLODIST CRM з GitHub (гілка main).
# Запускається systemd-таймером кожні 3 хв. Якщо в main є нові коміти:
#   бекап БД → git merge --ff-only → docker compose up -d --build app → перевірка /health
#   → не піднялось за 3 хв → відкат на попередній коміт + повідомлення.
# Результат — у Telegram власнику (TELEGRAM_ALERT_CHAT_ID). Секрети з .env не виводяться.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/molodist-crm}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8010/health}"
BRANCH="${BRANCH:-main}"
LOG="${LOG:-/var/log/molodist-autodeploy.log}"

cd "$APP_DIR" || exit 1
exec 9>/var/lock/molodist-autodeploy.lock
flock -n 9 || exit 0

envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }
TG_TOKEN="$(envval TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(envval TELEGRAM_ALERT_CHAT_ID)"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
notify() {
  log "$1"
  [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] || return 0
  curl -s -m 20 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null || true
}

git fetch -q origin "$BRANCH" 2>>"$LOG" || { log "git fetch не вдався"; exit 1; }
OLD="$(git rev-parse HEAD)"
NEW="$(git rev-parse "origin/$BRANCH")"
[ "$OLD" = "$NEW" ] && exit 0
# цей коміт уже відкочувався — чекаємо наступного
[ -f .autodeploy-skip ] && [ "$(cat .autodeploy-skip)" = "$NEW" ] && exit 0

# Хтось правив код прямо на сервері — не затираємо, зупиняємось.
if ! git diff --quiet || ! git diff --cached --quiet; then
  if [ ! -f .autodeploy-blocked ] || [ "$(cat .autodeploy-blocked)" != "$NEW" ]; then
    echo "$NEW" > .autodeploy-blocked
    notify "⚠️ CRM: на сервері є незакомічені зміни ($(git diff --name-only | head -5 | tr '\n' ' ')). Автооновлення зупинено, нічого не змінено."
  fi
  exit 1
fi
rm -f .autodeploy-blocked

SUBJ="$(git log -1 --format=%s "$NEW")"
notify "🔄 CRM оновлюється: ${SUBJ}"

# 1) Бекап БД перед оновленням
if ! docker compose exec -T app python -c "from app.services.backups import run_backup; print(run_backup('pre-deploy'))" >>"$LOG" 2>&1; then
  notify "❌ CRM: бекап перед оновленням не вдався — оновлення скасовано."
  exit 1
fi

deploy_to() {
  git merge --ff-only -q "$1" >>"$LOG" 2>&1 || git reset -q --hard "$1"
  git rev-parse --short HEAD > app/VERSION
  docker compose up -d --build app >>"$LOG" 2>&1
}

healthy() {
  for _ in $(seq 1 36); do
    sleep 5
    curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1 && return 0
  done
  return 1
}

if ! git merge-base --is-ancestor "$OLD" "$NEW"; then
  notify "❌ CRM: нова версія не продовжує поточну (історія розійшлась) — оновлення скасовано."
  exit 1
fi

deploy_to "$NEW"
if healthy; then
  notify "✅ CRM оновлено: ${SUBJ} ($(git rev-parse --short HEAD))"
  docker image prune -f >/dev/null 2>&1 || true
  exit 0
fi

TAIL="$(docker compose logs --tail 15 app 2>&1 | grep -viE 'token|password|secret|key=' | tail -8)"
notify "❌ CRM після оновлення не відповідає — повертаю попередню версію.
${TAIL}"
git reset -q --hard "$OLD"
git rev-parse --short HEAD > app/VERSION
docker compose up -d --build app >>"$LOG" 2>&1
if healthy; then
  notify "↩️ CRM повернуто на попередню версію ($(git rev-parse --short HEAD)), працює. Новий коміт ${NEW:0:7} не застосовано."
  echo "$NEW" > .autodeploy-skip
else
  notify "🆘 CRM не піднялась навіть після відкату. Потрібна ручна перевірка: cd $APP_DIR && sudo docker compose logs app"
fi
exit 1
