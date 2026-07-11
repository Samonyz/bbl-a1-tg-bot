# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [2.1.0-beta] - 2026-07-12

### Added

- Active spool display in print status: color (emoji) and slot position (`T1`-`T4`, `A`-`D`) for Creality CFS and Bambu AMS in a unified `[-emoji--] T1B PLA` format. Without AMS/CFS, or when printing from the external holder, a separate line shows the external filament's material/color if known.

### Changed

- Moonraker progress percentage is now computed from `virtual_sdcard.progress` (file byte position) instead of `display_status.progress` (slicer time estimate, drifts on prints with filament-change pauses).

### Note

- The Bambu AMS logic is based on the publicly documented MQTT schema (`tray_now`), not verified against real hardware yet - needs confirmation once an AMS unit is connected.

## [2.0.1] - 2026-07-11

### Fixed

- Duplicate progress messages caused by resetting `progress_message_id` on network errors and by editing captions on text-only messages. Progress messages are now always photos: the last camera frame for the current print, or a placeholder.

## [2.0.0] - 2026-07-05

### Added

- Support for an arbitrary number of printers of either type via a YAML manifest, `printers.yaml` (template: `printers.example.yaml`). The printer name from the manifest is used as a tag in messages (`[name] ...`) and as a command suffix.
- Support for Klipper/Moonraker printers (`MoonrakerPrinterMonitor` class) alongside Bambu Lab: status and camera snapshot are fetched via the Moonraker REST API (`/printer/objects/query`, `camera_snapshot_url`).
- Optional `api_key` field for Moonraker printers, sent as the `X-Api-Key` header when the Moonraker instance requires authentication.
- `/add_printer` — an interactive Telegram dialog: choose printer type, enter parameters one by one, test the connection, and on success add the printer to monitoring without restarting the bot, persisting the entry to `printers.yaml`.
- `/list_printers` — lists printers currently under monitoring along with their type.
- Per-printer commands `/status_<name>`, `/photo_<name>` for every printer in the manifest; `/light_on_<name>`, `/light_off_<name>` for Bambu printers. `/status` with no argument shows a summary across all printers at once.

### Changed

- Printer configuration moved out of `.env` into `printers.yaml`. `.env` now only holds settings shared across the whole bot: Telegram bot token and chat_id, locale, timezone, and default poll intervals (`DEFAULT_POLL_INTERVAL_SECONDS`, `DEFAULT_PROGRESS_UPDATE_SECONDS`).
- Commands `/light_on` and `/light_off` were renamed to `/light_on_<name>` and `/light_off_<name>` to support multiple printers of the same type (breaking change relative to 1.0.0).
- `/start` now builds the list of available commands dynamically, based on the printers actually loaded at the time it's called.

### Fixed

- Synchronous `bambulabs_api` calls (reading status, grabbing a camera frame, toggling the light, testing a connection when adding a printer) now run in a separate thread via `asyncio.to_thread`. Previously they ran directly inside coroutines on the shared event loop: a blocking MQTT call to one printer (Bambu A1) would freeze the entire process indefinitely, including polling of the other printer (Moonraker) and handling of Telegram commands — events would queue up and all arrive at once as soon as the block cleared.

## [1.0.0] - 2026-07-04

### Added

- Initial version of the bot: monitoring a single Bambu Lab A1 printer over MQTT (`bambulabs_api`).
- Notifications on print status changes (started, paused, resumed, finished, failed) with a camera snapshot.
- A periodically edited progress message during an active print (file name, percentage, layer, remaining time).
- Commands `/status`, `/photo`, `/light_on`, `/light_off`.
- Message localization (`ru`/`en`).
