#!/bin/bash
# Installer for the Sunshine headless-streaming setup.
# Run as a normal user with sudo access (you will be prompted for password).

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
USER_UID="$(id -u)"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

SUNSHINE_VIRT_DISPLAY_REPO="https://github.com/itsmattkc/sunshine_virt_display.git"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$EUID" -eq 0 ] && die "run as a normal user, not root — the script will use sudo where needed"

say "Installer for user $USER_NAME (uid $USER_UID, home $USER_HOME)"

# ---------- 1. Dependencies ----------
say "Checking dependencies…"
missing=()
for p in python3-evdev python3-pyudev python3-dbus; do
    rpm -q "$p" >/dev/null 2>&1 || missing+=("$p")
done
command -v kscreen-doctor >/dev/null || die "kscreen-doctor not found — need Plasma 6.6 or newer"
if [ "${#missing[@]}" -gt 0 ]; then
    warn "Missing RPMs: ${missing[*]}"
    warn "On Bazzite run:  rpm-ostree install ${missing[*]}  (then reboot)"
    warn "Install them and re-run this script."
    exit 1
fi

# ---------- 2. sunshine_virt_display (external repo, needed for gen_edid.py) ----------
VIRT_DIR="$USER_HOME/.local/share/sunshine_virt_display"
if [ ! -d "$VIRT_DIR" ]; then
    say "Cloning sunshine_virt_display to $VIRT_DIR"
    mkdir -p "$(dirname "$VIRT_DIR")"
    git clone --depth 1 "$SUNSHINE_VIRT_DISPLAY_REPO" "$VIRT_DIR"
else
    say "sunshine_virt_display already present at $VIRT_DIR (skipping clone)"
fi

# ---------- 3. Template substitution into a staging dir ----------
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
say "Substituting templates into staging dir $STAGE"
cp -r "$REPO_DIR/files" "$STAGE/"
find "$STAGE/files" -type f -exec sed -i \
    -e "s|{{USER_NAME}}|$USER_NAME|g" \
    -e "s|{{USER_UID}}|$USER_UID|g" \
    -e "s|{{USER_HOME}}|$USER_HOME|g" \
    {} +

# ---------- 4. System files (sudo) ----------
say "Installing system files (sudo needed)…"
sudo install -m 0755 -o root -g root "$STAGE/files/system/usr/local/bin/sunshine-emergency-stop"       /usr/local/bin/
sudo install -m 0755 -o root -g root "$STAGE/files/system/usr/local/bin/sunshine-emergency-reset"      /usr/local/bin/
sudo install -m 0755 -o root -g root "$STAGE/files/system/usr/local/bin/sunshine-hotkey-daemon.py"    /usr/local/bin/
sudo install -m 0755 -o root -g root "$STAGE/files/system/usr/local/bin/sunshine-idle-daemon.py"      /usr/local/bin/
sudo install -m 0755 -o root -g root "$STAGE/files/system/usr/local/bin/sunshine-virt-display-handler" /usr/local/bin/

sudo install -m 0644 -o root -g root "$STAGE/files/system/etc/systemd/system/sunshine-hotkey.service"  /etc/systemd/system/
sudo install -m 0644 -o root -g root "$STAGE/files/system/etc/systemd/system/sunshine-idle.service"    /etc/systemd/system/

sudo install -m 0755 -o root -g root "$STAGE/files/system/etc/systemd/system-sleep/sunshine-sleep-handler" /etc/systemd/system-sleep/

sudo mkdir -p /etc/systemd/system/systemd-suspend.service.d \
              /etc/systemd/system/systemd-hybrid-sleep.service.d \
              /etc/systemd/system/systemd-suspend-then-hibernate.service.d
for d in systemd-suspend.service.d systemd-hybrid-sleep.service.d systemd-suspend-then-hibernate.service.d; do
    sudo install -m 0644 -o root -g root "$STAGE/files/system/etc/systemd/system/systemd-suspend.service.d/timeout.conf" \
                                         "/etc/systemd/system/$d/timeout.conf"
done

sudo install -m 0440 -o root -g root "$STAGE/files/system/etc/sudoers.d/sunshine-virt-display"  /etc/sudoers.d/
sudo install -m 0440 -o root -g root "$STAGE/files/system/etc/sudoers.d/sunshine-virt-handler"  /etc/sudoers.d/
sudo visudo -cf /etc/sudoers.d/sunshine-virt-display >/dev/null
sudo visudo -cf /etc/sudoers.d/sunshine-virt-handler >/dev/null

sudo install -m 0644 -o root -g root "$REPO_DIR/docs/sunshine-setup.md" /etc/sunshine-setup.md

# ---------- 5. User files ----------
say "Installing user files to $USER_HOME…"
install -Dm 0755 "$STAGE/files/user/.local/bin/sunshine-display-setup.sh"    "$USER_HOME/.local/bin/sunshine-display-setup.sh"
install -Dm 0755 "$STAGE/files/user/.local/bin/sunshine-display-teardown.sh" "$USER_HOME/.local/bin/sunshine-display-teardown.sh"

# ---------- 6. KDE Power Management: disable KDE-native screen-off/dim ----------
say "Configuring KDE to never auto-dim/off the screen (our idle-daemon takes over)…"
BIG=2147483
for profile in AC Battery LowBattery; do
    kwriteconfig6 --file powermanagementprofilesrc --group "$profile" --group DPMSControl --key idleTime "$BIG" || true
    kwriteconfig6 --file powermanagementprofilesrc --group "$profile" --group DimDisplay  --key idleTime "$BIG" || true
done
systemctl --user restart plasma-powerdevil.service 2>/dev/null || true

# ---------- 7. Sunshine config: output_name + prep_cmd ----------
SUNSHINE_CONF="$USER_HOME/.config/sunshine/sunshine.conf"
if [ -f "$SUNSHINE_CONF" ]; then
    cp "$SUNSHINE_CONF" "${SUNSHINE_CONF}.bak.$(date +%s)"
    say "Backed up existing sunshine.conf"
fi
mkdir -p "$(dirname "$SUNSHINE_CONF")"

# Detect the DRM connector the handler will use as the primary virtual.
# Sunshine (with the linux/kms connector-name patch shipped in this fork)
# accepts the connector name directly as output_name, so the value stays
# stable even when monitor enumeration order changes between boots.
say "Detecting virtual DRM connector…"
VIRT_CONN="$(sudo /usr/local/bin/sunshine-virt-display-handler print-connector 2>&1)" || {
    warn "Could not auto-detect a virtual connector: $VIRT_CONN"
    warn "Falling back to numeric output_name = 1; you can change it later."
    VIRT_CONN="1"
}
say "Using output_name = $VIRT_CONN"

# Add / replace output_name and global_prep_cmd idempotently.
if grep -q "^output_name" "$SUNSHINE_CONF" 2>/dev/null; then
    sed -i "s|^output_name.*|output_name = $VIRT_CONN|" "$SUNSHINE_CONF"
else
    echo "output_name = $VIRT_CONN" >> "$SUNSHINE_CONF"
fi
PREP_CMD="global_prep_cmd = [{\"do\":\"$USER_HOME/.local/bin/sunshine-display-setup.sh\",\"undo\":\"$USER_HOME/.local/bin/sunshine-display-teardown.sh\",\"elevated\":\"false\"}]"
if grep -q "^global_prep_cmd" "$SUNSHINE_CONF"; then
    sed -i "s|^global_prep_cmd.*|$PREP_CMD|" "$SUNSHINE_CONF"
else
    echo "$PREP_CMD" >> "$SUNSHINE_CONF"
fi

# ---------- 8. Reload systemd + enable services ----------
say "Reloading systemd and enabling services…"
sudo systemctl daemon-reload
systemctl --user daemon-reload
sudo systemctl enable --now sunshine-hotkey.service
sudo systemctl enable --now sunshine-idle.service
# Restart Sunshine to pick up new config
SUNSHINE_UNIT="app-dev.lizardbyte.app.Sunshine.service"
systemctl --user is-enabled --quiet "$SUNSHINE_UNIT" 2>/dev/null && \
    systemctl --user restart "$SUNSHINE_UNIT" || \
    warn "Sunshine user service not found — start it manually via your launcher."

# ---------- done ----------
say "Installation complete."
cat <<EOF

  Emergency hotkey:  Ctrl+Alt+Shift+Q  (works at login/lock screen too)
  Idle timeout:      5 minutes of physical inactivity → monitor off
  Docs:              /etc/sunshine-setup.md

  Verify:
    sudo journalctl -u sunshine-hotkey -f
    sudo journalctl -u sunshine-idle -f

EOF
