# Sunshine / Moonlight Streaming — Custom Setup

Everything in this setup is designed to be **hardware-agnostic**: no device
names, USB IDs, monitor names, or connector names are hardcoded. Discovery is
done at runtime (EDID parsing, evdev capability inspection, DRM sysfs scan,
udev hotplug events).

## Goals this setup addresses

1. **Moonlight connection always possible**, even if the physical monitor is in
   DPMS-off / KDE hasn't lit it up yet. Sunshine captures a virtual display
   that is kept permanently active.
2. **Physical monitor behaves like a standalone machine's screen** — it goes
   dark after real user inactivity at the PC, does *not* wake up just because
   a Moonlight client is sending keystrokes.
3. **Graceful recovery** — if Sunshine crashes or a session ends unexpectedly,
   the physical monitor comes back automatically so the user isn't locked
   out.
4. **Emergency escape hatch** — `Ctrl+Alt+Shift+Q` works at the lock screen
   and SDDM greeter to kill Sunshine, restore the display, and let the user
   log in normally.

## Components at a glance

| Path | Purpose |
|---|---|
| `/usr/local/bin/sunshine-emergency-stop` | Tears down the stream, kills the Sunshine user service, notifies the user, restarts Sunshine. Safe to run any time. |
| `/usr/local/bin/sunshine-hotkey-daemon.py` | Runs as root, watches every physical keyboard for `Ctrl+Alt+Shift+Q`, launches emergency-stop. Session-independent → works at lock/SDDM. |
| `/etc/systemd/system/sunshine-hotkey.service` | Starts the hotkey daemon at boot. |
| `/usr/local/bin/sunshine-idle-daemon.py` | Runs as root, watches physical input devices only (filters Sunshine's uinput virtual devices by name + capability shape), powers physical monitors off/on via `kscreen-doctor` when user is truly idle. DRM + udev hotplug aware. |
| `/etc/systemd/system/sunshine-idle.service` | Starts the idle daemon. |
| `/etc/systemd/system-sleep/sunshine-sleep-handler` | Pre/post suspend hook. Tears down Sunshine session before suspend and restarts it after resume. Ceiling ~12 s with SIGKILL fallback. |
| `/etc/systemd/system/systemd-suspend.service.d/timeout.conf` | Raises suspend service timeout to 45 s so the sleep-handler has headroom. Applied to `systemd-suspend`, `systemd-hybrid-sleep`, `systemd-suspend-then-hibernate`. |
| `~/.local/bin/sunshine-idle-watchdog.sh` | User-level periodic check for stale virtual-display state after a client disconnect. |
| `~/.config/systemd/user/sunshine-idle-watchdog.{service,timer}` | Runs the watchdog every 30 s while Sunshine is active. |
| `~/.local/bin/sunshine-display-setup.sh` | Sunshine `prep_cmd` Do — sets idle inhibitor, invokes the virt-display-handler with the client's resolution. |
| `~/.local/bin/sunshine-display-teardown.sh` | Sunshine `prep_cmd` Undo — releases idle inhibitor, restores the baseline placeholder. |
| `/usr/local/bin/sunshine-virt-display-handler` | Python helper called by the wrappers. Finds the virtual connector dynamically (marker file + EDID heuristic), generates a matching EDID via `gen_edid`, writes it to debugfs `edid_override`, cycles the connector off/on so KWin re-reads the mode list, then applies the new mode + off-screen position via kscreen-doctor. Does NOT touch physical monitors. |
| `~/.local/share/sunshine_virt_display/gen_edid.py` | Used only as a library for EDID byte generation; its `main.py` connect/disconnect flow is NOT used anymore (it forcibly turned off physical monitors via sysfs `status`). |
| `/etc/sudoers.d/sunshine-virt-handler` | NOPASSWD sudo rule so the Sunshine user wrappers can call the handler. |
| `/run/sunshine-virt-connector` | Runtime marker file: name of the current virtual connector. Written by the handler on connect; read by both handler and idle-daemon for positive identification. |

## KDE power settings

Set once via `kwriteconfig6` — disables KDE's built-in DPMS/dim on *all* power
profiles (AC, Battery, LowBattery) so only the idle daemon decides when the
physical monitor goes off:

```
kwriteconfig6 --file powermanagementprofilesrc --group {AC,Battery,LowBattery} \
    --group {DPMSControl,DimDisplay} --key idleTime 2147483
```

Lock screen / auto-lock are *not* touched — Plasma's `kscreenlockerrc` keeps
its normal timeouts. The idle daemon only powers the display, never unlocks.

## Hardware-agnostic detection strategy

- **Physical input device** = evdev device whose capabilities contain a real
  keyboard letter (`KEY_A`), a mouse button (`BTN_LEFT/MOUSE/RIGHT`), a touch
  button, or relative motion — AND whose `name` does *not* match
  `passthrough|uinput|sunshine`.
- **Physical monitor connector** = DRM connector reported as `connected` that
  (a) has a readable EDID AND (b) whose EDID product name does *not* match
  `virtual|uqd|sunshine`. Connectors that are `connected` but have no EDID
  are almost always kernel-forced virtuals and are excluded.
- **Hotplug** — the daemon subscribes to udev events for both `input` and
  `drm` subsystems, so plugging a new keyboard or monitor (or losing one) is
  picked up without a restart.

## Emergency hotkey

`Ctrl+Alt+Shift+Q` on any physical keyboard:
1. runs display teardown (restores physical monitor)
2. stops the Sunshine user service (kills any active Moonlight session)
3. sends a KDE notification
4. restarts Sunshine so the next connect works without manual steps
5. does **not** unlock the lock screen — lock stays up for security

## Verification commands

```
# Idle daemon live log
sudo journalctl -u sunshine-idle -f

# Hotkey daemon live log
sudo journalctl -u sunshine-hotkey -f

# Current physical output detection
sudo python3 -c "import sys; sys.path.insert(0, '/usr/local/bin'); \
    import importlib.util, types; \
    spec = importlib.util.spec_from_file_location('d', '/usr/local/bin/sunshine-idle-daemon.py'); \
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); \
    print(m.discover_physical_outputs())"

# Sleep-handler manual test (does NOT suspend)
sudo /etc/systemd/system-sleep/sunshine-sleep-handler pre suspend
sudo /etc/systemd/system-sleep/sunshine-sleep-handler post suspend
```

## Where to edit behavior

- Idle timeout: `IDLE_TIMEOUT_S` in `/usr/local/bin/sunshine-idle-daemon.py`
- Hotkey combo: `TRIGGER_KEY` / `MODIFIER_KEYS` in
  `/usr/local/bin/sunshine-hotkey-daemon.py`
- Virtual-name regex (for EDID and input device filtering):
  `VIRTUAL_EDID_RE` / `EXCLUDE_INPUT_NAME_RE` in the idle daemon;
  `EXCLUDE_NAME_RE` in the hotkey daemon.

## Sunshine output_name

`output_name` in `~/.config/sunshine/sunshine.conf` must name the **virtual**
connector (currently `DP-3`). If it's set to `0` (first monitor) or to the
physical connector, Sunshine will capture the wrong display and the client
stream will show the physical monitor's content fitted into the client's
aspect ratio (observed as "widescreen squashed into 640×640" with a square
client).

If the virtual connector's kernel name ever changes (driver/kernel updates
sometimes re-enumerate DRM connectors), check `/run/sunshine-virt-connector`
for the current name and update `output_name` accordingly. Sunshine restart
needed after any change (`systemctl --user restart app-dev.lizardbyte.app.Sunshine.service`).

## KScreen baseline

Applied once via `kscreen-doctor` (persisted by KScreen to
`~/.local/share/kscreen/outputs/`):

- Virtual connector (currently DP-3) is **enabled**, at **640×480@60**,
  position **(4000, 4000)** — Extended Desktop far outside the primary's
  bounds so the cursor can't naturally reach it.
- Physical connector (currently DP-2) is primary at native resolution.

During a stream, the handler installs a **three-output topology**:

```
  DP-3  (primary virtual — Plasma renders here, Sunshine captures)
   ├── DP-1  (fallback virtual — replica of DP-3, injected per stream;
   │         keeps KWin rendering even when DP-2 is disabled for idle-off)
   └── DP-2  (physical LG — replica of DP-3; can idle-off during stream)
```

**Why two virtual connectors?** KWin stops rendering the Plasma compositor
when the only enabled output is a single replica source. With a second
virtual always enabled as a replica, KWin has two live render targets →
keeps rendering even when DP-2 is disabled. This is what makes idle-off on
the physical monitor safe during streaming.

**On connect:** handler injects the client-resolution EDID into the primary
virtual (DP-3) AND into a fallback DRM connector (preferring a
currently-disconnected DisplayPort connector — typically DP-1), kicks both
to re-read EDID, then applies the 3-output topology atomically:

```
kscreen-doctor \
    output.DP-3.priority.1 output.DP-3.enable output.DP-3.mode.<mode> \
                           output.DP-3.position.0,0 \
    output.DP-1.priority.2 output.DP-1.enable output.DP-1.mode.<mode> \
                           output.DP-1.position.0,0 output.DP-1.mirror.DP-3 \
    output.DP-2.priority.3 output.DP-2.enable output.DP-2.position.0,0 \
                           output.DP-2.mirror.DP-3
```

**On disconnect:** handler breaks all mirror bindings (otherwise disabling
a replica source triggers the "negative position" KWin bug), disables and
tears down the fallback virtual (clears its EDID override so its DRM
status goes back to `disconnected`), and restores DP-2 as priority-1 at
its native mode.

**Mirror command syntax (Plasma 6.6+):**

```
kscreen-doctor output.<target>.mirror.<source>   # make target a replica of source
kscreen-doctor output.<target>.mirror.none       # clear replica binding
```

## Verification: full round-trip

```
# Simulate client connect at 1920x1080@60 — DP-3 should switch modes
SUNSHINE_CLIENT_WIDTH=1920 SUNSHINE_CLIENT_HEIGHT=1080 SUNSHINE_CLIENT_FPS=60 \
    ~/.local/bin/sunshine-display-setup.sh
kscreen-doctor -o | grep -A2 DP-3

# Simulate disconnect — DP-3 should return to 640x480 baseline
~/.local/bin/sunshine-display-teardown.sh
kscreen-doctor -o | grep -A2 DP-3
```

## Still open

- KWin rule to prevent windows from being placed on the virtual connector.
  Not critical because the (4000, 4000) position keeps the output outside
  reachable cursor territory; if a rogue app still tries to spawn there we
  can add a rule to `~/.config/kwinrulesrc`.
- Live end-to-end test with an actual Moonlight client at varying resolutions
  (phone / laptop / Steam Deck).
