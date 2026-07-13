import asyncio
import io
import logging

import bambulabs_api as bl

from locales import t
from models import PrinterState, PrintState, SpoolInfo, SpoolStatus

from .base import ConnectionTestResult, PrinterConnector

log = logging.getLogger("bambu-bot.connectors.bambu")

HMS_WIKI_URL = "https://wiki.bambulab.com/en/hms/error-code"

# GcodeState и PrintState совпадают по именам значений 1:1 - явная таблица
# на будущее, если это перестанет быть так (или появится расхождение в
# новой версии bambulabs_api).
_STATE_MAP = {state: PrintState[state.name] for state in bl.GcodeState}


def _percent(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _active_ams_spool(ams_info: dict) -> SpoolInfo | None:
    """Возвращает активную катушку AMS, либо None если печать идёт с
    внешней катушки, AMS не подключена или для выбранного слота нет данных.

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
            return SpoolInfo(
                SpoolStatus.ACTIVE,
                tag=f"T{unit + 1}{letter}",
                material=material,
                color_hex=color_hex,
            )
    return None


def _vt_tray_spool(vt_tray: dict) -> SpoolInfo | None:
    """Возвращает материал/цвет внешней катушки Bambu (vt_tray - единственный
    держатель принтера без AMS), либо None если данных нет."""
    if not vt_tray:
        return None
    color_hex = (vt_tray.get("tray_color") or "")[:6]
    material = vt_tray.get("tray_type") or ""
    if not color_hex or not material:
        return None
    return SpoolInfo(SpoolStatus.EXTERNAL, material=material, color_hex=color_hex)


class BambuConnector(PrinterConnector):
    TYPE_KEY = "bambu"
    DISPLAY_NAME = "Bambu Lab"
    SECTION_KEY = "bambu_printers"
    FIELD_SPECS = [
        ("ip", "field.bambu_ip"),
        ("access_code", "field.bambu_access_code"),
        ("serial", "field.bambu_serial"),
    ]
    REQUIRED_FIELDS = ("name", "ip", "access_code", "serial")
    HAS_LIGHT_CONTROL = True
    ERROR_REFERENCE_URL = HMS_WIKI_URL

    def __init__(self, printer: bl.Printer, tag: str):
        self.printer = printer
        self.tag = tag

    @classmethod
    def from_config(cls, cfg: dict, *, tag: str) -> "BambuConnector":
        printer = bl.Printer(cfg["ip"], cfg["access_code"], cfg["serial"])
        log.info("Connecting to Bambu printer %r at %s", tag, cfg["ip"])
        try:
            printer.connect()
        except Exception:
            log.exception("Failed to connect to Bambu printer %r, continuing anyway", tag)
        return cls(printer, tag)

    @classmethod
    async def test_connection(cls, draft: dict) -> ConnectionTestResult:
        printer = bl.Printer(draft["ip"], draft["access_code"], draft["serial"])
        ok = False
        error_text = None
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
            return ConnectionTestResult(ok=True, connector=cls(printer, draft["name"]))
        await asyncio.to_thread(printer.disconnect)
        return ConnectionTestResult(ok=False, error=error_text)

    @staticmethod
    def draft_summary(draft: dict) -> str:
        return f"name={draft['name']}\nip={draft['ip']}\nserial={draft['serial']}"

    async def poll(self) -> PrinterState:
        state_raw = await asyncio.to_thread(self.printer.get_state)
        error_code = await asyncio.to_thread(self.printer.print_error_code)
        state = _STATE_MAP.get(state_raw, PrintState.UNKNOWN)

        percent = _percent(self.printer.get_percentage())
        remaining_raw = self.printer.get_time()
        # bambulabs_api.get_time() отдаёт сырое поле mc_remaining_time MQTT,
        # которое несмотря на название/докстринг - в минутах, не в секундах.
        remaining_seconds = remaining_raw * 60 if isinstance(remaining_raw, int) else None
        layer_current = self.printer.current_layer_num()
        layer_total = self.printer.total_layer_num()
        filename = self.printer.subtask_name() or self.printer.gcode_file() or None

        spool = None
        if state in (PrintState.RUNNING, PrintState.PAUSE):
            print_data = self.printer.mqtt_dump().get("print", {})
            ams_info = print_data.get("ams") or {}
            if ams_info and ams_info.get("ams_exist_bits", "0") != "0":
                spool = _active_ams_spool(ams_info) or SpoolInfo(SpoolStatus.EXTERNAL)
            else:
                spool = _vt_tray_spool(print_data.get("vt_tray") or {}) or SpoolInfo(
                    SpoolStatus.EXTERNAL
                )

        return PrinterState(
            state=state,
            filename=filename,
            percent=percent,
            layer_current=layer_current,
            layer_total=layer_total,
            remaining_seconds=remaining_seconds,
            error_code=error_code or 0,
            spool=spool,
        )

    async def get_photo(self) -> bytes | None:
        if not self.printer.camera_client_alive():
            return None
        try:
            image = await asyncio.to_thread(self.printer.get_camera_image)
        except Exception:
            log.exception("Failed to grab camera frame [%s]", self.tag)
            return None
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        return buf.getvalue()

    async def set_light(self, on: bool) -> None:
        if on:
            await asyncio.to_thread(self.printer.turn_light_on)
        else:
            await asyncio.to_thread(self.printer.turn_light_off)

    async def close(self) -> None:
        await asyncio.to_thread(self.printer.disconnect)


CONNECTOR_CLASS = BambuConnector
