# sunshine-headless

Hardware-agnostic Sunshine / Moonlight setup on **KDE Plasma 6.6+ Wayland** (tested on Bazzite with an NVIDIA GPU) that gives you:

- **Cloned streaming** — physical monitor and the streamed virtual display show the same content at the client's resolution.
- **Idle-off of the physical monitor during a stream** — when you walk away from the PC the main monitor goes into real DPMS standby, but the Moonlight stream keeps running thanks to a second virtual display that keeps the compositor alive.
- **Automatic wake-up** — the first physical keystroke after coming back re-enables the main monitor and restores the mirror binding to the stream.
- **Emergency hotkeys** that work at the lock screen and SDDM greeter:
  - `Ctrl+Alt+Shift+Q` — graceful: stops the stream and restarts Sunshine.
  - `Ctrl+Alt+Shift+R` — hard reset: clears virtual-display DRM overrides at sysfs level, kills stuck kscreen processes, restores the physical monitor. Works even when the user's D-Bus session is wedged and the Q-hotkey can't get through.
- **Suspend-safe** — ends the Sunshine session cleanly before suspend and restarts it after resume.
- **No hardcoded device or connector names** — everything is discovered at runtime via evdev capabilities, EDID parsing and DRM sysfs.

## Architecture (while streaming)

```
DP-3  primary virtual, Plasma renders here, Sunshine captures
 ├── DP-1 / HDMI-A-1  second virtual, replica of DP-3
 │                    keeps KWin rendering if the physical is idle-off
 └── DP-2 (LG, or whatever) physical monitor, replica of DP-3
                    can idle-off any time without killing the stream
```

- **[SPEC.md](SPEC.md)** — **authoritative** target behavior: roles, state
  machine (S0 off-stream → S1 stream → S2 idle → S3 wake → S4 end),
  invariants, code-location map. Start here.
- **[docs/sunshine-setup.md](docs/sunshine-setup.md)** (also installed to
  `/etc/sunshine-setup.md`) — architecture walkthrough: three-output
  topology, EDID injection strategy, connect/disconnect flow, debugging
  commands.
- **[docs/behavior-spec.md](docs/behavior-spec.md)** — historical
  state-machine spec written against an earlier "always-on virtual"
  mental model. Kept for reference; where it conflicts with `SPEC.md`,
  `SPEC.md` wins.

## Requirements

- **KDE Plasma 6.6** or newer — `kscreen-doctor` with `mirror`, `addCustomMode`, `removeCustomMode` subcommands
- NVIDIA or any GPU exposing at least one extra DRM connector you can force-activate via debugfs `edid_override` (typically HDMI or DisplayPort ports you're not using)
- Python packages: `python3-evdev`, `python3-pyudev`, `python3-dbus`
- `edid-decode`, `git`
- **Sunshine with the `linux/kms` connector-name patch** — the installer
  writes a DRM connector name (e.g. `output_name = DP-2`) into
  `sunshine.conf`, which only resolves on a Sunshine build that includes
  the patch from the `fix/linux-kms-connector-name-resolution` branch of
  this fork. Stock upstream Sunshine treats non-numeric `output_name` as
  garbage; the installer falls back to `output_name = 1` (with a warning)
  if connector-name detection fails.

On Bazzite (rpm-ostree), first install deps:

```
rpm-ostree install python3-evdev python3-pyudev python3-dbus edid-decode
sudo systemctl reboot
```

## Install

```
git clone https://github.com/<you>/sunshine-headless.git
cd sunshine-headless
./install.sh
```

The installer:

1. Checks all dependencies
2. Clones `sunshine_virt_display` (gen_edid.py) into `~/.local/share/`
3. Substitutes your username / UID / home path into all files
4. Installs system files to `/usr/local/bin`, `/etc/systemd/system`, `/etc/sudoers.d`
5. Installs user files to `~/.local/bin` and `~/.config/systemd/user`
6. Disables KDE's built-in screen-off / dim timers (our idle daemon takes over)
7. Configures Sunshine's `output_name = 1` and the `global_prep_cmd` hook (existing sunshine.conf is backed up)
8. Enables + starts all services
9. Restarts Sunshine

The **first Moonlight connect** injects the client resolution into the virtual display and sets up the replica topology automatically.

## Uninstall

```
./uninstall.sh
```

Removes services and scripts. Leaves `sunshine.conf` and the `sunshine_virt_display` clone in place.

## Debug

```
sudo journalctl -u sunshine-hotkey -f          # emergency-hotkey daemon
sudo journalctl -u sunshine-idle -f            # idle-detection daemon
journalctl --user -u app-dev.lizardbyte.app.Sunshine.service -f
kscreen-doctor -o                              # current output topology
```

## Caveats

- **LG OSD**: set "Aspect Ratio" / "Just Scan" mode (not "Full Wide") if you stream at non-native aspect ratios — otherwise the physical monitor stretches the streamed content.
- **NVIDIA driver is picky about custom modes**: exotic client resolutions (square or very small) may be rejected; handler falls back to a scaled-replica layout in that case.
- **DP-1 / HDMI-A-1 injection**: the second virtual uses one of your currently-disconnected DRM connectors. If you plug in a real monitor there later, restart the Sunshine handler to pick a different fallback.
- **KScreen auto-reapply** can occasionally revert the topology on hotplug events. If that happens, a Moonlight reconnect re-triggers the handler and restores everything.
- **Idle-daemon compositor deadlock** (rare): disabling the primary physical monitor via `kscreen-doctor` while it is a replica source has on occasion deadlocked KWin + input on NVIDIA + Plasma 6.6. If it happens, SSH in from another machine and `systemctl --user restart plasma-kwin_wayland.service` or do a hard reboot. Worth keeping an eye on the daemon log for clues.

## License

MIT — use freely.
