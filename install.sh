#!/usr/bin/env bash
# Install the GoPro webcam script system-wide and the two front-ends into your
# home. Run it as yourself, NOT as root -- it will sudo for the one step that
# needs it (copying the webcam script into /usr/local/sbin).
#
# This replaces upstream's install.sh, which only did that one copy. Upstream's
# is kept verbatim as install.upstream.sh; see NOTICE.
#
#   ./install.sh              install
#   ./install.sh --uninstall  put everything back
#
# SPDX-License-Identifier: MIT
set -euo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
BIN_DIR=${XDG_BIN_HOME:-$HOME/.local/bin}
DATA_DIR=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/gopro-panel
SBIN=/usr/local/sbin/gopro

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$*"; }

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  red "Run this as your normal user, not with sudo."
  dim "It installs into your home directory and only sudos for $SBIN."
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$BIN_DIR/gopro-cam" "$BIN_DIR/gopro-panel"
  rm -rf "$DATA_DIR/gopro-panel"
  rm -f "$DATA_DIR/applications/gopro-panel.desktop"
  sudo rm -f "$SBIN"
  green "Removed. Your config in $CONFIG_DIR was left alone."
  exit 0
fi

# ---------------------------------------------------------------- dependencies
missing=()
for cmd in python3 ffmpeg curl lsusb ip awk; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
command -v pkexec >/dev/null || dim "note: pkexec not found — the GUI's Start/Stop needs it."
modinfo v4l2loopback >/dev/null 2>&1 || missing+=("v4l2loopback (kernel module)")
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || missing+=("python3-gi + gir1.2-gtk-3.0")
python3 -c 'import requests' 2>/dev/null || missing+=("python3-requests")

if [ ${#missing[@]} -gt 0 ]; then
  red "Missing: ${missing[*]}"
  echo
  echo "  Debian/Ubuntu:"
  echo "    sudo apt install -y v4l2loopback-dkms v4l-utils ffmpeg curl usbutils \\"
  echo "                        python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-requests policykit-1"
  echo "  Fedora:"
  echo "    sudo dnf install -y v4l2loopback v4l-utils ffmpeg curl usbutils \\"
  echo "                        python3-gobject gtk3 python3-requests polkit"
  echo
  read -r -p "Install anyway? [y/N] " reply
  [ "$reply" = "y" ] || [ "$reply" = "Y" ] || exit 1
fi

# ------------------------------------------------------------------- the files
green "Installing the webcam script to $SBIN (needs sudo)"
sudo install -D -m 0755 "$HERE/gopro" "$SBIN"

install -D -m 0755 "$HERE/bin/gopro-cam"   "$BIN_DIR/gopro-cam"
install -D -m 0755 "$HERE/bin/gopro-panel" "$BIN_DIR/gopro-panel"
install -D -m 0644 "$HERE/gopro_panel.py"  "$DATA_DIR/gopro-panel/gopro_panel.py"
install -D -m 0644 "$HERE/share/applications/gopro-panel.desktop" \
                   "$DATA_DIR/applications/gopro-panel.desktop"
command -v update-desktop-database >/dev/null && \
  update-desktop-database "$DATA_DIR/applications" 2>/dev/null || true

if [ ! -f "$CONFIG_DIR/config" ]; then
  install -d "$CONFIG_DIR"
  cat > "$CONFIG_DIR/config" <<'CONF'
# gopro-panel / gopro-cam settings. Both tools read this file.
VIDEO_NR=42
FOV=linear
RESOLUTION=1080
DEST_DIR=~/Videos/GoPro
# Substring of the PulseAudio source you use as a microphone. The camera's own
# mic is not carried over USB, so this is only a reminder of which one to pick.
# MIC_MATCH=fifine
# Path to upstream's webcam script, if it is not in /usr/local/sbin.
# GOPRO_SCRIPT=/path/to/gopro-linux-panel/gopro
CONF
  green "Wrote a default config to $CONFIG_DIR/config"
fi

echo
green "Done."
dim "  gopro-panel            the GUI (also in your app menu as “GoPro Panel”)"
dim "  gopro-cam start|stop   the webcam, from a terminal"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) red "note: $BIN_DIR is not on your PATH — add it to your shell profile." ;;
esac
