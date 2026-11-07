# EventRadar: первый запуск на сервере Termeet

Инструкция для Ubuntu/Debian с уже работающим Docker Engine и Compose v2.
Используем отдельный Compose-проект `eventradar`, каталог `/srv/eventradar`,
пользователя `eventradar` и volume `eventradar_data`. Порты наружу бот не открывает.
Команды Termeet и его контейнеры менять не требуется. Сборка EventRadar расходует
общие ресурсы сервера; выполняйте первый запуск в удобное время.

Не выполняйте весь документ одним скриптом: между шагами требуется добавить ключи
в GitHub и заполнить конфигурацию. `SERVER_HOST` ниже — IP/DNS сервера Termeet;
`SERVER_PORT` — его SSH-порт. Эти значения берутся из вашего действующего SSH-доступа.
Все команды ниже выполняет владелец. Никакие ключи или токены в переписку отправлять не нужно.

## 1. Подготовить код на компьютере

Сначала отправьте актуальный код проекта в `main` репозитория
`Kiryshka1922/eventradar` вашим обычным Git-процессом. В коммит входят исходники,
`compose.yaml`, `Dockerfile`, `infra/deploy.sh`, workflow и документация.
`.env`, `data/`, `runtime/` и SSH-ключи в Git не входят.
До завершения переноса не устанавливайте `DEPLOY_ENABLED=true`.

## 2. Создать пользователя на сервере

В существующем SSH-сеансе администратора сервера Termeet:

```bash
sudo adduser --disabled-password --gecos '' eventradar
sudo usermod -aG docker eventradar
sudo install -d -m 700 -o eventradar -g eventradar /home/eventradar/.ssh
sudo install -d -m 750 -o eventradar -g eventradar /srv/eventradar
```

Если пользователь уже существует, не создавайте его повторно. Docker, Compose,
Git, OpenSSH и `flock` (пакет util-linux) должны быть установлены. Не переустанавливайте
Docker работающего Termeet. После изменения групп нужен новый вход пользователя.
Членство в `docker` фактически дает полномочия root:
[официальная документация Docker](https://docs.docker.com/engine/install/linux-postinstall/).

## 3. Ключ для GitHub Actions → сервер

На компьютере создайте отдельную пару. Если это имя занято, выберите новое;
не подтверждайте перезапись существующего ключа.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/eventradar_actions -C eventradar-actions -N ''
pbcopy < ~/.ssh/eventradar_actions.pub
```

На сервере от администратора откройте файл:

```bash
sudo -u eventradar nano /home/eventradar/.ssh/authorized_keys
```

Вставьте скопированный публичный ключ отдельной строкой, сохранив существующие
ключи. Перед `ssh-ed25519` можно поставить `restrict `: деплою не нужны
PTY, agent forwarding и port forwarding. Сохраните файл и задайте права:

```bash
sudo chown eventradar:eventradar /home/eventradar/.ssh/authorized_keys
sudo chmod 600 /home/eventradar/.ssh/authorized_keys
```

На компьютере задайте реальные параметры и подключитесь. Эти переменные нужны в
каждом новом локальном терминале, где выполняете дальнейшие команды:

```bash
read -r 'SERVER_HOST?IP или DNS сервера Termeet: '
read -r 'SERVER_PORT?SSH-порт (обычно 22): '
ssh -i ~/.ssh/eventradar_actions -o IdentitiesOnly=yes -p "$SERVER_PORT" "eventradar@$SERVER_HOST"
```

При первом подключении сравните fingerprint с полученным через уже доверенный
администраторский доступ к серверу:

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Закройте сеанс `eventradar` командой `exit`, если продолжаете следующие шаги
из администраторского терминала.

## 4. Ключ для сервер → приватный GitHub-репозиторий

На сервере от администратора:

```bash
sudo -u eventradar ssh-keygen -t ed25519 -f /home/eventradar/.ssh/github_eventradar -C eventradar-readonly -N ''
sudo cat /home/eventradar/.ssh/github_eventradar.pub
```

Скопируйте только публичную строку. В GitHub откройте репозиторий → Settings →
Deploy keys → Add deploy key. Название `eventradar-server`, ключ — эта строка,
**Allow write access выключен**. Приватный ключ остается на сервере.

Откройте от администратора:

```bash
sudo -u eventradar nano /home/eventradar/.ssh/config
```

Добавьте блок (если уже есть соответствующие настройки, согласуйте их):

```sshconfig
Host github.com
    HostName github.com
    User git
    IdentityFile /home/eventradar/.ssh/github_eventradar
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Откройте known_hosts и добавьте публичный ключ GitHub:

```bash
sudo -u eventradar nano /home/eventradar/.ssh/known_hosts
```

```text
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
```

Ключ приведен из [официальной документации GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
При смене ключей используйте актуальные данные из этого источника.

```bash
sudo chmod 600 /home/eventradar/.ssh/config /home/eventradar/.ssh/known_hosts
sudo -u eventradar git clone git@github.com:Kiryshka1922/eventradar.git /srv/eventradar
```

Если каталог уже содержит checkout, не клонируйте поверх него и не удаляйте его:
сначала определите состояние имеющейся установки.

## 5. Подготовить серверный .env на компьютере

```bash
cd /Users/kipabelousov/eventradar
cp -n .env data/server.env
chmod 600 data/server.env
nano data/server.env
```

Копия остается внутри игнорируемого `data/`. Сохраните реальные локальные значения:

- `TELEGRAM_BOT_TOKEN`: токен того же бота от BotFather.
- `OWNER_TELEGRAM_ID`: ваш числовой Telegram ID.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`: значения приложения с my.telegram.org.
- `TELEGRAM_READER_SESSION`: уже действующая локальная сессия читателя.
- `CHATGPT_MODEL`: текущее значение или пусто для автоматического выбора.
- Остальные лимиты, часовые пояса и параметры — как в `.env.example`/локальном `.env`.

Обязательно замените локальные абсолютные пути:

```dotenv
DATABASE_PATH=/data/eventradar.sqlite3
CHATGPT_AUTH_DIR=/data/chatgpt
```

Не добавляйте `OPENAI_API_KEY`, OAuth access/refresh tokens и `RELEASE_TAG`.
Пример остальных параметров: `.env.example`. Каналы, промпт, расписание, адрес и
выбранная группа находятся в базе, а не в `.env`.

Передайте конфигурацию напрямую:

```bash
ssh -i ~/.ssh/eventradar_actions -o IdentitiesOnly=yes -p "$SERVER_PORT" "eventradar@$SERVER_HOST" 'umask 077; cat > /srv/eventradar/.env' < data/server.env
```

В GitHub Secret `BOT_ENV` позже положите **содержимое этого серверного файла**.

## 6. Подготовить каталог ChatGPT и собрать образ

На сервере от администратора, после клонирования:

```bash
sudo install -d -m 755 -o eventradar -g eventradar /srv/eventradar/runtime
sudo install -d -m 700 -o 10001 -g 10001 /srv/eventradar/runtime/chatgpt
```

UID/GID 10001 — пользователь приложения внутри образа, не UID SSH-пользователя.
Поэтому `eventradar` не должен читать credentials.json напрямую с хоста.

Далее в новом SSH-сеансе пользователя `eventradar`:

```bash
cd /srv/eventradar
docker compose build --pull bot
```

Эта команда собирает локальный образ `eventradar:local`, но самого бота не запускает.

## 7. Остановить локального бота и перенести данные

Остановите текущий локальный процесс EventRadar. Если он запущен в терминале —
Ctrl+C; если через Codex — попросите остановить именно локальный EventRadar.
Не завершайте все Python-процессы. После этой точки не меняйте настройки локально
и не запускайте второй экземпляр с тем же bot token и ChatGPT-сессией.

На компьютере:

```bash
cd /Users/kipabelousov/eventradar
source .venv312-uv/bin/activate
python -m eventradar.chatgpt_auth --directory data/chatgpt login --no-browser
python -m eventradar.storage backup --output data/eventradar-server-transfer.sqlite3
```

Свежий вход нужен потому, что импорт проверяет срок действия подписанного ID token.
После успешного входа сразу выполните оба переноса:

```bash
ssh -i ~/.ssh/eventradar_actions -o IdentitiesOnly=yes -p "$SERVER_PORT" "eventradar@$SERVER_HOST" 'cd /srv/eventradar && docker compose run --rm --no-deps -T bot python -m eventradar.chatgpt_auth --directory /data/chatgpt import-session' < data/chatgpt/credentials.json
ssh -i ~/.ssh/eventradar_actions -o IdentitiesOnly=yes -p "$SERVER_PORT" "eventradar@$SERVER_HOST" 'cd /srv/eventradar && docker compose run --rm --no-deps -T bot python -m eventradar.storage restore --stdin' < data/eventradar-server-transfer.sqlite3
```

Импорт базы откажется перезаписывать существующую серверную базу; импорт сессии —
существующую авторизацию. Не удаляйте их для обхода отказа: сначала выясните,
какие данные уже есть. Если имя локального снимка занято, выберите новое имя.

С этого момента OAuth-токены обновляет только сервер. Локальную копию сессии
не используйте; **logout на ней не выполняйте**, он может отозвать перенесенную
авторизацию. Для отдельной локальной разработки авторизуйте отдельный каталог.
Сессия Telegram тоже переносится как часть BOT_ENV; одновременно ее локально
не используйте. SQLite-снимок не содержит OAuth-файлы и Telegram-секреты, но
содержит личные настройки/адрес/историю, поэтому храните его закрыто.

## 8. Первый запуск на сервере

От пользователя `eventradar`:

```bash
cd /srv/eventradar
docker compose up -d --no-build --wait --wait-timeout 180 bot
docker compose ps
docker compose logs --tail 100 bot
```

Затем в Telegram проверьте `/channels`, `/prompt`, `/schedule`, `/destination`.
`/publish` отправляет сохраненный отчет, `/digest` запускает новый анализ.
Проверка healthcheck подтверждает heartbeat приложения, но не все внешние API.

## 9. Заполнить GitHub

Репозиторий → Settings → Environments → New environment → `production`.
В нем создайте Environment secrets:

| Имя | Точное содержимое / источник |
| --- | --- |
| `SSH_HOST` | Тот же IP/DNS, что SERVER_HOST, без user@, схемы и порта |
| `SSH_USER` | `eventradar` |
| `SSH_PRIVATE_KEY` | Полный приватный ключ с компьютера `~/.ssh/eventradar_actions`, включая BEGIN/END и переносы строк |
| `SSH_KNOWN_HOSTS` | Строка с публичным host key сервера, получение ниже |
| `BOT_ENV` | Полное содержимое `data/server.env`, не путь к файлу |

На Mac скопируйте по очереди нужное значение в буфер, не публикуя в терминале:

```bash
pbcopy < ~/.ssh/eventradar_actions
pbcopy < /Users/kipabelousov/eventradar/data/server.env
```

Вставляйте каждое значение в соответствующий Secret перед следующей командой.
`SSH_PRIVATE_KEY` — файл **без .pub**. Он не имеет отношения к API-ключу ChatGPT.

Для `SSH_KNOWN_HOSTS` в доверенном администраторском терминале сервера:

```bash
sudo cat /etc/ssh/ssh_host_ed25519_key.pub
```

Из вывода возьмите первые два поля `ssh-ed25519 AAAA...`, без комментария.
Перед ними поставьте SSH_HOST:

```text
SERVER_HOST ssh-ed25519 AAAA...
```

Здесь SERVER_HOST замените реальным IP/DNS. Для порта, отличного от 22:

```text
[SERVER_HOST]:2222 ssh-ed25519 AAAA...
```

Порт тоже замените реальным. Это публичный ключ сервера, **не** ключ пользователя
и **не** ключ GitHub из шага 4.

Репозиторий → Settings → Secrets and variables → Actions → Variables:

| Repository variable | Значение |
| --- | --- |
| `SSH_PORT` | `22` или ваш SSH-порт |
| `DEPLOY_ENABLED` | `true`, только после успешного переноса и первого запуска |

Registry, Y_TOKEN, CONTAINER_REGISTRY, OPENAI_API_KEY и API-организация не нужны.
После включения: Actions → Deploy EventRadar → Run workflow → main.
Затем push в main будет запускать деплой автоматически.

## 10. Последующие обновления и обслуживание

Деплой делает fetch и checkout точного SHA, а не merge через git pull: так
однозначно известно, какой коммит запущен. Серверный checkout остается detached.
Не редактируйте отслеживаемые исходники на сервере — pipeline откажется их затирать.

Команды состояния, логов и остановки от пользователя eventradar:

```bash
cd /srv/eventradar
docker compose ps
docker compose logs --tail 100 bot
docker compose stop bot
```

Для ручного повторного запуска актуального образа после workflow используйте
его SHA, поскольку без RELEASE_TAG Compose выбирает первый образ `local`:

```bash
cd /srv/eventradar
export RELEASE_TAG="$(git rev-parse HEAD)"
docker compose up -d --no-build --pull never --wait --wait-timeout 180 bot
```

Если автоматический деплой еще ни разу не выполнялся, первый образ называется
`local`: используйте команды первого запуска из шага 8.

Перед обновлением работающего бота workflow делает SQLite backup в
`/data/backups` внутри volume; сохраняет последние 10 копий. Это локальные
резервные копии на том же диске. Внешнее резервирование на другой носитель
настраивается отдельно. Старые Docker-образы автоматически не удаляются.
При откате образа база автоматически не откатывается; несовместимая миграция
может потребовать ручного восстановления согласованной резервной копии.

Не выполняйте `docker compose down -v`, `docker volume prune` и глобальную
очистку Docker: они могут удалить данные EventRadar или Termeet.
Обновления BOT_ENV в GitHub вступают в силу при следующем деплое. Если меняете
`.env` только на сервере, синхронизируйте GitHub Secret, иначе следующий деплой
вернет значения из BOT_ENV. Если не хотите хранить конфигурацию в GitHub,
можно вовсе не задавать BOT_ENV и поддерживать только серверный `.env`.
