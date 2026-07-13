"""Рендер PrinterState/SpoolInfo в текст сообщения. Единственное место
в проекте (кроме самих коннекторов), которое обращается к t()."""

from locales import t
from models import PAUSED_STATES, RUNNING_STATES, PrinterState, SpoolInfo, SpoolStatus

# Палитра для приближённого отображения цвета катушки эмодзи-кругом. Это
# потолок обычных Unicode-эмодзи без кастомных emoji (те видны только у
# Telegram Premium) - 10 цветов, дальше точнее в тексте эмодзи не передать.
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


def color_emoji(hex_color: str) -> str:
    hex_color = hex_color[-6:]
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return min(
        _COLOR_EMOJI,
        key=lambda entry: sum((a - c) ** 2 for a, c in zip((r, g, b), entry[0])),
    )[1]


def format_spool_line(spool: SpoolInfo | None) -> str | None:
    """Строка с катушкой для сообщения о печати, либо None если коннектор
    вообще не сообщает о мультиматериальной системе (нет box/AMS)."""
    if spool is None:
        return None
    if spool.status == SpoolStatus.CHANGING:
        return t("snapshot.spool_changing")
    if spool.status == SpoolStatus.EXTERNAL:
        if spool.material and spool.color_hex:
            return t(
                "snapshot.spool_external_material",
                emoji=color_emoji(spool.color_hex),
                material=spool.material,
            )
        return t("snapshot.spool_external")

    # ACTIVE: позиция слота A-D показана смещением цветного эмодзи внутри '[----]'
    idx = "ABCD".index(spool.tag[-1])
    emoji = color_emoji(spool.color_hex)
    bar = "[" + "-" * idx + emoji + "-" * (3 - idx) + "]"
    return t("snapshot.spool", bar=bar, tag=spool.tag, material=spool.material)


def format_snapshot(state: PrinterState, type_key: str) -> str:
    status_label = t(f"state.{state.state.name}")
    lines = [t("snapshot.status", status=status_label)]

    if state.state not in RUNNING_STATES and state.state not in PAUSED_STATES:
        return "\n".join(lines)

    if state.filename:
        lines.append(t("snapshot.file", name=state.filename))
    if state.percent is not None:
        lines.append(t("snapshot.progress", percent=state.percent))
    if state.layer_current and state.layer_total:
        lines.append(
            t("snapshot.layer", current=state.layer_current, total=state.layer_total)
        )
    if state.height_current is not None and state.height_total is not None:
        lines.append(
            t(
                "snapshot.height",
                current=round(state.height_current, 1),
                total=round(state.height_total, 1),
            )
        )
    if state.remaining_seconds is not None:
        lines.append(t("snapshot.remaining", remaining=state.remaining_seconds // 60))

    spool_line = format_spool_line(state.spool)
    if spool_line is not None:
        lines.append(spool_line)

    for extra in state.extra:
        lines.append(t(f"{type_key}.{extra.key}", **extra.values))

    return "\n".join(lines)
