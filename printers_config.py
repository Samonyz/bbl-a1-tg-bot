import os
import re
import tempfile

import yaml

from connectors.base import PrinterConnector

NAME_RE = re.compile(r"^[a-z0-9_]{1,20}$")


class PrintersConfigError(Exception):
    pass


def validate_entry(entry: dict, connector_cls: type[PrinterConnector], existing_names: set) -> None:
    section = connector_cls.SECTION_KEY
    required = connector_cls.REQUIRED_FIELDS

    missing = [field for field in required if not entry.get(field)]
    if missing:
        raise PrintersConfigError(
            f"{section}: запись {entry!r} не содержит обязательных полей {missing}"
        )

    name = entry["name"]
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PrintersConfigError(
            f"{section}: имя {name!r} невалидно, допускаются только "
            f"a-z, 0-9, _ длиной 1-20 символов"
        )
    if name in existing_names:
        raise PrintersConfigError(
            f"{section}: имя {name!r} повторяется (имена должны быть уникальны "
            f"среди всех типов принтеров вместе)"
        )


def load_printers(path: str, connector_types: dict[str, type[PrinterConnector]]) -> dict[str, list[dict]]:
    """Возвращает {type_key: [entry, ...]} для каждого зарегистрированного
    типа коннектора, найденного в YAML-манифесте по его SECTION_KEY."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise PrintersConfigError(f"Файл манифеста принтеров не найден: {path}") from e
    except yaml.YAMLError as e:
        raise PrintersConfigError(f"Не удалось разобрать YAML {path}: {e}") from e

    result: dict[str, list[dict]] = {}
    seen_names: set = set()

    for type_key, connector_cls in connector_types.items():
        entries = data.get(connector_cls.SECTION_KEY) or []
        for entry in entries:
            validate_entry(entry, connector_cls, seen_names)
            seen_names.add(entry["name"])
        result[type_key] = entries

    if not any(result.values()):
        sections = ", ".join(cls.SECTION_KEY for cls in connector_types.values())
        raise PrintersConfigError(f"{path}: не задано ни одного принтера ни в одной секции ({sections})")

    return result


def append_printer(path: str, connector_cls: type[PrinterConnector], entry: dict) -> None:
    """Дописывает новую запись принтера в YAML-манифест атомарно.

    Файл переписывается целиком через yaml.safe_dump, поэтому ручные
    комментарии в нём не сохраняются.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}

    section = connector_cls.SECTION_KEY
    data.setdefault(section, []).append(entry)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".printers.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
