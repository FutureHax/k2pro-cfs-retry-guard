# K2 Pro CFS retry guard

A workaround for the Creality K2 Pro "endless purge" after the CFS falsely reports a filament runout. It has two parts: a `box.cfg` patch that caps the purge, and a Klipper extra that changes what Retry does in one specific error state.

## What this is for

The issue is pretty simple, and Creality could probably fix it in a CFS update. We do not have access to the CFS code, so this is a janky solution that works from the outside.

When the CFS wrongly decides a roll has run out, the printer purges filament until it hits an internal limit of 3 meters. On this printer that takes about 10 minutes, and the roll was never empty.

## What triggers the issue

The CFS slot sensor erroneously detects a filament runout. That conflicts with the print head sensor, which still sees filament. The CFS assumes the roll is out and has the print head extrude what is left to clear the lines.

On this printer it happened while the CFS was unloading a slot for a color change. The log shows key839 ("no filament detected at box extrude position"), then `retrude_err`. The slot sensor misread. Because the CFS kept feeding real filament, the print head sensor kept seeing filament, so the purge only stopped at the 3000 mm limit.

That limit appears to be the `max_tube_length` setting under `[box]`. This is inferred from the matching 3000 default and the purge's log message, not confirmed from Creality's source.

## What we are doing to fix it

**The `box.cfg` patch** addresses the symptom, not the cause. It sets `max_tube_length: 2000` under `[box]`, so a false runout no longer dumps 3 m of filament and takes 10 minutes to stop. Measure your own CFS-to-toolhead tube and use that length plus about 300 mm. Setting it lower than your actual tube length may cause issues when loading filament. Pressing Retry again would probably fix that, since it should push up to `max_tube_length` again. This is not confirmed.

**The retry guard** (`cfs_retry_guard`) attempts the actual fix. When you press Retry in the disagreeing-sensor state, it:

1. Clears the error internally.
2. Retracts the errored slot back to the CFS.
3. Loads the slot the print was switching to.
4. Resumes the print. By default it resumes automatically. With `auto_resume: False` it leaves the print paused for you to press Resume.

To do this it has to fully hijack the stock retry command (`BOX_TNN_RETRY_PROCESS`). If the disagreeing-sensor state is not detected, it skips its custom behavior and calls the stock command. The custom behavior only runs on a `retrude_err` where the print head sensor sees filament and the CFS reports the old slot empty while still showing it connected. If it cannot read that state, or a check fails partway, it stops and leaves the print paused.

When a firmware update happens, all of this gets wiped and has to be reapplied. Maybe Creality will fix it in the next update so it will not matter.

## Status

Draft. The guard is unit-tested against fake printer objects only. It has not been run on real hardware yet.

## Install

Follow [docs/TEST_PLAN.md](docs/TEST_PLAN.md). In short:

- Only do this while the printer is idle. Every change needs a Klipper restart (`FIRMWARE_RESTART`). Never restart mid-print.
- Back up `box.cfg` and `printer.cfg` first.
- Purge cap: add the line from [config/box.cfg.patch](config/box.cfg.patch) under `[box]` in `box.cfg`.
- Guard: copy `klipper/extras/cfs_retry_guard.py` to `/usr/share/klipper/klippy/extras/`, copy `config/cfs_retry_guard.cfg` to the printer config directory, and add `[include cfs_retry_guard.cfg]` after `[include box.cfg]` in `printer.cfg`.
- After the restart, `klippy.log` should say `cfs_retry_guard: wrapped BOX_TNN_RETRY_PROCESS`. `CFS_RETRY_GUARD_STATUS` reports what the guard would do without moving anything.

A firmware update (OTA) replaces `/usr/share/klipper` and deletes the extra. If the include is still in `printer.cfg`, Klipper will not start. After an update, copy the extra back or remove the include before restarting.

## Settings

In `config/cfs_retry_guard.cfg`, section `[cfs_retry_guard]`:

| Option | Default | Meaning |
|---|---|---|
| `enabled` | `True` | `False` sends every retry to the stock command. |
| `auto_resume` | `True` | Resumes the print after a successful recovery. `False` leaves it paused for you to press Resume. |
| `filament_sensor` | `filament_sensor` | Name of the print head `filament_switch_sensor`. |
| `sensor_refresh_gcode` | `BOX_GET_FILAMENT_SENSOR_STATE` | Command run to refresh the CFS sensor state before deciding. |
| `unreadable_policy` | `refuse` | What to do on a `retrude_err` when the state cannot be read. `refuse` leaves the print paused; `stock` calls the stock command. |

With `auto_resume: True`, the print resumes as soon as the target slot is loaded and the print head sensor sees filament. The guard cannot check the color. An automatic resume may also collide with the screen's own resume and show a "Print is not paused" popup; this is untested. Set `auto_resume: False` if you want to check before resuming.

## Rollback

- Quick disable: set `enabled: False`, then restart Klipper. Every retry goes to the stock command.
- Keep the guard but stop automatic resume: set `auto_resume: False`, then restart Klipper.
- Full removal: remove the `[include cfs_retry_guard.cfg]` line first, then delete the extra, then restart Klipper.
- Purge cap: remove the `max_tube_length` line, or restore your `box.cfg` backup.
- The stock retry can be run by hand at any time as `_BOX_TNN_RETRY_PROCESS_BASE`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Standard library only. The tests use fake printer, CFS, and G-code objects, so no printer is needed.
