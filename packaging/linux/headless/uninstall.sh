#!/bin/bash
# Remove everything installed by install.sh. Does NOT touch
# sunshine.conf edits or the sunshine_virt_display clone (those are
# left alone so you can restore them manually).

set -e
USER_NAME="$(id -un)"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Stopping services"
sudo systemctl disable --now sunshine-hotkey.service 2>/dev/null || true
sudo systemctl disable --now sunshine-idle.service   2>/dev/null || true

say "Removing system files"
sudo rm -f /usr/local/bin/sunshine-emergency-stop \
           /usr/local/bin/sunshine-emergency-reset \
           /usr/local/bin/sunshine-hotkey-daemon.py \
           /usr/local/bin/sunshine-idle-daemon.py \
           /usr/local/bin/sunshine-virt-display-handler \
           /etc/systemd/system/sunshine-hotkey.service \
           /etc/systemd/system/sunshine-idle.service \
           /etc/systemd/system-sleep/sunshine-sleep-handler \
           /etc/sudoers.d/sunshine-virt-display \
           /etc/sudoers.d/sunshine-virt-handler \
           /etc/sunshine-setup.md \
           /etc/systemd/system/systemd-suspend.service.d/timeout.conf \
           /etc/systemd/system/systemd-hybrid-sleep.service.d/timeout.conf \
           /etc/systemd/system/systemd-suspend-then-hibernate.service.d/timeout.conf

say "Removing user files"
rm -f "$USER_HOME/.local/bin/sunshine-display-setup.sh" \
      "$USER_HOME/.local/bin/sunshine-display-teardown.sh"

sudo systemctl daemon-reload
systemctl --user daemon-reload

say "Done. sunshine.conf edits and $USER_HOME/.local/share/sunshine_virt_display left in place."
