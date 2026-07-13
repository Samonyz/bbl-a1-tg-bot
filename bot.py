import asyncio
import io
import logging
import os
import time
from datetime import datetime

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

from connectors import CONNECTOR_TYPES
from formatting import format_snapshot
from locales import t
from models import PAUSED_STATES, RUNNING_STATES, PrinterState, PrintState
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


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


class PrinterSession:
    """Обвязка Telegram (отправка/редактирование сообщений, кэш фото,
    обнаружение переходов состояния) вокруг одного PrinterConnector.
    Ничего здесь не знает про конкретное железо - только про PrinterState."""

    def __init__(self, connector, chat_id: int, progress_update_seconds: float):
        self.connector = connector
        self.tag = connector.tag
        self.chat_id = chat_id
        self.progress_update_seconds = progress_update_seconds
        self.prev_state: PrintState | None = None
        self.prev_error_code: int = 0
        self.last_progress_update: float | None = None
        self.progress_message_id: int | None = None
        self._last_photo: bytes | None = None

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

    async def get_photo(self) -> bytes:
        try:
            fresh = await self.connector.get_photo()
        except Exception:
            log.exception("Failed to grab camera frame [%s]", self.tag)
            fresh = None
        return await self._cached_photo(fresh)

    async def get_snapshot_text(self) -> str:
        state = await self.connector.poll()
        return format_snapshot(state, self.connector.TYPE_KEY)

    async def send_event(self, context: ContextTypes.DEFAULT_TYPE, text: str, state: PrinterState) -> None:
        details = format_snapshot(state, self.connector.TYPE_KEY)
        photo = await self.get_photo()
        timestamp = t("footer.time", time=_now_str())
        message = self._tagged(f"{text}\n\n{details}\n\n{timestamp}")
        await context.bot.send_photo(chat_id=self.chat_id, photo=photo, caption=message)

    async def update_progress_message(self, context: ContextTypes.DEFAULT_TYPE, state: PrinterState) -> None:
        timestamp = t("footer.last_updated", time=_now_str())
        caption = self._tagged(f"{format_snapshot(state, self.connector.TYPE_KEY)}\n\n{timestamp}")
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

    async def poll_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            state = await self.connector.poll()
        except Exception:
            log.exception("Failed to read printer state [%s]", self.tag)
            return

        events = []
        just_started = False
        progress_due = False

        if self.prev_state is not None and state.state != self.prev_state:
            if state.state in RUNNING_STATES and self.prev_state not in PAUSED_STATES:
                events.append(t("event.started"))
                just_started = True
            elif state.state in RUNNING_STATES and self.prev_state in PAUSED_STATES:
                events.append(t("event.resumed"))
            elif state.state in PAUSED_STATES:
                events.append(t("event.paused"))
            elif state.state == PrintState.FINISH:
                events.append(t("event.finished"))
            elif state.state == PrintState.FAILED:
                events.append(t("event.failed"))

            if state.state not in RUNNING_STATES and state.state not in PAUSED_STATES:
                # печать закончилась (или сброшена в IDLE) - следующая начнётся
                # с нового сообщения прогресса и своего кадра камеры
                self.progress_message_id = None
                self.last_progress_update = None
                self._last_photo = None

        if state.error_code and state.error_code != self.prev_error_code:
            if self.connector.ERROR_REFERENCE_URL:
                events.append(
                    t("event.error", code=state.error_code, url=self.connector.ERROR_REFERENCE_URL)
                )
            else:
                events.append(t("event.error_no_url", code=state.error_code))

        if state.state in RUNNING_STATES:
            now = time.monotonic()
            progress_due = (
                just_started
                or self.last_progress_update is None
                or (now - self.last_progress_update) >= self.progress_update_seconds
            )
            if progress_due:
                self.last_progress_update = now

        self.prev_state = state.state
        self.prev_error_code = state.error_code

        for event in events:
            await self.send_event(context, event, state)

        if progress_due:
            await self.update_progress_message(context, state)


def authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == TELEGRAM_CHAT_ID


async def _reply_status(update: Update, session: PrinterSession) -> None:
    text = f"{await session.get_snapshot_text()}\n\n{t('footer.time', time=_now_str())}"
    photo = await session.get_photo()
    await update.message.reply_photo(photo=photo, caption=text)


async def _reply_photo(update: Update, session: PrinterSession) -> None:
    photo = await session.get_photo()
    if photo == _placeholder_photo():
        await update.message.reply_text(t("cmd.camera_unavailable"))
    else:
        await update.message.reply_photo(photo=photo)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    sessions = list(context.bot_data["sessions"].values())
    texts = await asyncio.gather(*(s.get_snapshot_text() for s in sessions))
    text = "\n\n".join(s._tagged(txt) for s, txt in zip(sessions, texts))
    await update.message.reply_text(text)


def make_status_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _reply_status(update, context.bot_data["sessions"][name])

    return handler


def make_photo_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        await _reply_photo(update, context.bot_data["sessions"][name])

    return handler


def make_light_on_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        session: PrinterSession = context.bot_data["sessions"][name]
        await session.connector.set_light(True)
        await update.message.reply_text(t("cmd.light_on"))

    return handler


def make_light_off_handler(name: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            return
        session: PrinterSession = context.bot_data["sessions"][name]
        await session.connector.set_light(False)
        await update.message.reply_text(t("cmd.light_off"))

    return handler


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(build_help_text(context.bot_data["sessions"]))


async def cmd_list_printers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    sessions = context.bot_data["sessions"]
    if not sessions:
        await update.message.reply_text(t("wizard.no_printers"))
        return
    lines = [f"{name} ({session.connector.DISPLAY_NAME})" for name, session in sessions.items()]
    await update.message.reply_text("\n".join(lines))


def build_help_text(sessions: dict) -> str:
    lines = [t("cmd.start_intro"), "/status " + t("cmd.help_status_all")]
    for name, session in sessions.items():
        lines.append(f"/status_{name}, /photo_{name} — {name}")
        if session.connector.HAS_LIGHT_CONTROL:
            lines.append(f"/light_on_{name}, /light_off_{name} — {name}")
    lines.append("/add_printer " + t("cmd.help_add_printer"))
    lines.append("/list_printers " + t("cmd.help_list_printers"))
    return "\n".join(lines)


def _register_printer_handlers(application: Application, name: str, connector_cls) -> None:
    application.add_handler(CommandHandler(f"status_{name}", make_status_handler(name)))
    application.add_handler(CommandHandler(f"photo_{name}", make_photo_handler(name)))
    if connector_cls.HAS_LIGHT_CONTROL:
        application.add_handler(CommandHandler(f"light_on_{name}", make_light_on_handler(name)))
        application.add_handler(CommandHandler(f"light_off_{name}", make_light_off_handler(name)))


# --- /add_printer wizard ---

ADD_CHOOSING_TYPE, ADD_ENTERING_NAME, ADD_ENTERING_FIELD, ADD_CONFIRMING = range(4)


async def cmd_add_printer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not authorized(update):
        return ConversationHandler.END
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton(cls.DISPLAY_NAME, callback_data=key)]
        for key, cls in CONNECTOR_TYPES.items()
    ]
    keyboard.append([InlineKeyboardButton(t("wizard.cancel_button"), callback_data="cancel")])
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
    if name in context.bot_data["sessions"]:
        await update.message.reply_text(t("wizard.name_taken", name=name))
        return ADD_ENTERING_NAME

    context.user_data["draft"]["name"] = name
    connector_cls = CONNECTOR_TYPES[context.user_data["printer_type"]]
    context.user_data["remaining_fields"] = list(connector_cls.FIELD_SPECS)
    return await _prompt_next_field(context, update.effective_chat.id)


async def cmd_entering_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    connector_cls = CONNECTOR_TYPES[context.user_data["printer_type"]]
    field_key, _ = context.user_data["remaining_fields"][0]
    value = update.message.text.strip()
    if field_key in connector_cls.OPTIONAL_FIELDS and value == "-":
        value = None
    elif not value:
        await update.message.reply_text(t("wizard.value_required"))
        return ADD_ENTERING_FIELD

    context.user_data["draft"][field_key] = value
    context.user_data["remaining_fields"] = context.user_data["remaining_fields"][1:]
    return await _prompt_next_field(context, update.effective_chat.id)


async def _prompt_next_field(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    connector_cls = CONNECTOR_TYPES[context.user_data["printer_type"]]
    fields = context.user_data["remaining_fields"]
    if not fields:
        return await _run_connection_test(context, chat_id)
    field_key, prompt_key = fields[0]
    suffix = f" {t('wizard.optional_hint')}" if field_key in connector_cls.OPTIONAL_FIELDS else ""
    await context.bot.send_message(chat_id=chat_id, text=t(prompt_key) + suffix)
    return ADD_ENTERING_FIELD


async def _run_connection_test(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    connector_cls = CONNECTOR_TYPES[context.user_data["printer_type"]]
    draft = context.user_data["draft"]
    status_msg = await context.bot.send_message(chat_id=chat_id, text=t("wizard.testing"))

    result = await connector_cls.test_connection(draft)

    if result.ok:
        context.user_data["tested_connector"] = result.connector
        camera_note = "\n" + t("wizard.camera_warning") if result.camera_warning else ""
        text = t("wizard.test_ok", summary=connector_cls.draft_summary(draft)) + camera_note
        keyboard = [
            [InlineKeyboardButton(t("wizard.confirm_button"), callback_data="confirm")],
            [
                InlineKeyboardButton(t("wizard.retry_button"), callback_data="retry"),
                InlineKeyboardButton(t("wizard.cancel_button"), callback_data="cancel"),
            ],
        ]
    else:
        text = t("wizard.test_failed", error=result.error)
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


async def cb_confirming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "cancel":
        connector = context.user_data.pop("tested_connector", None)
        if connector is not None:
            await connector.close()
        context.user_data.clear()
        await query.edit_message_text(t("wizard.cancelled"))
        return ConversationHandler.END

    if action == "retry":
        connector = context.user_data.pop("tested_connector", None)
        if connector is not None:
            await connector.close()
        name = context.user_data["draft"]["name"]
        context.user_data["draft"] = {"name": name}
        connector_cls = CONNECTOR_TYPES[context.user_data["printer_type"]]
        context.user_data["remaining_fields"] = list(connector_cls.FIELD_SPECS)
        await query.edit_message_text(t("wizard.retry_prompt"))
        return await _prompt_next_field(context, query.message.chat_id)

    # action == "confirm"
    connector_cls = CONNECTOR_TYPES[context.user_data["printer_type"]]
    draft = context.user_data["draft"]
    name = draft["name"]

    try:
        validate_entry(draft, connector_cls, set(context.bot_data["sessions"]))
    except PrintersConfigError as e:
        connector = context.user_data.pop("tested_connector", None)
        if connector is not None:
            await connector.close()
        context.user_data.clear()
        await query.edit_message_text(str(e))
        return ConversationHandler.END

    connector = context.user_data["tested_connector"]
    session = PrinterSession(
        connector, chat_id=TELEGRAM_CHAT_ID, progress_update_seconds=DEFAULT_PROGRESS_UPDATE_SECONDS
    )

    context.bot_data["sessions"][name] = session
    application = context.application
    _register_printer_handlers(application, name, connector_cls)
    application.job_queue.run_repeating(
        session.poll_job, interval=DEFAULT_POLL_INTERVAL_SECONDS, first=5, name=f"poll_{name}"
    )

    entry = {k: v for k, v in draft.items() if v is not None}
    try:
        append_printer(PRINTERS_CONFIG, connector_cls, entry)
        saved_note = ""
    except Exception:
        log.exception("Failed to persist new printer %r to %s", name, PRINTERS_CONFIG)
        saved_note = "\n" + t("wizard.saved_live_only")

    context.user_data.clear()
    await query.edit_message_text(t("wizard.added", name=name) + saved_note)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    connector = context.user_data.pop("tested_connector", None)
    if connector is not None:
        await connector.close()
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
        printers_by_type = load_printers(PRINTERS_CONFIG, CONNECTOR_TYPES)
    except PrintersConfigError as e:
        log.error("Ошибка конфигурации принтеров: %s", e)
        raise SystemExit(1)

    sessions: dict[str, PrinterSession] = {}
    cfg_by_name: dict[str, dict] = {}
    type_by_name: dict[str, type] = {}

    for type_key, entries in printers_by_type.items():
        connector_cls = CONNECTOR_TYPES[type_key]
        for cfg in entries:
            name = cfg["name"]
            connector = connector_cls.from_config(cfg, tag=name)
            sessions[name] = PrinterSession(
                connector,
                chat_id=TELEGRAM_CHAT_ID,
                progress_update_seconds=cfg.get(
                    "progress_update_seconds", DEFAULT_PROGRESS_UPDATE_SECONDS
                ),
            )
            cfg_by_name[name] = cfg
            type_by_name[name] = connector_cls

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data["sessions"] = sessions

    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("list_printers", cmd_list_printers))
    application.add_handler(build_add_printer_conversation())

    for i, (name, session) in enumerate(sessions.items()):
        connector_cls = type_by_name[name]
        _register_printer_handlers(application, name, connector_cls)

        interval = cfg_by_name[name].get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        application.job_queue.run_repeating(
            session.poll_job, interval=interval, first=10 + i * 5, name=f"poll_{name}"
        )

    log.info("Starting Telegram bot with printers: %s", list(sessions))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
