"""Общие типы данных для обмена между коннекторами оборудования и ядром бота.

Ничего здесь не знает про Telegram, HTTP, MQTT или локализацию - только
форма данных, которую коннектор поставляет ядру.
"""

from dataclasses import dataclass, field
from enum import Enum


class PrintState(Enum):
    IDLE = "IDLE"
    PREPARE = "PREPARE"
    RUNNING = "RUNNING"
    PAUSE = "PAUSE"
    FINISH = "FINISH"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


RUNNING_STATES = (PrintState.RUNNING,)
PAUSED_STATES = (PrintState.PAUSE,)


class SpoolStatus(Enum):
    ACTIVE = "active"      # есть активный слот с материалом/цветом
    CHANGING = "changing"  # мультиматериальная система подключена, идёт смена прутка
    EXTERNAL = "external"  # печать с внешнего держателя (или активный слот не выбран)


@dataclass
class SpoolInfo:
    status: SpoolStatus
    tag: str | None = None          # например "T1B" - бокс/AMS-юнит + буква слота, только для ACTIVE
    material: str | None = None
    color_hex: str | None = None


@dataclass
class ExtraField:
    """Информация, специфичная для конкретного коннектора, не входящая в
    типизированные поля PrinterState. key - ненамерженный ключ локализации
    (см. locales.register_connector_locales); values - kwargs для шаблона."""

    key: str
    values: dict = field(default_factory=dict)


@dataclass
class PrinterState:
    state: PrintState
    filename: str | None = None
    percent: int | None = None
    layer_current: int | None = None
    layer_total: int | None = None
    height_current: float | None = None
    height_total: float | None = None
    remaining_seconds: int | None = None
    error_code: int = 0
    spool: SpoolInfo | None = None
    extra: list[ExtraField] = field(default_factory=list)
