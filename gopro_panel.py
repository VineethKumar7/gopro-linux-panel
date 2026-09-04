#!/usr/bin/env python3
"""
gopro-panel — one window for a GoPro on USB.

A GoPro gets plugged into a laptop for two unrelated reasons: to be a webcam, and
to get footage off the card. Both travel over the same USB "GoPro Connect"
ethernet link, so neither needs the camera flipped to MTP, and both live here.

  Webcam    start/stop gopro-cam, pick resolution and FOV, watch the video node
  Transfer  list the card over HTTP, download with resume, delete after offload
  Header    battery %, SD free, model and firmware — repolled every few seconds

Root is only needed to load v4l2loopback, so Start/Stop go through pkexec; every
other thing in here runs as you.

Settings live in ~/.config/gopro-panel/config as KEY=value lines, and are shared
with the gopro-cam shell wrapper:

  VIDEO_NR=42          the /dev/videoN gopro-cam creates
  FOV=linear           field of view the Webcam tab opens on
  RESOLUTION=1080      resolution the Webcam tab opens on
  MIC_MATCH=fifine     substring of the PulseAudio source you use as a microphone
  DEST_DIR=~/Videos/GoPro   where the Transfer tab saves by default

SPDX-License-Identifier: MIT
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, GdkPixbuf, GObject, Pango

import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import requests

HOME = Path(os.path.expanduser("~"))
IFACE_PREFIX = "enx"          # what the camera's USB ethernet calls itself
CAMERA_HOST_OCTET = 51        # the camera answers on .51 of its own /24
POLL_SECONDS = 4
THUMB_W = 80                  # thumbnails keep their own aspect; only width is fixed

# The FOV names the camera's webcam settings endpoint understands, and its ids.
FOVS = [("linear", 4), ("narrow", 2), ("wide", 0), ("superview", 3)]
RESOLUTIONS = ["1080", "720", "480"]


def read_config():
    """The same KEY=value file gopro-cam sources, parsed the boring way."""
    path = Path(os.environ.get(
        "GOPRO_PANEL_CONFIG",
        Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "gopro-panel/config"))
    values = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


CONFIG = read_config()
VIDEO_DEV = "/dev/video%s" % CONFIG.get("VIDEO_NR", "42")
DEFAULT_DEST = Path(os.path.expanduser(CONFIG.get("DEST_DIR", "~/Videos/GoPro")))
MIC_MATCH = CONFIG.get("MIC_MATCH", "")
DEFAULT_FOV = CONFIG.get("FOV", "linear")
DEFAULT_RESOLUTION = CONFIG.get("RESOLUTION", "1080")
DEFAULT_BLUR = CONFIG.get("BLUR", "off").lower() in ("1", "on", "true", "yes")
DEFAULT_STRENGTH = int(CONFIG.get("BLUR_STRENGTH", "8"))


def runtime_dir():
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "gopro-panel"


def control_file():
    """Where the running blur worker looks for live settings."""
    return runtime_dir() / "blur.conf"


def blur_worker_alive():
    """By pidfile, not by name — the name would match anything mentioning it."""
    try:
        pid = int((runtime_dir() / "worker.pid").read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return False
    return b"gopro_blur.py" in cmdline


def remember(**pairs):
    """Write settings back to the config file, touching only those keys."""
    path = Path(os.environ.get(
        "GOPRO_PANEL_CONFIG",
        Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "gopro-panel/config"))
    try:
        lines = path.read_text().splitlines() if path.exists() else []
    except OSError:
        return
    for key, value in pairs.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# The stream runs in its own transient service, not as a child of this window.
# A webcam should outlive the panel that started it: closing the window, or the
# panel being restarted, must not cut the camera in the middle of a call.
STREAM_UNIT = "gopro-panel-stream"


def find_gopro_cam():
    """Installed on PATH, or sitting next to us in a clone."""
    found = shutil.which("gopro-cam")
    if found:
        return found
    sibling = Path(__file__).resolve().parent / "bin/gopro-cam"
    return str(sibling) if sibling.exists() else "gopro-cam"


GOPRO_CAM = find_gopro_cam()

# Fields we read out of /gopro/camera/state's flat id->value map.
ST_BATTERY_BARS = "2"
ST_BATTERY_PCT = "70"
ST_SD_FREE_KB = "54"
ST_BUSY = "8"
ST_ENCODING = "10"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


class CameraLink:
    """The camera's HTTP endpoint — or nothing, when it isn't plugged in."""

    def __init__(self):
        self.ip = None

    def discover(self):
        """The camera hands us a /24 over USB ethernet and sits on .51 of it."""
        try:
            out = subprocess.run(["ip", "-4", "-oneline", "addr"],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            self.ip = None
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or not parts[1].startswith(IFACE_PREFIX):
                continue
            if "inet" not in parts:
                continue
            addr = parts[parts.index("inet") + 1].split("/")[0]
            a, b, c, _ = addr.split(".")
            self.ip = f"{a}.{b}.{c}.{CAMERA_HOST_OCTET}"
            return self.ip
        self.ip = None
        return None

    def _get(self, path, timeout=6, **kw):
        if not self.ip:
            raise RuntimeError("no camera")
        return requests.get(f"http://{self.ip}{path}", timeout=timeout, **kw)

    def info(self):
        return self._get("/gopro/camera/info").json()

    def state(self):
        return self._get("/gopro/camera/state").json().get("status", {})

    def media(self):
        """Flatten the per-directory listing into one list of files."""
        data = self._get("/gopro/media/list", timeout=15).json()
        files = []
        for d in data.get("media", []):
            folder = d.get("d", "")
            for f in d.get("fs", []):
                files.append({
                    "dir": folder,
                    "name": f.get("n", ""),
                    "bytes": int(f.get("s", 0) or 0),
                    "created": int(f.get("cre", 0) or 0),
                })
        files.sort(key=lambda f: f["created"], reverse=True)
        return files

    def thumbnail(self, folder, name):
        r = self._get(f"/gopro/media/thumbnail?path={folder}/{name}", timeout=10)
        r.raise_for_status()
        return r.content

    def set_fov(self, fov_id):
        return self._get(f"/gp/gpWebcam/SETTINGS?fov={fov_id}").json()

    def webcam_off(self):
        """End the stream and leave webcam mode; returns the camera's status.

        Without this the camera keeps streaming to nobody with its recording
        light on, however thoroughly this end is torn down. 2 = streaming,
        1 = in webcam mode but idle, 0 = out of it.
        """
        self._get("/gp/gpWebcam/STOP")
        self._get("/gopro/webcam/exit")
        return self._get("/gopro/webcam/status").json().get("status")

    def delete(self, folder, name):
        return self._get(f"/gopro/media/delete/file?path={folder}/{name}", timeout=20)

    def download_url(self, folder, name):
        return f"http://{self.ip}:8080/videos/DCIM/{folder}/{name}"


def webcam_state():
    """(device present, something feeding it, the blur worker running)."""
    present = os.path.exists(VIDEO_DEV)
    feeding = False
    if present:
        try:
            out = subprocess.run(["pgrep", "-fa", "ffmpeg"],
                                 capture_output=True, text=True, timeout=4).stdout
            feeding = VIDEO_DEV in out
        except Exception:
            pass
    return present, feeding, blur_worker_alive()


def mic_source():
    """The PulseAudio source matching MIC_MATCH, if the user named one."""
    if not MIC_MATCH:
        return None
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) > 1 and MIC_MATCH.lower() in cols[1].lower() \
                and not cols[1].endswith(".monitor"):
            return cols[1]
    return None


class GoProPanel(Gtk.Window):
    def __init__(self):
        super().__init__(title="GoPro Panel")
        self.set_default_size(880, 660)
        self.set_icon_name("camera-video")

        self.cam = CameraLink()
        self.stop_event = threading.Event()
        self.cancel_download = threading.Event()
        self.files = []
        self.busy_with_transfer = False
        self.worker = None          # the gopro-cam stream child, when we started it

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)
        outer.pack_start(self._build_header(), False, False, 0)

        nb = Gtk.Notebook()
        nb.set_border_width(8)
        nb.append_page(self._build_webcam_tab(), Gtk.Label(label="Webcam"))
        nb.append_page(self._build_transfer_tab(), Gtk.Label(label="Transfer"))
        outer.pack_start(nb, True, True, 0)

        self.connect("destroy", self._on_destroy)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---------------------------------------------------------------- header

    def _build_header(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(10)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lbl_model = Gtk.Label(xalign=0)
        self.lbl_model.set_markup("<b>Looking for a GoPro…</b>")
        top.pack_start(self.lbl_model, True, True, 0)
        self.lbl_conn = Gtk.Label(xalign=1)
        top.pack_start(self.lbl_conn, False, False, 0)
        box.pack_start(top, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.bat_bar = Gtk.LevelBar()
        self.bat_bar.set_min_value(0)
        self.bat_bar.set_max_value(100)
        self.bat_bar.set_size_request(160, 12)
        self.bat_bar.set_valign(Gtk.Align.CENTER)
        row.pack_start(Gtk.Label(label="Battery"), False, False, 0)
        row.pack_start(self.bat_bar, False, False, 0)
        self.lbl_bat = Gtk.Label(label="—", xalign=0)
        row.pack_start(self.lbl_bat, False, False, 0)
        self.lbl_sd = Gtk.Label(label="", xalign=1)
        row.pack_start(self.lbl_sd, True, True, 0)
        box.pack_start(row, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 4)
        return box

    # ---------------------------------------------------------------- webcam

    def _build_webcam_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)

        self.lbl_video = Gtk.Label(xalign=0)
        box.pack_start(self.lbl_video, False, False, 0)

        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opts.pack_start(Gtk.Label(label="Resolution"), False, False, 0)
        self.cmb_res = Gtk.ComboBoxText()
        for r in RESOLUTIONS:
            self.cmb_res.append_text(r)
        self.cmb_res.set_active(RESOLUTIONS.index(DEFAULT_RESOLUTION)
                                if DEFAULT_RESOLUTION in RESOLUTIONS else 0)
        opts.pack_start(self.cmb_res, False, False, 0)

        opts.pack_start(Gtk.Label(label="   FOV"), False, False, 0)
        self.cmb_fov = Gtk.ComboBoxText()
        names = [n for n, _ in FOVS]
        for name in names:
            self.cmb_fov.append_text(name)
        self.cmb_fov.set_active(names.index(DEFAULT_FOV) if DEFAULT_FOV in names else 0)
        self.cmb_fov.connect("changed", self._on_fov_changed)
        opts.pack_start(self.cmb_fov, False, False, 0)
        box.pack_start(opts, False, False, 0)

        blur = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chk_blur = Gtk.CheckButton(label="Blur the background")
        self.chk_blur.set_active(DEFAULT_BLUR)
        self.chk_blur.set_tooltip_text(
            "Applied before the video device, so browsers and meeting apps get it "
            "already blurred — no plugin, no Meet setting.")
        self.chk_blur.connect("toggled", self._on_blur_changed)
        blur.pack_start(self.chk_blur, False, False, 0)

        self.adj_strength = Gtk.Adjustment(value=DEFAULT_STRENGTH, lower=1, upper=30,
                                           step_increment=1, page_increment=5)
        self.sld_strength = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                      adjustment=self.adj_strength)
        self.sld_strength.set_digits(0)
        self.sld_strength.set_size_request(180, -1)
        self.sld_strength.set_value_pos(Gtk.PositionType.RIGHT)
        self.sld_strength.set_sensitive(DEFAULT_BLUR)
        self.sld_strength.connect("value-changed", self._on_blur_changed)
        blur.pack_start(self.sld_strength, False, False, 0)
        box.pack_start(blur, False, False, 0)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_start = Gtk.Button(label="Start webcam")
        self.btn_start.connect("clicked", lambda _w: self._start_webcam())
        btns.pack_start(self.btn_start, False, False, 0)
        self.btn_stop = Gtk.Button(label="Stop")
        self.btn_stop.connect("clicked", lambda _w: self._stop_webcam())
        btns.pack_start(self.btn_stop, False, False, 0)

        btn_nudge = Gtk.Button(label="Rescan")
        btn_nudge.set_tooltip_text(
            "GNOME's Camera app takes cameras from PipeWire, which only registers "
            "one if the device looked like a camera when it last looked — and this "
            "one does not until the stream is running. This makes it look again. "
            "Chrome, Firefox and Zoom read the device directly and never need it.")
        btn_nudge.connect("clicked", lambda _w: self._spawn(
            [str(GOPRO_CAM), "nudge"], then=lambda: None))
        btns.pack_end(btn_nudge, False, False, 0)
        box.pack_start(btns, False, False, 0)

        self.lbl_mic = Gtk.Label(xalign=0)
        self.lbl_mic.set_line_wrap(True)
        box.pack_start(self.lbl_mic, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 2)
        self.log = Gtk.TextView(editable=False, monospace=True)
        self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.log)
        box.pack_start(sw, True, True, 0)
        return box

    def _on_fov_changed(self, combo):
        """A running stream takes a new FOV without a restart, so just send it."""
        name = combo.get_active_text()
        fov_id = dict(FOVS).get(name)
        feeding = webcam_state()[1]
        if not feeding or fov_id is None or not self.cam.ip:
            return
        def work():
            try:
                self.cam.set_fov(fov_id)
                GLib.idle_add(self._append_log, f"FOV set to {name} on the live stream\n")
            except Exception as e:
                GLib.idle_add(self._append_log, f"FOV change failed: {e}\n")
        threading.Thread(target=work, daemon=True).start()

    def _on_blur_changed(self, _widget=None):
        """Write the live-settings file; the worker picks it up mid-stream."""
        enabled = self.chk_blur.get_active()
        strength = int(self.adj_strength.get_value())
        self.sld_strength.set_sensitive(enabled)
        path = control_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"enabled={1 if enabled else 0}\nstrength={strength}\n")
        except OSError as e:
            self._append_log(f"could not write {path}: {e}\n")
        remember(BLUR="on" if enabled else "off", BLUR_STRENGTH=strength)

    def _spawn(self, cmd, then=None, new_session=False):
        """Run a command, funnel its output into the log, don't block the UI."""
        self._append_log("$ %s\n" % " ".join(cmd))

        def work():
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        start_new_session=new_session)
                if then is None:
                    self.worker = proc
                for line in proc.stdout:
                    GLib.idle_add(self._append_log, line)
                proc.wait()
                if proc.returncode not in (0, -15):
                    GLib.idle_add(self._append_log, f"[exited {proc.returncode}]\n")
                elif then is not None:
                    GLib.idle_add(then)
                    return
            except Exception as e:
                GLib.idle_add(self._append_log, f"failed: {e}\n")
            GLib.idle_add(self._set_webcam_buttons_sensitive, True)
        threading.Thread(target=work, daemon=True).start()

    def _start_webcam(self):
        """Two halves: the root one loads the module, the user one carries frames.

        Keeping them apart is what lets the blur worker run as you — it writes to
        the loopback node, which logind's ACL already gives you — so only the
        module load ever asks for a password.
        """
        if not shutil.which("pkexec"):
            self._append_log("pkexec is missing — run `gopro-cam start` in a terminal.\n")
            return
        self._set_webcam_buttons_sensitive(False)
        self._on_blur_changed()
        res, fov = self.cmb_res.get_active_text(), self.cmb_fov.get_active_text()
        setup = ["pkexec", str(GOPRO_CAM), "setup", res, fov]
        self._spawn(setup, then=lambda: self._start_stream(res))

    def _start_stream(self, res):
        stream = [str(GOPRO_CAM), "stream", res,
                  "--strength", str(int(self.adj_strength.get_value()))]
        stream.append("--blur" if self.chk_blur.get_active() else "--no-blur")
        self.btn_stop.set_sensitive(True)

        if shutil.which("systemd-run"):
            subprocess.run(["systemctl", "--user", "stop", STREAM_UNIT],
                           capture_output=True)
            self._spawn(["systemd-run", "--user", "--collect",
                         f"--unit={STREAM_UNIT}"] + stream,
                        then=lambda: self._append_log(
                            "stream is its own service now — closing this window "
                            "leaves the camera running; use Stop to end it\n"))
        else:
            # No systemd: at least leave our process group, so a signal to the
            # panel does not take the stream with it.
            self._spawn(stream, new_session=True)

    def _stop_webcam(self):
        """Stop the camera first, then this end.

        The camera half needs no root, so it runs here rather than inside the
        pkexec'd script: cancel the password prompt and the light still goes out.
        """
        self._set_webcam_buttons_sensitive(False)
        subprocess.run(["systemctl", "--user", "stop", STREAM_UNIT], capture_output=True)
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
        self.worker = None

        def tell_camera():
            try:
                state = self.cam.webcam_off()
                GLib.idle_add(self._append_log,
                              "camera left webcam mode\n" if state == 0 else
                              f"camera still reports webcam status {state}\n")
            except Exception as e:
                GLib.idle_add(self._append_log, f"could not reach the camera: {e}\n")
        threading.Thread(target=tell_camera, daemon=True).start()

        self._spawn(["pkexec", str(GOPRO_CAM), "stop"], then=lambda: None)
        GLib.timeout_add(2000, self._set_webcam_buttons_sensitive, True)

    def _set_webcam_buttons_sensitive(self, on):
        self.btn_start.set_sensitive(on)
        self.btn_stop.set_sensitive(on)

    ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def _append_log(self, text):
        text = self.ANSI.sub("", text)          # gopro-cam colours its output
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self.log.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0, 0)

    # -------------------------------------------------------------- transfer

    def _build_transfer_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)

        dest = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dest.pack_start(Gtk.Label(label="Save to"), False, False, 0)
        self.chooser = Gtk.FileChooserButton(title="Where to put the footage",
                                             action=Gtk.FileChooserAction.SELECT_FOLDER)
        DEFAULT_DEST.mkdir(parents=True, exist_ok=True)
        self.chooser.set_filename(str(DEFAULT_DEST))
        dest.pack_start(self.chooser, True, True, 0)
        btn_open = Gtk.Button(label="Open")
        btn_open.connect("clicked", lambda _w: subprocess.Popen(
            ["xdg-open", self.chooser.get_filename() or str(DEFAULT_DEST)]))
        dest.pack_start(btn_open, False, False, 0)
        box.pack_start(dest, False, False, 0)

        # selected, thumbnail, name, size, date, status, folder, bytes.
        # bytes is int64 on purpose: a single 5.3K clip runs past a 32-bit gint.
        self.store = Gtk.ListStore(bool, GdkPixbuf.Pixbuf, str, str, str, str, str,
                                   GObject.TYPE_INT64)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_rubber_banding(True)

        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_row_toggled)
        self.tree.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        self.tree.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererPixbuf(), pixbuf=1))
        for title, col, width, grow in (("File", 2, 190, True), ("Size", 3, 90, False),
                                        ("Recorded", 4, 150, False), ("Status", 5, 130, False)):
            r = Gtk.CellRendererText()
            if grow:
                r.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
            c = Gtk.TreeViewColumn(title, r, text=col)
            c.set_resizable(True)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            c.set_fixed_width(width)
            c.set_expand(grow)
            self.tree.append_column(c)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        box.pack_start(sw, True, True, 0)

        self.lbl_transfer = Gtk.Label(xalign=0)
        self.lbl_transfer.set_line_wrap(True)
        box.pack_start(self.lbl_transfer, False, False, 0)

        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_text("idle")
        box.pack_start(self.progress, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, fn in (("Refresh", self._refresh_media),
                          ("All", lambda _w: self._select_all(True)),
                          ("None", lambda _w: self._select_all(False))):
            b = Gtk.Button(label=label)
            b.connect("clicked", fn)
            row.pack_start(b, False, False, 0)

        self.btn_delete = Gtk.Button(label="Delete from camera")
        self.btn_delete.connect("clicked", self._on_delete)
        row.pack_end(self.btn_delete, False, False, 0)
        self.btn_cancel = Gtk.Button(label="Cancel")
        self.btn_cancel.set_sensitive(False)
        self.btn_cancel.connect("clicked", lambda _w: self.cancel_download.set())
        row.pack_end(self.btn_cancel, False, False, 0)
        self.btn_download = Gtk.Button(label="Download selected")
        self.btn_download.connect("clicked", self._on_download)
        row.pack_end(self.btn_download, False, False, 0)
        box.pack_start(row, False, False, 0)
        return box

    def _on_row_toggled(self, _renderer, path):
        self.store[path][0] = not self.store[path][0]

    def _select_all(self, value):
        for row in self.store:
            row[0] = value

    def _blank_thumb(self):
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8,
                                  THUMB_W, round(THUMB_W * 9 / 16))
        pb.fill(0x00000000)
        return pb

    def _refresh_media(self, _widget=None):
        if not self.cam.ip:
            self.lbl_transfer.set_text("No camera — plug it in and set USB to GoPro Connect.")
            return
        self.lbl_transfer.set_text("Reading the card…")

        def work():
            try:
                files = self.cam.media()
            except Exception as e:
                GLib.idle_add(self.lbl_transfer.set_text, f"Could not read the card: {e}")
                return
            GLib.idle_add(self._populate, files)
        threading.Thread(target=work, daemon=True).start()

    def _populate(self, files):
        self.files = files
        self.store.clear()
        blank = self._blank_thumb()
        dest = Path(self.chooser.get_filename() or DEFAULT_DEST)
        total = 0
        for f in files:
            total += f["bytes"]
            local = dest / f["name"]
            have = local.exists() and local.stat().st_size == f["bytes"]
            when = datetime.fromtimestamp(f["created"]).strftime("%Y-%m-%d %H:%M") \
                if f["created"] else ""
            self.store.append([False, blank, f["name"], human_bytes(f["bytes"]),
                               when, "on disk" if have else "", f["dir"], f["bytes"]])
        self.lbl_transfer.set_text(f"{len(files)} files on the card, {human_bytes(total)}.")
        threading.Thread(target=self._thumb_loop, daemon=True).start()

    def _thumb_loop(self):
        """Thumbnails are a nicety — fetch them one by one behind the list."""
        for i, f in enumerate(list(self.files)):
            if self.stop_event.is_set():
                return
            try:
                data = self.cam.thumbnail(f["dir"], f["name"])
                loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
                loader.write(data)
                loader.close()
                full = loader.get_pixbuf()
                height = max(1, round(THUMB_W * full.get_height() / full.get_width()))
                pb = full.scale_simple(THUMB_W, height, GdkPixbuf.InterpType.BILINEAR)
            except Exception:
                continue
            GLib.idle_add(self._set_thumb, i, pb)

    def _set_thumb(self, index, pixbuf):
        if index < len(self.store):
            self.store[index][1] = pixbuf

    def _selected_rows(self):
        return [i for i, row in enumerate(self.store) if row[0]]

    def _on_download(self, _widget):
        rows = self._selected_rows()
        if not rows:
            self.lbl_transfer.set_text("Nothing ticked.")
            return
        dest = Path(self.chooser.get_filename() or DEFAULT_DEST)
        dest.mkdir(parents=True, exist_ok=True)
        if webcam_state()[1]:
            self._append_log("Note: the webcam stream is live; downloads will share the link.\n")
        self.cancel_download.clear()
        self.btn_download.set_sensitive(False)
        self.btn_delete.set_sensitive(False)
        self.btn_cancel.set_sensitive(True)
        threading.Thread(target=self._download_worker, args=(rows, dest), daemon=True).start()

    def _download_worker(self, rows, dest):
        total = sum(self.store[i][7] for i in rows)
        done = 0
        for n, i in enumerate(rows, 1):
            if self.cancel_download.is_set():
                GLib.idle_add(self.lbl_transfer.set_text, "Cancelled.")
                break
            name, folder, size = self.store[i][2], self.store[i][6], self.store[i][7]
            target = dest / name
            GLib.idle_add(self._row_status, i, "downloading…")
            GLib.idle_add(self.lbl_transfer.set_text,
                          f"[{n}/{len(rows)}] {name} → {dest}")
            try:
                got = self._fetch_one(folder, name, target, size, done, total)
                done += got
                GLib.idle_add(self._row_status, i, "on disk")
            except Exception as e:
                GLib.idle_add(self._row_status, i, f"failed: {e}")
                done += size
        GLib.idle_add(self._download_finished)

    def _fetch_one(self, folder, name, target, size, done_before, grand_total):
        """Resume a half-finished file rather than starting it over."""
        have = target.stat().st_size if target.exists() else 0
        if have == size:
            GLib.idle_add(self._set_progress, (done_before + size) / max(grand_total, 1))
            return size
        headers, mode = {}, "wb"
        if 0 < have < size:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"
        else:
            have = 0
        url = self.cam.download_url(folder, name)
        with requests.get(url, stream=True, headers=headers, timeout=30) as r:
            r.raise_for_status()
            with open(target, mode) as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if self.cancel_download.is_set():
                        raise RuntimeError("cancelled")
                    fh.write(chunk)
                    have += len(chunk)
                    GLib.idle_add(self._set_progress,
                                  (done_before + have) / max(grand_total, 1))
        return size

    def _set_progress(self, fraction):
        fraction = min(max(fraction, 0.0), 1.0)
        self.progress.set_fraction(fraction)
        self.progress.set_text(f"{fraction * 100:.0f}%")

    def _row_status(self, index, text):
        if index < len(self.store):
            self.store[index][5] = text

    def _download_finished(self):
        self.btn_download.set_sensitive(True)
        self.btn_delete.set_sensitive(True)
        self.btn_cancel.set_sensitive(False)
        self.progress.set_text("idle")
        if not self.cancel_download.is_set():
            self.lbl_transfer.set_text("Downloads finished.")

    def _on_delete(self, _widget):
        rows = self._selected_rows()
        if not rows:
            self.lbl_transfer.set_text("Nothing ticked.")
            return
        names = [self.store[i][2] for i in rows]
        not_saved = [self.store[i][2] for i in rows if self.store[i][5] != "on disk"]
        msg = f"Delete {len(names)} file(s) from the camera's card?"
        detail = "This cannot be undone."
        if not_saved:
            detail += (f"\n\n{len(not_saved)} of them are not on this laptop yet: "
                       + ", ".join(not_saved[:5])
                       + (" …" if len(not_saved) > 5 else ""))
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=msg)
        dlg.format_secondary_text(detail)
        if dlg.run() != Gtk.ResponseType.OK:
            dlg.destroy()
            return
        dlg.destroy()

        self.btn_delete.set_sensitive(False)
        self.btn_download.set_sensitive(False)

        def work():
            for i in rows:
                try:
                    self.cam.delete(self.store[i][6], self.store[i][2])
                except Exception as e:
                    GLib.idle_add(self._row_status, i, f"delete failed: {e}")
            # Don't claim anything — re-read the card and let the new list say.
            GLib.idle_add(self.btn_delete.set_sensitive, True)
            GLib.idle_add(self.btn_download.set_sensitive, True)
            GLib.idle_add(self._refresh_media)
        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------------- polls

    def _poll_loop(self):
        first = True
        while not self.stop_event.is_set():
            ip = self.cam.discover()
            snapshot = {"ip": ip, "info": None, "state": None}
            if ip:
                try:
                    snapshot["info"] = self.cam.info()
                    snapshot["state"] = self.cam.state()
                except Exception:
                    snapshot["ip"] = None
            snapshot["webcam"] = webcam_state()
            snapshot["mic"] = mic_source()
            GLib.idle_add(self._apply_snapshot, snapshot)
            if first and snapshot["ip"]:
                first = False
                GLib.idle_add(self._refresh_media)
            self.stop_event.wait(POLL_SECONDS)

    def _apply_snapshot(self, s):
        if not s["ip"]:
            self.lbl_model.set_markup("<b>No GoPro on USB</b>")
            self.lbl_conn.set_markup("<span foreground='#b00'>disconnected</span>")
            self.lbl_bat.set_text("—")
            self.bat_bar.set_value(0)
            self.lbl_sd.set_text("Set the camera's USB Connection to “GoPro Connect”.")
            self.btn_start.set_sensitive(False)
        else:
            info = s["info"] or {}
            self.lbl_model.set_markup(
                f"<b>{info.get('model_name', 'GoPro')}</b>  "
                f"<span foreground='#888'>{info.get('firmware_version', '')}</span>")
            self.lbl_conn.set_markup("<span foreground='#080'>connected</span>")
            st = s["state"] or {}
            pct = st.get(ST_BATTERY_PCT)
            if pct is not None:
                self.bat_bar.set_value(float(pct))
                bars = st.get(ST_BATTERY_BARS)
                self.lbl_bat.set_text(f"{pct}%" + (f"  ({bars}/4 bars)" if bars is not None else ""))
            free_kb = st.get(ST_SD_FREE_KB)
            bits = []
            if free_kb is not None:
                bits.append(f"SD free {human_bytes(int(free_kb) * 1024)}")
            if st.get(ST_ENCODING):
                bits.append("recording")
            elif st.get(ST_BUSY) and not s["webcam"][1]:  # webcam mode pins busy high
                bits.append("busy")
            self.lbl_sd.set_text("   ".join(bits))
            self.btn_start.set_sensitive(True)

        present, feeding, blurring = s["webcam"]
        if feeding:
            extra = " · background blurred" if blurring and self.chk_blur.get_active() else ""
            self.lbl_video.set_markup(
                f"<span foreground='#080'>● live</span>  {VIDEO_DEV} — "
                f"pick “GoPro” as the camera{extra}")
        elif present:
            self.lbl_video.set_markup(
                f"<span foreground='#a60'>◐ loopback loaded but nothing is feeding it</span>  {VIDEO_DEV}")
        else:
            self.lbl_video.set_markup(
                f"<span foreground='#888'>○ off</span>  {VIDEO_DEV} does not exist yet")

        mic = s["mic"]
        note = ("Microphone is separate — the camera's own mic isn't carried over this link.")
        if mic:
            note += f"\nPick <b>{GLib.markup_escape_text(mic)}</b> as the mic."
        elif MIC_MATCH:
            note += f"\nNothing matching “{GLib.markup_escape_text(MIC_MATCH)}” is plugged in."
        else:
            note += "\nSet MIC_MATCH in the config file to have yours named here."
        self.lbl_mic.set_markup(note)
        return False

    def _on_destroy(self, _w):
        # Deliberately leaves the stream alone. Closing the window is not the
        # same as wanting your camera switched off mid-call; Stop is.
        self.stop_event.set()
        self.cancel_download.set()
        Gtk.main_quit()


def main():
    win = GoProPanel()
    win.show_all()
    win.btn_cancel.set_sensitive(False)
    Gtk.main()


if __name__ == "__main__":
    main()
