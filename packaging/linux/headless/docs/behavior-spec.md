# Desired Behavior — State Machine

This document is the **target specification** for how the system should
behave across all combinations of: physical-monitor state, stream state,
and wake-up trigger. It is the spec the daemons and handlers implement
against.

## 1. Components

| Name | Purpose | EDID source |
|------|---------|------------|
| **DP-2** (physical, LG) | User's actual monitor | Physical hardware EDID |
| **DP-3** (primary virtual) | Where Plasma renders while streaming; Sunshine captures this | Injected via debugfs `edid_override` by the handler (or baseline 640×480 otherwise) |
| **DP-1** (fallback virtual) | Secondary replica of DP-3 that keeps KWin rendering when DP-2 is idle-off — exists only during an active stream | Injected on stream connect, removed on disconnect |

The assignment of which physical DRM connector plays each virtual role
is discovered at runtime, but the three logical roles are always the
same. "DP-1 / DP-3" in the text below stand for whichever connectors
are currently playing those roles.

## 2. State variables

Two independent axes:

- **Physical monitor (DP-2)**: `active` | `idle-off`
- **Stream**: `disconnected` | `connected`

That's 4 combined states. Plus the transient in-between during
transitions.

## 3. Target state for each combination

### 3.1 `Stream disconnected` × `DP-2 active`  — idle desktop work

```
DP-2   enabled, native 3440×1440, priority 1, NOT a replica
DP-3   enabled, baseline 640×480, off-screen position, NOT a replica
DP-1   disconnected (no EDID override, not in KScreen)
```

User sees normal Plasma on the LG. DP-3 is parked small so Sunshine
has something to capture if someone connects. The idle daemon is
watching but **does NOT disable DP-2** in this state (see 4.1).

### 3.2 `Stream disconnected` × `DP-2 idle-off` — user away, no stream

Reached either by:
- The daemon's idle timer firing while DP-2 is at native (no stream
  running, user walked away), or
- The stream ending while DP-2 was already idle-off — the handler
  leaves DP-2 disabled (§4.4 step 3).

While in this state: DP-2 hardware is in DPMS standby. DP-3 is at
baseline 640×480 off-screen. DP-1 has been torn down. DP-3 alone is
a valid render target for KWin so nothing freezes. The state resolves
on the next physical keystroke (§4.2 "no active stream" branch
restores DP-2 to native) or on a new stream connect.

### 3.3 `Stream connected` × `DP-2 active` — streaming, user also at the PC

```
DP-2   enabled, client-res, priority 3, replica of DP-3
DP-3   enabled, client-res, priority 1 (primary)
DP-1   enabled, client-res, priority 2, replica of DP-3  (fallback)
```

LG shows exactly what the client sees. DP-3 is primary; Sunshine
captures it. DP-1 is active as a second replica, invisible to the user,
used only to keep KWin rendering if DP-2 goes away.

### 3.4 `Stream connected` × `DP-2 idle-off` — streaming, user walked away

```
DP-2   disabled (DPMS standby on the LG panel)
DP-3   enabled, client-res, priority 1 (primary, still captured)
DP-1   enabled, client-res, priority 2, replica of DP-3
```

Client keeps receiving frames because DP-3 is still rendered and
captured. DP-1 as a replica ensures KWin doesn't stop rendering in
single-output mode. The daemon has stored `{DP-2: source=DP-3}`
internally so it can restore the binding on wake-up.

## 4. Transitions

### 4.1 Physical idle (no physical keyboard/mouse activity for N minutes)

Disables physical outputs to send them into DPMS standby. The safety
guard differs depending on whether a stream is running:

```
stream_active = /run/sunshine-virt-fallback-connector exists
pre-check:
  stream_active AND DP-2.replication_source == 0:
    # Stream is running but DP-2 is not a replica — disabling would
    # collapse the topology to only the primary virtual (mode drifts
    # back to baseline, client gets 640×480). Skip.
    skip; log "skip disable — stream active but not a replica"
  otherwise:
    atomic: output.DP-2.mirror.none && output.DP-2.disable
    remember {DP-2: source=<replica target or "">}
```

Outside a stream, disabling a standalone DP-2 is safe: the primary
virtual (baseline EDID) remains enabled as KWin's render target.

### 4.2 Physical activity (any real keyboard/mouse event)

Reset idle timer. If any outputs are in `_outputs_off`, wake them:

```
stream_active = /run/sunshine-virt-fallback-connector exists
for each (out, source) in _outputs_off:
    if source AND stream_active:
        # Stream still running. Restore the replica binding so the
        # physical monitor immediately rejoins the clone at client-res.
        atomic: output.<out>.enable
                output.<out>.mirror.<source>
    else:
        # No active stream (stream ended while we were idle-off, or the
        # output was never a replica). Come back at native, not at the
        # primary virtual's baseline.
        atomic: output.<out>.enable
                output.<out>.mirror.none
                output.<out>.priority.1
                output.<out>.mode.<preferred>
```

Enable + mirror binding are applied in the SAME kscreen-doctor call so
KWin never re-evaluates the layout with the output enabled-but-not-
replica. `<preferred>` comes from parsing `kscreen-doctor -o` (the
mode flagged with `!`), so we don't depend on the DBus backend being
healthy.

### 4.3 Stream connects (Moonlight client pairs & starts)

Sunshine fires the `prep_cmd` → `sunshine-display-setup.sh` →
`sunshine-virt-display-handler connect --width W --height H --refresh R`.

The handler:

1. Finds the primary virtual connector (marker file, or first
   disconnected DP-* with EDID override available).
2. Finds a fallback virtual connector (second disconnected DP-* /
   HDMI-*, preferring DP-*).
3. Injects a `(W, H, R)` EDID into both connectors via debugfs, kicks
   both status toggles so KWin re-reads EDIDs.
4. Waits up to 5 s for KScreen to see the new modes.
5. Atomic kscreen-doctor batch:
   - `DP-3 priority.1 enable mode.WxH@R position.0,0`
   - `DP-1 priority.2 enable mode.WxH@R position.0,0 mirror.DP-3`
   - `DP-2 priority.3 enable position.0,0 mirror.DP-3`
6. If the idle daemon had DP-2 in `_outputs_off` (state 3.4), this
   step implicitly re-enables it as a replica. The daemon clears
   `_outputs_off[DP-2]` on its next tick because it sees the state
   already correct.

### 4.4 Stream disconnects (client closes / times out)

Sunshine fires the `undo_cmd` → `sunshine-display-teardown.sh` →
`handler disconnect`.

The handler:

1. Reads markers to know which connectors were primary and fallback.
2. Breaks all mirror bindings in one batch (avoids the
   "negative-position" KWin bug).
3. **If the physical monitor was enabled** (user was physically at the
   PC during the stream): restore it to its preferred native mode at
   priority 1.
   **If the physical monitor was idle-off** (user walked away before
   the stream ended): leave it disabled. Waking it now just to let
   the daemon cycle it off again would be noise. The next physical
   keystroke wakes it via §4.2.
4. Tears down the fallback virtual: clears its EDID override, writes
   `detect` to its status so it returns to `disconnected`, removes
   the fallback marker.
5. Restores the primary virtual to its 640×480 off-screen baseline.

Post-condition: `Stream disconnected × (DP-2 active)` = state 3.1
(user was present during disconnect), or `Stream disconnected ×
(DP-2 idle-off)` = a transient state that resolves as soon as the
user touches any physical input (§4.2 restores DP-2 to native since
no stream is active).

## 5. Wake-up scenarios and who triggers re-enable

### 5.1 "Both monitors off" is actually "DP-2 off, DP-3 small baseline still on"

There is never a state where DRM has zero active connectors, because
DP-3 with its baseline EDID is always enabled as a tiny off-screen
output. So the question of "all monitors off" translates to: DP-2 is
in DPMS standby, DP-3 is showing a tiny 640×480 frame that no one
sees. KWin still has at least one render target.

### 5.2 Wake-up sources while DP-2 is idle-off during a stream (state 3.4)

Three independent triggers can move DP-2 out of idle-off:

| Trigger | Effect on DP-2 | Effect on stream |
|--------|---------------|------------------|
| **Physical input** (user returns to the desk) | Daemon enables DP-2 + restores `mirror.<primary-virtual>` (see §4.2 "stream_active" branch) | No change, stream was already running |
| **Stream disconnect** (client closes session) | Handler leaves DP-2 disabled (§4.4 step 3); state becomes 3.2 until a physical key wakes it | Stream ends |
| **New stream connect** (client reconnects / different client) | Handler `connect` re-applies the 3-output topology at the new client's resolution. DP-2 comes back as replica in the new resolution. | New stream starts at the new client's resolution |

The daemon's `_outputs_off` tracking resolves in all three:
- Physical input → daemon wakes DP-2 itself, removes from tracking
- Stream disconnect → DP-2 stays disabled. Daemon's tracking still
  has it. When the user next presses a key, daemon sees
  `stream_active == false` (fallback marker gone) and restores DP-2
  to preferred native mode — NOT to the stale client-res replica.
- New stream connect → handler re-enables DP-2 as replica; daemon's
  `reconcile()` observes DP-2 is enabled now and drops the tracking.

### 5.3 Triggering only the client side (no physical wake)

If a stream starts (Moonlight connect) while DP-2 is idle-off with no
physical activity:

- Handler `connect` activates DP-1 fallback and brings DP-2 back as
  replica. The LG comes OUT of DPMS standby and shows the stream
  content.
- This is the intended behavior: we want the LG to mirror the stream
  while it's live, even if the user isn't physically at the PC. If the
  user returns mid-stream they see what the remote user sees.

If you want the LG to **stay off during remote streaming** (e.g. for
power savings), that's a future enhancement — today there's no flag
for it. The monitor's own OSD can do a manual shut-off that works
regardless of software.

### 5.4 Triggering only the host side (physical key while no stream active)

Two flavors, both handled correctly:

**A. Stream had already ended before the physical key.** State 3.2.
- DP-2 is idle-off, no fallback virtual, primary virtual at baseline
  640×480, no replicas.
- Physical input → daemon's §4.2 "no active stream" branch fires:
  `output.DP-2.enable + mirror.none + priority.1 + mode.<preferred>`.
- DP-2 comes back at its native 3440×1440. State 3.1.

**B. DP-2 was disabled by some other means** (manual kscreen-doctor,
crash recovery) and the daemon never tracked it.
- Daemon's `_outputs_off` is empty, so §4.2 is a no-op.
- Recovery: physical key + `Ctrl+Alt+Shift+R` if the state is
  otherwise weird. The reset hotkey converges to state 3.1.

**Reconcile loop** (every `RESCAN_POLL_S` = 2 s): the daemon walks
its `_outputs_off` tracking and drops entries whose connector is
actually enabled right now. Keeps the tracking from going stale if
the handler or a user action re-enabled an output independently.

## 6. Emergency reset (`Ctrl+Alt+Shift+R`)

Orthogonal escape hatch — triggered only by physical keyboard, works
even when the user's D-Bus session is broken. Always converges to
state 3.1 (stream disconnected, DP-2 native) regardless of the
previous state:

1. Kill any stuck `kscreen-doctor` and `kscreen_backend_launcher`.
2. Walk all DRM connectors; if we own their EDID override (name
   contains `Sunshine`/`UQD`) clear it and write `detect` to release
   any forced-on status.
3. Remove marker files (`/run/sunshine-virt-connector`,
   `/run/sunshine-virt-fallback-connector`).
4. Re-inject the baseline 640×480 EDID into one virtual connector
   (preferring DP-3), position it off-screen so Sunshine has something
   to capture on the next connect.
5. Wait up to 5 s for KScreen to see the re-activated virtual.
6. Restart `plasma-kscreen.service` (in case its DBus backend decayed).
7. Restart the Sunshine service, retry once after 3 s if it fails.
8. Send a desktop notification.

## 7. Invariants

These must hold in all steady states:

1. **No resolution drops during active streaming**: once a stream is
   connected at resolution `R`, the stream stays at `R` until the
   client disconnects. Idle-off of DP-2 does not change the stream
   resolution.
2. **The primary virtual is always enabled** with at least a baseline
   EDID, so Sunshine can always find something to capture.
3. **At least one render target is always enabled** for KWin (the
   primary virtual at minimum).
4. **Physical keyboard events always win over any state**: the daemon
   re-enables DP-2 within ~2 s of the event, at the right resolution
   for the current stream state (client-res if streaming, native
   otherwise).
5. **Stream end preserves the physical monitor's pre-existing state**:
   if DP-2 was enabled during the stream, it stays enabled at native
   after disconnect; if DP-2 was idle-off, it stays idle-off after
   disconnect (user's next physical key wakes it to native).
6. **Daemon never wakes DP-2 into a stale mirror**: if the stream
   ended while DP-2 was disabled, the next physical-wake event
   restores DP-2 to its own preferred mode, never to the now-torn-down
   primary virtual's baseline resolution.

## 8. Known transient failures and their expected recovery

| Symptom | Likely cause | Recovery |
|--------|--------------|---------|
| Stream resolution drops to 640×480 mid-session | `_current_replication_source` query failed → daemon restored without mirror → KScreen reverted DP-3 to preferred (baseline) mode | Moonlight reconnect → handler re-applies topology. Daemon's replica-check safeguard now prevents the disable that caused this. |
| "Couldn't find monitor [N]" on Sunshine start after reset | Sunshine scanned before the virtual's kick completed | Reset script waits up to 5 s, then retries Sunshine restart once. |
| KScreen DBus `/backend` unresponsive | Plasma bug — backend can decay on long sessions | Daemon and handler fall back to parsing `kscreen-doctor -o` text output. Reset hotkey additionally restarts `plasma-kscreen.service`. |
| Daemon spins on "Connection reset by peer" | `systemd-run --machine --user` failing on degraded user systemd | Daemon now uses `machinectl shell` with explicit env. On repeated disable failures, exponential backoff (10 s → 30 s → 1 m → 5 m). |
| Stream collapsed to 640×480 mid-session | The legacy `sunshine-idle-watchdog.timer` mis-identified an active UDP-only stream as "no client" (it checked TCP ports only) and ran the handler's disconnect 60 s into every stream. The timer has been **disabled and removed** from the installer. Sunshine itself calls `prep_cmd undo` reliably on real disconnects, so no watchdog is needed. |
| DP-2 won't wake from idle even with key press | Keyboards not enumerated (KVM on client side, or USB autosuspend) | Daemon picks up the keyboard via udev hotplug as soon as it reappears. For extreme cases: physical monitor power button. |
