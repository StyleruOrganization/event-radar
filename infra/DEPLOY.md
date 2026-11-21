# EventRadar на общей инфраструктуре Termeet

Исходники приложения находятся в `StyleruOrganization/event-radar`.
Общий репозиторий `StyleruOrganization/infra` управляет сборкой и релизами,
как для `termeet/bot`. Инвентарь EventRadar находится в `projects/event-radar/`.
Проект использует сервер `primary` и корень `/opt/platform-infra` из существующего
`hosts/primary/host.json`. Если нужен отдельный сервер, сначала добавьте его
в `hosts/` и замените `host` в инвентаре проекта.

Релизы запускаются вручную через GitHub Actions общего репозитория infra.
Пуш приложения публикует исходники. Затем владелец выбирает `Release service`,
`project=event-radar`, `service=bot`, `ref=main` или полный SHA приложения.
SSH-секреты берутся из существующих `SERVER_HOST`, `SERVER_USER`,
`SERVER_SSH_PRIVATE_KEY`, `SERVER_SSH_FINGERPRINT` репозитория infra.

## Подготовка репозиториев

Опубликуйте приложение в новом репозитории и изменения общего deploy.py вместе
с `projects/event-radar/project.json` и `secrets.example.json` в infra.
Доступ сервера к новому приватному репозиторию настройте тем же способом,
которым он получает `StyleruOrganization/Termeet-bot`.

В локальной копии infra создайте секреты проекта штатной командой.
Значение `--recipient` должно содержать публичные age-ключи владельца и сервера,
которыми зашифрованы другие проекты этого хоста.

```bash
cd /Users/kipabelousov/termeet/infra
read -r -p 'Публичные age-ключи владельца и сервера через запятую: ' AGE_RECIPIENTS
python3 tools/manage_secrets.py init project event-radar --recipient "$AGE_RECIPIENTS"
python3 tools/manage_secrets.py edit project event-radar
```

Заполните пять значений из текущей конфигурации EventRadar.

- `TELEGRAM_BOT_TOKEN`
- `OWNER_TELEGRAM_ID`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_READER_SESSION`

Сохраните `projects/event-radar/secrets.enc.json` в Git.
Параметры часового пояса и лимитов задаются в `project.json`.
Перенесите собственные значения со старого сервера, если они отличаются.
Файлы авторизации ChatGPT хранятся только на сервере в `runtime/chatgpt`.

В Actions infra запустите `Release infrastructure` с `project=event-radar`,
`update_host=false`. Будут подготовлены сеть и
`/opt/platform-infra/projects/event-radar/env/bot.env`.
Traefik и другие сервисы хоста должны быть уже установлены, как для Termeet.

## Одноразовая подготовка нового сервера

Команды выполняются в SSH-сеансе под тем же пользователем деплоя, что у Termeet.
Для создания приватных каталогов данных потребуется sudo.

```bash
sudo install -d -m 700 -o 10001 -g 10001 \
  /opt/platform-infra/projects/event-radar/data \
  /opt/platform-infra/projects/event-radar/runtime/chatgpt
```

Для первоначального импорта нужна утилита приложения внутри образа.
Соберите его один раз из нового репозитория. Следующие сборки выполняет workflow.

```bash
set -e
IMPORT_SOURCE=$(mktemp -d /tmp/event-radar-import.XXXXXX)
git clone https://github.com/StyleruOrganization/event-radar.git "$IMPORT_SOURCE"
APP_SHA=$(git -C "$IMPORT_SOURCE" rev-parse HEAD)
docker build --tag "platform/event-radar-bot:$APP_SHA" "$IMPORT_SOURCE"
printf '%s\n' "$APP_SHA"
```

Сохраните напечатанный SHA для импорта. После миграции можно удалить каталог
`$IMPORT_SOURCE`. База и ChatGPT-сессия остаются в каталоге проекта.

## Первый запуск с пустой базой

Владелец выбрал заново добавить каналы на новом сервере.
При первом релизе приложение создаст новую SQLite-базу в каталоге `data`.
Каналы, персональные промпты и настройки задаются заново через команды бота.
Первый дайджест использует историю за `INITIAL_LOOKBACK_DAYS`, по умолчанию 7 дней.

Перед запуском новой версии отключите автоматический деплой старого репозитория
через `DEPLOY_ENABLED=false` и остановите прежнего бота. На старом сервере
под `kalan` выполните следующую команду.

```bash
sudo -u eventradar bash -c 'cd /srv/eventradar && docker compose stop bot'
```

Так один Telegram-токен будет обслуживать только новый сервер.
Старая база остаётся в Docker-томе прежнего сервера.

## Вход в новый аккаунт ChatGPT

На Mac выполните вход в отдельном каталоге. Держите терминал открытым,
откройте показанную ссылку и выберите новый аккаунт на странице OpenAI.

```bash
cd /Users/kipabelousov/eventradar
source .venv312-uv/bin/activate
python -m eventradar.chatgpt_auth --directory data/chatgpt-account-20261121 login --no-browser
read -r -p 'SSH нового сервера в формате пользователь@хост: ' NEW_SERVER
scp data/chatgpt-account-20261121/credentials.json "$NEW_SERVER:~/eventradar-session.json"
```

Передайте сессию сразу после успешного входа, пока её ID token действителен.
Если этот локальный каталог уже использовался для другой регистрации,
выберите новое имя каталога. Повторный вход на тот же аккаунт может использовать
его существующий каталог.

На новом сервере в сеансе пользователя деплоя выполните импорт в пустой каталог.
Значение `APP_SHA` берётся из предыдущего шага или вводится заново.

```bash
read -r -p 'SHA образа приложения: ' APP_SHA
docker run --rm -i \
  --volume /opt/platform-infra/projects/event-radar/runtime/chatgpt:/data/chatgpt \
  "platform/event-radar-bot:$APP_SHA" \
  python -m eventradar.chatgpt_auth --directory /data/chatgpt import-session \
  < "$HOME/eventradar-session.json"
docker run --rm \
  --volume /opt/platform-infra/projects/event-radar/runtime/chatgpt:/data/chatgpt \
  "platform/event-radar-bot:$APP_SHA" \
  python -m eventradar.chatgpt_auth --directory /data/chatgpt status
```

Если на новом сервере уже есть авторизация, импорт остановится.
Сначала остановите активный контейнер EventRadar, выполните `logout`
тем же `docker run` и затем удалите только старый `credentials.json` через
`sudo rm -- /opt/platform-infra/projects/event-radar/runtime/chatgpt/credentials.json`.
Это очищает регистрацию прежнего аккаунта перед импортом нового.

После успешного импорта удалите временный `~/eventradar-session.json`.
Сервер становится единственным процессом, обновляющим эту сессию.
Локальную копию после переноса не используйте для запросов и logout.
SQLite-снимки не включают файлы авторизации из `runtime/chatgpt`.

## Первый релиз и следующие обновления

Запустите `Release service` в `StyleruOrganization/infra`.

- `project` — `event-radar`
- `service` — `bot`
- `ref` — `main` или полный SHA приложения

Релиз строит образ, проверяет окружение и наличие ChatGPT-сессии,
останавливает предыдущий контейнер проекта и выполняет SQLite-бэкап через
одноразовый контейнер. Только после успешного бэкапа запускается новая версия.
При первом запуске с пустым каталогом данных шаг резервного копирования
завершается сообщением о создании новой базы при старте приложения.
Readiness проверяется через `python -m eventradar.health`.
Копии сохраняются в `data/backups`, остаются последние десять файлов.

На сервере посмотреть фактическую версию и состояние можно так.

```bash
docker ps -a --filter label=platform.project=event-radar \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
cat /opt/platform-infra/projects/event-radar/state.json
```

Успешный workflow заканчивается строкой `Released event-radar/bot`.
После первого старта отправьте `/ai retry` со своего Telegram-аккаунта владельца,
чтобы очистить перенесённую паузу от прежней сессии. Затем проверьте `/channels`.

Если новая версия не запускается, её логи сохраняются в
`projects/event-radar/diagnostics/bot-last-failure.log`, а старый контейнер
повторно запускается с прежним окружением и образом. При несовместимой миграции
базы восстановление из бэкапа выполняется вручную после остановки приложения.
Параллельные релизы одного проекта сериализованы серверным файловым lock.

После подтверждения работы нового бота и добавления нужных каналов
можно завершить работу со старым сервером.
