import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "klipper", "extras"))

import cfs_retry_guard as g  # noqa: E402


class CommandError(Exception):
    pass


class FakeGcode:
    def __init__(self, log):
        self.handlers = {}
        self.log = log
        self.fail_on = set()
        self.on_run = None

    def register_command(self, cmd, func, desc=None):
        if func is None:
            return self.handlers.pop(cmd, None)
        assert cmd not in self.handlers
        self.handlers[cmd] = func

    def run_script_from_command(self, script):
        self.log.append(script)
        if script in self.fail_on:
            raise CommandError(script)
        if self.on_run:
            self.on_run(script)


class FakeConfig:
    def __init__(self, printer, **opts):
        self.printer, self.opts = printer, opts

    def get_printer(self):
        return self.printer

    def getboolean(self, k, d):
        return self.opts.get(k, d)

    def get(self, k, d):
        return self.opts.get(k, d)

    def getchoice(self, k, choices, d):
        return choices[self.opts.get(k, d)]


class FakeGcmd:
    def __init__(self):
        self.out = []

    def respond_info(self, m):
        self.out.append(m)

    def error(self, m):
        return CommandError(m)

    def get_int(self, k, d, minval=None, maxval=None):
        return d


class Rig:
    def __init__(self, err="retrude_err", material=11, connections=4,
                 head=True, **opts):
        self.log = []
        self.gcode = FakeGcode(self.log)
        self.stock_calls = []
        self.gcode.register_command(
            g.BASE_CMD, lambda gcmd: self.stock_calls.append(gcmd))
        save = types.SimpleNamespace(
            err=err, last_err="filament_err",
            error_tnn={"last_tnn": "T1C", "tnn": "T1B", "addr": 1,
                       "vtnn": None})
        state = types.SimpleNamespace(Tn_inner_data={"T1": {
            "filament_sensor": {"material": material,
                                "connections": connections}}})
        self.box = types.SimpleNamespace(box_save=save, box_state=state)
        self.helper = types.SimpleNamespace(filament_present=head)
        self.pr = types.SimpleNamespace(is_paused=True)
        objs = {"gcode": self.gcode, "box": self.box,
                "filament_switch_sensor filament_sensor":
                    types.SimpleNamespace(runout_helper=self.helper),
                "pause_resume": self.pr}
        self.events = {}
        printer = types.SimpleNamespace(
            command_error=CommandError,
            lookup_object=lambda n, d=None: objs.get(n, d),
            register_event_handler=lambda e, f: self.events.__setitem__(e, f))
        self.guard = g.load_config(FakeConfig(printer, **opts))
        self.events["klippy:connect"]()

    def retry(self):
        gcmd = FakeGcmd()
        self.gcode.handlers[g.BASE_CMD](gcmd)
        return gcmd


class GuardTest(unittest.TestCase):
    def test_wrapping(self):
        r = Rig()
        self.assertIn(g.RENAMED_CMD, r.gcode.handlers)
        self.assertIn("CFS_RETRY_GUARD_STATUS", r.gcode.handlers)

    def test_non_retrude_goes_stock(self):
        r = Rig(err="filament_err")
        r.retry()
        self.assertEqual(len(r.stock_calls), 1)

    def test_slot_present_goes_stock(self):
        r = Rig(material=15)
        r.retry()
        self.assertEqual(len(r.stock_calls), 1)

    def test_true_empty_goes_stock(self):
        r = Rig(connections=0)
        r.retry()
        self.assertEqual(len(r.stock_calls), 1)

    def test_head_clear_goes_stock(self):
        r = Rig(head=False)
        r.retry()
        self.assertEqual(len(r.stock_calls), 1)

    def test_unreadable_refuses(self):
        r = Rig()
        r.box.box_state.Tn_inner_data = {}
        out = r.retry()
        self.assertEqual(r.stock_calls, [])
        self.assertIn("not retrying", out.out[0])

    def test_refresh_failure_refuses(self):
        r = Rig()
        r.gcode.fail_on.add("BOX_GET_FILAMENT_SENSOR_STATE")
        r.retry()
        self.assertEqual(r.stock_calls, [])
        self.assertNotIn("BOX_ERROR_CLEAR", r.log)

    def _simulate_unload_load(self, r):
        def on_run(script):
            fs = r.box.box_state.Tn_inner_data["T1"]["filament_sensor"]
            if script == "BOX_QUIT_MATERIAL_RETRUDE_MATERIAL":
                r.helper.filament_present = False
                fs["connections"], fs["material"] = 0, 15
            if script.startswith("BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL"):
                r.helper.filament_present = True
                fs["connections"] = 2
        r.gcode.on_run = on_run

    def test_false_empty_recovers_without_purge(self):
        r = Rig()
        self._simulate_unload_load(r)
        out = r.retry()
        self.assertEqual(r.stock_calls, [])
        self.assertNotIn("BOX_EXTRUSION_ALL_MATERIALS", r.log)
        self.assertIn("BOX_ERROR_CLEAR", r.log)
        self.assertIn("BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL TNN=T1B", r.log)
        self.assertNotIn("RESUME", r.log)
        self.assertIn("Press Resume", out.out[-1])

    def test_auto_resume(self):
        r = Rig(auto_resume=True)
        self._simulate_unload_load(r)
        r.retry()
        self.assertEqual(r.log[-1], "RESUME")

    def test_unload_failure_stays_paused(self):
        r = Rig()
        r.gcode.fail_on.add("BOX_QUIT_MATERIAL_RETRUDE_MATERIAL")
        with self.assertRaises(CommandError):
            r.retry()
        self.assertFalse(any(s.startswith("BOX_LOAD") for s in r.log))
        self.assertFalse(r.guard.busy)

    def test_head_still_loaded_after_unload_refuses(self):
        r = Rig()
        out = r.retry()
        self.assertFalse(any(s.startswith("BOX_LOAD") for s in r.log))
        self.assertIn("stopped", out.out[-1])

    def test_disabled_goes_stock(self):
        r = Rig(enabled=False)
        r.retry()
        self.assertEqual(len(r.stock_calls), 1)

    def test_status_sends_nothing(self):
        r = Rig()
        gcmd = FakeGcmd()
        r.gcode.handlers["CFS_RETRY_GUARD_STATUS"](gcmd)
        self.assertEqual(r.log, [])
        self.assertIn("would do: guard", gcmd.out[0])


if __name__ == "__main__":
    unittest.main()
