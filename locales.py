LOCALES = {
    "ru": {
        "state.IDLE": "простаивает",
        "state.PREPARE": "готовится к печати",
        "state.RUNNING": "печатает",
        "state.PAUSE": "на паузе",
        "state.FINISH": "завершил печать",
        "state.FAILED": "печать не удалась",
        "state.UNKNOWN": "статус неизвестен",
        "snapshot.status": "Статус: {status}",
        "snapshot.file": "Файл: {name}",
        "snapshot.progress": "Прогресс: {percent}%",
        "snapshot.layer": "Слой: {current}/{total}",
        "snapshot.remaining": "Осталось: ~{remaining} мин",
        "event.started": "Начата новая печать",
        "event.resumed": "Печать возобновлена",
        "event.paused": "Печать поставлена на паузу",
        "event.finished": "Печать успешно завершена",
        "event.failed": "Печать завершилась с ошибкой",
        "event.error": "Обнаружена ошибка принтера, код: {code}\nРасшифровка: {url}",
        "cmd.start": (
            "Bambu A1 бот запущен.\n"
            "/status — статус и фото\n"
            "/photo — только фото\n"
            "/light_on, /light_off — свет камеры"
        ),
        "cmd.camera_unavailable": "Камера недоступна",
        "cmd.light_on": "Свет включен",
        "cmd.light_off": "Свет выключен",
        "footer.time": "Время: {time}",
        "footer.last_updated": "Последнее обновление: {time}",
    },
    "en": {
        "state.IDLE": "idle",
        "state.PREPARE": "preparing to print",
        "state.RUNNING": "printing",
        "state.PAUSE": "paused",
        "state.FINISH": "finished printing",
        "state.FAILED": "print failed",
        "state.UNKNOWN": "unknown status",
        "snapshot.status": "Status: {status}",
        "snapshot.file": "File: {name}",
        "snapshot.progress": "Progress: {percent}%",
        "snapshot.layer": "Layer: {current}/{total}",
        "snapshot.remaining": "Remaining: ~{remaining} min",
        "event.started": "New print started",
        "event.resumed": "Print resumed",
        "event.paused": "Print paused",
        "event.finished": "Print finished successfully",
        "event.failed": "Print failed",
        "event.error": "Printer error detected, code: {code}\nReference: {url}",
        "cmd.start": (
            "Bambu A1 bot is running.\n"
            "/status - status and photo\n"
            "/photo - photo only\n"
            "/light_on, /light_off - chamber light"
        ),
        "cmd.camera_unavailable": "Camera unavailable",
        "cmd.light_on": "Light turned on",
        "cmd.light_off": "Light turned off",
        "footer.time": "Time: {time}",
        "footer.last_updated": "Last updated: {time}",
    },
}

DEFAULT_LOCALE = "ru"


def get_translator(locale: str):
    strings = LOCALES.get(locale, LOCALES[DEFAULT_LOCALE])

    def t(key: str, **kwargs) -> str:
        template = strings.get(key, key)
        return template.format(**kwargs) if kwargs else template

    return t
