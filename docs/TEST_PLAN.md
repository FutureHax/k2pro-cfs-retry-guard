# cfs_retry_guard test plan

Run this only while the printer is idle. Every step that restarts Klipper must never happen mid-print.

Paths in this repo: the extra is `klipper/extras/cfs_retry_guard.py`, the config is `config/cfs_retry_guard.cfg`, and the purge cap is `config/box.cfg.patch`. Paths starting with `/mnt/UDISK` or `/usr/share` are on the printer.

## 0. Backups
- `cp /mnt/UDISK/printer_data/config/box.cfg /mnt/UDISK/printer_data/config/box.cfg.pre-guard`
- `cp /mnt/UDISK/printer_data/config/printer.cfg /mnt/UDISK/printer_data/config/printer.cfg.pre-guard`
- Keep a copy of `cfs_retry_guard.py` in `/mnt/UDISK/printer_data/config/extras-src/` so it can be restored after an OTA.

## 1. Stage 1: purge cap only (no new code)
1. Add `max_tube_length: 2000` under `[box]` in `box.cfg` (see `config/box.cfg.patch`).
2. `FIRMWARE_RESTART` (needed for any config or extra change).
3. In the Fluidd console, confirm the parsed value: query `configfile.settings.box.max_tube_length` (Moonraker `printer/objects/query?configfile`) and check that it reads 2000.

## 2. Stage 2: install the guard
1. Copy `klipper/extras/cfs_retry_guard.py` to `/usr/share/klipper/klippy/extras/`.
2. Copy `config/cfs_retry_guard.cfg` to the config dir (`/mnt/UDISK/printer_data/config/`) and add `[include cfs_retry_guard.cfg]` after `[include box.cfg]` in `printer.cfg`.
3. `FIRMWARE_RESTART`. Check `klippy.log` for `cfs_retry_guard: wrapped BOX_TNN_RETRY_PROCESS`.
   - If it says "not registered at connect", the stock command is registered later than connect. Stock retry still works unchanged; the hook needs to move to `klippy:ready`.

## 3. Read-only checks (idle printer, no motion)
1. `CFS_RETRY_GUARD_STATUS` (no RS485 traffic, cached state only). Expected:
   - `installed: True`
   - `box_save attrs:` lists the real attribute names. Confirm that one of them holds the error name (e.g. `None` when idle) and one is `error_tnn`.
   - `would do: stock (no readable error, ...)` when idle.
2. `CFS_RETRY_GUARD_STATUS REFRESH=1` (sends one GET_FILAMENT_SENSOR_STATE query, the same query the stock retry sends). With all four slots loaded, the snapshot should show `material: 15`. With nothing loaded at the head, it should show `connections: 0`.
3. `HELP` should list `_BOX_TNN_RETRY_PROCESS_BASE`.

## 4. Error-path test (sacrificial short print, two slots)
Use a two-color test print with a short PLA spool or a spool you can open.
1. During a print, force a key839 on the outgoing slot. Option A: `BOX_TEST_MAKE_ERROR` (exists in the `.so`; parameters unknown, try `HELP`). Option B: mask the slot sensor by pulling filament back past it by hand right as the tool change starts.
2. When the popup shows, run `CFS_RETRY_GUARD_STATUS` before pressing Retry. Expected: `err: retrude_err`, `error_tnn` with `last_tnn`/`tnn`, `would do: guard ...` (for a false empty) or `stock ...`.
3. Press Retry on the screen (default `auto_resume: True`). Expected for a false empty:
   - No "extrude all material" line in klippy.log.
   - BOX_ERROR_CLEAR, then the quit sequence, then the load sequence with `TNN=<target>`, then `RESUME`.
   - The print continues on its own. Confirm the correct color prints.
   - Watch master-server for a duplicate-resume key16 popup ("Print is not paused, resume aborted"). If one appears, set `auto_resume: False`.
4. Failure check: block the outgoing spool from turning during step 3. Expected: the unload error is raised again, nothing loads, nothing resumes, and the print stays paused.
5. Optional manual-mode check: set `auto_resume: False`, `FIRMWARE_RESTART` while idle, and repeat step 3. Expected: same sequence without `RESUME`, then the message "Press Resume". Press Resume on the screen and confirm the correct color prints.

## 5. Rollback
- Fast disable: set `enabled: False` in `cfs_retry_guard.cfg`, then `FIRMWARE_RESTART`. The stock handler runs for every retry.
- Keep the guard but stop automatic resume: set `auto_resume: False`, then `FIRMWARE_RESTART`. The guard recovers and leaves the print paused for you to press Resume.
- Full removal: remove the `[include cfs_retry_guard.cfg]` line FIRST, then delete `extras/cfs_retry_guard.py`, then `FIRMWARE_RESTART`.
- Purge cap: remove the `max_tube_length` line, or restore `box.cfg.pre-guard`.
- The stock command can be run by hand at any time as `_BOX_TNN_RETRY_PROCESS_BASE`.

## 6. OTA risk
- An OTA replaces `/usr/share/klipper`, which deletes the extra. If the include is still present, Klipper fails with an unknown-section error and the printer will not start. After any OTA, either re-copy the extra from `extras-src/` or remove the include before restarting.
- The OTA may also reset `box.cfg`. cfg-guardian already re-applies `Tn_retrude`. Add `max_tube_length` there if it supports new keys.
- Re-check after OTA that the `.so` still has `BOX_TNN_RETRY_PROCESS`, `box_save`, `box_state`, `Tn_inner_data` (run `CFS_RETRY_GUARD_STATUS`).
