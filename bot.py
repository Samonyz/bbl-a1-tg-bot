import asyncio
import io
import logging
import os
import time
from datetime import datetime

import bambulabs_api as bl
import httpx
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from locales import DEFAULT_LOCALE, LOCALES, get_translator
from printers_config import NAME_RE, PrintersConfigError, append_printer, load_printers, validate_entry

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bambu-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
PRINTERS_CONFIG = os.environ.get("PRINTERS_CONFIG", "printers.yaml")
DEFAULT_POLL_INTERVAL_SECONDS = float(os.environ.get("DEFAULT_POLL_INTERVAL_SECONDS", 5))
DEFAULT_PROGRESS_UPDATE_SECONDS = float(
    os.environ.get("DEFAULT_PROGRESS_UPDATE_SECONDS", 60)
)

LOCALE = os.environ.get("LOCALE", DEFAULT_LOCALE).lower()
if LOCALE not in LOCALES:
    log.warning("Unknown LOCALE %r, falling back to %r", LOCALE, DEFAULT_LOCALE)
    LOCALE = DEFAULT_LOCALE
t = get_translator(LOCALE)

RUNNING_STATES = (bl.GcodeState.RUNNING,)
PAUSED_STATES = (bl.GcodeState.PAUSE,)

HMS_WIKI_URL = "https://wiki.bambulab.com/en/hms/error-code"

MOONRAKER_STATE_MAP = {
    "printing": bl.GcodeState.RUNNING,
    "paused": bl.GcodeState.PAUSE,
    "standby": bl.GcodeState.IDLE,
    "complete": bl.GcodeState.FINISH,
    "cancelled": bl.GcodeState.FAILED,
    "error": bl.GcodeState.FAILED,
}


def _percent(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Палитра для приближённого отображения цвета катушки эмодзи-кругом.
_COLOR_EMOJI = [
    ((255, 255, 255), "⚪"),
    ((0, 0, 0), "⚫"),
    ((128, 128, 128), "🔘"),
    ((237, 28, 36), "🔴"),
    ((255, 140, 0), "🟠"),
    ((255, 221, 0), "🟡"),
    ((0, 153, 68), "🟢"),
    ((0, 120, 215), "🔵"),
    ((150, 60, 180), "🟣"),
    ((140, 90, 60), "🟤"),
]


def _color_emoji(hex_color: str) -> str:
    hex_color = hex_color[-6:]
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return min(
        _COLOR_EMOJI,
        key=lambda entry: sum((a - c) ** 2 for a, c in zip((r, g, b), entry[0])),
    )[1]


def _active_spool(box: dict) -> tuple[str, str, str] | None:
    """Возвращает (тег слота вида 'T1B', материал, цвет-эмодзи) активной
    катушки CFS, либо None, если CFS не подключена или слот не выбран."""
    same_material = box.get("same_material") or []
    for box_key in ("T1", "T2", "T3", "T4"):
        slot = box.get(box_key)
        if not isinstance(slot, dict) or slot.get("state") != "connect":
            continue
        letter = slot.get("filament")
        if not letter or letter == "None":
            continue
        tag = f"{box_key}{letter}"
        for _material_code, color_hex, tags, material_name in same_material:
            if tag in tags:
                return tag, material_name, _color_emoji(color_hex)
    return None


def _active_ams_spool(ams_info: dict) -> tuple[str, str, str] | None:
    """Возвращает (тег слота вида 'T1B', материал, цвет-эмодзи) активной
    катушки AMS, либо None, если печать идёт с внешней катушки, AMS не
    подключена или для выбранного слота нет данных.

    Основано на публично известной схеме поля "ams" в MQTT-статусе Bambu
    (bambulabs_api парсит сами трэи, но не поле tray_now) - надо будет
    перепроверить на реальном железе, когда приедет AMS.
    """
    try:
        idx = int(ams_info.get("tray_now"))
    except (TypeError, ValueError):
        return None
    if idx >= 254:  # 254 = внешняя катушка, 255 = не выбрана
        return None
    unit, slot = divmod(idx, 4)
    letter = "ABCD"[slot]
    for ams_unit in ams_info.get("ams", []):
        if int(ams_unit.get("id", -1)) != unit:
            continue
        for tray in ams_unit.get("tray", []):
            if int(tray.get("id", -1)) != slot:
                continue
            color_hex = (tray.get("tray_color") or "")[:6]
            if not color_hex:
                return None
            material = tray.get("tray_type") or "?"
            return f"T{unit + 1}{letter}", material, _color_emoji(color_hex)
    return None


def _vt_tray_spool(vt_tray: dict) -> tuple[str, str] | None:
    """Возвращает (материал, цвет-эмодзи) внешней катушки Bambu (vt_tray -
    единственный держатель принтера без AMS), либо None, если данных нет."""
    if not vt_tray:
        return None
    color_hex = (vt_tray.get("tray_color") or "")[:6]
    material = vt_tray.get("tray_type") or ""
    if not color_hex or not material:
        return None
    return material, _color_emoji(color_hex)


def _spool_line(spool: tuple[str, str, str] | None) -> str:
    """Строка с катушкой для сообщения о печати: позиция слота A-D показана
    смещением цветного эмодзи внутри '[----]', плюс тег слота и материал."""
    if spool is None:
        return t("snapshot.spool_external")
    tag, material, emoji = spool
    idx = "ABCD".index(tag[-1])
    bar = "[" + "-" * idx + emoji + "-" * (3 - idx) + "]"
    return t("snapshot.spool", bar=bar, tag=tag, material=material)


_placeholder_photo_cache: bytes | None = None


def _placeholder_photo() -> bytes:
    """Нейтральная заглушка, когда реальный кадр с камеры недоступен.

    Используется, чтобы сообщение прогресса ВСЕГДА было фото-сообщением -
    иначе редактирование текстового сообщения (без caption) как caption
    гарантированно валится с "There is no caption in the message to edit".
    """
    global _placeholder_photo_cache
    if _placeholder_photo_cache is None:
        img = Image.new("RGB", (640, 480), color=(45, 45, 51))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        _placeholder_photo_cache = buf.getvalue()
    return _placeholder_photo_cache


class TelegramNotifierMixin:
    """Общая отправка/редактирование сообщений для мониторов принтеров.

    Подклассы обязаны иметь атрибуты tag, chat_id, progress_message_id,
    _last_photo и методы snapshot() / get_photo().
    """

    def _tagged(self, text: str) -> str:
        return f"[{self.tag}] {text}"

    async def _cached_photo(self, fresh: bytes | None) -> bytes:
        """Кэширует последний реально полученный кадр с камеры на время
        текущей печати. Если новый кадр получить не удалось - отдаём
        последний известный, а не сразу заглушку (заглушка только пока
        вообще ни одного кадра ещё не было, например в начале печати).
        """
        if fresh is not None:
            self._last_photo = fresh
            return fresh
        if self._last_photo is not None:
            return self._last_photo
        return _placeholder_photo()

    async def send_event(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        details = await self.get_snapshot()
        photo = await self.get_photo()
        timestamp = t("footer.time", time=_now_str())
        message = self._tagged(f"{text}\n\n{details}\n\n{timestamp}")
        await context.bot.send_photo(chat_id=self.chat_id, photo=photo, caption=message)

    async def update_progress_message(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        timestamp = t("footer.last_updated", time=_now_str())
        caption = self._tagged(f"{await self.get_snapshot()}\n\n{timestamp}")
        photo = await self.get_photo()

        if self.progress_message_id is not None:
            try:
                await context.bot.edit_message_media(
                    chat_id=self.chat_id,
                    message_id=self.progress_message_id,
                    media=InputMediaPhoto(photo, caption=caption),
                )
                return
            except (NetworkError, RetryAfter) as e:
                # Таймаут/сетевой сбой/флуд-контроль не значит, что сообщение
                # потеряно - редактирование могло и пройти на сервере. Пробуем
                # отредактировать то же самое сообщение на следующем цикле,
                # а не считаем его "битым" и не шлём новое.
                log.warning(
                    "Временная ошибка при редактировании сообщения прогресса [%s], "
                    "повторим на следующем цикле: %s",
                    self.tag,
                    e,
                )
                return
            except TelegramError as e:
                log.warning(
                    "Не удалось отредактировать сообщение прогресса [%s]: %s",
                    self.tag,
                    e,
                )
                self.progress_message_id = None

        msg = await context.bot.send_photo(
            chat_id=self.chat_id, photo=photo, caption=caption
        )
        self.progress_message_id = msg.message_id


class PrinterMonitor(TelegramNotifierMixin):
    def __init__(
        self,
        printer: bl.Printer,
        chat_id: int,
        tag: str,
        progress_update_seconds: float,
    ):
        self.printer = printer
        self.chat_id = chat_id
        self.tag = tag
        self.progress_update_seconds = progress_update_seconds
        self.prev_state: bl.GcodeState | None = None
        self.prev_error_code: int = 0
        self.last_progress_update: float | None = None
        self.progress_message_id: int | None = None
        self._last_photo: bytes | None = None

    def snapshot(self) -> str:
        state = self.printer.get_state()
        percent = _percent(self.printer.get_percentage())
        remaining = self.printer.get_time()
        layer_cur = self.printer.current_layer_num()
        layer_total = self.printer.total_layer_num()
        name = self.printer.subtask_name() or self.printer.gcode_file() or "?"

        status_label = t(f"state.{state.name}")
        lines = [t("snapshot.status", status=status_label)]
        if state in RUNNING_STATES or state in PAUSED_STATES:
            lines.append(t("snapshot.file", name=name))
            if percent is not None:
                lines.append(t("snapshot.progress", percent=percent))
            if layer_cur and layer_total:
                lines.append(t("snapshot.layer", current=layer_cur, total=layer_total))
            if remaining not in (None, "N/A"):
                lines.append(t("snapshot.remaining", remaining=remaining))
            print_data = self.printer.mqtt_dump().get("print", {})
            ams_info = print_data.get("ams") or {}
            if ams_info and ams_info.get("ams_exist_bits", "0") != "0":
                lines.append(_spool_line(_active_ams_spool(ams_info)))
            else:
                ext = _vt_tray_spool(print_data.get("vt_tray") or {})
                if ext is not None:
                    material, emoji = ext
                    lines.append(
                        t("snapshot.spool_external_material", emoji=emoji, material=material)
                    )
                else:
                    lines.append(t("snapshot.spool_external"))
        return "\n".join(lines)

    async def get_snapshot(self) -> str:
        # bambulabs_api-вызовы синхронные и могут блокироваться (mqtt publish
        # внутри), поэтому уводим их в отдельный поток, чтобы не подвесить
        # общий asyncio event loop (а с ним - опрос других принтеров и приём
        # команд Telegram).
        return await asyncio.to_thread(self.snapshot)

    async def get_photo(self) -> bytes:
        if not self.printer.camera_client_alive():
            return await self._cached_photo(None)
        try:
            image = await asyncio.to_thread(self.printer.get_camera_image)
        except Exception:
            log.exception("Failed to grab camera frame [%s]", self.tag)
            return await self._cached_photo(None)
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        return await self._cached_photo(buf.getvalue())

    async def poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            state = await asyncio.to_thread(self.printer.get_state)
            error_code = await asyncio.to_thread(self.printer.print_error_code)
        except Exception:
            log.exception("Failed to read printer state [%s]", self.tag)
            return

        events = []
        just_started = False
        progress_due = False

        if self.prev_state is not None and state != self.prev_state:
            if state in RUNNING_STATES and self.prev_state not in PAUSED_STATES:
                events.append(t("event.started"))
                just_started = True
            elif state in RUNNING_STATES and self.prev_state in PAUSED_STATES:
                events.append(t("event.resumed"))
            elif state in PAUSED_STATES:
                events.append(t("event.paused"))
            elif state == bl.GcodeState.FINISH:
                events.append(t("event.finished"))
            elif state == bl.GcodeState.FAILED:
                events.append(t("event.failed"))

            if state not in RUNNING_STATES and state not in PAUSED_STATES:
                # печать закончилась (или сброшена в IDLE) - следующая начнётся
                # с нового сообщения прогресса и своего кадра камеры
                self.progress_message_id = None
                self.last_progress_update = None
                self._last_photo = None

        if error_code and error_code != self.prev_error_code:
            events.append(t("event.error", code=error_code, url=HMS_WIKI_URL))

        if state in RUNNING_STATES:
            now = time.monotonic()
            progress_due = (
                just_started
                or self.last_progress_update is None
                or (now - self.last_progress_update) >= self.progress_update_seconds
            )
            if progress_due:
                self.last_progress_update = now

        self.prev_state = state
        self.prev_error_code = error_code

        for event in events:
            await self.send_event(context, event)

        if progress_due:
            await self.update_progress_message(context)


class MoonrakerPrinterMonitor(TelegramNotifierMixin):
    def __init__(
        self,
        base_url: str,
        camera_snapshot_url: str,
        chat_id: int,
        progress_update_seconds: float,
        tag: str,
        api_key: str | None = None,
    ):
        headers = {"X-Api-Key": api_key} if api_key else {}
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10, headers=headers)
        self.camera_snapshot_url = camera_snapshot_url
        self.chat_id = chat_id
        self.progress_update_seconds = progress_update_seconds
        self.tag = tag
        self.prev_state: bl.GcodeState | None = None
        self.last_progress_update: float | None = None
        self.progress_message_id: int | None = None
        self._last_photo: bytes | None = None
        self._last_status: dict | None = None

    async def _fetch_status(self) -> dict:
        resp = await self.client.get(
            "/printer/objects/query",
            params={"print_stats": "", "virtual_sdcard": "", "box": ""},
        )
        resp.raise_for_status()
        return resp.json()["result"]["status"]

    def snapshot(self) -> str:
        if self._last_status is None:
            return t(f"state.{bl.GcodeState.UNKNOWN.name}")

        print_stats = self._last_status["print_stats"]
        # virtual_sdcard.progress - доля прочитанных байт файла, точнее
        # display_status.progress (тот часто основан на оценке слайсера по
        # времени, которая на CFS-печатях с паузами на смену прутка уплывает).
        virtual_sdcard = self._last_status["virtual_sdcard"]
        state = MOONRAKER_STATE_MAP.get(print_stats["state"], bl.GcodeState.UNKNOWN)
        percent = _percent(round((virtual_sdcard.get("progress") or 0) * 100))
        name = print_stats.get("filename") or "?"

        status_label = t(f"state.{state.name}")
        lines = [t("snapshot.status", status=status_label)]
        if state in RUNNING_STATES or state in PAUSED_STATES:
            lines.append(t("snapshot.file", name=name))
            if percent is not None:
                lines.append(t("snapshot.progress", percent=percent))
            box = self._last_status.get("box")
            if box:
                lines.append(_spool_line(_active_spool(box)))
        return "\n".join(lines)

    async def get_snapshot(self) -> str:
        # snapshot() здесь читает уже закэшированный self._last_status,
        # без сети - можно звать напрямую без отдельного потока.
        return self.snapshot()

    async def get_photo(self) -> bytes:
        try:
            resp = await self.client.get(self.camera_snapshot_url)
            resp.raise_for_status()
            return await self._cached_photo(resp.content)
        except Exception:
            log.exception("Failed to grab camera frame [%s]", self.tag)
            return await self._cached_photo(None)

    async def poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            status = await self._fetch_status()
        except Exception:
            log.exception("Failed to read printer state [%s]", self.tag)
            return

        self._last_status = status
        print_stats = status["print_stats"]
        state = MOONRAKER_STATE_MAP.get(print_stats["state"], bl.GcodeState.UNKNOWN)

        events = []
        just_started = False

        if self.prev_state is not None and state != self.prev_state:
            if state in RUNNING_STATES and self.prev_state not in PAUSED_STATES:
                events.append(t("event.started"))
                just_started = True
            elif state in RUNNING_STATES and self.prev_state in PAUSED_STATES:
                events.append(t("event.resumed"))
            elif state in PAUSED_STATES:
                events.append(t("event.paused"))
            elif state == bl.GcodeState.FINISH:
                events.append(t("event.finished"))
            elif state == bl.GcodeState.FAILED:
                events.append(t("event.failed"))

            if state not in RUNNING_STATES and state not in PAUSED_STATES:
                self.progress_message_id = None
                self.last_progress_update = None
                self._last_photo = None

        progress_due = False
        if state in RUNNING_STATES:
            now = time.monotonic()
            progress_due = (
                just_started
                or self.last_progress_update is None
                or (now - self.last_progress_update) >= self.progress_update_seconds
            )
            if progress_due:
                self.last_progress_update = now

        self.prev_state = state

        for event in events:
            await self.send_event(context, event)

        if progress_due:
            await self.update_progress_message(context)


def authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == TELEGRAM_CHAT_ID


async def _reply_status(update: Update, monitor) -> None:
    text = f"{await monitor.get_snapshot()}\n\n{t('footer.time', time=_now_str())}"
    photo = await monitor.get_photo()
    await update.message.reply_photo(photo=photo, caption=text)


async def _reply_photo(update: Update, monitor) -> None:
    photo = await monitor.get_photo()
    if photo == _placeholder_photo():
        await update.message.reply_text(t("cmd.camera_unavailable"))
    else:
        await update.message.reply_photo(photo=photo)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    monitors = list(context.bot_data["monitors"].values())
    snapshots = await asyncio.gather(*(m.get_snapshot() for m in monitors))
    text = "\n\n".join(m._tagged(s) for m, s in zip(monitors, snapshots))
    await update.message.reply_text(text)


def make_status_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _reply_status(update, context.bot_data["monitors"][name])

    return handler


def make_photo_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _reply_photo(update, context.bot_data["monitors"][name])

    return handler


def make_light_on_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        monitor: PrinterMonitor = context.bot_data["monitors"][name]
        await asyncio.to_thread(monitor.printer.turn_light_on)
        await update.message.reply_text(t("cmd.light_on"))

    return handler


def make_light_off_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        monitor: PrinterMonitor = context.bot_data["monitors"][name]
        await asyncio.to_thread(monitor.printer.turn_light_off)
        await update.message.reply_text(t("cmd.light_off"))

    return handler


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(build_help_text(context.bot_data["monitors"]))


async def cmd_list_printers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    monitors = context.bot_data["monitors"]
    if not monitors:
        await update.message.reply_text(t("wizard.no_printers"))
        return
    lines = []
    for name, monitor in monitors.items():
        kind = "Bambu" if isinstance(monitor, PrinterMonitor) else "Moonraker"
        lines.append(f"{name} ({kind})")
    await update.message.reply_text("\n".join(lines))


def build_help_text(monitors: dict) -> str:
    lines = [t("cmd.start_intro"), "/status " + t("cmd.help_status_all")]
    for name, monitor in monitors.items():
        lines.append(f"/status_{name}, /photo_{name} — {name}")
        if isinstance(monitor, PrinterMonitor):
            lines.append(f"/light_on_{name}, /light_off_{name} — {name}")
    lines.append("/add_printer " + t("cmd.help_add_printer"))
    lines.append("/list_printers " + t("cmd.help_list_printers"))
    return "\n".join(lines)


# --- /add_printer wizard ---

ADD_CHOOSING_TYPE, ADD_ENTERING_NAME, ADD_ENTERING_FIELD, ADD_CONFIRMING = range(4)

FIELD_SPECS = {
    "bambu": [
        ("ip", "field.bambu_ip"),
        ("access_code", "field.bambu_access_code"),
        ("serial", "field.bambu_serial"),
    ],
    "moonraker": [
        ("moonraker_url", "field.moonraker_url"),
        ("camera_snapshot_url", "field.moonraker_camera"),
        ("api_key", "field.moonraker_api_key"),
    ],
}
OPTIONAL_FIELDS = {"api_key"}


def _draft_summary(printer_type: str, draft: dict) -> str:
    if printer_type == "bambu":
        return f"name={draft['name']}\nip={draft['ip']}\nserial={draft['serial']}"
    lines = [f"name={draft['name']}", f"moonraker_url={draft['moonraker_url']}",
              f"camera_snapshot_url={draft['camera_snapshot_url']}"]
    if draft.get("api_key"):
        lines.append("api_key=***")
    return "\n".join(lines)


async def cmd_add_printer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        return ConversationHandler.END
    context.user_data.clear()
    keyboard = [
        [
            InlineKeyboardButton("Bambu Lab", callback_data="bambu"),
            InlineKeyboardButton("Moonraker", callback_data="moonraker"),
        ],
        [InlineKeyboardButton(t("wizard.cancel_button"), callback_data="cancel")],
    ]
    await update.message.reply_text(t("wizard.choose_type"), reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_CHOOSING_TYPE


async def cb_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text(t("wizard.cancelled"))
        return ConversationHandler.END

    context.user_data["printer_type"] = query.data
    context.user_data["draft"] = {}
    context.user_data["remaining_fields"] = []
    await query.edit_message_text(t("wizard.enter_name"))
    return ADD_ENTERING_NAME


async def cmd_entering_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip().lower()
    if not NAME_RE.match(name):
        await update.message.reply_text(t("wizard.bad_name"))
        return ADD_ENTERING_NAME
    if name in context.bot_data["monitors"]:
        await update.message.reply_text(t("wizard.name_taken", name=name))
        return ADD_ENTERING_NAME

    context.user_data["draft"]["name"] = name
    context.user_data["remaining_fields"] = list(FIELD_SPECS[context.user_data["printer_type"]])
    return await _prompt_next_field(context, update.effective_chat.id)


async def cmd_entering_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field_key, _ = context.user_data["remaining_fields"][0]
    value = update.message.text.strip()
    if field_key in OPTIONAL_FIELDS and value == "-":
        value = None
    elif not value:
        await update.message.reply_text(t("wizard.value_required"))
        return ADD_ENTERING_FIELD

    context.user_data["draft"][field_key] = value
    context.user_data["remaining_fields"] = context.user_data["remaining_fields"][1:]
    return await _prompt_next_field(context, update.effective_chat.id)


async def _prompt_next_field(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    fields = context.user_data["remaining_fields"]
    if not fields:
        return await _run_connection_test(context, chat_id)
    field_key, prompt_key = fields[0]
    suffix = f" {t('wizard.optional_hint')}" if field_key in OPTIONAL_FIELDS else ""
    await context.bot.send_message(chat_id=chat_id, text=t(prompt_key) + suffix)
    return ADD_ENTERING_FIELD


async def _run_connection_test(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    printer_type = context.user_data["printer_type"]
    draft = context.user_data["draft"]
    status_msg = await context.bot.send_message(chat_id=chat_id, text=t("wizard.testing"))

    ok = False
    error_text = None
    camera_note = ""

    if printer_type == "bambu":
        printer = bl.Printer(draft["ip"], draft["access_code"], draft["serial"])
        try:
            await asyncio.to_thread(printer.connect)
            for _ in range(16):
                await asyncio.sleep(0.5)
                try:
                    await asyncio.to_thread(printer.get_state)
                    ok = True
                    break
                except Exception:
                    continue
            if not ok:
                error_text = t("wizard.bambu_timeout")
        except Exception as e:
            error_text = str(e)
        if ok:
            context.user_data["draft_printer"] = printer
        else:
            await asyncio.to_thread(printer.disconnect)
    else:
        headers = {"X-Api-Key": draft["api_key"]} if draft.get("api_key") else {}
        client = httpx.AsyncClient(base_url=draft["moonraker_url"], timeout=8, headers=headers)
        try:
            resp = await client.get(
                "/printer/objects/query", params={"print_stats": "", "display_status": ""}
            )
            resp.raise_for_status()
            resp.json()["result"]["status"]
            ok = True
        except Exception as e:
            error_text = str(e)
        if ok:
            try:
                cam_resp = await client.get(draft["camera_snapshot_url"])
                cam_resp.raise_for_status()
            except Exception:
                camera_note = "\n" + t("wizard.camera_warning")
        await client.aclose()

    if ok:
        text = t("wizard.test_ok", summary=_draft_summary(printer_type, draft)) + camera_note
        keyboard = [
            [InlineKeyboardButton(t("wizard.confirm_button"), callback_data="confirm")],
            [
                InlineKeyboardButton(t("wizard.retry_button"), callback_data="retry"),
                InlineKeyboardButton(t("wizard.cancel_button"), callback_data="cancel"),
            ],
        ]
    else:
        text = t("wizard.test_failed", error=error_text)
        keyboard = [
            [
                InlineKeyboardButton(t("wizard.retry_button"), callback_data="retry"),
                InlineKeyboardButton(t("wizard.cancel_button"), callback_data="cancel"),
            ],
        ]

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_CONFIRMING


def _register_printer_handlers(application: Application, name: str, printer_type: str) -> None:
    application.add_handler(CommandHandler(f"status_{name}", make_status_handler(name)))
    application.add_handler(CommandHandler(f"photo_{name}", make_photo_handler(name)))
    if printer_type == "bambu":
        application.add_handler(CommandHandler(f"light_on_{name}", make_light_on_handler(name)))
        application.add_handler(CommandHandler(f"light_off_{name}", make_light_off_handler(name)))


async def cb_confirming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "cancel":
        draft_printer = context.user_data.pop("draft_printer", None)
        if draft_printer is not None:
            await asyncio.to_thread(draft_printer.disconnect)
        context.user_data.clear()
        await query.edit_message_text(t("wizard.cancelled"))
        return ConversationHandler.END

    if action == "retry":
        draft_printer = context.user_data.pop("draft_printer", None)
        if draft_printer is not None:
            await asyncio.to_thread(draft_printer.disconnect)
        name = context.user_data["draft"]["name"]
        context.user_data["draft"] = {"name": name}
        context.user_data["remaining_fields"] = list(FIELD_SPECS[context.user_data["printer_type"]])
        await query.edit_message_text(t("wizard.retry_prompt"))
        return await _prompt_next_field(context, query.message.chat_id)

    # action == "confirm"
    printer_type = context.user_data["printer_type"]
    draft = context.user_data["draft"]
    name = draft["name"]

    try:
        validate_entry(draft, printer_type, set(context.bot_data["monitors"]))
    except PrintersConfigError as e:
        draft_printer = context.user_data.pop("draft_printer", None)
        if draft_printer is not None:
            await asyncio.to_thread(draft_printer.disconnect)
        context.user_data.clear()
        await query.edit_message_text(str(e))
        return ConversationHandler.END

    if printer_type == "bambu":
        monitor = PrinterMonitor(
            context.user_data["draft_printer"],
            chat_id=TELEGRAM_CHAT_ID,
            tag=name,
            progress_update_seconds=DEFAULT_PROGRESS_UPDATE_SECONDS,
        )
    else:
        monitor = MoonrakerPrinterMonitor(
            base_url=draft["moonraker_url"],
            camera_snapshot_url=draft["camera_snapshot_url"],
            chat_id=TELEGRAM_CHAT_ID,
            progress_update_seconds=DEFAULT_PROGRESS_UPDATE_SECONDS,
            tag=name,
            api_key=draft.get("api_key"),
        )

    context.bot_data["monitors"][name] = monitor
    application = context.application
    _register_printer_handlers(application, name, printer_type)
    application.job_queue.run_repeating(
        monitor.poll, interval=DEFAULT_POLL_INTERVAL_SECONDS, first=5, name=f"poll_{name}"
    )

    entry = {k: v for k, v in draft.items() if v is not None}
    try:
        append_printer(PRINTERS_CONFIG, printer_type, entry)
        saved_note = ""
    except Exception:
        log.exception("Failed to persist new printer %r to %s", name, PRINTERS_CONFIG)
        saved_note = "\n" + t("wizard.saved_live_only")

    context.user_data.clear()
    await query.edit_message_text(t("wizard.added", name=name) + saved_note)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft_printer = context.user_data.pop("draft_printer", None)
    if draft_printer is not None:
        await asyncio.to_thread(draft_printer.disconnect)
    context.user_data.clear()
    await update.message.reply_text(t("wizard.cancelled"))
    return ConversationHandler.END


def build_add_printer_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("add_printer", cmd_add_printer_start)],
        states={
            ADD_CHOOSING_TYPE: [CallbackQueryHandler(cb_choose_type)],
            ADD_ENTERING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_entering_name)],
            ADD_ENTERING_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_entering_field)],
            ADD_CONFIRMING: [CallbackQueryHandler(cb_confirming)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300,
        per_message=False,
    )


def main() -> None:
    try:
        bambu_cfgs, moonraker_cfgs = load_printers(PRINTERS_CONFIG)
    except PrintersConfigError as e:
        log.error("Ошибка конфигурации принтеров: %s", e)
        raise SystemExit(1)

    monitors: dict = {}

    for cfg in bambu_cfgs:
        name = cfg["name"]
        printer = bl.Printer(cfg["ip"], cfg["access_code"], cfg["serial"])
        log.info("Connecting to Bambu printer %r at %s", name, cfg["ip"])
        try:
            printer.connect()
        except Exception:
            log.exception("Failed to connect to Bambu printer %r, continuing anyway", name)
        monitors[name] = PrinterMonitor(
            printer,
            chat_id=TELEGRAM_CHAT_ID,
            tag=name,
            progress_update_seconds=cfg.get(
                "progress_update_seconds", DEFAULT_PROGRESS_UPDATE_SECONDS
            ),
        )

    for cfg in moonraker_cfgs:
        name = cfg["name"]
        monitors[name] = MoonrakerPrinterMonitor(
            base_url=cfg["moonraker_url"],
            camera_snapshot_url=cfg["camera_snapshot_url"],
            chat_id=TELEGRAM_CHAT_ID,
            progress_update_seconds=cfg.get(
                "progress_update_seconds", DEFAULT_PROGRESS_UPDATE_SECONDS
            ),
            tag=name,
            api_key=cfg.get("api_key"),
        )

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data["monitors"] = monitors

    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("list_printers", cmd_list_printers))
    application.add_handler(build_add_printer_conversation())

    poll_intervals = {cfg["name"]: cfg for cfg in bambu_cfgs + moonraker_cfgs}

    for i, (name, monitor) in enumerate(monitors.items()):
        printer_type = "bambu" if isinstance(monitor, PrinterMonitor) else "moonraker"
        _register_printer_handlers(application, name, printer_type)

        interval = poll_intervals[name].get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        application.job_queue.run_repeating(
            monitor.poll, interval=interval, first=10 + i * 5, name=f"poll_{name}"
        )

    log.info("Starting Telegram bot with printers: %s", list(monitors))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
