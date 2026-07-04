import io
import logging
import os
import time
from datetime import datetime

import bambulabs_api as bl
from telegram import InputMediaPhoto, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from locales import DEFAULT_LOCALE, LOCALES, get_translator

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bambu-bot")

PRINTER_IP = os.environ["PRINTER_IP"]
PRINTER_ACCESS_CODE = os.environ["PRINTER_ACCESS_CODE"]
PRINTER_SERIAL = os.environ["PRINTER_SERIAL"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", 5))
PROGRESS_UPDATE_SECONDS = float(os.environ.get("PROGRESS_UPDATE_SECONDS", 60))

LOCALE = os.environ.get("LOCALE", DEFAULT_LOCALE).lower()
if LOCALE not in LOCALES:
    log.warning("Unknown LOCALE %r, falling back to %r", LOCALE, DEFAULT_LOCALE)
    LOCALE = DEFAULT_LOCALE
t = get_translator(LOCALE)

RUNNING_STATES = (bl.GcodeState.RUNNING,)
PAUSED_STATES = (bl.GcodeState.PAUSE,)

HMS_WIKI_URL = "https://wiki.bambulab.com/en/hms/error-code"


def _percent(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PrinterMonitor:
    def __init__(self, printer: bl.Printer):
        self.printer = printer
        self.prev_state: bl.GcodeState | None = None
        self.prev_error_code: int = 0
        self.last_progress_update: float | None = None
        self.progress_message_id: int | None = None

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
        return "\n".join(lines)

    async def get_photo(self) -> bytes | None:
        if not self.printer.camera_client_alive():
            return None
        try:
            image = self.printer.get_camera_image()
        except Exception:
            log.exception("Failed to grab camera frame")
            return None
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        return buf.getvalue()

    async def poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            state = self.printer.get_state()
            error_code = self.printer.print_error_code()
        except Exception:
            log.exception("Failed to read printer state")
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
                # с нового сообщения прогресса
                self.progress_message_id = None
                self.last_progress_update = None

        if error_code and error_code != self.prev_error_code:
            events.append(t("event.error", code=error_code, url=HMS_WIKI_URL))

        if state in RUNNING_STATES:
            now = time.monotonic()
            progress_due = (
                just_started
                or self.last_progress_update is None
                or (now - self.last_progress_update) >= PROGRESS_UPDATE_SECONDS
            )
            if progress_due:
                self.last_progress_update = now

        self.prev_state = state
        self.prev_error_code = error_code

        for event in events:
            await self.send_event(context, event)

        if progress_due:
            await self.update_progress_message(context)

    async def send_event(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        details = self.snapshot()
        photo = await self.get_photo()
        timestamp = t("footer.time", time=_now_str())
        message = f"{text}\n\n{details}\n\n{timestamp}"
        if photo:
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=photo, caption=message
            )
        else:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

    async def update_progress_message(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        timestamp = t("footer.last_updated", time=_now_str())
        caption = f"{self.snapshot()}\n\n{timestamp}"
        photo = await self.get_photo()

        if self.progress_message_id is not None:
            try:
                if photo:
                    await context.bot.edit_message_media(
                        chat_id=TELEGRAM_CHAT_ID,
                        message_id=self.progress_message_id,
                        media=InputMediaPhoto(photo, caption=caption),
                    )
                else:
                    await context.bot.edit_message_caption(
                        chat_id=TELEGRAM_CHAT_ID,
                        message_id=self.progress_message_id,
                        caption=caption,
                    )
                return
            except TelegramError as e:
                log.warning("Не удалось отредактировать сообщение прогресса: %s", e)
                self.progress_message_id = None

        if photo:
            msg = await context.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=photo, caption=caption
            )
        else:
            msg = await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption)
        self.progress_message_id = msg.message_id


def authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == TELEGRAM_CHAT_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(t("cmd.start"))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    monitor: PrinterMonitor = context.bot_data["monitor"]
    text = f"{monitor.snapshot()}\n\n{t('footer.time', time=_now_str())}"
    photo = await monitor.get_photo()
    if photo:
        await update.message.reply_photo(photo=photo, caption=text)
    else:
        await update.message.reply_text(text)


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    monitor: PrinterMonitor = context.bot_data["monitor"]
    photo = await monitor.get_photo()
    if photo:
        await update.message.reply_photo(photo=photo)
    else:
        await update.message.reply_text(t("cmd.camera_unavailable"))


async def cmd_light_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    printer: bl.Printer = context.bot_data["printer"]
    printer.turn_light_on()
    await update.message.reply_text(t("cmd.light_on"))


async def cmd_light_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    printer: bl.Printer = context.bot_data["printer"]
    printer.turn_light_off()
    await update.message.reply_text(t("cmd.light_off"))


def main() -> None:
    printer = bl.Printer(PRINTER_IP, PRINTER_ACCESS_CODE, PRINTER_SERIAL)
    log.info("Connecting to printer at %s", PRINTER_IP)
    printer.connect()

    monitor = PrinterMonitor(printer)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data["printer"] = printer
    application.bot_data["monitor"] = monitor

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("photo", cmd_photo))
    application.add_handler(CommandHandler("light_on", cmd_light_on))
    application.add_handler(CommandHandler("light_off", cmd_light_off))

    application.job_queue.run_repeating(
        monitor.poll, interval=POLL_INTERVAL_SECONDS, first=10
    )

    log.info("Starting Telegram bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
