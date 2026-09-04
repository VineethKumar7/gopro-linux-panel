#!/usr/bin/env python3
"""
cam-panel — one window for an ordinary USB camera.

The GoPro needs a whole contraption to become a webcam on Linux. A camera that
already speaks UVC — a Sony ZV-E10 II, say — needs none of it: the kernel gives
you /dev/videoN and every app can use it. What such a camera still cannot do by
itself is blur its background, and that is what this is for.

  Camera    pick one, see what it actually offers, watch it live
  Blur      publish a blurred copy on a loopback node, and pick THAT in your
            meeting app -- the blur happens before the app, so it needs no
            plugin and no per-call setting

With blur off there is nothing to run: use the camera directly. Root is needed
only to create the loopback node, so that one step goes through pkexec.

SPDX-License-Identifier: MIT
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, GdkPixbuf, Pango

import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
SYS_V4L = Path("/sys/class/video4linux")
LOOPBACK_DRIVER = "v4l2 loopback"
OUT_NR = int(os.environ.get("CAM_PANEL_VIDEO_NR", "43"))
OUT_DEV = f"/dev/video{OUT_NR}"
OUT_LABEL = "Blurred"
STREAM_UNIT = "cam-panel-stream"
PREVIEW_W, PREVIEW_H = 480, 270
PREVIEW_FPS = 12
POLL_SECONDS = 4
RESOLUTIONS = ["1080", "720", "480"]


def here(name):
    """A file from this checkout, or from where install.sh put it."""
    for candidate in (Path(__file__).resolve().parent / name,
                      Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share"))
                      / "gopro-panel" / name):
        if candidate.exists():
            return str(candidate)
    return name


def run(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


class Camera:
    """A capture device, and what it will actually give you."""

    def __init__(self, path, name, driver):
        self.path, self.name, self.driver = path, name, driver
        self.formats = []          # [(fourcc, width, height, fps)]

    @property
    def is_loopback(self):
        return self.driver == LOOPBACK_DRIVER

    def best(self):
        return self.formats[0] if self.formats else None

    def describe(self):
        best = self.best()
        if not best:
            return f"{self.name} — offers no capture format"
        fourcc, w, h, fps = best
        extra = f", {len(self.formats) - 1} more" if len(self.formats) > 1 else ""
        return f"{self.name} — {fourcc} {w}x{h} @ {fps:g} fps{extra}"


def probe(path):
    """Read a device's driver and formats. Returns None if it is not a camera.

    Plenty of /dev/video* nodes are not cameras: a UVC device usually brings a
    metadata node along with the real one, and it enumerates no formats at all.
    """
    out = run(["v4l2-ctl", "-d", path, "--all"])
    if "Video Capture" not in out:
        return None
    driver = ""
    match = re.search(r"Driver name\s*:\s*(.+)", out)
    if match:
        driver = match.group(1).strip()
    name = ""
    match = re.search(r"Card type\s*:\s*(.+)", out)
    if match:
        name = match.group(1).strip().strip("'")
    cam = Camera(path, name or Path(path).name, driver)

    fourcc = None
    for line in run(["v4l2-ctl", "-d", path, "--list-formats-ext"]).splitlines():
        match = re.search(r"\]:\s*'(\w+)'", line)
        if match:
            fourcc = match.group(1)
            continue
        match = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if match and fourcc:
            cam.formats.append([fourcc, int(match.group(1)), int(match.group(2)), 0.0])
            continue
        match = re.search(r"Interval: Discrete [\d.]+s \(([\d.]+) fps\)", line)
        if match and cam.formats and cam.formats[-1][3] == 0.0:
            cam.formats[-1][3] = float(match.group(1))
    cam.formats = [tuple(f) for f in cam.formats]
    cam.formats.sort(key=lambda f: (f[1] * f[2], f[3]), reverse=True)
    return cam if cam.formats else None


def cameras():
    found = []
    for node in sorted(SYS_V4L.glob("video*"), key=lambda p: int(p.name[5:])):
        cam = probe(f"/dev/{node.name}")
        if cam and not cam.is_loopback:
            found.append(cam)
    return found


def device_users(path):
    """Which processes hold the device open — the answer to 'why is it busy?'."""
    users = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            for fd in (proc / "fd").iterdir():
                if os.readlink(fd) == path:
                    cmd = (proc / "comm").read_text().strip()
                    users.append(f"{cmd}({proc.name})")
                    break
        except OSError:
            continue
    return users


class CamPanel(Gtk.Window):
    ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self):
        super().__init__(title="Camera Panel")
        self.set_default_size(760, 720)
        self.set_icon_name("camera-web")

        self.cams = []
        self.stop_event = threading.Event()
        self.preview_proc = None
        self.preview_stop = threading.Event()
        self.preview_latest = None
        self.preview_pending = False
        self.preview_source = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)
        outer.pack_start(self._build_header(), False, False, 0)
        outer.pack_start(self._build_body(), True, True, 0)

        self.connect("destroy", self._on_destroy)
        self.refresh_cameras()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---------------------------------------------------------------- header

    def _build_header(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(10)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.pack_start(Gtk.Label(label="Camera"), False, False, 0)
        self.cmb_cam = Gtk.ComboBoxText()
        self.cmb_cam.connect("changed", self._on_camera_changed)
        row.pack_start(self.cmb_cam, True, True, 0)
        btn_rescan = Gtk.Button(label="Rescan")
        btn_rescan.connect("clicked", lambda _w: self.refresh_cameras())
        row.pack_start(btn_rescan, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.lbl_detail = Gtk.Label(xalign=0)
        self.lbl_detail.set_line_wrap(True)
        box.pack_start(self.lbl_detail, False, False, 0)

        self.lbl_busy = Gtk.Label(xalign=0)
        self.lbl_busy.set_line_wrap(True)
        box.pack_start(self.lbl_busy, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 4)
        return box

    # ------------------------------------------------------------------ body

    def _build_body(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)

        self.lbl_out = Gtk.Label(xalign=0)
        box.pack_start(self.lbl_out, False, False, 0)

        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chk_blur = Gtk.CheckButton(label="Blur the background")
        self.chk_blur.set_active(True)
        self.chk_blur.connect("toggled", lambda _w: self._on_blur_changed())
        opts.pack_start(self.chk_blur, False, False, 0)

        self.adj_strength = Gtk.Adjustment(value=8, lower=1, upper=30,
                                           step_increment=1, page_increment=5)
        self.sld_strength = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                      adjustment=self.adj_strength)
        self.sld_strength.set_digits(0)
        self.sld_strength.set_size_request(180, -1)
        self.sld_strength.set_value_pos(Gtk.PositionType.RIGHT)
        self.sld_strength.connect("value-changed", lambda _w: self._on_blur_changed())
        opts.pack_start(self.sld_strength, False, False, 0)

        opts.pack_start(Gtk.Label(label="   Output"), False, False, 0)
        self.cmb_res = Gtk.ComboBoxText()
        for r in RESOLUTIONS:
            self.cmb_res.append_text(r)
        self.cmb_res.set_active(1)
        opts.pack_start(self.cmb_res, False, False, 0)
        box.pack_start(opts, False, False, 0)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_start = Gtk.Button(label="Publish blurred copy")
        self.btn_start.connect("clicked", lambda _w: self._start())
        btns.pack_start(self.btn_start, False, False, 0)
        self.btn_stop = Gtk.Button(label="Stop")
        self.btn_stop.connect("clicked", lambda _w: self._stop())
        btns.pack_start(self.btn_stop, False, False, 0)
        self.btn_preview = Gtk.ToggleButton(label="Preview")
        self.btn_preview.set_tooltip_text(
            "Watch the camera, or the blurred copy once it is running. Only one "
            "program can open a camera at a time, so switch this off before a call.")
        self.btn_preview.connect("toggled", self._on_preview_toggled)
        btns.pack_end(self.btn_preview, False, False, 0)
        box.pack_start(btns, False, False, 0)

        self.preview = Gtk.Image()
        self.preview.set_size_request(PREVIEW_W, PREVIEW_H)
        self.preview.set_halign(Gtk.Align.START)
        self.preview.set_no_show_all(True)
        box.pack_start(self.preview, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 2)
        self.log = Gtk.TextView(editable=False, monospace=True)
        self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.log)
        box.pack_start(sw, True, True, 0)
        return box

    # ------------------------------------------------------------- cameras

    def refresh_cameras(self):
        def work():
            found = cameras()
            GLib.idle_add(self._apply_cameras, found)
        threading.Thread(target=work, daemon=True).start()

    def _apply_cameras(self, found):
        previous = self.selected().path if self.selected() else None
        self.cams = found
        self.cmb_cam.remove_all()
        for cam in found:
            self.cmb_cam.append_text(f"{cam.name}  ({cam.path})")
        if not found:
            self.lbl_detail.set_markup(
                "<b>No camera found</b> — plug one in, and if it is a GoPro use "
                "the GoPro panel instead; this one is for cameras the kernel "
                "already understands.")
            return
        index = next((i for i, c in enumerate(found) if c.path == previous), 0)
        self.cmb_cam.set_active(index)
        return False

    def selected(self):
        index = self.cmb_cam.get_active()
        return self.cams[index] if 0 <= index < len(self.cams) else None

    def _on_camera_changed(self, _widget):
        cam = self.selected()
        if not cam:
            return
        self.lbl_detail.set_markup(f"<b>{GLib.markup_escape_text(cam.describe())}</b>")
        if self.btn_preview.get_active():
            self.btn_preview.set_active(False)

    # ---------------------------------------------------------------- blur

    def _on_blur_changed(self):
        self.sld_strength.set_sensitive(self.chk_blur.get_active())
        path = self._control_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"enabled={1 if self.chk_blur.get_active() else 0}\n"
                            f"strength={int(self.adj_strength.get_value())}\n")
        except OSError as e:
            self._log(f"could not write {path}: {e}\n")

    def _control_file(self):
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        return runtime / "cam-panel/blur.conf"

    def _start(self):
        cam = self.selected()
        if not cam:
            return
        if not shutil.which("pkexec"):
            self._log("pkexec is missing — cannot create the loopback device.\n")
            return
        self.btn_start.set_sensitive(False)
        self._on_blur_changed()
        self._spawn(["pkexec", here("bin/cam-loopback"), "ensure", str(OUT_NR), OUT_LABEL],
                    then=lambda: self._start_worker(cam))

    def _start_worker(self, cam):
        subprocess.run(["systemctl", "--user", "stop", STREAM_UNIT], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", STREAM_UNIT],
                       capture_output=True)
        cmd = ["systemd-run", "--user", "--collect", f"--unit={STREAM_UNIT}",
               f"--setenv=XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')}",
               "python3", here("gopro_blur.py"),
               "--source", f"v4l2:{cam.path}",
               "--device", OUT_DEV,
               "--resolution", self.cmb_res.get_active_text(),
               "--control", str(self._control_file()),
               "--strength", str(int(self.adj_strength.get_value()))]
        if not self.chk_blur.get_active():
            cmd.append("--no-blur")
        venv = (Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share"))
                / "gopro-panel/venv/bin/python")
        if venv.exists():
            cmd[cmd.index("python3")] = str(venv)
        self._spawn(cmd, then=lambda: self._log(
            f"publishing to {OUT_DEV} — pick “{OUT_LABEL}” in your meeting app\n"))
        GLib.timeout_add(2000, self._set_buttons, True)

    def _stop(self):
        self.btn_preview.set_active(False)
        subprocess.run(["systemctl", "--user", "stop", STREAM_UNIT], capture_output=True)
        self._log("stopped publishing\n")
        self._set_buttons(True)

    def _set_buttons(self, on):
        self.btn_start.set_sensitive(on)
        return False

    # -------------------------------------------------------------- preview

    def _on_preview_toggled(self, button):
        if not button.get_active():
            self._stop_preview()
            return
        cam = self.selected()
        if not cam:
            button.set_active(False)
            return
        # Prefer the blurred copy when it exists: that is what the meeting app
        # will see, and it is the thing worth checking.
        source = OUT_DEV if os.path.exists(OUT_DEV) and self._publishing() else cam.path
        busy = device_users(source)
        if busy:
            self._log(f"{source} is already open by {', '.join(busy)}\n")
            button.set_active(False)
            return
        self.preview_source = source
        self.preview_stop.clear()
        self.preview.show()
        self._log(f"preview of {source}\n")
        threading.Thread(target=self._preview_worker, daemon=True).start()

    def _publishing(self):
        return subprocess.run(["systemctl", "--user", "is-active", STREAM_UNIT],
                              capture_output=True, text=True).stdout.strip() == "active"

    def _stop_preview(self):
        self.preview_stop.set()
        proc, self.preview_proc = self.preview_proc, None
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.preview.hide()

    def _preview_worker(self):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "v4l2", "-i", self.preview_source,
               "-vf", f"fps={PREVIEW_FPS},scale={PREVIEW_W}:{PREVIEW_H}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        frame_bytes = PREVIEW_W * PREVIEW_H * 3
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=frame_bytes,
                                    start_new_session=True)
        except Exception as e:
            GLib.idle_add(self._log, f"preview failed to start: {e}\n")
            GLib.idle_add(self.btn_preview.set_active, False)
            return
        self.preview_proc = proc
        while not self.preview_stop.is_set():
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            self.preview_latest = raw          # newest frame wins; never queue
            if not self.preview_pending:
                self.preview_pending = True
                GLib.idle_add(self._show_frame)
        if proc.poll() is None:
            proc.terminate()
        if not self.preview_stop.is_set():
            GLib.idle_add(self._log, "preview ended\n")
            GLib.idle_add(self.btn_preview.set_active, False)

    def _show_frame(self):
        raw, self.preview_pending = self.preview_latest, False
        if raw is None:
            return False
        self.preview.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(raw), GdkPixbuf.Colorspace.RGB, False, 8,
            PREVIEW_W, PREVIEW_H, PREVIEW_W * 3))
        return False

    # ----------------------------------------------------------------- misc

    def _spawn(self, cmd, then=None):
        self._log("$ %s\n" % " ".join(cmd))

        def work():
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    GLib.idle_add(self._log, line)
                proc.wait()
                if proc.returncode != 0:
                    GLib.idle_add(self._log, f"[exited {proc.returncode}]\n")
                    GLib.idle_add(self._set_buttons, True)
                elif then:
                    GLib.idle_add(then)
            except Exception as e:
                GLib.idle_add(self._log, f"failed: {e}\n")
                GLib.idle_add(self._set_buttons, True)
        threading.Thread(target=work, daemon=True).start()

    def _log(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), self.ANSI.sub("", text))
        self.log.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0, 0)
        return False

    def _poll_loop(self):
        while not self.stop_event.is_set():
            cam = self.selected()
            snapshot = {
                "publishing": self._publishing(),
                "out_exists": os.path.exists(OUT_DEV),
                "busy": device_users(cam.path) if cam else [],
            }
            GLib.idle_add(self._apply_snapshot, snapshot)
            self.stop_event.wait(POLL_SECONDS)

    def _apply_snapshot(self, s):
        if s["publishing"] and s["out_exists"]:
            self.lbl_out.set_markup(
                f"<span foreground='#080'>● publishing</span>  {OUT_DEV} — "
                f"pick “{OUT_LABEL}” in your meeting app")
        elif s["out_exists"]:
            self.lbl_out.set_markup(
                f"<span foreground='#a60'>◐ {OUT_DEV} exists but nothing is feeding it</span>")
        else:
            self.lbl_out.set_markup(
                "<span foreground='#888'>○ not publishing</span>  — apps can use the "
                "camera directly; publish only if you want the background blurred")
        self.lbl_busy.set_text(
            "In use by " + ", ".join(s["busy"]) if s["busy"] else "")
        return False

    def _on_destroy(self, _w):
        self.stop_event.set()
        self._stop_preview()
        Gtk.main_quit()


def main():
    win = CamPanel()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
