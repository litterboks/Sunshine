# Headless streaming — behavior specification

This is the target behavior for the headless-streaming setup shipped under
`packaging/linux/headless/`. Tested on Bazzite + KDE Plasma 6.6+ Wayland
with NVIDIA but written to be hardware-agnostic.

The setup uses three logical roles. Concrete connectors (DP-1, HDMI-A-1, …)
are picked at runtime by the handler — never hard-coded.

| Role | Lifetime | Purpose |
|---|---|---|
| **VirtualMainScreen** | Active only during a stream | KDE's primary output during a stream; this is the surface Sunshine captures. |
| **SunshineVirtualScreen** | Active only during a stream | Holds the EDID with the Moonlight client's exact resolution; VirtualMainScreen mirrors its mode. Also keeps the compositor rendering when the physical monitor is idle-off. |
| **Physical Display** | Always present (it's the actual hardware) | The user's real monitor. Off-stream it behaves like an ordinary Linux display. On-stream it becomes a replica of VirtualMainScreen. Idle-off and wake are independent of the stream. |

Any *additional* physical monitors (a second LG, a TV on HDMI, etc.) are
left alone — KDE manages them as ordinary Linux displays in every state
below.

---

## States

### S0 — Off-stream (idle)

```
Physical Display(s)  -> ordinary Linux outputs at native resolution
VirtualMainScreen    -> does not exist
SunshineVirtualScreen-> does not exist
```

KDE behaves exactly as a vanilla Linux desktop. No virtual display is
active. No mirroring is forced. Multi-monitor users see their normal
extended-desktop layout. This is the steady state when no Moonlight client
is connected.

### S1 — Stream starts

Triggered by Sunshine's `prep_cmd Do` when a Moonlight client connects.

1. **SunshineVirtualScreen** is created, EDID-injected with the client's
   `WIDTH × HEIGHT @ FPS`.
2. **VirtualMainScreen** is created.
3. KDE topology is reconfigured so:
   - VirtualMainScreen is the **primary** at `(0,0)`, mode = client res.
   - SunshineVirtualScreen mirrors VirtualMainScreen.
   - The primary physical display mirrors VirtualMainScreen at the same
     mode (driver may scale internally if the panel can't natively
     accept the client mode).
4. Sunshine begins capturing VirtualMainScreen.

```
VirtualMainScreen     primary, client-res, captured by Sunshine
 ├─ SunshineVirtualScreen   replica of VirtualMainScreen
 └─ Primary Physical        replica of VirtualMainScreen
Other physical displays     untouched (regular Linux outputs)
```

### S2 — Physical idle (during a stream)

The user walks away. The OS-level idle timer DPMS-off's the primary
physical display. Stream is unaffected.

```
VirtualMainScreen        active, captured by Sunshine
SunshineVirtualScreen    active, mirroring VirtualMainScreen
Primary Physical         DPMS off
Other physical displays  unchanged
```

The compositor keeps rendering because two virtual outputs are still
enabled. Moonlight client sees no interruption.

### S3 — Physical wakes (during a stream)

User comes back, presses any key. Physical display wakes.

1. Physical Display is re-enabled.
2. It immediately becomes a replica of VirtualMainScreen again,
   adopting whatever resolution VirtualMainScreen is currently at
   (= client res).
3. SunshineVirtualScreen is **not** affected.

State is identical to S1.

### S4 — Stream ends

Triggered by Sunshine's `prep_cmd Undo` (or emergency-stop) when the
Moonlight client disconnects.

1. Physical Display's mirror binding is removed; it returns to its
   native mode and acts as an ordinary Linux output.
2. SunshineVirtualScreen is torn down (DRM connector deactivated, EDID
   override cleared).
3. VirtualMainScreen is torn down.
4. KDE returns to the off-stream layout.

Final state == S0.

---

## Invariants

The implementation must hold these regardless of state transitions or
race conditions:

1. **Off-stream is pure Linux.** No virtual displays, no forced
   mirrors, no custom modes lingering. The user's normal multi-monitor
   layout is restored exactly.
2. **Stream survives physical idle-off.** S1 → S2 → S1 must not
   interrupt capture, drop frames in a way visible to the client, or
   change the client-resolution EDID.
3. **Wake-up never grows the stream resolution.** Physical waking up
   in S3 must not switch VirtualMainScreen's mode — the mirror binding
   is from physical → VirtualMainScreen, not the other way around.
4. **Additional physical monitors are never touched.** The handler only
   acts on the connectors it discovered for the three roles. Anything
   else is left in whatever state KDE/the user configured.
5. **Connector identity is stable across one stream session.** The
   connector chosen for VirtualMainScreen at S1 is the same one Sunshine
   captures and the same one teardown clears at S4 — even if other
   displays plug/unplug mid-session.
6. **Emergency hotkeys reach S0.** `Ctrl+Alt+Shift+R` from anywhere
   (including SDDM / lock screen) returns the system to S0 and
   restarts Sunshine, even when the user's D-Bus session is wedged.

---

## Failure modes that are explicitly out of scope

- The client never reaches S1 if the GPU has fewer DRM connectors than
  the setup needs. The handler logs the missing connector and refuses.
- A connector that vanishes mid-session (cable yanked) is treated as
  an unrecoverable error for that session — Sunshine ends the stream
  via its existing client-disconnect path.
- Off-stream behavior is whatever KDE does. We don't try to "fix"
  enumeration order, EDID emulation modes, or user multi-monitor layout
  decisions when no stream is running.

---

## Where each role lives in the code

| Role | Implementation |
|---|---|
| Stream-start S1 sequence | `files/system/usr/local/bin/sunshine-virt-display-handler` `connect` subcommand |
| Stream-end S4 sequence | same handler, `disconnect` subcommand |
| Physical idle (S1→S2) | `files/system/usr/local/bin/sunshine-idle-daemon.py` |
| Physical wake (S2→S3) | `files/system/usr/local/bin/sunshine-idle-daemon.py` (evdev) |
| Hotkey reset (any → S0) | `files/system/usr/local/bin/sunshine-hotkey-daemon.py` and `sunshine-emergency-{stop,reset}` |
| Suspend/resume around streams | `files/system/etc/systemd/system-sleep/sunshine-sleep-handler` |
| `prep_cmd Do/Undo` glue | `files/user/.local/bin/sunshine-display-{setup,teardown}.sh` |

A more verbose architecture walkthrough — three-output topology rationale,
EDID-injection mechanism, debug commands — is in `docs/sunshine-setup.md`.
The historical state-machine spec (with idle-off semantics that match the
older "always-on virtual" mental model) is in `docs/behavior-spec.md`;
where it conflicts with this document, **this document is authoritative**.
