#!/usr/bin/env python3
"""
gopro_blur.py — the stage that sits between the camera and the video device.

Upstream's script points ffmpeg straight at the loopback node. This does the
same job with a stop in the middle: decode the camera's MPEG-TS to raw frames,
separate the person from the room, blur what is behind them, and write the
result out. Because it happens before /dev/videoN, everything downstream — the
browser, Meet, Zoom, OBS — sees an already-blurred camera and needs no plugin
and no help from Google.

    camera --UDP 8554--> ffmpeg --raw--> [ segment | blur | composite ] --raw--> ffmpeg --> /dev/videoN

Blur can be toggled and tuned while it runs, through a small control file that
the GUI writes; with blur off the frames are copied through untouched, so
leaving this in the pipeline costs almost nothing.

SPDX-License-Identifier: MIT
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# mediapipe is the one heavy dependency, and only the blur path needs it, so
# it is imported lazily -- passthrough still works without it installed.
_segmenter = None

FRAME_SIZES = {"1080": (1920, 1080), "720": (1280, 720), "480": (854, 480)}


def log(*a):
    print("[gopro-blur]", *a, file=sys.stderr, flush=True)


def control_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "gopro-panel/blur.conf"


class Control:
    """Live settings, re-read only when the file's mtime moves."""

    def __init__(self, path, enabled, strength):
        self.path = Path(path)
        self.enabled = enabled
        self.strength = strength
        self._mtime = None
        self.reload()

    def reload(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            for line in self.path.read_text().splitlines():
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "enabled":
                    self.enabled = value not in ("0", "off", "false", "")
                elif key == "strength":
                    self.strength = max(1, min(30, int(value)))
        except (OSError, ValueError):
            pass


def get_segmenter():
    global _segmenter
    if _segmenter is None:
        import mediapipe as mp
        # model_selection=1 is the landscape model: coarser, several times
        # cheaper, and the difference is invisible once the mask is feathered.
        _segmenter = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=1)
    return _segmenter


class Compositor:
    """Person kept sharp, everything else blurred.

    The mask is computed on a small copy -- the model works at its own low
    resolution anyway -- and feathered while it is still small, which is where
    the CPU savings are. The blur itself is a downscale, a blur, and an
    upscale: a wide Gaussian on a full frame costs far more and looks the same
    once it is this soft.
    """

    def __init__(self, width, height, seg_width=384, use_opencl=True):
        self.w, self.h = width, height
        self.seg_w = seg_width
        self.seg_h = max(1, round(seg_width * height / width))
        self.small_w, self.small_h = max(1, width // 4), max(1, height // 4)
        self.use_opencl = use_opencl and cv2.ocl.haveOpenCL()
        cv2.ocl.setUseOpenCL(self.use_opencl)

    def describe(self):
        where = "OpenCL" if self.use_opencl else "CPU"
        return f"{self.w}x{self.h}, mask at {self.seg_w}x{self.seg_h}, {where}"

    def mask_for(self, frame):
        small = cv2.resize(frame, (self.seg_w, self.seg_h), interpolation=cv2.INTER_AREA)
        result = get_segmenter().process(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        mask = cv2.GaussianBlur(result.segmentation_mask, (0, 0), 2)
        return cv2.resize((mask * 255).astype(np.uint8), (self.w, self.h))

    def __call__(self, frame, strength):
        mask = self.mask_for(frame)
        if self.use_opencl:
            return self._composite_ocl(frame, mask, strength)
        return self._composite_cpu(frame, mask, strength)

    def _background(self, frame, strength):
        small = cv2.resize(frame, (self.small_w, self.small_h), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), max(1.0, strength))
        return cv2.resize(small, (self.w, self.h))

    def _composite_cpu(self, frame, mask, strength):
        background = self._background(frame, strength)
        alpha = cv2.merge([mask, mask, mask]).astype(np.uint16)
        blended = (frame.astype(np.uint16) * alpha
                   + background.astype(np.uint16) * (255 - alpha)) >> 8
        return blended.astype(np.uint8)

    def _composite_ocl(self, frame, mask, strength):
        uframe = cv2.UMat(frame)
        ualpha = cv2.UMat(cv2.merge([mask, mask, mask]))
        background = self._background(uframe, strength)
        fore = cv2.multiply(uframe, ualpha, dtype=cv2.CV_16U)
        inverse = cv2.subtract(cv2.UMat(np.full((self.h, self.w, 3), 255, np.uint8)), ualpha)
        back = cv2.multiply(background, inverse, dtype=cv2.CV_16U)
        return cv2.convertScaleAbs(cv2.add(fore, back), alpha=1.0 / 255.0).get()


def read_exactly(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return bytes(chunks)


def main():
    ap = argparse.ArgumentParser(description="Blur the background before the video device sees it.")
    ap.add_argument("--device", default="/dev/video42")
    ap.add_argument("--resolution", default="720", choices=sorted(FRAME_SIZES))
    ap.add_argument("--port", type=int, default=8554)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--strength", type=int, default=8, help="blur radius, 1-30")
    ap.add_argument("--seg-width", type=int, default=384, help="width the mask is computed at")
    ap.add_argument("--opencl", choices=("auto", "on", "off"), default="auto")
    ap.add_argument("--no-blur", action="store_true", help="start in passthrough")
    ap.add_argument("--control", default=None, help="live-settings file")
    args = ap.parse_args()

    width, height = FRAME_SIZES[args.resolution]
    frame_bytes = width * height * 3
    control = Control(args.control or control_path(), not args.no_blur, args.strength)

    url = (f"udp://@0.0.0.0:{args.port}"
           f"?overrun_nonfatal=1&fifo_size=50000000&timeout=15000000")
    # The decoder is deliberately quiet: joining a live stream mid-GOP always
    # produces a burst of "non-existing PPS" complaints that mean nothing, and
    # they would drown the GUI's log. A stream that never arrives shows up as
    # EOF here anyway. The encoder stays at "error", because that is where a
    # real problem -- the video device being taken -- actually surfaces.
    decoder = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "fatal", "-fflags", "nobuffer",
         "-f", "mpegts", "-i", url,
         "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=frame_bytes)
    encoder = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
         "-r", str(args.fps), "-i", "-",
         "-f", "v4l2", "-pix_fmt", "yuv420p", args.device],
        stdin=subprocess.PIPE)

    # ffmpeg gives up on a busy loopback node straight away, and the only clue
    # otherwise is a broken pipe on the first frame.
    time.sleep(1.2)
    if encoder.poll() is not None:
        log(f"{args.device} would not accept video — is something else already "
            f"writing to it? Try `gopro-cam stop` first.")
        decoder.terminate()
        return 1

    compositor = Compositor(width, height, args.seg_width,
                            use_opencl=(args.opencl != "off"))
    if args.opencl == "on" and not compositor.use_opencl:
        log("OpenCL was asked for but is not available; using the CPU path")
    log(f"{compositor.describe()} -> {args.device}")

    stop = False

    def handle(_signum, _frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    frames, slow_frames, since = 0, 0, time.time()
    budget = 1.0 / args.fps
    status = 0
    try:
        while not stop:
            raw = read_exactly(decoder.stdout, frame_bytes)
            if raw is None:
                log("the camera stream ended")
                status = 1
                break
            control.reload()
            if control.enabled:
                started = time.time()
                frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3)
                out = compositor(frame, control.strength)
                if time.time() - started > budget:
                    slow_frames += 1
                payload = out.tobytes()
            else:
                payload = raw          # passthrough copies nothing
            try:
                encoder.stdin.write(payload)
            except BrokenPipeError:
                # Dying within the first second means ffmpeg never got the
                # device, not that it lost it -- almost always another writer.
                # (ffmpeg accepts a frame or two into the pipe buffer before it
                # notices it has nowhere to put them, so "frame 0" is too strict.)
                if frames < args.fps:
                    log(f"{args.device} would not accept video — is something else "
                        f"already writing to it? Try `gopro-cam stop` first.")
                else:
                    log("the video device went away")
                status = 1
                break

            frames += 1
            if time.time() - since >= 10:
                if slow_frames > frames * 0.2:
                    log(f"{slow_frames}/{frames} frames missed {args.fps} fps — "
                        f"try a lower resolution or a smaller --seg-width")
                frames, slow_frames, since = 0, 0, time.time()
    finally:
        for proc in (decoder, encoder):
            if proc.poll() is None:
                proc.terminate()
        try:
            encoder.wait(timeout=3)
        except subprocess.TimeoutExpired:
            encoder.kill()
    return 0 if stop else status


if __name__ == "__main__":
    sys.exit(main())
