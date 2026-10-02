# Guarded wrapper around the stock CFS BOX_TNN_RETRY_PROCESS command.
#
# Handles one case itself: a retrude_err (key839 during a tool change) where
# the box reports the old slot empty while its connection bit is still set and
# the toolhead sensor still sees filament. Stock would run
# extrusion_all_materials (purge up to max_tube_length). This module instead
# clears the error, retries the unload, loads the target slot and leaves the
# print paused (or resumes if auto_resume is set). Every other case is passed
# to the stock handler unchanged.
import inspect
import logging

BASE_CMD = "BOX_TNN_RETRY_PROCESS"
RENAMED_CMD = "_BOX_TNN_RETRY_PROCESS_BASE"
ERROR_NAMES = (
    "retrude_err", "filament_err", "box_extrude_err", "flush_err",
    "macro_err", "print_end_err", "empty_print", "extruder_extrude_err",
    "cr_break_err",
)
SLOT_BITS = {"A": 1, "B": 2, "C": 4, "D": 8}
QUIT_STEPS = (
    "BOX_ERROR_CLEAR",
    "BOX_QUIT_MATERIAL_HEATING",
    "BOX_QUIT_MATERIAL_CUT_MATERIAL",
    "BOX_QUIT_MATERIAL_RETRUDE_MATERIAL",
    "BOX_QUIT_MATERIAL_END",
)
LOAD_STEPS = (
    "BOX_LOAD_MATERIAL_HEATING",
    "BOX_LOAD_MATERIAL_CUT_MATERIAL",
    "BOX_LOAD_MATERIAL_RETRUDE_MATERIAL",
    "BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL TNN={tnn}",
    "BOX_LOAD_MATERIAL_MATERIAL_FLUSH TNN={tnn}",
    "BOX_LOAD_MATERIAL_END",
)


class GuardRefused(Exception):
    pass


class CfsRetryGuard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.enabled = config.getboolean("enabled", True)
        self.auto_resume = config.getboolean("auto_resume", False)
        self.sensor_name = config.get("filament_sensor", "filament_sensor")
        self.refresh_gcode = config.get(
            "sensor_refresh_gcode", "BOX_GET_FILAMENT_SENSOR_STATE")
        self.unreadable_policy = config.getchoice(
            "unreadable_policy", {"refuse": "refuse", "stock": "stock"},
            "refuse")
        self.box = None
        self.base_handler = None
        self.busy = False
        self.last_decision = None
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect)
        self.gcode.register_command(
            "CFS_RETRY_GUARD_STATUS", self.cmd_CFS_RETRY_GUARD_STATUS,
            desc="Report what the CFS retry guard would do. No motion."
                 " REFRESH=1 queries the box sensors first.")

    def _handle_connect(self):
        self.box = self.printer.lookup_object("box", None)
        if self.box is None:
            logging.warning("cfs_retry_guard: no [box] object, not installed")
            return
        handler = self.gcode.register_command(BASE_CMD, None)
        if handler is None:
            logging.warning("cfs_retry_guard: %s not registered at connect,"
                            " not installed", BASE_CMD)
            return
        self.base_handler = handler
        self.gcode.register_command(
            RENAMED_CMD, handler, desc="Stock CFS retry process")
        self.gcode.register_command(
            BASE_CMD, self.cmd_BOX_TNN_RETRY_PROCESS,
            desc="CFS retry process with false box-empty guard")
        logging.info("cfs_retry_guard: wrapped %s", BASE_CMD)

    # State reads. None means "could not read"; callers treat that as unknown.

    def _scan(self, obj, depth=2, prefix=""):
        if obj is None or depth < 0:
            return
        try:
            items = vars(obj).items() if not isinstance(obj, dict) \
                else obj.items()
        except TypeError:
            return
        for key, value in list(items):
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            yield path, value
            if isinstance(value, dict) and depth > 0:
                for sub in self._scan(value, depth - 1, path):
                    yield sub

    def _read_error(self):
        save = getattr(self.box, "box_save", None)
        errs, tnns = set(), []
        for obj in (save, self.box):
            for path, value in self._scan(obj):
                leaf = path.rsplit(".", 1)[-1].lower()
                if "last" in leaf or "resume" in leaf:
                    continue
                if isinstance(value, str) and value in ERROR_NAMES:
                    errs.add(value)
                elif (leaf == "error_tnn" and isinstance(value, dict)
                      and "last_tnn" in value and "tnn" in value):
                    tnns.append(value)
        err = errs.pop() if len(errs) == 1 else None
        error_tnn = tnns[0] if tnns else None
        return err, error_tnn, len(errs) > 1

    def _read_sensor_bits(self, box_id):
        state = getattr(self.box, "box_state", None)
        data = getattr(state, "Tn_inner_data", None)
        try:
            fs = data[box_id]["filament_sensor"]
            return int(fs["material"]), int(fs["connections"])
        except (TypeError, KeyError, ValueError):
            return None, None

    def _read_head(self):
        sensor = self.printer.lookup_object(
            "filament_switch_sensor " + self.sensor_name, None)
        helper = getattr(sensor, "runout_helper", None)
        if helper is None:
            return None
        return bool(helper.filament_present)

    def _read_paused(self):
        pr = self.printer.lookup_object("pause_resume", None)
        return None if pr is None else bool(pr.is_paused)

    def _refresh(self):
        try:
            self.gcode.run_script_from_command(self.refresh_gcode)
            return True
        except self.printer.command_error as e:
            logging.warning("cfs_retry_guard: refresh failed: %s", e)
            return False

    def _snapshot(self, refresh):
        err, error_tnn, ambiguous = self._read_error()
        snap = {"err": err, "error_tnn": error_tnn, "ambiguous": ambiguous,
                "refreshed": None, "material": None, "connections": None,
                "head": self._read_head(), "paused": self._read_paused()}
        last = (error_tnn or {}).get("last_tnn")
        if isinstance(last, str) and len(last) == 3:
            if refresh:
                snap["refreshed"] = self._refresh()
            snap["material"], snap["connections"] = \
                self._read_sensor_bits(last[:2])
        return snap

    def _decide(self, snap):
        if not self.enabled:
            return "stock", "guard disabled"
        if snap["err"] is None:
            if snap["ambiguous"]:
                return "stock", "error state ambiguous"
            return "stock", "no readable error, stock handles it"
        if snap["err"] != "retrude_err":
            return "stock", "error is %s" % snap["err"]
        etnn = snap["error_tnn"] or {}
        last, tnn = etnn.get("last_tnn"), etnn.get("tnn")
        bit = SLOT_BITS.get(last[2:]) if isinstance(last, str) else None
        unreadable = (bit is None or not isinstance(tnn, str)
                      or snap["refreshed"] is False
                      or snap["material"] is None
                      or snap["connections"] is None
                      or snap["head"] is None)
        if unreadable:
            return self.unreadable_policy, "retrude_err but state unreadable"
        if not snap["head"]:
            return "stock", "toolhead sensor clear"
        if snap["material"] & bit:
            return "stock", "box sees %s, normal retry" % last
        if not snap["connections"] & bit:
            return "stock", "%s left the hub, true empty" % last
        if tnn == last:
            return "refuse", "target equals source %s" % last
        return "guard", "false empty on %s, target %s" % (last, tnn)

    # Command handlers.

    def cmd_BOX_TNN_RETRY_PROCESS(self, gcmd):
        if self.busy:
            raise gcmd.error("cfs_retry_guard: recovery already running")
        snap = self._snapshot(refresh=True)
        action, reason = self._decide(snap)
        self.last_decision = (action, reason)
        logging.info("cfs_retry_guard: %s (%s) snap=%s", action, reason, snap)
        if action == "stock":
            self.base_handler(gcmd)
            return
        if action == "refuse":
            gcmd.respond_info(
                "CFS retry guard: not retrying (%s). Print stays paused."
                " Run %s to use the stock retry." % (reason, RENAMED_CMD))
            return
        gcmd.respond_info("CFS retry guard: %s" % reason)
        self.busy = True
        try:
            self._recover(gcmd, snap["error_tnn"])
        except GuardRefused as e:
            gcmd.respond_info("CFS retry guard stopped: %s. Print stays"
                              " paused." % e)
        finally:
            self.busy = False

    def _run(self, script):
        logging.info("cfs_retry_guard: run %s", script)
        self.gcode.run_script_from_command(script)

    def _recover(self, gcmd, error_tnn):
        last, tnn = error_tnn["last_tnn"], error_tnn["tnn"]
        bit = SLOT_BITS[last[2:]]
        for step in QUIT_STEPS:
            self._run(step)
        self._refresh()
        material, connections = self._read_sensor_bits(last[:2])
        if self._read_head():
            raise GuardRefused("toolhead still sees filament after unload")
        if connections is not None and connections & bit:
            raise GuardRefused("%s still in hub after unload" % last)
        for step in LOAD_STEPS:
            self._run(step.format(tnn=tnn))
        if not self._read_head():
            raise GuardRefused("toolhead does not see %s after load" % tnn)
        if self.auto_resume and self._read_paused():
            self._run("RESUME")
            return
        gcmd.respond_info("CFS retry guard: %s unloaded, %s loaded. Press"
                          " Resume." % (last, tnn))

    def _signature(self, obj, name):
        func = getattr(obj, name, None)
        if func is None:
            return "missing"
        try:
            return str(inspect.signature(func))
        except (TypeError, ValueError):
            return "present, signature unavailable"

    def cmd_CFS_RETRY_GUARD_STATUS(self, gcmd):
        refresh = gcmd.get_int("REFRESH", 0, minval=0, maxval=1)
        lines = ["installed: %s" % (self.base_handler is not None),
                 "enabled: %s auto_resume: %s unreadable_policy: %s"
                 % (self.enabled, self.auto_resume, self.unreadable_policy),
                 "last_decision: %s" % (self.last_decision,)]
        if self.box is None:
            gcmd.respond_info("\n".join(lines + ["no [box] object"]))
            return
        snap = self._snapshot(refresh=bool(refresh))
        lines.append("snapshot: %s" % snap)
        lines.append("would do: %s (%s)" % self._decide(snap))
        save = getattr(self.box, "box_save", None)
        state = getattr(self.box, "box_state", None)
        for label, obj in (("box_save", save), ("box_state", state)):
            try:
                keys = sorted(vars(obj).keys())
            except TypeError:
                keys = "no __dict__"
            lines.append("%s attrs: %s" % (label, keys))
        lines.append("box_save.get_err%s" % self._signature(save, "get_err"))
        lines.append("box.error_clear%s"
                     % self._signature(self.box, "error_clear"))
        gcmd.respond_info("\n".join(lines))


def load_config(config):
    return CfsRetryGuard(config)
