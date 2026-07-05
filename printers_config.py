import os
import re
import tempfile

import yaml

NAME_RE = re.compile(r"^[a-z0-9_]{1,20}$")

SECTION_KEYS = {"bambu": "bambu_printers", "moonraker": "moonraker_printers"}
REQUIRED_FIELDS = {
    "bambu": ("name", "ip", "access_code", "serial"),
    "moonraker": ("name", "moonraker_url", "camera_snapshot_url"),
}


class PrintersConfigError(Exception):
    pass


def validate_entry(entry: dict, printer_type: str, existing_names: set) -> None:
    section = SECTION_KEYS[printer_type]
    required = REQUIRED_FIELDS[printer_type]

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
            f"среди bambu_printers и moonraker_printers вместе)"
        )


def load_printers(path: str) -> tuple[list[dict], list[dict]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise PrintersConfigError(f"Файл манифеста принтеров не найден: {path}") from e
    except yaml.YAMLError as e:
        raise PrintersConfigError(f"Не удалось разобрать YAML {path}: {e}") from e

    bambu_printers = data.get("bambu_printers") or []
    moonraker_printers = data.get("moonraker_printers") or []

    if not bambu_printers and not moonraker_printers:
        raise PrintersConfigError(
            f"{path}: не задано ни одного принтера в bambu_printers/moonraker_printers"
        )

    seen_names: set = set()

    for entry in bambu_printers:
        validate_entry(entry, "bambu", seen_names)
        seen_names.add(entry["name"])

    for entry in moonraker_printers:
        validate_entry(entry, "moonraker", seen_names)
        seen_names.add(entry["name"])

    return bambu_printers, moonraker_printers


def append_printer(path: str, printer_type: str, entry: dict) -> None:
    """Дописывает новую запись принтера в YAML-манифест атомарно.

    Файл переписывается целиком через yaml.safe_dump, поэтому ручные
    комментарии в нём не сохраняются.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}

    section = SECTION_KEYS[printer_type]
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
