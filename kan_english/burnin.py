"""Captions burned into the picture: when each one is on screen, and a crop of it to read.

Kan's VOD encodes have the Hebrew subtitles in the pixels (the HLS stream carries no text track), so the
only way to the words is through the image. The bottom BAND of every frame, FPS times a second, is turned
into a mask of subtitle strokes: pixels that are white and sit next to something dark, which is what a
white caption with a black outline looks like and what a bright wall, a shirt or a lamp does not. Frames
whose masks overlap (IoU >= SAME) are one caption; a frame with too few stroke pixels ends it. Each caption
is returned with its on-screen interval and a JPEG crop of its text from the frame where it is clearest,
for the model to transcribe (kan.py). Timing comes from the frame clock, so it is exact to 1/FPS.
"""
from __future__ import annotations

import io
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

log = logging.getLogger(__name__)

FPS = 5                 # frames sampled a second: a caption boundary is placed within 0.2 s
BAND = 0.22             # the bottom share of the frame that is scanned
WIDTH = 1280            # the band is scaled to this width before masking
MIN_TEXT_PX = 160       # fewer stroke pixels than this is not a caption
MIN_FRAMES = 2          # a caption on screen for one frame only is noise
SAME = 0.45             # IoU between consecutive masks that still counts as the same caption
PAD = 14                # pixels of picture kept around the text in the crop


@dataclass
class Caption:
    start: float        # seconds
    end: float
    crop: bytes         # JPEG of the text and a little picture around it
    frames: int         # how many sampled frames showed it
    px: int             # stroke pixels in the clearest frame


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy or dx:
                out |= np.roll(m, (dy, dx), axis=(0, 1))
    return out


def text_mask(rgb: np.ndarray) -> np.ndarray:
    """Subtitle strokes: near-white pixels within 2 px of a dark one (the outline)."""
    mx = rgb.max(axis=2).astype(np.int16)
    mn = rgb.min(axis=2).astype(np.int16)
    white = (mn > 180) & ((mx - mn) < 50)
    dark = mx < 85
    return white & _dilate(dark, 2)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = int((a | b).sum())
    return int((a & b).sum()) / union if union else 0.0


class Segmenter:
    """Groups consecutive frames into captions as they stream in; keeps only the running caption's clearest frame.

    A frame joins the running caption when its mask overlaps the previous frame's by IoU >= same (consecutive
    frames, so a fade-in or a lighting change does not split a caption); a frame under min_px strokes, or one
    that overlaps too little, ends it. The clearest frame is the one with the most stroke pixels."""

    def __init__(self, min_px: int = MIN_TEXT_PX, same: float = SAME, min_frames: int = MIN_FRAMES, fps: float = FPS):
        self.min_px, self.same, self.min_frames, self.half = min_px, same, min_frames, 0.5 / fps
        self.prev: np.ndarray | None = None
        self.first_t = self.last_t = 0.0
        self.n = 0
        self.best: tuple[int, int, object, np.ndarray] | None = None   # (px, frame index, picture, mask)

    def _close(self) -> tuple[float, float, int, object, np.ndarray] | None:
        done = None
        if self.n >= self.min_frames and self.best is not None:
            px, k, pic, mask = self.best
            done = (max(0.0, self.first_t - self.half), self.last_t + self.half, k, pic, mask)
        self.prev, self.n, self.best = None, 0, None
        return done

    def push(self, i: int, t: float, mask: np.ndarray, picture: object = None):
        """-> a finished caption (start, end, frame index, picture, mask) when this frame ended one, else None."""
        px = int(mask.sum())
        if px < self.min_px:
            return self._close()
        done = None
        if self.prev is not None and _iou(self.prev, mask) < self.same:
            done = self._close()
        if self.n == 0:
            self.first_t = t
        self.last_t, self.prev = t, mask
        self.n += 1
        if self.best is None or px > self.best[0]:
            self.best = (px, i, picture, mask)
        return done

    def finish(self):
        return self._close()


def segment(frames: Iterable[tuple[float, np.ndarray]], **kw) -> list[tuple[float, float, int]]:
    """Captions in a sequence of (time, mask) -> [(start, end, index of the clearest frame)]. Pure, tested."""
    seg = Segmenter(**kw)
    out = []
    for i, (t, mask) in enumerate(frames):
        done = seg.push(i, t, mask)
        if done:
            out.append(done[:3])
    done = seg.finish()
    if done:
        out.append(done[:3])
    return out


def _probe(video: Path) -> tuple[int, int]:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True)
    w, h = p.stdout.strip().split("\n")[0].split(",")[:2]
    return int(w), int(h)


def frames(video: Path, fps: int = FPS, band: float = BAND, width: int = WIDTH) -> Iterator[tuple[float, np.ndarray]]:
    """The bottom band of the video, fps times a second, as (time, RGB array)."""
    w, h = _probe(video)
    crop_h = int(h * band)
    out_h = max(2, int(round(crop_h * width / w / 2)) * 2)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video),
           "-vf", f"fps={fps},crop=iw:{crop_h}:0:{h - crop_h},scale={width}:{out_h}:flags=area",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    size = width * out_h * 3
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.stdout is not None
    i = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(out_h, width, 3)
            i += 1
    finally:
        p.stdout.close()
        err = p.stderr.read().decode(errors="replace") if p.stderr else ""
        p.wait()
        if p.returncode not in (0, None) and err.strip():
            log.warning("ffmpeg frames: %s", err.strip()[-300:])


def crop_jpeg(rgb: np.ndarray, mask: np.ndarray, pad: int = PAD) -> bytes:
    """The text's bounding box plus a little picture around it, as JPEG."""
    from PIL import Image
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    y0, y1 = max(0, rows[0] - pad), min(rgb.shape[0], rows[-1] + pad + 1)
    x0, x1 = max(0, cols[0] - pad), min(rgb.shape[1], cols[-1] + pad + 1)
    buf = io.BytesIO()
    Image.fromarray(rgb[y0:y1, x0:x1]).save(buf, "JPEG", quality=88)
    return buf.getvalue()


def extract(video: Path, fps: int = FPS) -> list[Caption]:
    """Every caption burned into the video, in order. Decodes the file once (minutes on an N150 for an hour of 1080p)."""
    seg = Segmenter(fps=fps)
    caps: list[Caption] = []
    n = 0

    def take(done) -> None:
        start, end, _, rgb, mask = done
        caps.append(Caption(start, end, crop_jpeg(rgb, mask), frames=int(round((end - start) * fps)), px=int(mask.sum())))

    for i, (t, rgb) in enumerate(frames(video, fps=fps)):
        n = i + 1
        m = text_mask(rgb)
        done = seg.push(i, t, m, rgb.copy() if int(m.sum()) >= MIN_TEXT_PX else None)
        if done:
            take(done)
    done = seg.finish()
    if done:
        take(done)
    log.info("burnin: %d captions in %s (%d frames)", len(caps), video.name, n)
    return caps
