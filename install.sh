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
  rm -f "$BIN_DIR/gopro-cam" "$BIN_DIR/gopro-panel" "$BIN_DIR/cam-panel"   # the venv goes with $DATA_DIR below
  rm -rf "$DATA_DIR/gopro-panel"
  rm -f "$DATA_DIR/applications/gopro-panel.desktop" \
        "$DATA_DIR/applications/cam-panel.desktop"
  rm -f "$DATA_DIR/icons/hicolor/scalable/apps/gopro-panel.svg" \
        "$DATA_DIR/icons/hicolor/scalable/apps/cam-panel.svg"
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
install -D -m 0644 "$HERE/gopro_blur.py"   "$DATA_DIR/gopro-panel/gopro_blur.py"
install -D -m 0644 "$HERE/cam_panel.py"    "$DATA_DIR/gopro-panel/cam_panel.py"
install -D -m 0755 "$HERE/bin/cam-loopback" "$DATA_DIR/gopro-panel/bin/cam-loopback"
install -D -m 0755 "$HERE/bin/cam-panel"   "$BIN_DIR/cam-panel"
# Point the menu entry at the absolute path: a desktop launcher does not
# necessarily inherit a shell PATH that includes ~/.local/bin.
install -D -m 0644 "$HERE/share/applications/gopro-panel.desktop" \
                   "$DATA_DIR/applications/gopro-panel.desktop"
sed -i "s|^Exec=.*|Exec=$BIN_DIR/gopro-panel|" "$DATA_DIR/applications/gopro-panel.desktop"
install -D -m 0644 "$HERE/share/applications/cam-panel.desktop" \
                   "$DATA_DIR/applications/cam-panel.desktop"
sed -i "s|^Exec=.*|Exec=$BIN_DIR/cam-panel|" "$DATA_DIR/applications/cam-panel.desktop"
command -v update-desktop-database >/dev/null && \
  update-desktop-database "$DATA_DIR/applications" 2>/dev/null || true

# The launcher icons. They go in the user's icon theme rather than being
# referenced by path, because that is the only form a pinned dock entry keeps
# across a reinstall.
for icon in gopro-panel cam-panel; do
  install -D -m 0644 "$HERE/share/icons/hicolor/scalable/apps/$icon.svg" \
                     "$DATA_DIR/icons/hicolor/scalable/apps/$icon.svg"
done
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -qtf "$DATA_DIR/icons/hicolor" 2>/dev/null || true

# ------------------------------------------------------------- background blur
# mediapipe is a large dependency and only the blur path needs it, so it gets
# its own venv rather than a place in the system's Python packages. Skip this
# and everything else still works -- you just cannot turn blur on.
VENV=$DATA_DIR/gopro-panel/venv
if [ "${GOPRO_SKIP_BLUR:-0}" = "1" ]; then
  dim "Skipping the background-blur venv (GOPRO_SKIP_BLUR=1)."
elif [ -x "$VENV/bin/python" ]; then
  dim "Background-blur venv already present at $VENV"
else
  echo
  echo "Background blur needs mediapipe in its own venv (~1 GB, one download)."
  read -r -p "Set it up now? [Y/n] " reply
  if [ "$reply" = "n" ] || [ "$reply" = "N" ]; then
    dim "Skipped. Re-run install.sh later to add it."
  elif ! python3 -m venv "$VENV" 2>/dev/null; then
    red "Could not create the venv — install python3-venv and re-run."
  elif ! "$VENV/bin/pip" install -q "mediapipe==0.10.21"; then
    red "mediapipe failed to install; blur will stay unavailable."
    rm -rf "$VENV"
  else
    green "Background blur ready."
  fi
fi

if [ ! -f "$CONFIG_DIR/config" ]; then
  install -d "$CONFIG_DIR"
  cat > "$CONFIG_DIR/config" <<'CONF'
# gopro-panel / gopro-cam settings. Both tools read this file.
VIDEO_NR=42
FOV=linear
RESOLUTION=1080
DEST_DIR=~/Videos/GoPro
# Blur the background before the video device sees it, and how hard (1-30).
BLUR=off
BLUR_STRENGTH=8
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
