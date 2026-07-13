"""Абстрактный интерфейс коннектора оборудования.

Чтобы добавить поддержку нового принтера/мультиматериальной системы,
достаточно создать в этом пакете новый модуль с классом-наследником
PrinterConnector и присвоить его CONNECTOR_CLASS на уровне модуля - ядро
бота подхватит его автоматически (см. connectors/__init__.py), никакой
другой файл редактировать не нужно.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from models import PrinterState


@dataclass
class ConnectionTestResult:
    """Результат проверки подключения в диалоге /add_printer."""

    ok: bool
    error: str | None = None
    camera_warning: bool = False
    connector: "PrinterConnector | None" = None  # готовый инстанс при ok=True


class PrinterConnector(ABC):
    # --- метаданные типа, задаются в подклассе как атрибуты класса ---

    TYPE_KEY: ClassVar[str]  # "bambu", "moonraker" - уникальный, используется как
    # ключ реестра, секция printers.yaml (через SECTION_KEY) и неймспейс
    # для собственных строк локализации (см. LOCALES ниже)
    DISPLAY_NAME: ClassVar[str]  # "Bambu Lab" - подпись кнопки в /add_printer
    SECTION_KEY: ClassVar[str]  # "bambu_printers" - ключ секции в printers.yaml
    FIELD_SPECS: ClassVar[list[tuple[str, str]]]  # [(field_key, locale-ключ подсказки), ...]
    OPTIONAL_FIELDS: ClassVar[set[str]] = set()
    REQUIRED_FIELDS: ClassVar[tuple[str, ...]]  # для validate_entry, "name" неявно всегда обязателен
    HAS_LIGHT_CONTROL: ClassVar[bool] = False
    ERROR_REFERENCE_URL: ClassVar[str | None] = None  # ссылка-справочник для event.error, если есть
    LOCALES: ClassVar[dict[str, dict[str, str]]] = {}  # опционально: свои строки локализации

    tag: str

    @classmethod
    @abstractmethod
    def from_config(
        cls, cfg: dict, *, tag: str, progress_update_seconds: float
    ) -> "PrinterConnector":
        """Строит коннектор из записи printers.yaml (или совместимого dict)."""

    @classmethod
    @abstractmethod
    async def test_connection(cls, draft: dict) -> ConnectionTestResult:
        """Проверяет подключение по данным, введённым в диалоге /add_printer.

        При успехе result.connector должен быть уже готовым к работе
        инстансом (не просто bool) - переиспользуем протестированное
        соединение вместо повторного подключения при подтверждении."""

    @staticmethod
    @abstractmethod
    def draft_summary(draft: dict) -> str:
        """Краткое резюме введённых данных для подтверждения в диалоге."""

    @abstractmethod
    async def poll(self) -> PrinterState:
        """Опрашивает оборудование и возвращает актуальный PrinterState."""

    @abstractmethod
    async def get_photo(self) -> bytes | None:
        """Снимок с камеры, либо None если недоступен."""

    async def set_light(self, on: bool) -> None:
        """Вызывается только если HAS_LIGHT_CONTROL истинно."""
        raise NotImplementedError

    async def close(self) -> None:
        """Освобождает ресурсы (соединение и т.п.) - например при отмене/retry
        в диалоге /add_printer до подтверждения. По умолчанию no-op."""
