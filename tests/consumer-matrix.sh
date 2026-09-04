#!/usr/bin/env bash
# Which pixel formats can a consumer actually take off our loopback node?
#
# GNOME's Snapshot segfaults on some of them, which is a confusing thing to
# debug from the app's side: it looks like a broken camera. This feeds the node
# a format at a time and reports, for each, whether the device enumerates it,
# whether a direct V4L2 read works, whether PipeWire builds a camera source,
# whether a PipeWire consumer can play it, and whether Snapshot survives.
#
# Needs a GoPro already streaming to UDP 8554 -- run `gopro-cam setup` first.
# It writes to the node itself, so stop `gopro-cam stream` before running it.
#
# SPDX-License-Identifier: MIT
set -uo pipefail

DEV=${DEV:-/dev/video42}
PORT=${PORT:-8554}
UNIT=gopro-matrix-writer
SNAP_UNIT=gopro-matrix-snapshot
COMBOS=${COMBOS:-"yuv420p:1920:1080 yuv420p:1280:720 yuyv422:1280:720 yuyv422:640:480"}

have() { command -v "$1" >/dev/null; }
stop_unit() { systemctl --user stop "$1" 2>/dev/null; systemctl --user reset-failed "$1" 2>/dev/null; }

pipewire_source() {
  pw-dump 2>/dev/null | python3 -c '
import sys, json
txt = sys.stdin.read(); dec = json.JSONDecoder(); i = 0; objs = []
while i < len(txt):
    while i < len(txt) and txt[i].isspace(): i += 1
    if i >= len(txt): break
    val, i = dec.raw_decode(txt, i)
    objs.extend(val if isinstance(val, list) else [val])
for o in objs:
    p = ((o.get("info") or {}).get("props") or {})
    if p.get("api.v4l2.path") == sys.argv[1] and str(p.get("media.class","")).startswith("Video/Source"):
        print(p.get("object.serial")); break
' "$DEV"
}

feed() {
  local fmt=$1 w=$2 h=$3
  stop_unit "$UNIT"; sleep 1
  systemd-run --user --collect --unit="$UNIT" --setenv=XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    ffmpeg -nostdin -loglevel error -fflags nobuffer -f mpegts \
      -i "udp://@0.0.0.0:$PORT?overrun_nonfatal=1&fifo_size=50000000&timeout=15000000" \
      -vf "scale=$w:$h" -f v4l2 -pix_fmt "$fmt" "$DEV" >/dev/null 2>&1
  # ffmpeg cannot set a format until it has decoded a frame, and joining a live
  # stream means waiting for the next I-frame. Six seconds is not always enough,
  # and testing before then measures nothing but impatience.
  local tries=0
  until v4l2-ctl -d "$DEV" --list-formats-ext 2>/dev/null | grep -q Discrete; do
    tries=$((tries+1))
    [ $tries -ge 30 ] && { echo "  (writer never attached for $fmt ${w}x${h})" >&2; return 1; }
    sleep 1
  done
  sleep 1
}

# WirePlumber only calls a node a camera if it looked like one when probed, so
# this has to happen after the writer is attached, never before.
reprobe() {
  systemctl --user restart wireplumber
  local tries=0
  until [ -n "$(pipewire_source)" ] || [ $tries -ge 15 ]; do sleep 1; tries=$((tries+1)); done
  sleep 2
}

snapshot_survives() {
  have snapshot || { echo "n/a"; return; }
  stop_unit "$SNAP_UNIT"
  systemd-run --user --collect --unit="$SNAP_UNIT" \
    --setenv=WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" --setenv=DISPLAY="${DISPLAY:-:0}" \
    --setenv=XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    --setenv=DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    /usr/bin/snapshot >/dev/null 2>&1
  sleep 12
  local crashed
  crashed=$(journalctl --user -u "$SNAP_UNIT" --since "-20s" --no-pager 2>/dev/null | grep -cE 'SEGV|core-dump')
  stop_unit "$SNAP_UNIT"
  [ "$crashed" -gt 0 ] && echo "CRASH" || echo "ok"
}

printf '%-18s %-9s %-8s %-8s %-8s %-8s\n' FORMAT ENUM V4L2 PW-SRC PW-PLAY SNAPSHOT
for combo in $COMBOS; do
  IFS=: read -r fmt w h <<< "$combo"
  if ! feed "$fmt" "$w" "$h"; then
    printf '%-18s %-9s %-8s %-8s %-8s %-8s\n' "$fmt ${w}x${h}" "-" "-" "-" "-" "skipped"
    continue
  fi

  enum=no
  v4l2-ctl -d "$DEV" --list-formats-ext 2>/dev/null | grep -q Discrete && enum=yes

  v4l2=no
  timeout 20 ffmpeg -v error -f v4l2 -i "$DEV" -frames:v 1 -f null - >/dev/null 2>&1 && v4l2=yes

  reprobe
  serial=$(pipewire_source)
  src=no; [ -n "$serial" ] && src=yes

  play=no
  if [ -n "$serial" ] && have gst-launch-1.0; then
    timeout 20 gst-launch-1.0 pipewiresrc target-object="$serial" num-buffers=10 \
      ! videoconvert ! fakesink 2>&1 | grep -q 'Got EOS' && play=yes
  fi

  printf '%-18s %-9s %-8s %-8s %-8s %-8s\n' "$fmt ${w}x${h}" "$enum" "$v4l2" "$src" "$play" "$(snapshot_survives)"
done
stop_unit "$UNIT"
