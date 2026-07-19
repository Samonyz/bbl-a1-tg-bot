# Printer Telegram Bot

Телеграм-бот для мониторинга нескольких 3D-принтеров по локальной сети — Bambu Lab (по MQTT) и Klipper/Moonraker (по REST API, например Creality K1C с рутом) — одним ботом с одним токеном. Список принтеров задаётся декларативно в `printers.yaml`; новый принтер можно добавить и прямо из чата, командой `/add_printer`, с тестом подключения перед сохранением.

## Требования

- Любое количество принтеров любого из двух типов:
  - **Bambu Lab** (A1, A1 mini, P1-серия и т.п.) с включённым доступом по LAN — нужны IP, серийный номер и LAN access code (экран сетевых настроек принтера или приложение Bambu Handy).
  - **Klipper/Moonraker** — нужен доступный по сети URL Moonraker (обычно `http://<ip>:7125`) и URL снапшота камеры (например, mjpeg-streamer/camera-streamer, `http://<ip>:8080/?action=snapshot`). Если Moonraker закрыт авторизацией — также API-ключ (Settings → Moonraker → API Key в Mainsail/Fluidd).
- Токен телеграм-бота, полученный через [@BotFather](https://core.telegram.org/bots#botfather).
- Числовой ID чата, куда слать уведомления и из которого принимаются команды.
- Docker и Docker Compose.

## Используемые библиотеки и инструменты

- [bambulabs_api](https://github.com/acse-ci223/bambulabs_api) ([PyPI](https://pypi.org/project/bambulabs-api/)) — MQTT-клиент и клиент потока камеры для принтеров Bambu Lab.
- [Moonraker](https://moonraker.readthedocs.io/) — REST API Klipper-принтеров; бот обращается к `/printer/objects/query` напрямую через HTTP, без отдельного клиента.
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) ([документация](https://docs.python-telegram-bot.org/)) — обёртка над Telegram Bot API: job queue для периодического опроса и `ConversationHandler` для диалога добавления принтера.
- [httpx](https://www.python-httpx.org/) — асинхронный HTTP-клиент для запросов к Moonraker и снимков камеры.
- [PyYAML](https://pyyaml.org/) — разбор и запись `printers.yaml`.
- [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) — MQTT-клиент, используется bambulabs_api внутри.
- [Pillow](https://pillow.readthedocs.io/) — обработка изображений с камеры Bambu.
- [Docker](https://docs.docker.com/) / [Docker Compose](https://docs.docker.com/compose/) — развёртывание в контейнере.
- [Справочник кодов ошибок Bambu Lab HMS](https://wiki.bambulab.com/en/hms/error-code) — ссылка добавляется в уведомления об ошибках Bambu-принтеров для расшифровки кодов.

## Конфигурация

### `.env`

Скопируйте `.env.example` в `.env` и заполните значения — сюда идут только параметры, общие для всего бота (не привязанные к конкретному принтеру):

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather. |
| `TELEGRAM_CHAT_ID` | Числовой ID чата: сюда шлются уведомления, отсюда принимаются команды. |
| `PRINTERS_CONFIG` | Путь к манифесту принтеров внутри контейнера. По умолчанию: `printers.yaml` — но реально в `docker-compose.yml` смонтирована директория `./data:/data`, так что путь нужно указывать как `/data/printers.yaml` (см. `.env.example`). |
| `DEFAULT_POLL_INTERVAL_SECONDS` | Дефолтный интервал опроса принтера, секунды, если не переопределён в `printers.yaml` для конкретного принтера. По умолчанию: 5. |
| `DEFAULT_PROGRESS_UPDATE_SECONDS` | Дефолтный интервал обновления сообщения прогресса, секунды, если не переопределён в `printers.yaml`. По умолчанию: 60. |
| `LOCALE` | Язык сообщений, отправляемых в Telegram: `ru` или `en`. По умолчанию: `ru`. При некорректном значении откатывается на `ru` с предупреждением в логах. |
| `TZ` | Часовой пояс для таймстампов в сообщениях, например `Europe/Moscow`. По умолчанию: `UTC`. Требует пакет `tzdata`, который уже установлен в образе. |
| `TELEGRAM_USE_TOPICS` | `true`/`false`. По умолчанию `false` (один общий чат, как раньше). При `true` — свой форум-топик на каждый принтер: туда идут его уведомления и сообщение прогресса, оно же закреплено там на время печати. |

`TELEGRAM_USE_TOPICS=true` требует, чтобы `TELEGRAM_CHAT_ID` был супергруппой с включёнными Topics (Settings → Topics в клиенте). Включение Topics в обычной группе конвертирует её в супергруппу и меняет chat_id — обновите `TELEGRAM_CHAT_ID` после конвертации. Топик и id закреплённого сообщения сохраняются в `printers.yaml` и переживают перезапуск бота — топик не пересоздаётся, сообщение прогресса продолжает редактироваться, а не дублируется.

### `printers.yaml`

Список принтеров задаётся отдельно от `.env` — скопируйте `printers.example.yaml` в `data/printers.yaml` (файл гарантированно не попадёт в git — он в `.gitignore`, как и `.env`; директория `data/` целиком монтируется в контейнер, см. ниже) и опишите свои принтеры:

```yaml
bambu_printers:
  - name: a1
    ip: "192.168.1.50"
    access_code: "12345678"
    serial: "01P00A000000000"
    poll_interval_seconds: 5        # опционально
    progress_update_seconds: 60     # опционально

moonraker_printers:
  - name: k1c
    moonraker_url: "http://192.168.1.92:7125"
    camera_snapshot_url: "http://192.168.1.92:8080/?action=snapshot"
    api_key: "your-moonraker-api-key"   # опционально, если Moonraker закрыт авторизацией
    poll_interval_seconds: 5            # опционально
    progress_update_seconds: 60         # опционально
```

`name` — латиница в нижнем регистре, цифры и `_`, до 20 символов. Используется как тег в сообщениях (`[name] ...`) и как суффикс команд (`/status_<name>` и т.д.). Имена должны быть уникальны среди `bambu_printers` и `moonraker_printers` вместе — бот откажется стартовать с понятной ошибкой, если найдёт дубликат, невалидное имя или отсутствующее обязательное поле.

Директория `data/` монтируется в контейнер как volume целиком (см. `docker-compose.yml`), поэтому файл можно менять без пересборки образа — только `docker compose restart`. Правки, сделанные ботом через `/add_printer` (см. ниже), тоже пишутся в этот файл, но без сохранения комментариев (файл перезаписывается целиком через YAML-дамп). **Важно:** монтируется именно директория, а не сам файл — атомарная перезапись (временный файл + rename) не работает, если в контейнер смонтирован непосредственно файл (`EBUSY`).

## Запуск

```
docker compose up -d --build
```

Образ собирается на базе `python:3.12-slim` и устанавливает зависимости из `requirements.txt`. Сборка происходит нативно под архитектуру той машины, на которой запускается (amd64 или arm64), без необходимости кросс-компиляции.

## Поведение

Для каждого принтера из `printers.yaml` бот заводит отдельную периодическую задачу опроса (интервал — `poll_interval_seconds` принтера, либо `DEFAULT_POLL_INTERVAL_SECONDS`). При смене состояния печати отправляется новое сообщение с тегом принтера:

- Начата новая печать
- Печать поставлена на паузу
- Печать возобновлена
- Печать завершена
- Печать не удалась
- Обнаружен код ошибки принтера (только для Bambu — ненулевой `print_error_code`, со ссылкой на справочник HMS)

Каждое такое сообщение включает снимок с камеры, если он доступен, и таймстамп момента отправки.

Пока печать активна, бот редактирует одно и то же сообщение раз в `progress_update_seconds` этого принтера, показывая текущее состояние, имя файла и процент прогресса (для Bambu — также номер слоя и оставшееся время), плюс метку "последнее обновление". После завершения печати сообщение больше не используется — следующая печать начнёт новое.

Синхронные вызовы к Bambu-принтеру (MQTT) выполняются в отдельном потоке, а не в основном event loop — чтобы подвисание одного принтера не блокировало опрос остальных и приём команд.

## Команды бота

Команды принимаются только от чата с ID, указанным в `TELEGRAM_CHAT_ID`; сообщения от любого другого чата игнорируются.

| Команда | Описание |
|---|---|
| `/start` | Справка со списком актуальных команд для загруженных принтеров. |
| `/status` | Краткий статус всех принтеров сразу. Внутри топика принтера (см. `TELEGRAM_USE_TOPICS`) — статус только этого принтера. |
| `/status_<name>`, `/photo_<name>` | Статус (со снимком камеры) или только снимок конкретного принтера. |
| `/photo` | Снимок конкретного принтера — только внутри его топика. |
| `/light_on_<name>`, `/light_off_<name>` | Включить/выключить свет камеры — только для принтеров Bambu. |
| `/list_printers` | Список принтеров, находящихся под мониторингом, с указанием типа. |
| `/add_printer` | Диалог добавления нового принтера (см. ниже). |
| `/cancel` | Прервать диалог `/add_printer` на любом шаге. |

### Добавление принтера через чат

`/add_printer` запускает диалог: выбор типа принтера (Bambu Lab / Moonraker) кнопками, затем последовательный запрос имени и параметров (для Bambu — IP, access code, серийный номер; для Moonraker — URL, URL снапшота камеры, опционально API-ключ — чтобы пропустить опциональное поле, отправьте `-`). После ввода бот проверяет подключение (для Bambu — реальное MQTT-соединение и попытка прочитать статус; для Moonraker — запрос к REST API; недоступность камеры не блокирует добавление, только предупреждение). При успехе принтер сразу начинает мониториться — без перезапуска контейнера — и сохраняется в `printers.yaml` для персистентности между перезапусками. Диалог автоматически завершается по таймауту (5 минут неактивности) или командой `/cancel`.

## Локализация

Все сообщения, отправляемые в Telegram (метки статусов, уведомления о событиях, тексты диалога добавления принтера, ответы на команды), берутся из `locales.py` в зависимости от переменной окружения `LOCALE`. Поддерживаются значения `ru` и `en`. Чтобы добавить другой язык, добавьте новую запись в словарь `LOCALES` в `locales.py` с тем же набором ключей.

## Поддержка нового оборудования

Логика конкретного железа изолирована в `connectors/` — ядро бота (`bot.py`) знает только про интерфейс `PrinterConnector` (`connectors/base.py`) и про типы `PrinterState`/`SpoolInfo` (`models.py`), а не про MQTT/HTTP/протоколы конкретных принтеров.

Чтобы добавить новый тип принтера или мультиматериальной системы:

1. Создайте `connectors/my_printer.py` с классом-наследником `PrinterConnector`, реализующим `from_config`, `test_connection`, `draft_summary`, `poll`, `get_photo` (и опционально `set_light`/`close`). Задайте `TYPE_KEY`, `DISPLAY_NAME`, `SECTION_KEY`, `FIELD_SPECS`, `REQUIRED_FIELDS`.
2. Присвойте класс `CONNECTOR_CLASS` на уровне модуля — бот подхватит его автоматически при старте (авто-обнаружение в `connectors/__init__.py`), никакой другой файл редактировать не нужно. Новая секция в `printers.yaml` (по вашему `SECTION_KEY`) и кнопка в `/add_printer` появятся сами.
3. Известные типы данных (процент, слой, высота, катушка) — типизированные поля `PrinterState`. Всё остальное специфичное для вашего железа (температура камеры, влажность и т.п.) кладите в `PrinterState.extra` списком `ExtraField(key, values)` — ядро отрендерит их через `t(f"{TYPE_KEY}.{key}", **values)`.
4. Для своих строк локализации задайте `LOCALES = {"ru": {...}, "en": {...}}` на уровне класса — того же вида, что верхнеуровневый `LOCALES` в `locales.py`. Ключи автоматически неймспейсятся под ваш `TYPE_KEY` при загрузке (`chamber_temp` → `my_printer.chamber_temp`), так что коллизии с другими коннекторами или ядром исключены.

`connectors/bambu.py` и `connectors/moonraker.py` — рабочие примеры (MQTT- и HTTP-коннекторы соответственно).

## Примечания

- На Bambu-принтере должен быть включён LAN-режим / доступ только по LAN — иначе порты MQTT (8883) и камеры (6000) недоступны в локальной сети.
- Поток камеры Bambu принимает ограниченное число одновременных подключений. Если Bambu Studio или Bambu Handy в этот момент подключены к камере, клиент бота может не подключиться или получить таймаут.
- Moonraker должен быть доступен по HTTP из сети, где работает бот; если он закрыт авторизацией — укажите `api_key` в `printers.yaml` (заголовок `X-Api-Key`). URL снапшота камеры обычно даёт mjpeg-streamer/camera-streamer/crowsnest через `?action=snapshot` — один JPEG-кадр за запрос, без разбора mjpeg-потока.
- Секреты (токен бота, access code, api_key) хранятся в `.env` и `printers.yaml`, оба исключены из версионирования через `.gitignore`. Не помещайте реальные учётные данные в `.env.example`/`printers.example.yaml`.
- Если один и тот же `TELEGRAM_BOT_TOKEN` использовать в двух независимых запущенных процессах одновременно (например, старый бот для одного из принтеров и этот бот — для другого), Telegram разрешает только одного потребителя `getUpdates` на токен: второй процесс будет получать `409 Conflict`, и боты будут попеременно отваливаться. Решение — вести все принтеры одним процессом через `printers.yaml`, а не несколькими ботами с одним токеном.
- Отображение мультицветной системы для Moonraker (`box` object) разрабатывалось и тестировалось только на рутованном Creality K1C с CFS. На других принтерах/прошивках или других мультиматериальных системах (AMS lite, ERCF, Box Turtle и т.п.) работоспособность не гарантируется — схема объекта может отличаться. PR и issues от владельцев такого железа welcome.

---

# Printer Telegram Bot

A Telegram bot for monitoring multiple 3D printers on the local network — Bambu Lab (over MQTT) and Klipper/Moonraker (over REST API, e.g. a rooted Creality K1C) — as a single bot under a single token. The printer list is declared in `printers.yaml`; a new printer can also be added right from the chat via `/add_printer`, with a connection test before it's saved.

## Requirements

- Any number of printers of either type:
  - **Bambu Lab** (A1, A1 mini, P1 series, etc.) with LAN access enabled — you need the IP, serial number, and LAN access code (printer's network settings screen, or the Bambu Handy app).
  - **Klipper/Moonraker** — you need a network-reachable Moonraker URL (usually `http://<ip>:7125`) and a camera snapshot URL (e.g. mjpeg-streamer/camera-streamer, `http://<ip>:8080/?action=snapshot`). If Moonraker requires authentication, also an API key (Settings → Moonraker → API Key in Mainsail/Fluidd).
- A Telegram bot token, created via [@BotFather](https://core.telegram.org/bots#botfather).
- The numeric chat ID to send notifications to and accept commands from.
- Docker and Docker Compose.

## Libraries and tools used

- [bambulabs_api](https://github.com/acse-ci223/bambulabs_api) ([PyPI](https://pypi.org/project/bambulabs-api/)) — MQTT client and camera stream client for Bambu Lab printers.
- [Moonraker](https://moonraker.readthedocs.io/) — Klipper's REST API; the bot talks to `/printer/objects/query` directly over HTTP, no dedicated client library.
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) ([docs](https://docs.python-telegram-bot.org/)) — Telegram Bot API wrapper: job queue for periodic polling, and `ConversationHandler` for the add-printer dialog.
- [httpx](https://www.python-httpx.org/) — async HTTP client for Moonraker requests and camera snapshots.
- [PyYAML](https://pyyaml.org/) — parsing and writing `printers.yaml`.
- [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) — MQTT client, used internally by bambulabs_api.
- [Pillow](https://pillow.readthedocs.io/) — image handling for Bambu camera frames.
- [Docker](https://docs.docker.com/) / [Docker Compose](https://docs.docker.com/compose/) — containerized deployment.
- [Bambu Lab HMS error code reference](https://wiki.bambulab.com/en/hms/error-code) — linked in error notifications for Bambu printers, to decode error codes.

## Configuration

### `.env`

Copy `.env.example` to `.env` and fill in the values — only bot-wide settings go here, nothing printer-specific:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | Numeric chat ID: notifications go here, commands are accepted from here. |
| `PRINTERS_CONFIG` | Path to the printer manifest inside the container. Default: `printers.yaml` — but `docker-compose.yml` actually mounts the `./data:/data` directory, so the path should be `/data/printers.yaml` (see `.env.example`). |
| `DEFAULT_POLL_INTERVAL_SECONDS` | Default polling interval in seconds, used when a printer entry in `printers.yaml` doesn't override it. Default: 5. |
| `DEFAULT_PROGRESS_UPDATE_SECONDS` | Default progress-message update interval in seconds, used when a printer entry doesn't override it. Default: 60. |
| `LOCALE` | Language of the messages sent to Telegram: `ru` or `en`. Default: `ru`. Falls back to `ru` with a warning in the logs if set to anything else. |
| `TZ` | Timezone used for message timestamps, e.g. `Europe/Moscow`. Default: `UTC`. Requires `tzdata`, which is installed in the image. |
| `TELEGRAM_USE_TOPICS` | `true`/`false`. Default `false` (one shared chat, as before). When `true` — a dedicated forum topic per printer: its notifications and progress message go there, and the progress message stays pinned there while printing. |

`TELEGRAM_USE_TOPICS=true` requires `TELEGRAM_CHAT_ID` to be a supergroup with Topics enabled (Settings → Topics in the client). Enabling Topics on a regular group converts it to a supergroup and changes its chat_id — update `TELEGRAM_CHAT_ID` after converting. The topic id and the pinned message's id are saved to `printers.yaml` and survive bot restarts — the topic isn't recreated, and the progress message keeps getting edited rather than duplicated.

### `printers.yaml`

The printer list lives separately from `.env` — copy `printers.example.yaml` to `data/printers.yaml` (guaranteed not to be committed — it's in `.gitignore`, same as `.env`; the whole `data/` directory is mounted into the container, see below) and describe your printers:

```yaml
bambu_printers:
  - name: a1
    ip: "192.168.1.50"
    access_code: "12345678"
    serial: "01P00A000000000"
    poll_interval_seconds: 5        # optional
    progress_update_seconds: 60     # optional

moonraker_printers:
  - name: k1c
    moonraker_url: "http://192.168.1.92:7125"
    camera_snapshot_url: "http://192.168.1.92:8080/?action=snapshot"
    api_key: "your-moonraker-api-key"   # optional, if Moonraker requires auth
    poll_interval_seconds: 5            # optional
    progress_update_seconds: 60         # optional
```

`name` must be lowercase Latin letters, digits, and `_`, up to 20 characters. It's used as a tag in messages (`[name] ...`) and as a command suffix (`/status_<name>`, etc). Names must be unique across `bambu_printers` and `moonraker_printers` combined — the bot refuses to start with a clear error if it finds a duplicate, an invalid name, or a missing required field.

The `data/` directory is mounted into the container as a volume in full (see `docker-compose.yml`), so the file can be edited without rebuilding the image — just `docker compose restart`. Edits made by the bot itself via `/add_printer` (see below) are also written to this file, but comments are not preserved (the file is rewritten wholesale via a YAML dump). **Important:** the directory is mounted, not the file itself — an atomic rewrite (temp file + rename) doesn't work if the container has the file itself bind-mounted directly (`EBUSY`).

## Running

```
docker compose up -d --build
```

The image builds from the `python:3.12-slim` base and installs the dependencies listed in `requirements.txt`. It builds natively for whatever architecture the host is running (amd64 or arm64), no cross-compilation configuration is required.

## Behavior

Each printer in `printers.yaml` gets its own periodic polling job (interval — that printer's `poll_interval_seconds`, or `DEFAULT_POLL_INTERVAL_SECONDS`). On a print status transition, a new message is sent tagged with that printer's name:

- Print started
- Print paused
- Print resumed
- Print finished
- Print failed
- Printer error code detected (Bambu only — non-zero `print_error_code`, with a link to the HMS error code reference)

Each of these messages includes a camera snapshot when available, and a timestamp of when the message was sent.

While a print is running, the bot edits a single message in place every `progress_update_seconds` for that printer, showing the current state, file name, and progress percentage (for Bambu, also layer count and remaining time), plus a "last updated" timestamp. This message is replaced with a fresh one after the print ends.

Synchronous calls to a Bambu printer (MQTT) run in a separate thread rather than on the main event loop, so a stall on one printer doesn't block polling of the others or command handling.

## Bot commands

Commands are only accepted from the chat ID configured in `TELEGRAM_CHAT_ID`; messages from any other chat are ignored.

| Command | Description |
|---|---|
| `/start` | Help message listing the commands available for the currently loaded printers. |
| `/status` | Short status of all printers at once. Inside a printer's topic (see `TELEGRAM_USE_TOPICS`) — status of that printer only. |
| `/status_<name>`, `/photo_<name>` | Status (with camera snapshot) or just the snapshot for a specific printer. |
| `/photo` | Snapshot of a specific printer — only inside its topic. |
| `/light_on_<name>`, `/light_off_<name>` | Turn the chamber light on/off — Bambu printers only. |
| `/list_printers` | List printers currently under monitoring, with their type. |
| `/add_printer` | Dialog for adding a new printer (see below). |
| `/cancel` | Abort the `/add_printer` dialog at any step. |

### Adding a printer from chat

`/add_printer` starts a dialog: pick the printer type (Bambu Lab / Moonraker) via buttons, then enter the name and parameters one by one (for Bambu — IP, access code, serial; for Moonraker — URL, camera snapshot URL, and an optional API key — send `-` to skip an optional field). Once entered, the bot tests the connection (for Bambu — a real MQTT connection and an attempt to read status; for Moonraker — a REST API request; a failed camera check doesn't block adding the printer, only warns). On success, the printer is immediately monitored — no container restart needed — and saved to `printers.yaml` so it survives restarts. The dialog ends automatically after a 5-minute timeout, or via `/cancel`.

## Localization

All messages sent to Telegram (state labels, event notifications, add-printer dialog text, command replies) are looked up from `locales.py` based on the `LOCALE` environment variable. Supported values are `ru` and `en`. To add another language, add a new entry to the `LOCALES` dictionary in `locales.py` with the same set of keys.

## Adding hardware support

Hardware-specific logic is isolated in `connectors/` — the core (`bot.py`) only knows about the `PrinterConnector` interface (`connectors/base.py`) and the `PrinterState`/`SpoolInfo` types (`models.py`), not about any particular printer's MQTT/HTTP protocol.

To add a new printer type or multi-material system:

1. Create `connectors/my_printer.py` with a `PrinterConnector` subclass implementing `from_config`, `test_connection`, `draft_summary`, `poll`, `get_photo` (and optionally `set_light`/`close`). Set `TYPE_KEY`, `DISPLAY_NAME`, `SECTION_KEY`, `FIELD_SPECS`, `REQUIRED_FIELDS`.
2. Assign the class to a module-level `CONNECTOR_CLASS` — the bot picks it up automatically at startup (auto-discovery in `connectors/__init__.py`), no other file needs editing. The `printers.yaml` section (your `SECTION_KEY`) and the `/add_printer` button appear on their own.
3. Well-known fields (percent, layer, height, spool) are typed `PrinterState` fields. Anything specific to your hardware (chamber temp, humidity, etc.) goes into `PrinterState.extra` as a list of `ExtraField(key, values)` — the core renders each via `t(f"{TYPE_KEY}.{key}", **values)`.
4. For your own locale strings, set a class-level `LOCALES = {"ru": {...}, "en": {...}}`, shaped like the top-level `LOCALES` in `locales.py`. Keys are auto-namespaced under your `TYPE_KEY` on load (`chamber_temp` → `my_printer.chamber_temp`), so there's no risk of colliding with the core or other connectors.

`connectors/bambu.py` and `connectors/moonraker.py` are working examples (an MQTT-based and an HTTP-based connector, respectively).

## Notes

- The Bambu printer must have LAN mode / LAN-only access enabled; otherwise the MQTT (port 8883) and camera (port 6000) ports are not reachable on the local network.
- The Bambu camera stream accepts a limited number of concurrent connections. If Bambu Studio or Bambu Handy is connected to the camera at the same time, the bot's camera client may fail to connect or time out.
- Moonraker must be reachable over HTTP from wherever the bot runs; if it requires authentication, set `api_key` in `printers.yaml` (sent as the `X-Api-Key` header). The camera snapshot URL is usually served by mjpeg-streamer/camera-streamer/crowsnest via `?action=snapshot` — a single JPEG frame per request, no mjpeg stream parsing needed.
- Secrets (bot token, access code, api_key) live in `.env` and `printers.yaml`, both excluded from version control via `.gitignore`. Do not put real credentials in `.env.example`/`printers.example.yaml`.
- If the same `TELEGRAM_BOT_TOKEN` is used by two independent running processes at once (e.g. an old bot for one printer, and this bot for another), Telegram only allows one `getUpdates` consumer per token: the second process gets `409 Conflict`, and the bots keep knocking each other offline. The fix is to run all printers through one process via `printers.yaml`, not multiple bots sharing one token.
- The Moonraker multi-material display (the `box` object) was developed and tested only against a rooted Creality K1C with CFS. On other printers/firmware or other multi-material systems (AMS lite, ERCF, Box Turtle, etc.) it's not guaranteed to work — the object schema may differ. PRs and issues from owners of such hardware are welcome.
