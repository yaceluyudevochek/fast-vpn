#!/usr/bin/env bash
# =============================================================================
# Выкладка бота на сервер — те же шаги, что делались вручную, но одной командой:
#   1. рабочая копия чистая, тесты проходят (иначе стоп);
#   2. сравнение файлов с сервером — выкладываются только изменённые;
#   3. бэкап базы и заменяемых файлов на сервере;
#   4. загрузка, pip install (если поменялись requirements), перезапуск;
#   5. проверка: служба активна, в логах после старта нет ошибок.
#
# Использование:
#   DEPLOY_HOST=root@1.2.3.4 bot/scripts/deploy.sh   # выложить текущий коммит (спросит подтверждение)
#   scripts/deploy.sh --yes           # без вопроса
#   scripts/deploy.sh rollback <папка из backups/>   # вернуть код из бэкапа и перезапустить
# Папку и службу можно поменять: DEPLOY_DIR=/opt/fastvpnbot DEPLOY_SERVICE=fastvpnbot
# =============================================================================
set -euo pipefail

HOST="${DEPLOY_HOST:?Укажите сервер: DEPLOY_HOST=root@1.2.3.4 bot/scripts/deploy.sh}"
DIR="${DEPLOY_DIR:-/opt/fastvpnbot}"
SERVICE="${DEPLOY_SERVICE:-fastvpnbot}"
SSH="ssh -o BatchMode=yes $HOST"
# Шум журнала, не означающий поломку (обрыв связи с Telegram, остановка старого процесса)
NOISE='Failed to fetch updates|Unclosed client session|Unclosed connector|client_session:|connections:|connector:'

cd "$(git rev-parse --show-toplevel)/bot"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31mСТОП: %s\033[0m\n' "$*" >&2; exit 1; }

restart_and_check() {
    say "Перезапуск $SERVICE и проверка логов"
    $SSH "start=\$(date '+%Y-%m-%d %H:%M:%S'); systemctl restart $SERVICE; sleep 15
          systemctl is-active --quiet $SERVICE || { echo 'служба не запустилась'; journalctl -u $SERVICE -n 30 --no-pager; exit 1; }
          journalctl -u $SERVICE --since \"\$start\" --no-pager | grep -E 'миграц|Run polling' | sed -E 's/^.* - (INFO|ERROR) - //'
          errors=\$(journalctl -u $SERVICE --since \"\$start\" --no-pager | grep -E ' - ERROR - |Traceback' | grep -vE '$NOISE' || true)
          if [ -n \"\$errors\" ]; then echo 'ОШИБКИ ПОСЛЕ СТАРТА:'; echo \"\$errors\" | tail -20; exit 2; fi
          echo 'служба активна, ошибок нет'"
}

if [ "${1:-}" = "rollback" ]; then
    backup="${2:-}"
    [ -n "$backup" ] || die "укажите папку бэкапа: scripts/deploy.sh rollback code_before_..."
    say "Возвращаю код из $DIR/backups/$backup"
    $SSH "cd $DIR && test -d backups/$backup && cp -a backups/$backup/. ./ && find backups/$backup -type f | sed 's#^#  #'"
    restart_and_check
    echo "База не откатывалась. Если нужна и она — копия bot_database.db.before_* рядом, восстанавливайте вручную при остановленном боте."
    exit 0
fi

ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

# ---------- 1. чистая рабочая копия и тесты ----------
[ -z "$(git status --porcelain)" ] || die "есть незакоммиченные изменения — закоммитьте или уберите их"
COMMIT=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
say "Коммит $COMMIT ($BRANCH): $(git log -1 --format=%s)"
[ "$BRANCH" = "main" ] || echo "Внимание: выкладывается не main, а ветка $BRANCH"

PY=venv/bin/python
[ -x "$PY" ] || PY=python3
say "Тесты"
"$PY" tests/run_all.py > /tmp/vpnbot-deploy-tests.log 2>&1 || { tail -30 /tmp/vpnbot-deploy-tests.log; die "тесты не прошли"; }
grep -E '^(OK|==)' /tmp/vpnbot-deploy-tests.log | grep -c '^OK' | sed 's/^/  пройдено проверок: /'

# ---------- 2. что изменилось относительно сервера ----------
FILES=$(git ls-files | grep -vE '^(tests/|scripts/|\.github/|README\.md$|\.gitignore$|config/config\.py\.example$)')
say "Сравниваю $(echo "$FILES" | wc -l | tr -d ' ') файлов с сервером"
local_sums=$(for f in $FILES; do printf '%s  %s\n' "$(tr -d '\r' < "$f" | shasum | cut -c1-40)" "$f"; done)
remote_sums=$(echo "$FILES" | $SSH "cd $DIR && while read -r f; do
    if [ -f \"\$f\" ]; then printf '%s  %s\n' \"\$(tr -d '\r' < \"\$f\" | sha1sum | cut -c1-40)\" \"\$f\"; else printf 'missing  %s\n' \"\$f\"; fi
  done")
CHANGED=$(diff <(echo "$local_sums") <(echo "$remote_sums") | grep '^<' | sed -E 's/^< [0-9a-f]+  //' || true)
if [ -z "$CHANGED" ]; then
    echo "На сервере уже этот код — выкладывать нечего"
    exit 0
fi
echo "$CHANGED" | while read -r f; do
    if echo "$remote_sums" | grep -qxF "missing  $f"; then echo "  + $f (новый)"; else echo "  ~ $f"; fi
done

if [ "$ASSUME_YES" != 1 ]; then
    printf '\nВыложить на %s? [y/N] ' "$HOST"
    read -r answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ] || die "отменено"
fi

# ---------- 3. бэкап на сервере ----------
STAMP=$($SSH 'date +%Y%m%d_%H%M%S')
BACKUP="code_before_${COMMIT}_${STAMP}"
say "Бэкап: bot_database.db.before_${COMMIT}_${STAMP}, backups/$BACKUP/"
echo "$CHANGED" | $SSH "cd $DIR && sqlite3 bot_database.db \".backup bot_database.db.before_${COMMIT}_${STAMP}\" \
    && mkdir -p backups/$BACKUP && while read -r f; do [ -f \"\$f\" ] && cp --parents \"\$f\" backups/$BACKUP/; done; true"

# ---------- 4. загрузка ----------
say "Загрузка"
# shellcheck disable=SC2086
COPYFILE_DISABLE=1 tar czf - $CHANGED 2>/dev/null | $SSH "cd $DIR && tar xzf - 2>/dev/null; echo $COMMIT > DEPLOYED_COMMIT"
if echo "$CHANGED" | grep -qx 'requirements'; then
    say "Изменились зависимости — pip install"
    $SSH "cd $DIR && venv/bin/pip install -q -r requirements"
fi

# ---------- 5. перезапуск и проверка ----------
if restart_and_check; then
    say "Готово: на сервере $COMMIT. Откат кода: scripts/deploy.sh rollback $BACKUP"
else
    die "после перезапуска есть проблемы. Откат кода: scripts/deploy.sh rollback $BACKUP"
fi
