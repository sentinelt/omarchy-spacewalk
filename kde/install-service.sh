#!/bin/sh
# Installs the treadmill bridge as a systemd user service on the D-Bus
# session bus, for desktops whose widgets talk D-Bus (KDE Plasma).
#
#   kde/install-service.sh            install, enable and start it
#   kde/install-service.sh uninstall  stop it and remove the files
#
# The service runs the code from this checkout; step history stays in
# ~/.local/state/omarchy-spacewalk either way.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
unit=spacewalk-widget.service
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
activation="${XDG_DATA_HOME:-$HOME/.local/share}/dbus-1/services/io.github.ncr.Spacewalk.service"

case "${1:-install}" in
install)
    python3 -c "import bleak, dbus_fast" 2>/dev/null || {
        echo "bleak is missing: sudo apt install python3-bleak (or pacman -S python-bleak)" >&2
        exit 1
    }
    mkdir -p "$units" "$(dirname "$activation")"
    sed "s|@ROOT@|$root|g" "$root/kde/$unit.in" > "$units/$unit"
    cp "$root/kde/io.github.ncr.Spacewalk.service" "$activation"
    systemctl --user daemon-reload
    systemctl --user enable --now "$unit"
    echo "Installed $unit, running from $root"
    ;;
uninstall)
    systemctl --user disable --now "$unit" 2>/dev/null || true
    rm -f "$units/$unit" "$activation"
    systemctl --user daemon-reload
    echo "Removed $unit"
    ;;
*)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
