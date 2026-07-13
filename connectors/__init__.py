"""Реестр коннекторов оборудования, собирается авто-обнаружением: каждый
модуль в этом пакете, определяющий CONNECTOR_CLASS на уровне модуля,
попадает в CONNECTOR_TYPES под своим TYPE_KEY. Чтобы добавить новый тип
принтера, достаточно положить сюда новый .py файл - ничего в этом пакете
и вне его редактировать не нужно."""

import importlib
import pkgutil

from locales import register_connector_locales

from .base import ConnectionTestResult, PrinterConnector

CONNECTOR_TYPES: dict[str, type[PrinterConnector]] = {}

for _, _module_name, _ in pkgutil.iter_modules(__path__):
    if _module_name == "base":
        continue
    _module = importlib.import_module(f"{__name__}.{_module_name}")
    _connector_cls = getattr(_module, "CONNECTOR_CLASS", None)
    if _connector_cls is None:
        continue
    CONNECTOR_TYPES[_connector_cls.TYPE_KEY] = _connector_cls
    if _connector_cls.LOCALES:
        register_connector_locales(_connector_cls.TYPE_KEY, _connector_cls.LOCALES)

__all__ = ["CONNECTOR_TYPES", "ConnectionTestResult", "PrinterConnector"]
