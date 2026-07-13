import logging

import httpx

from models import PrinterState, PrintState, SpoolInfo, SpoolStatus

from .base import ConnectionTestResult, PrinterConnector

log = logging.getLogger("bambu-bot.connectors.moonraker")

MOONRAKER_STATE_MAP = {
    "printing": PrintState.RUNNING,
    "paused": PrintState.PAUSE,
    "standby": PrintState.IDLE,
    "complete": PrintState.FINISH,
    "cancelled": PrintState.FAILED,
    "error": PrintState.FAILED,
}


def _percent(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _active_spool(box: dict) -> SpoolInfo:
    """Возвращает активную катушку CFS. status=CHANGING, если бокс подключён,
    но сейчас идёт смена прутка (слот временно не выбран - подтверждено живым
    наблюдением: T1.filament становится 'None', пока T1.state остаётся
    'connect'); status=EXTERNAL, если CFS не подключена вообще (печать с
    внешнего держателя)."""
    same_material = box.get("same_material") or []
    any_connected = False
    for box_key in ("T1", "T2", "T3", "T4"):
        slot = box.get(box_key)
        if not isinstance(slot, dict) or slot.get("state") != "connect":
            continue
        any_connected = True
        letter = slot.get("filament")
        if not letter or letter == "None":
            continue
        tag = f"{box_key}{letter}"
        for _material_code, color_hex, tags, material_name in same_material:
            if tag in tags:
                return SpoolInfo(
                    SpoolStatus.ACTIVE, tag=tag, material=material_name, color_hex=color_hex
                )
    return SpoolInfo(SpoolStatus.CHANGING if any_connected else SpoolStatus.EXTERNAL)


class MoonrakerConnector(PrinterConnector):
    TYPE_KEY = "moonraker"
    DISPLAY_NAME = "Moonraker"
    SECTION_KEY = "moonraker_printers"
    FIELD_SPECS = [
        ("moonraker_url", "field.moonraker_url"),
        ("camera_snapshot_url", "field.moonraker_camera"),
        ("api_key", "field.moonraker_api_key"),
    ]
    OPTIONAL_FIELDS = {"api_key"}
    REQUIRED_FIELDS = ("name", "moonraker_url", "camera_snapshot_url")

    def __init__(
        self, base_url: str, camera_snapshot_url: str, tag: str, api_key: str | None = None
    ):
        headers = {"X-Api-Key": api_key} if api_key else {}
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10, headers=headers)
        self.camera_snapshot_url = camera_snapshot_url
        self.tag = tag
        self._metadata: dict | None = None
        self._metadata_filename: str | None = None

    @classmethod
    def from_config(cls, cfg: dict, *, tag: str) -> "MoonrakerConnector":
        return cls(
            cfg["moonraker_url"], cfg["camera_snapshot_url"], tag, api_key=cfg.get("api_key")
        )

    @classmethod
    async def test_connection(cls, draft: dict) -> ConnectionTestResult:
        headers = {"X-Api-Key": draft["api_key"]} if draft.get("api_key") else {}
        client = httpx.AsyncClient(base_url=draft["moonraker_url"], timeout=8, headers=headers)
        ok = False
        error_text = None
        camera_warning = False
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
                camera_warning = True
        await client.aclose()

        connector = None
        if ok:
            connector = cls(
                draft["moonraker_url"],
                draft["camera_snapshot_url"],
                draft["name"],
                api_key=draft.get("api_key"),
            )
        return ConnectionTestResult(
            ok=ok, error=error_text, camera_warning=camera_warning, connector=connector
        )

    @staticmethod
    def draft_summary(draft: dict) -> str:
        lines = [
            f"name={draft['name']}",
            f"moonraker_url={draft['moonraker_url']}",
            f"camera_snapshot_url={draft['camera_snapshot_url']}",
        ]
        if draft.get("api_key"):
            lines.append("api_key=***")
        return "\n".join(lines)

    async def _fetch_metadata(self, filename: str) -> dict:
        resp = await self.client.get("/server/files/metadata", params={"filename": filename})
        resp.raise_for_status()
        return resp.json()["result"]

    async def poll(self) -> PrinterState:
        resp = await self.client.get(
            "/printer/objects/query",
            params={"print_stats": "", "virtual_sdcard": "", "box": "", "gcode_move": ""},
        )
        resp.raise_for_status()
        status = resp.json()["result"]["status"]

        print_stats = status["print_stats"]
        # virtual_sdcard.progress - доля прочитанных байт файла, точнее
        # display_status.progress (тот часто основан на оценке слайсера по
        # времени, которая на CFS-печатях с паузами на смену прутка уплывает).
        virtual_sdcard = status["virtual_sdcard"]
        gcode_move = status.get("gcode_move") or {}
        state = MOONRAKER_STATE_MAP.get(print_stats["state"], PrintState.UNKNOWN)
        percent = _percent(round((virtual_sdcard.get("progress") or 0) * 100))
        filename = print_stats.get("filename") or None

        if filename and filename != self._metadata_filename:
            # Метаданные (высота модели, число слоёв из слайсера) не
            # меняются для данного файла - запрашиваем один раз за печать,
            # а не на каждом опросе. _metadata_filename обновляем только при
            # успехе, чтобы при сбое повторить на следующем цикле.
            self._metadata = None
            try:
                self._metadata = await self._fetch_metadata(filename)
                self._metadata_filename = filename
            except Exception:
                log.exception("Failed to fetch gcode metadata [%s]", self.tag)

        metadata = self._metadata or {}
        layer_current = virtual_sdcard.get("layer")
        layer_total = metadata.get("layer_count") or virtual_sdcard.get("layer_count")

        position = gcode_move.get("position")
        height_current = position[2] if position else None
        height_total = metadata.get("object_height")

        spool = None
        box = status.get("box")
        if box:
            spool = _active_spool(box)

        return PrinterState(
            state=state,
            filename=filename,
            percent=percent,
            layer_current=layer_current,
            layer_total=layer_total,
            height_current=height_current,
            height_total=height_total,
            spool=spool,
        )

    async def get_photo(self) -> bytes | None:
        try:
            resp = await self.client.get(self.camera_snapshot_url)
            resp.raise_for_status()
            return resp.content
        except Exception:
            log.exception("Failed to grab camera frame [%s]", self.tag)
            return None

    async def close(self) -> None:
        await self.client.aclose()


CONNECTOR_CLASS = MoonrakerConnector
