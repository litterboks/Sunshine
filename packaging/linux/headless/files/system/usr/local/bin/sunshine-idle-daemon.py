#!/usr/bin/env python3
"""Runs as root. Watches every physical input device, ignores Sunshine's
virtual uinput devices, and toggles every physical monitor off/on via
kscreen-doctor based on real user activity at the PC.

Physical vs. virtual monitor detection is done by parsing the EDID — any
connector whose product name matches VIRTUAL_EDID_RE is treated as a
Sunshine virtual display and left alone. That way physical monitors can
be unplugged/plugged and extra ones added without reconfiguring.
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import evdev
from evdev import ecodes
import pyudev

# ---------------- config ----------------
USER_NAME = "{{USER_NAME}}"
USER_UID = {{USER_UID}}
IDLE_TIMEOUT_S = 2 * 60
RESCAN_POLL_S = 2.0

EXCLUDE_INPUT_NAME_RE = re.compile(r"passthrough|uinput|sunshine", re.IGNORECASE)
VIRTUAL_EDID_RE = re.compile(r"virtual|uqd|sunshine", re.IGNORECASE)
RELEVANT_EV_TYPES = {ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS}

DRM_ROOT = Path("/sys/class/drm")
CONNECTOR_RE = re.compile(r"^card\d+-(?P<name>[A-Za-z0-9-]+)$")
# --------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sunshine-idle")


# --------- input device filtering ---------

def is_physical_input_device(dev: evdev.InputDevice) -> bool:
    """A device counts as physical input only if it looks like a real keyboard,
    mouse, tablet, or touchpad — RGB controllers, power buttons, audio jacks,
    Sunshine's virtual uinputs etc. must not reset the idle timer."""
    name = dev.name or ""
    if EXCLUDE_INPUT_NAME_RE.search(name):
        return False
    caps = dev.capabilities()
    keys = set(caps.get(ecodes.EV_KEY, []))
    has_letter_key = ecodes.KEY_A in keys  # real keyboard
    has_mouse_button = bool({ecodes.BTN_LEFT, ecodes.BTN_MOUSE, ecodes.BTN_RIGHT} & keys)
    has_touch = ecodes.BTN_TOUCH in keys
    has_rel_motion = ecodes.EV_REL in caps
    return has_letter_key or has_mouse_button or has_touch or has_rel_motion


# --------- EDID parsing (just enough to pull the product name) ---------

def parse_edid_product_name(edid: bytes) -> str | None:
    """EDID 1.3/1.4 puts up to four 18-byte descriptors starting at offset 54.
    A descriptor with header 00 00 00 FC 00 carries the monitor name (ASCII,
    terminated by 0x0A, padded with 0x20)."""
    if len(edid) < 128:
        return None
    for i in (54, 72, 90, 108):
        d = edid[i:i + 18]
        if len(d) != 18:
            continue
        if d[0:3] == b"\x00\x00\x00" and d[3] == 0xFC:
            raw = d[5:18]
            name = raw.split(b"\x0a", 1)[0].decode("ascii", errors="replace").strip()
            if name:
                return name
    return None


def _connector_path(connector_name: str) -> Path | None:
    """Find the sysfs directory for a connector by name, across any cardN."""
    for alt in DRM_ROOT.glob(f"card*-{connector_name}"):
        return alt
    return None


def classify_connector(connector_name: str) -> tuple[str, str | None]:
    """Returns (status, edid_product_name or None)."""
    base = _connector_path(connector_name)
    if base is None:
        return ("unknown", None)
    try:
        status = (base / "status").read_text().strip()
    except OSError:
        return ("unknown", None)
    try:
        edid = (base / "edid").read_bytes()
    except OSError:
        edid = b""
    name = parse_edid_product_name(edid) if edid else None
    return (status, name)


def list_drm_connectors() -> list[str]:
    """Every DRM connector on every card, de-duplicated."""
    names: set[str] = set()
    for p in DRM_ROOT.iterdir():
        m = CONNECTOR_RE.match(p.name)
        if m:
            names.add(m.group("name"))
    return sorted(names)


def discover_physical_outputs() -> list[str]:
    """Return connector names that are real physical monitors right now.

    Rules:
      - status == 'connected' (kernel sees a cable / forced-connected)
      - EDID is readable (non-empty) — real monitors always publish EDID;
        Sunshine's virtual connector is only non-empty while an EDID override
        is active and gets a 'Virtual'/'UQD' name that we explicitly exclude.
      - If setup.sh wrote /run/sunshine-virt-connector, that connector is
        always excluded (positive marker).
    """
    marked_virt = _marked_virtual_connector()
    physical = []
    for conn in list_drm_connectors():
        status, product = classify_connector(conn)
        if status != "connected":
            continue
        if conn == marked_virt:
            log.debug("  skip marked virtual: %s", conn)
            continue
        if product is None:
            # Connected but no EDID: almost always a kernel-forced virtual
            # connector that is currently idle. Never a real physical monitor.
            log.debug("  skip connected-without-edid: %s (likely virtual)", conn)
            continue
        if VIRTUAL_EDID_RE.search(product):
            log.debug("  skip virtual by name: %s (%s)", conn, product)
            continue
        physical.append(conn)
    return physical


def _marked_virtual_connector() -> str | None:
    try:
        return Path("/run/sunshine-virt-connector").read_text().strip() or None
    except OSError:
        return None


# --------- KScreen control (runs kscreen-doctor as the user) ---------

class KScreenController:
    def __init__(self):
        # Maps connector name -> its replication source name at the moment we
        # disabled it (empty string if it wasn't a replica). Lets us restore
        # the replica binding on wake-up; otherwise KScreen's on-disk config
        # may re-apply an old (pre-stream) layout.
        self._outputs_off: dict[str, str] = {}
        self._last_physical: list[str] = []
        # Backoff on disable failures so we don't spam kscreen-doctor every
        # 2s when user's systemd is degraded.
        self._disable_failures = 0
        self._backoff_until = 0.0
        # Rate-limit the "skip disable, not a replica" log — it's expected
        # in the no-stream state and would otherwise flood the journal.
        self._skipped_not_replica: set[str] = set()

    def _current_replication_source(self, conn: str) -> str:
        """Return the name of the output that `conn` is currently a replica
        of, or '' if it has no replica binding. Parses `kscreen-doctor -o`
        text output — the DBus backend path is unreliable (goes silent on
        long sessions) so we stick with the text layer kscreen-doctor
        itself always produces."""
        import re
        # `kscreen-doctor -o` is invoked through the same machinectl shell
        # path the rest of the daemon uses, so it works regardless of
        # systemd-run/user-DBus health.
        import shlex
        quoted = "-o"
        env_setup = (
            f"export XDG_RUNTIME_DIR=/run/user/{USER_UID} && "
            f"export WAYLAND_DISPLAY=wayland-0 && "
            f"export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{USER_UID}/bus && "
            f"export QT_QPA_PLATFORM=wayland && "
            f"export XDG_CURRENT_DESKTOP=KDE && "
            f"export KDE_FULL_SESSION=true && "
            f"export KDE_SESSION_VERSION=6"
        )
        cmd = [
            "machinectl", "shell", "--quiet", f"--uid={USER_UID}", ".host",
            "/bin/sh", "-c", f"{env_setup} && kscreen-doctor {quoted}",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception as e:
            log.debug("replica query (kscreen-doctor -o) failed: %s", e)
            return ""
        if r.returncode != 0 or not r.stdout:
            return ""
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", r.stdout)
        # Build id → name map AND find `conn`'s replication source id.
        id_to_name: dict[str, str] = {}
        conn_source_id = ""
        current_id = None
        current_is_target = False
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r"Output:\s+(\d+)\s+(\S+)", stripped)
            if m:
                current_id = m.group(1)
                name = m.group(2)
                id_to_name[current_id] = name
                current_is_target = (name == conn)
                continue
            if current_is_target:
                m = re.match(r"replication source:\s*(\d+)", stripped)
                if m:
                    conn_source_id = m.group(1)
        if not conn_source_id or conn_source_id == "0":
            return ""
        return id_to_name.get(conn_source_id, "")

    def _run(self, args: list[str]) -> bool:
        # Run kscreen-doctor inside the user session via `machinectl shell`.
        # machinectl's shell env is minimal — kscreen-doctor needs the
        # Wayland env to talk to KWin; without it Qt falls back to xcb,
        # can't find an X server, and silently fails (returning 0!) so
        # we must export them explicitly.
        import shlex
        quoted = " ".join(shlex.quote(a) for a in args)
        env_setup = (
            f"export XDG_RUNTIME_DIR=/run/user/{USER_UID} && "
            f"export WAYLAND_DISPLAY=wayland-0 && "
            f"export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{USER_UID}/bus && "
            f"export QT_QPA_PLATFORM=wayland && "
            f"export XDG_CURRENT_DESKTOP=KDE && "
            f"export KDE_FULL_SESSION=true && "
            f"export KDE_SESSION_VERSION=6"
        )
        cmd = [
            "machinectl", "shell", "--quiet", f"--uid={USER_UID}", ".host",
            "/bin/sh", "-c", f"{env_setup} && kscreen-doctor {quoted}",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            out = (r.stderr or "") + (r.stdout or "")
            # kscreen-doctor silently returns 0 on Qt plugin failure; treat
            # that as failure too.
            if r.returncode != 0 or "Could not load the Qt platform plugin" in out \
               or "could not connect to display" in out:
                log.warning("kscreen-doctor %s failed: %s", args, out.strip()[:400])
                return False
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("kscreen-doctor error: %s", e)
            return False

    def refresh_physical(self) -> list[str]:
        outputs = discover_physical_outputs()
        if outputs != self._last_physical:
            log.info("physical outputs: %s", outputs or "(none)")
            self._last_physical = outputs
        return outputs

    def off_all(self):
        now = time.monotonic()
        if now < self._backoff_until:
            return
        any_failure = False
        stream_active = Path("/run/sunshine-virt-fallback-connector").exists()
        for out in self.refresh_physical():
            if out in self._outputs_off:
                continue
            source = self._current_replication_source(out)
            # Safety narrowed to during-stream only: when a stream is
            # active, disabling a non-replica output would collapse the
            # stream topology (primary virtual becomes the sole render
            # target, modes drift back to baseline). Outside a stream,
            # disabling a standalone physical is the normal "user walked
            # away, turn the monitor off" case and is safe as long as
            # another output (primary virtual baseline) remains enabled.
            if stream_active and not source:
                if out not in self._skipped_not_replica:
                    log.info("skip disable %s — stream active but not a replica", out)
                    self._skipped_not_replica.add(out)
                continue
            self._skipped_not_replica.discard(out)
            log.info("idle → disable %s (was replica of %r)", out, source or "-")
            # Clear replica binding in the same batch — disabling a replica
            # source without first unbinding triggers KScreen's "Position of
            # enabled output is negative" bug.
            if self._run([f"output.{out}.mirror.none", f"output.{out}.disable"]):
                self._outputs_off[out] = source
                self._disable_failures = 0
            else:
                any_failure = True
        if any_failure:
            self._disable_failures += 1
            # Exponential backoff: 10s, 30s, 1m, 5m, capped at 5m.
            delays = [10, 30, 60, 300]
            delay = delays[min(self._disable_failures - 1, len(delays) - 1)]
            self._backoff_until = now + delay
            log.warning("disable failed %d time(s) in a row; backing off %ds",
                        self._disable_failures, delay)

    def on_all(self):
        if not self._outputs_off:
            return
        # The stream-topology is active while the handler's fallback marker
        # exists. If it's gone, the stream ended while we were idle-off —
        # restoring the saved replica binding would put the monitor at the
        # primary virtual's stale baseline (640×480) instead of native.
        stream_active = Path("/run/sunshine-virt-fallback-connector").exists()
        for out, source in list(self._outputs_off.items()):
            args = [f"output.{out}.enable"]
            if source and stream_active:
                log.info("activity → enable %s (restore replica of %r)", out, source)
                args.append(f"output.{out}.mirror.{source}")
            else:
                preferred = self._preferred_mode(out)
                log.info("activity → enable %s (no active stream; restore native %s)",
                         out, preferred or "(unknown preferred)")
                args.append(f"output.{out}.mirror.none")
                if preferred:
                    args.append(f"output.{out}.priority.1")
                    args.append(f"output.{out}.mode.{preferred}")
            if self._run(args):
                del self._outputs_off[out]
            else:
                # Connector may have vanished (monitor unplugged while off).
                del self._outputs_off[out]

    def _preferred_mode(self, conn: str) -> str:
        """Parse `kscreen-doctor -o` and return the '<WxH>@<R>' string for
        the preferred mode of `conn` (the one marked '!'). '' on failure."""
        import re, shlex
        env_setup = (
            f"export XDG_RUNTIME_DIR=/run/user/{USER_UID} && "
            f"export WAYLAND_DISPLAY=wayland-0 && "
            f"export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{USER_UID}/bus && "
            f"export QT_QPA_PLATFORM=wayland"
        )
        cmd = [
            "machinectl", "shell", "--quiet", f"--uid={USER_UID}", ".host",
            "/bin/sh", "-c", f"{env_setup} && kscreen-doctor -o",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", r.stdout)
        in_block = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("Output:"):
                in_block = (f" {conn} " in line) or line.rstrip().endswith(f" {conn}")
                continue
            if in_block and "Modes:" in line:
                for m in re.finditer(r"\d+:(\d+x\d+@\d+(?:\.\d+)?)(!)?", line):
                    if m.group(2) == "!":
                        w_h, _, rate = m.group(1).partition("@")
                        try:
                            rate_int = int(round(float(rate)))
                        except ValueError:
                            rate_int = rate
                        return f"{w_h}@{rate_int}"
                break
        return ""

    def reconcile(self):
        """Drop tracking entries for outputs that are no longer in the
        state we put them in — e.g. the stream handler re-enabled DP-2
        on disconnect (restore to native), so our 'I disabled DP-2'
        bookkeeping is stale. Without this, a later physical wake
        would try to re-assert a mirror binding that's no longer
        appropriate."""
        if not self._outputs_off:
            return
        for out in list(self._outputs_off.keys()):
            base = _connector_path(out)
            if base is None:
                # Connector vanished entirely
                del self._outputs_off[out]
                continue
            try:
                is_enabled = (base / "enabled").read_text().strip() == "enabled"
            except OSError:
                continue
            if is_enabled:
                log.info("reconcile: %s was re-enabled by someone else, forgetting tracking",
                         out)
                del self._outputs_off[out]


# --------- activity tracking ---------

class ActivityTracker:
    def __init__(self):
        self.last_activity = time.monotonic()

    def mark(self):
        self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_activity


async def watch_device(dev: evdev.InputDevice, tracker: ActivityTracker):
    log.info("  + watching %s (%s)", dev.path, dev.name)
    try:
        async for event in dev.async_read_loop():
            if event.type in RELEVANT_EV_TYPES:
                tracker.mark()
    except OSError as e:
        log.info("  - device %s gone: %s", dev.path, e)
    finally:
        try:
            dev.close()
        except Exception:
            pass


class DeviceSupervisor:
    def __init__(self, tracker: ActivityTracker):
        self.tracker = tracker
        self.tasks: dict[str, asyncio.Task] = {}

    def _add(self, path: str):
        if path in self.tasks:
            return
        try:
            dev = evdev.InputDevice(path)
        except (OSError, PermissionError) as e:
            log.debug("open %s failed: %s", path, e)
            return
        if not is_physical_input_device(dev):
            dev.close()
            return
        task = asyncio.create_task(watch_device(dev, self.tracker))
        self.tasks[path] = task

    def _remove(self, path: str):
        t = self.tasks.pop(path, None)
        if t and not t.done():
            t.cancel()

    def scan_initial(self):
        for path in evdev.list_devices():
            self._add(path)
        log.info("initial device scan: watching %d device(s)", len(self.tasks))


# --------- udev monitor: handles input AND drm hotplug ---------

async def udev_loop(supervisor: DeviceSupervisor, controller: KScreenController):
    ctx = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by(subsystem="input")
    monitor.filter_by(subsystem="drm")
    monitor.start()
    loop = asyncio.get_running_loop()
    fd = monitor.fileno()

    def drain():
        while True:
            d = monitor.poll(timeout=0)
            if d is None:
                return
            subsys = d.subsystem
            action = d.action
            if subsys == "input":
                node = d.device_node
                if not node or not node.startswith("/dev/input/event"):
                    continue
                if action == "add":
                    loop.call_later(0.15, supervisor._add, node)
                elif action == "remove":
                    supervisor._remove(node)
            elif subsys == "drm":
                # Connector change events: rescan on next idle tick anyway,
                # but log for visibility.
                if action in ("change", "add", "remove"):
                    log.info("drm hotplug: %s %s", action, d.sys_path)

    while True:
        fut: asyncio.Future = loop.create_future()
        def _ready():
            if not fut.done():
                fut.set_result(None)
        loop.add_reader(fd, _ready)
        try:
            await fut
        finally:
            loop.remove_reader(fd)
        drain()


# --------- main loop ---------

async def idle_loop(tracker: ActivityTracker, controller: KScreenController, timeout_s: float):
    while True:
        await asyncio.sleep(RESCAN_POLL_S)
        controller.reconcile()
        if tracker.idle_for() >= timeout_s:
            controller.off_all()
        else:
            controller.on_all()


async def main_async(args):
    if os.geteuid() != 0 and not args.dry_run:
        log.error("must run as root (needs to open all /dev/input/event* devices)")
        return 1

    tracker = ActivityTracker()
    controller = KScreenController()
    supervisor = DeviceSupervisor(tracker)
    supervisor.scan_initial()
    controller.refresh_physical()

    if args.dry_run:
        log.warning("DRY RUN — not toggling displays")
        controller._run = lambda args_: (log.info("(dry) kscreen-doctor %s", args_), True)[1]

    await asyncio.gather(
        udev_loop(supervisor, controller),
        idle_loop(tracker, controller, args.timeout),
    )
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--timeout", type=float, default=IDLE_TIMEOUT_S,
                   help="idle seconds before disabling physical monitors")
    p.add_argument("--dry-run", action="store_true",
                   help="log what would happen, don't change displays")
    args = p.parse_args()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        rc = 0
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
