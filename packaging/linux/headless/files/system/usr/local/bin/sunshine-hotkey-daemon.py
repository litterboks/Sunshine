#!/usr/bin/env python3
"""Listen on physical keyboards for session-independent Sunshine hotkeys:

  Ctrl+Alt+Shift+Q  → sunshine-emergency-stop
                      (graceful: stops stream, restarts Sunshine)
  Ctrl+Alt+Shift+R  → sunshine-emergency-reset
                      (hard reset: works even when user D-Bus is broken —
                       clears virtual DRM overrides at sysfs level, kills
                       stuck kscreen processes, restores physical monitor)

Runs as root, so the combos also fire at the SDDM greeter and KDE lock
screen. Virtual input devices created by Sunshine itself (name contains
"passthrough" or "uinput") are ignored so remote clients can't trigger.
"""

import asyncio
import logging
import os
import re
import subprocess
import sys

import evdev
from evdev import ecodes
import pyudev

ACTIONS = {
    ecodes.KEY_Q: "/usr/local/bin/sunshine-emergency-stop",
    ecodes.KEY_R: "/usr/local/bin/sunshine-emergency-reset",
}
TRIGGER_KEYS = set(ACTIONS)
MODIFIER_KEYS = {
    "ctrl": {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL},
    "alt": {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT},
    "shift": {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT},
}
EXCLUDE_NAME_RE = re.compile(r"passthrough|uinput|sunshine", re.IGNORECASE)
COOLDOWN_S = 3.0  # avoid re-firing while the script is still running

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sunshine-hotkey")


def find_keyboards():
    """Return evdev devices that look like real keyboards (have KEY_Q)
    and aren't virtual devices from Sunshine."""
    found = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (OSError, PermissionError) as e:
            log.warning("skip %s: %s", path, e)
            continue
        if EXCLUDE_NAME_RE.search(dev.name or ""):
            log.info("ignore virtual device: %s (%s)", path, dev.name)
            continue
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if TRIGGER_KEYS & set(caps):
            log.info("watching %s (%s)", path, dev.name)
            found.append(dev)
        else:
            dev.close()
    return found


class HotkeyState:
    def __init__(self):
        # Track modifier keys as a set of currently-pressed keycodes, not booleans,
        # so releasing one side doesn't clear the state while the other is held.
        self.pressed = {name: set() for name in MODIFIER_KEYS}
        self.last_fire = 0.0

    def update_modifier(self, keycode: int, pressed: bool):
        for name, codes in MODIFIER_KEYS.items():
            if keycode in codes:
                if pressed:
                    self.pressed[name].add(keycode)
                else:
                    self.pressed[name].discard(keycode)
                return

    def all_modifiers_held(self) -> bool:
        return all(self.pressed[name] for name in MODIFIER_KEYS)

    def can_fire(self, now: float) -> bool:
        return now - self.last_fire >= COOLDOWN_S


def fire(action_path: str):
    log.warning("HOTKEY fired — launching %s", action_path)
    try:
        subprocess.Popen([action_path], close_fds=True)
    except OSError as e:
        log.error("failed to launch %s: %s", action_path, e)


async def watch(dev: evdev.InputDevice, state: HotkeyState):
    try:
        async for event in dev.async_read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            keycode = event.code
            # value: 0=up, 1=down, 2=hold
            if event.value == 1:
                state.update_modifier(keycode, pressed=True)
                if keycode in ACTIONS and state.all_modifiers_held():
                    loop = asyncio.get_running_loop()
                    now = loop.time()
                    if state.can_fire(now):
                        state.last_fire = now
                        fire(ACTIONS[keycode])
            elif event.value == 0:
                state.update_modifier(keycode, pressed=False)
    except OSError as e:
        log.warning("device %s gone: %s", dev.path, e)


class KeyboardSupervisor:
    """Keeps a watch-task per live physical keyboard. Adds/removes tasks
    as devices come and go via udev (important for KVM-switch setups where
    keyboards only appear when the KVM routes them to this machine)."""

    def __init__(self, state: "HotkeyState"):
        self.state = state
        self.tasks: dict[str, asyncio.Task] = {}

    def _add(self, path: str) -> None:
        if path in self.tasks:
            return
        try:
            dev = evdev.InputDevice(path)
        except (OSError, PermissionError):
            return
        if EXCLUDE_NAME_RE.search(dev.name or ""):
            dev.close()
            return
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if not (TRIGGER_KEYS & set(caps)):
            dev.close()
            return
        log.info("+ watching %s (%s)", path, dev.name)
        self.tasks[path] = asyncio.create_task(watch(dev, self.state))

    def _remove(self, path: str) -> None:
        t = self.tasks.pop(path, None)
        if t and not t.done():
            t.cancel()

    def scan_initial(self) -> None:
        for path in evdev.list_devices():
            self._add(path)
        log.info("initial scan: watching %d device(s)", len(self.tasks))


async def udev_loop(sup: "KeyboardSupervisor") -> None:
    ctx = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by(subsystem="input")
    monitor.start()
    loop = asyncio.get_running_loop()
    fd = monitor.fileno()

    def drain():
        while True:
            d = monitor.poll(timeout=0)
            if d is None:
                return
            node = d.device_node
            if not node or not node.startswith("/dev/input/event"):
                continue
            if d.action == "add":
                loop.call_later(0.15, sup._add, node)
            elif d.action == "remove":
                sup._remove(node)

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


async def main():
    state = HotkeyState()
    sup = KeyboardSupervisor(state)
    sup.scan_initial()
    log.info("Ctrl+Alt+Shift+Q=emergency-stop  Ctrl+Alt+Shift+R=emergency-reset")
    await udev_loop(sup)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
