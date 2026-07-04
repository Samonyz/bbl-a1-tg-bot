# Bambu Telegram Bot

Телеграм-бот для мониторинга 3D-принтера Bambu Lab A1 по локальной сети. Подключается к MQTT-брокеру принтера и потоку камеры, отправляет уведомления о статусе печати (старт, пауза, возобновление, завершение, ошибка) со снимком с камеры в телеграм-чат.

## Требования

- Bambu Lab A1 (или A1 mini / серия P1) с включённым доступом по LAN.
- IP-адрес принтера, серийный номер и LAN access code (доступны на экране сетевых настроек принтера или в приложении Bambu Handy).
- Токен телеграм-бота, полученный через [@BotFather](https://core.telegram.org/bots#botfather).
- Числовой ID чата, куда слать уведомления.
- Docker и Docker Compose.

## Используемые библиотеки и инструменты

- [bambulabs_api](https://github.com/acse-ci223/bambulabs_api) ([PyPI](https://pypi.org/project/bambulabs-api/)) — MQTT-клиент и клиент потока камеры для принтеров Bambu Lab.
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) ([документация](https://docs.python-telegram-bot.org/)) — обёртка над Telegram Bot API, включая job queue для периодического опроса.
- [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) — MQTT-клиент, используется bambulabs_api внутри.
- [Pillow](https://pillow.readthedocs.io/) — обработка изображений с камеры.
- [Docker](https://docs.docker.com/) / [Docker Compose](https://docs.docker.com/compose/) — развёртывание в контейнере.
- [Справочник кодов ошибок Bambu Lab HMS](https://wiki.bambulab.com/en/hms/error-code) — ссылка добавляется в уведомления об ошибках для расшифровки кодов.

## Конфигурация

Скопируйте `.env.example` в `.env` и заполните значения:

| Переменная | Описание |
|---|---|
| `PRINTER_IP` | IP-адрес принтера в локальной сети. |
| `PRINTER_ACCESS_CODE` | LAN access code, указан на экране сетевых настроек принтера. |
| `PRINTER_SERIAL` | Серийный номер принтера. |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather. |
| `TELEGRAM_CHAT_ID` | Числовой ID чата для уведомлений. |
| `POLL_INTERVAL_SECONDS` | Как часто бот опрашивает принтер на предмет изменения статуса, в секундах. По умолчанию: 5. |
| `PROGRESS_UPDATE_SECONDS` | Как часто бот редактирует сообщение прогресса во время активной печати, в секундах. По умолчанию: 60. |
| `LOCALE` | Язык сообщений, отправляемых в Telegram: `ru` или `en`. По умолчанию: `ru`. При некорректном значении откатывается на `ru` с предупреждением в логах. |
| `TZ` | Часовой пояс для таймстампов в сообщениях, например `Europe/Moscow`. По умолчанию: `UTC`. Требует пакет `tzdata`, который уже установлен в образе. |

## Запуск

```
docker compose up -d --build
```

Образ собирается на базе `python:3.12-slim` и устанавливает зависимости из `requirements.txt`. Сборка происходит нативно под архитектуру той машины, на которой запускается (amd64 или arm64), без необходимости кросс-компиляции.

## Поведение

Бот опрашивает MQTT-статус принтера каждые `POLL_INTERVAL_SECONDS`. При смене состояния отправляется новое сообщение:

- Начата новая печать
- Печать поставлена на паузу
- Печать возобновлена
- Печать завершена
- Печать не удалась
- Обнаружен код ошибки принтера (ненулевой `print_error_code`, со ссылкой на справочник HMS)

Каждое такое сообщение включает снимок с камеры, если поток камеры доступен, и таймстамп момента отправки.

Пока печать активна, бот редактирует одно и то же сообщение раз в `PROGRESS_UPDATE_SECONDS`, показывая текущее состояние, имя файла, процент прогресса, номер слоя и оставшееся время, а также метку "последнее обновление" с моментом этого редактирования. После завершения текущей печати это сообщение больше не используется — следующая печать начнёт новое, а финальное состояние предыдущей останется видно в истории чата.

## Команды бота

Команды принимаются только от чата с ID, указанным в `TELEGRAM_CHAT_ID`; сообщения от любого другого чата игнорируются.

| Команда | Описание |
|---|---|
| `/start` | Короткая справка. |
| `/status` | Текущий статус принтера и снимок с камеры. |
| `/photo` | Только снимок с камеры. |
| `/light_on` | Включить свет камеры. |
| `/light_off` | Выключить свет камеры. |

## Локализация

Все сообщения, отправляемые в Telegram (метки статусов, уведомления о событиях, ответы на команды), берутся из `locales.py` в зависимости от переменной окружения `LOCALE`. Поддерживаются значения `ru` и `en`. Чтобы добавить другой язык, добавьте новую запись в словарь `LOCALES` в `locales.py` с тем же набором ключей.

## Примечания

- На принтере должен быть включён LAN-режим / доступ только по LAN — иначе порты MQTT (8883) и камеры (6000) недоступны в локальной сети.
- Поток камеры принимает ограниченное число одновременных подключений. Если Bambu Studio или Bambu Handy в этот момент подключены к камере, клиент бота может не подключиться или получить таймаут.
- Секреты хранятся в `.env`, который исключён из версионирования через `.gitignore`. Не помещайте реальные учётные данные в `.env.example`.

---

# Bambu Telegram Bot

Telegram bot for monitoring a Bambu Lab A1 3D printer over the local network. Connects to the printer's MQTT broker and camera stream, and sends print status notifications (start, pause, resume, finish, error) with a camera snapshot to a Telegram chat.

## Requirements

- Bambu Lab A1 (or A1 mini / P1 series) with LAN access enabled.
- Printer IP address, serial number, and LAN access code (available on the printer's network settings screen, or in the Bambu Handy app).
- A Telegram bot token, created via [@BotFather](https://core.telegram.org/bots#botfather).
- The numeric chat ID to send notifications to.
- Docker and Docker Compose.

## Libraries and tools used

- [bambulabs_api](https://github.com/acse-ci223/bambulabs_api) ([PyPI](https://pypi.org/project/bambulabs-api/)) — MQTT client and camera stream client for Bambu Lab printers.
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) ([docs](https://docs.python-telegram-bot.org/)) — Telegram Bot API wrapper, including the job queue used for periodic polling.
- [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) — MQTT client, used internally by bambulabs_api.
- [Pillow](https://pillow.readthedocs.io/) — image handling for camera frames.
- [Docker](https://docs.docker.com/) / [Docker Compose](https://docs.docker.com/compose/) — containerized deployment.
- [Bambu Lab HMS error code reference](https://wiki.bambulab.com/en/hms/error-code) — linked in error notifications for decoding printer error codes.

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Description |
|---|---|
| `PRINTER_IP` | IP address of the printer on the local network. |
| `PRINTER_ACCESS_CODE` | LAN access code, found on the printer's network settings screen. |
| `PRINTER_SERIAL` | Printer serial number. |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | Numeric ID of the chat to receive notifications. |
| `POLL_INTERVAL_SECONDS` | How often the bot polls the printer for state changes, in seconds. Default: 5. |
| `PROGRESS_UPDATE_SECONDS` | How often the bot edits the progress message during an active print, in seconds. Default: 60. |
| `LOCALE` | Language of the messages sent to Telegram: `ru` or `en`. Default: `ru`. Falls back to `ru` with a warning in the logs if set to anything else. |
| `TZ` | Timezone used for message timestamps, e.g. `Europe/Moscow`. Default: `UTC`. Requires `tzdata`, which is installed in the image. |

## Running

```
docker compose up -d --build
```

The image builds from the `python:3.12-slim` base and installs the dependencies listed in `requirements.txt`. It builds natively for whatever architecture the host is running (amd64 or arm64), no cross-compilation configuration is required.

## Behavior

The bot polls the printer's MQTT status every `POLL_INTERVAL_SECONDS`. On a state transition it sends a new message:

- Print started
- Print paused
- Print resumed
- Print finished
- Print failed
- Printer error code detected (non-zero `print_error_code`, with a link to the HMS error code reference)

Each of these messages includes a camera snapshot when the camera stream is available, and a timestamp of when the message was sent.

While a print is running, the bot edits a single message in place every `PROGRESS_UPDATE_SECONDS`, showing the current state, file name, progress percentage, layer count, and remaining time, plus a "last updated" timestamp that reflects when that edit occurred. This message is replaced with a fresh one after the current print ends, so the previous print's final state remains visible in the chat history.

## Bot commands

Commands are only accepted from the chat ID configured in `TELEGRAM_CHAT_ID`; messages from any other chat are ignored.

| Command | Description |
|---|---|
| `/start` | Show a short help message. |
| `/status` | Current printer status and a camera snapshot. |
| `/photo` | Camera snapshot only. |
| `/light_on` | Turn the chamber light on. |
| `/light_off` | Turn the chamber light off. |

## Localization

All messages sent to Telegram (state labels, event notifications, command replies) are looked up from `locales.py` based on the `LOCALE` environment variable. Supported values are `ru` and `en`. To add another language, add a new entry to the `LOCALES` dictionary in `locales.py` with the same set of keys.

## Notes

- The printer must have LAN mode / LAN-only access enabled; otherwise the MQTT (port 8883) and camera (port 6000) ports are not reachable on the local network.
- The camera stream accepts a limited number of concurrent connections. If Bambu Studio or Bambu Handy is connected to the camera at the same time, the bot's camera client may fail to connect or time out.
- Secrets belong in `.env`, which is excluded from version control by `.gitignore`. Do not put real credentials in `.env.example`.
