"""Kan 11 shows with English subtitles.

Kan's site (kan.org.il) streams its VOD as plain HLS (no DRM) and burns the Hebrew subtitles into the
picture; there is no text track to translate. So an episode goes: the show page behind Cloudflare
(curl_cffi impersonating Chrome; plain curl gets a 403) -> the 1080p HLS segments pulled into an MP4 ->
burnin.py finds every caption in the pixels with its exact on-screen time -> Claude reads the Hebrew off
strips of crops -> a notes pass over the whole episode (characters, Latin spellings, tone) and a chunked
Hebrew -> English translation -> .en.srt / .he.srt / .en.vtt next to the file, the English track embedded
as the default subtitle (mov_text) and the Hebrew as a second track -> Kan/<Show>/<Show> S01E01 - <Title>.mp4
in the data folder, listed on index.html with an in-browser player (play.html: <video> + WebVTT, the English
placed above the burned-in Hebrew).
"""
from __future__ import annotations

import base64
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin

from . import burnin, db

log = logging.getLogger(__name__)

MODEL = os.environ.get("KAN_ENGLISH_MODEL", "claude-opus-5-5")
PRICE_IN, PRICE_OUT, PRICE_CACHE = 4.0, 20.0, 0.20     # $/M tokens, Claude Opus 5.5
REFERER = "https://www.kan.org.il/"
CHECK_HOURS = 6         # how often a followed show's page is read for new episodes (watch mode)
RETRY_HOURS = 6         # a failed episode is tried again after this
MAX_ATTEMPTS = 3
STRIP = 12              # captions read per image
CHUNK = 100             # cues translated per call
CTX_BEFORE, CTX_AFTER = 8, 4
WORKERS = 4
MIN_VIDEO_MB = 20
EN_LINE = "line:70%"    # WebVTT position: above the Hebrew burned into the bottom of the picture

DATA = Path(os.environ.get("KAN_ENGLISH_DATA", "~/KanEnglish")).expanduser()


def configure(data: Path) -> None:
    """Where everything lives: Kan/<show>/ files, index.html, play.html, art/, work/, state.db."""
    global DATA
    DATA = Path(data).expanduser()
    DATA.mkdir(parents=True, exist_ok=True)
    db.configure(DATA / "state.db")


def kan_dir() -> Path:
    return DATA / "Kan"


# ------------------------------------------------------------------ the site

def _session():
    from curl_cffi import requests
    return requests.Session(impersonate="chrome", timeout=60, headers={"Referer": REFERER})


def fetch(url: str) -> str:
    r = _session().get(url)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}")
    return r.text


def fetch_bytes(url: str, session=None) -> bytes:
    r = (session or _session()).get(url)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}")
    return r.content


SHOW_ID_RE = re.compile(r"/p-(\d+)/?")
EP_RE = re.compile(r"https://www\.kan\.org\.il/content/kan/[\w-]+/p-(\d+)/s(\d+)/(\d+)/")
M3U8_RE = re.compile(r"(?:https?:)?//r\.il\.cdn-redge\.media/[^\"'\s\\]*\.m3u8[^\"'\s\\]*")
TITLE_RE = re.compile(r"<title>([^<]*)</title>")
META_RE = {k: re.compile(rf'(?:name|property)="{k}"\s+content="([^"]*)"') for k in ("description", "og:title", "og:image")}
DURATION_RE = re.compile(r'class="duration">\s*([\d:]+)\s*<')
EP_NUM_RE = re.compile(r"פרק\s+(\d+)")


def _meta(page: str, key: str) -> str:
    m = META_RE[key].search(page)
    return html.unescape(m.group(1)).strip() if m else ""


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def parse_show(url: str, page: str) -> dict:
    """A show page -> id, Hebrew title, description, image, and its episode pages in site order."""
    m = SHOW_ID_RE.search(url)
    if not m:
        raise ValueError(f"not a Kan show URL: {url}")
    sid = m.group(1)
    title = _meta(page, "og:title") or (TITLE_RE.search(page).group(1) if TITLE_RE.search(page) else "")
    title = re.sub(r"\s*[-|]\s*פרקים מלאים.*$", "", html.unescape(title)).strip()
    eps, seen = [], set()
    for show, season, ep in EP_RE.findall(page):
        if show == sid and ep not in seen:
            seen.add(ep)
            eps.append({"id": ep, "season": int(season),
                        "url": f"https://www.kan.org.il/content/kan/kan-11/p-{sid}/s{season}/{ep}/"})
    return {"id": sid, "url": url, "title_he": title, "description": _text(_meta(page, "description")),
            "image": _meta(page, "og:image"), "episodes": eps}


def parse_episode(page: str) -> dict:
    """An episode page -> Hebrew title, episode number (from 'פרק N'), stream URL, duration."""
    t = TITLE_RE.search(page)
    full = html.unescape(t.group(1)).strip() if t else ""
    title = full.split("|", 1)[1].strip() if "|" in full else full
    n = EP_NUM_RE.search(title)
    m = M3U8_RE.search(page.replace("\\/", "/"))      # the player config writes the URL with escaped slashes
    stream = m.group(0) if m else ""
    if stream.startswith("//"):
        stream = "https:" + stream
    d = DURATION_RE.search(page)
    return {"title_he": title, "episode": int(n.group(1)) if n else None, "stream": stream,
            "duration": d.group(1) if d else ""}


def pick_variant(master: str, base: str) -> tuple[str, str, int]:
    """The HLS master -> (video playlist URL of the tallest variant, audio playlist URL, height)."""
    audio = ""
    best: tuple[int, str] | None = None
    lines = [l.strip() for l in master.replace("\r", "").split("\n")]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            m = re.search(r'URI="([^"]+)"', line)
            if m and not audio:
                audio = m.group(1)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            h = int(m.group(2)) if m else 0
            uri = next((l for l in lines[i + 1:] if l and not l.startswith("#")), "")
            if uri and (best is None or h > best[0]):
                best = (h, uri)
    if not best:
        raise RuntimeError("no video variant in the HLS master")
    return urljoin(base, best[1]), (urljoin(base, audio) if audio else ""), best[0]


def segments(playlist: str, base: str) -> tuple[str, list[str]]:
    """A media playlist -> (init segment URL, segment URLs in order)."""
    init = ""
    segs = []
    for line in playlist.replace("\r", "").split("\n"):
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            init = urljoin(base, m.group(1)) if m else ""
        elif line and not line.startswith("#"):
            segs.append(urljoin(base, line))
    if not segs:
        raise RuntimeError("no segments in the playlist")
    return init, segs


def _pull_track(playlist_url: str, out: Path, label: str) -> None:
    """Init segment + every media segment, in order, into one fragmented-MP4 file. WORKERS fetch in parallel."""
    init, segs = segments(fetch(playlist_url), playlist_url)
    urls = ([init] if init else []) + segs
    data: dict[int, bytes] = {}
    local = threading.local()

    def get(i: int) -> None:
        s = getattr(local, "s", None)
        if s is None:
            s = local.s = _session()
        for attempt in range(3):
            try:
                data[i] = fetch_bytes(urls[i], s)
                return
            except Exception as ex:
                if attempt == 2:
                    raise
                time.sleep(2)
                log.warning("%s segment %d: %s (retrying)", label, i, ex)

    t0 = time.time()
    with out.open("wb") as f, ThreadPoolExecutor(WORKERS) as pool:
        next_write = 0
        for i, _ in enumerate(pool.map(get, range(len(urls)))):
            while next_write in data and next_write <= i:
                f.write(data.pop(next_write))
                next_write += 1
            if i and i % 200 == 0:
                log.info("%s: %d/%d segments", label, i, len(urls))
        for j in range(next_write, len(urls)):
            f.write(data.pop(j))
    log.info("%s: %d segments, %.0f MB in %.0fs", label, len(urls), out.stat().st_size / 1e6, time.time() - t0)


def download(stream: str, out: Path) -> None:
    """The tallest variant and its audio rendition, segment by segment, then remuxed by ffmpeg into an MP4."""
    master = fetch(stream)
    video_url, audio_url, height = pick_variant(master, stream)
    log.info("download: %dp %s", height, video_url)
    work = out.parent
    v, a = work / "video.m4s", work / "audio.m4s"
    _pull_track(video_url, v, "video")
    if audio_url:
        _pull_track(audio_url, a, "audio")
    tmp = out.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(v)]
    cmd += ["-i", str(a), "-map", "0:v:0", "-map", "1:a:0"] if audio_url else ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-movflags", "+faststart", str(tmp)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed: {p.stderr.strip()[-400:]}")
    if tmp.stat().st_size < MIN_VIDEO_MB * 1024 * 1024:
        raise RuntimeError(f"download is only {tmp.stat().st_size // 1024} KB")
    tmp.replace(out)
    v.unlink(missing_ok=True)
    a.unlink(missing_ok=True)


# ------------------------------------------------------------------ Claude

@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, u) -> None:
        with self._lock:
            self.calls += 1
            self.input += u.input_tokens
            self.cache_write += u.cache_creation_input_tokens or 0
            self.cache_read += u.cache_read_input_tokens or 0
            self.output += u.output_tokens

    def cost(self) -> float:
        return (self.input * PRICE_IN + self.cache_write * PRICE_IN * 1.25 + self.cache_read * PRICE_CACHE
                + self.output * PRICE_OUT) / 1e6


def _client():
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set (get a key at https://console.anthropic.com/)")
    return anthropic.Anthropic()


def _call(client, system: str, content, schema: dict, usage: Usage, max_tokens: int = 16000,
          effort: str = "medium", tag: str = "") -> dict:
    """One structured call; the answer is JSON matching schema. Refusals and truncation raise."""
    t0 = time.time()
    with client.beta.messages.stream(
        model=MODEL, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    ) as stream:
        resp = stream.get_final_message()
    usage.add(resp.usage)
    log.info("%s: %.0fs in=%d cache_r=%d out=%d", tag or "call", time.time() - t0, resp.usage.input_tokens,
             resp.usage.cache_read_input_tokens or 0, resp.usage.output_tokens)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refused: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("output truncated (max_tokens)")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


TITLE_SCHEMA = {"type": "object", "properties": {"title_en": {"type": "string"}, "synopsis_en": {"type": "string"}},
                "required": ["title_en", "synopsis_en"], "additionalProperties": False}

OCR_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"n": {"type": "integer"}, "text": {"type": "string"}},
    "required": ["n", "text"], "additionalProperties": False}}}, "required": ["items"], "additionalProperties": False}

NOTES_SCHEMA = {"type": "object", "properties": {
    "episode_title_en": {"type": "string"}, "synopsis": {"type": "string"},
    "characters": {"type": "array", "items": {"type": "object", "properties": {
        "name_he": {"type": "string"}, "name_en": {"type": "string"}, "role": {"type": "string"}},
        "required": ["name_he", "name_en", "role"], "additionalProperties": False}},
    "style_notes": {"type": "string"}},
    "required": ["episode_title_en", "synopsis", "characters", "style_notes"], "additionalProperties": False}

CHUNK_SCHEMA = {"type": "object", "properties": {"cues": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "en": {"type": "string"}},
    "required": ["i", "en"], "additionalProperties": False}}}, "required": ["cues"], "additionalProperties": False}

OCR_SYSTEM = """You read Hebrew subtitles off frames of Israeli television.

Each image is a stack of numbered strips. A strip is a crop from the bottom of a video frame where a subtitle was detected: white Hebrew text with a dark outline, one or two lines, sometimes a leading dash on each line for two speakers. Transcribe each strip's subtitle text exactly as written: the same words, letters, punctuation, quotation marks and line breaks (use "\\n" between the lines). Read right to left as Hebrew is read; do not translate, correct, complete or add anything. Digits and Latin letters stay as they appear.

Some strips are not subtitles: opening or closing credits (names in a display typeface, often with dots, colours or the channel logo), on-screen graphics, a bright object. For those return an empty string. Return one item per strip number, in order, as JSON matching the schema."""

TRANSLATE_SYSTEM = """You are a senior subtitle translator. You turn the Hebrew subtitle track of an Israeli television series into English subtitles of professional broadcast quality, for a viewer who does not know Hebrew and is watching with the Hebrew subtitles still burned into the picture.

How the material is given to you:
- Each cue has an index, a timestamp and the Hebrew text. The Hebrew was read from the picture by an OCR step, so a letter may occasionally be wrong (ר/ד, ו/י, ה/ח, ב/כ, a dropped final letter). Read through such slips by context and translate what was clearly meant; never reproduce them.
- A GUIDE lists the characters with one fixed Latin spelling per name, and notes on tone and recurring expressions.
- Cues marked CONTEXT ONLY are there for continuity and must NOT be translated or returned.

Rules:
1. Translate every cue you are asked to translate, one-to-one. Never merge, split, skip or leave a cue empty.
2. Natural spoken English as used in quality subtitles: idioms by meaning, not word for word; the register, humour, menace, politeness and profanity of the Hebrew preserved; Israeli slang and army/police/school jargon rendered by its English equivalent, not explained. A line in Arabic that the Hebrew track translated is translated from the Hebrew as given.
3. Names: the Latin spellings from the guide, identically every time. Other proper nouns transliterated sensibly. Digits stay digits.
4. Layout: at most 2 lines per cue, at most 42 characters per line, "\\n" between lines; break at a phrase boundary. If a Hebrew cue has lines starting with "-", keep a "-" at the start of the corresponding English lines.
5. On-screen text, signs, song lyrics and sound descriptions are translated too, concisely.

Return only JSON that matches the requested schema."""


def _strip_image(crops: list[bytes], labels: list[int], width: int = 900) -> bytes:
    """Stack numbered crops into one JPEG for the reader."""
    from PIL import Image, ImageDraw, ImageFont
    ims = []
    for b in crops:
        im = Image.open(io.BytesIO(b)).convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))))
        ims.append(im)
    margin, gap = 90, 12
    total_h = sum(im.height for im in ims) + gap * (len(ims) + 1)
    sheet = Image.new("RGB", (margin + width + gap, total_h), (28, 28, 28))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:    # older Pillow
        font = ImageFont.load_default()
    y = gap
    for n, im in zip(labels, ims):
        draw.text((8, y + max(0, im.height // 2 - 14)), f"#{n}", fill=(255, 220, 80), font=font)
        sheet.paste(im, (margin, y))
        y += im.height + gap
        draw.line([(0, y - gap // 2), (sheet.width, y - gap // 2)], fill=(70, 70, 70), width=1)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@dataclass
class Cue:
    start: float
    end: float
    he: str
    en: str = ""


def read_captions(caps: list[burnin.Caption], usage: Usage, client=None) -> list[Cue]:
    """Claude transcribes the Hebrew of every caption, STRIP at a time, in parallel. Empty readings are dropped."""
    client = client or _client()
    groups = [caps[i:i + STRIP] for i in range(0, len(caps), STRIP)]
    texts: dict[int, str] = {}

    def one(gi: int) -> None:
        group = groups[gi]
        labels = list(range(gi * STRIP + 1, gi * STRIP + len(group) + 1))
        img = base64.standard_b64encode(_strip_image([c.crop for c in group], labels)).decode()
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                   {"type": "text", "text": f"Strips #{labels[0]} to #{labels[-1]}, top to bottom. Transcribe each."}]
        got: dict[int, str] = {}
        for attempt in (1, 2):
            res = _call(client, OCR_SYSTEM, content, OCR_SCHEMA, usage, max_tokens=4000, effort="low", tag=f"read {gi + 1}/{len(groups)}")
            got = {it["n"]: it["text"] for it in res["items"]}
            if set(labels) <= set(got):
                break
            log.warning("read %d: strips %s missing (attempt %d)", gi + 1, sorted(set(labels) - set(got)), attempt)
        for n in labels:
            texts[n] = (got.get(n) or "").strip()

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(one, range(len(groups))))
    cues: list[Cue] = []
    for n, c in enumerate(caps, 1):
        t = texts.get(n, "")
        if not t:
            continue
        if cues and cues[-1].he == t and c.start - cues[-1].end <= 0.4:
            cues[-1].end = c.end          # one caption the detector split in two
        else:
            cues.append(Cue(c.start, c.end, t))
    log.info("read: %d captions -> %d cues", len(caps), len(cues))
    return cues


def _ts(s: float) -> str:
    m, sec = divmod(int(s), 60)
    return f"{m:02d}:{sec:02d}"


def notes_pass(show_row, ep_row, cues: list[Cue], usage: Usage, client=None) -> dict:
    client = client or _client()
    script = "\n".join(f"#{i} [{_ts(c.start)}] {c.he.replace(chr(10), ' | ')}" for i, c in enumerate(cues))
    user = (f'Below is the complete Hebrew subtitle track of an episode of the Israeli series "{show_row["title_he"]}"'
            f' ("{show_row["title_en"]}"; {show_row["synopsis_en"]}). The episode is titled "{ep_row["title_he"]}".\n\n'
            "Before translation begins, build a guide for the translators:\n"
            "1. An English title for the episode (translate the Hebrew episode title; keep it short).\n"
            "2. A 3-4 sentence synopsis of the episode.\n"
            "3. Every named or recurring character with ONE fixed Latin spelling of the name (the standard "
            "transliteration an English-speaking viewer would expect) and a short role description.\n"
            "4. Style notes: tone, recurring slang or catchphrases and how to render them, forms of address, anything "
            "a translator should keep consistent.\n\n" + script)
    return _call(client, TRANSLATE_SYSTEM, user, NOTES_SCHEMA, usage, max_tokens=8000, effort="medium", tag="notes")


def _guide(notes: dict) -> str:
    chars = "\n".join(f"- {c['name_he']} = {c['name_en']}: {c['role']}" for c in notes.get("characters", []))
    return (f"GUIDE\nSynopsis: {notes.get('synopsis', '')}\nCharacters:\n{chars or '- (none listed)'}\n"
            f"Style notes: {notes.get('style_notes', '')}\n")


def translate(cues: list[Cue], notes: dict, usage: Usage, client=None) -> None:
    """Hebrew -> English in CHUNK-sized calls with context on both sides, WORKERS in parallel. Fills cue.en."""
    client = client or _client()
    guide = _guide(notes)
    chunks = [(i, min(i + CHUNK, len(cues))) for i in range(0, len(cues), CHUNK)]

    def one(ci: int) -> None:
        a, b = chunks[ci]
        lines = []
        for i in range(max(0, a - CTX_BEFORE), min(len(cues), b + CTX_AFTER)):
            tag = "" if a <= i < b else " CONTEXT ONLY"
            lines.append(f"#{i} [{_ts(cues[i].start)}]{tag} HE: {cues[i].he.replace(chr(10), ' | ')}")
        user = (guide + f"\nTranslate cues #{a} to #{b - 1} (that is {b - a} cues) into English. "
                "Cues marked CONTEXT ONLY are not to be returned.\n\n" + "\n".join(lines))
        want = set(range(a, b))
        got: dict[int, str] = {}
        for attempt in (1, 2):
            res = _call(client, TRANSLATE_SYSTEM, user, CHUNK_SCHEMA, usage, max_tokens=16000, effort="high",
                        tag=f"translate {ci + 1}/{len(chunks)}")
            got = {c["i"]: c["en"].strip() for c in res["cues"] if c["en"].strip()}
            if want <= set(got):
                break
            log.warning("translate %d: cues %s missing (attempt %d)", ci + 1, sorted(want - set(got)), attempt)
        missing = sorted(want - set(got))
        if missing:
            raise RuntimeError(f"translation left cues {missing[:8]} empty")
        for i in want:
            cues[i].en = got[i]

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(one, range(len(chunks))))


# ------------------------------------------------------------------ files

def _srt_time(s: float) -> str:
    ms = int(round(s * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    sec, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def srt(cues: list[Cue], lang: str) -> str:
    out = []
    for n, c in enumerate(cues, 1):
        text = c.en if lang == "en" else c.he
        out.append(f"{n}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{text}\n")
    return "\n".join(out) + ("\n" if out else "")


def vtt(cues: list[Cue], setting: str = EN_LINE) -> str:
    """WebVTT for the browser player, every cue positioned by `setting` (above the burned-in Hebrew)."""
    out = ["WEBVTT", ""]
    for c in cues:
        a, b = _srt_time(c.start).replace(",", "."), _srt_time(c.end).replace(",", ".")
        out.append(f"{a} --> {b} {setting}".rstrip())
        out.append(c.en)
        out.append("")
    return "\n".join(out) + "\n"


def safe(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', " ", s)
    return re.sub(r"\s+", " ", s).strip(" .") or "untitled"


def library_path(show_en: str, season: int, ep: int, title_en: str) -> Path:
    name = f"{safe(show_en)} S{season:02d}E{ep:02d}" + (f" - {safe(title_en)}" if title_en else "")
    return kan_dir() / safe(show_en) / f"{name}.mp4"


def embed(video: Path, en_srt: Path, he_srt: Path, out: Path) -> None:
    """The MP4 with English as the default subtitle track and Hebrew as a second one (mov_text), faststart."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".embed-tmp.mp4")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(video), "-i", str(en_srt), "-i", str(he_srt),
           "-map", "0:v:0", "-map", "0:a:0", "-map", "1:0", "-map", "2:0", "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
           "-metadata:s:a:0", "language=heb", "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
           "-disposition:s:0", "default", "-metadata:s:s:1", "language=heb", "-metadata:s:s:1", "title=Hebrew",
           "-disposition:s:1", "0", "-movflags", "+faststart", str(tmp)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg embed failed: {p.stderr.strip()[-400:]}")
    tmp.replace(out)


# ------------------------------------------------------------------ shows and episodes

def _title_pass(title_he: str, description: str, usage: Usage) -> dict:
    user = (f'An Israeli television series on Kan 11 is titled "{title_he}". Its description: "{description}".\n'
            "Give its English title (the official international title if the series has one, otherwise a natural "
            "translation) and a one-sentence English synopsis.")
    return _call(_client(), TRANSLATE_SYSTEM, user, TITLE_SCHEMA, usage, max_tokens=800, effort="low", tag="title")


def add(url: str) -> str:
    """Follow a Kan show: read its page, name it in English, queue every episode."""
    url = url.strip().split("?")[0]
    m = re.match(r"(https://www\.kan\.org\.il/content/kan/[\w-]+/p-\d+/)", url)
    if not m:
        return "that is not a Kan show URL (expected https://www.kan.org.il/content/kan/kan-11/p-<id>/)"
    url = m.group(1)
    info = parse_show(url, fetch(url))
    row = db.show(info["id"])
    if row is None:
        named = _title_pass(info["title_he"], info["description"], Usage())
        with db.tx() as c:
            c.execute("INSERT INTO shows(id,url,title_he,title_en,synopsis_en,image,active,added_at,checked_at) "
                      "VALUES(?,?,?,?,?,?,1,?,?)",
                      (info["id"], url, info["title_he"], named["title_en"], named["synopsis_en"], info["image"], db.now(), db.now()))
        row = db.show(info["id"])
    else:
        with db.tx() as c:
            c.execute("UPDATE shows SET active=1, checked_at=? WHERE id=?", (db.now(), info["id"]))
    new = _queue_new(row, info["episodes"])
    write_page()
    total = len(db.episodes("show_id=?", (info["id"],)))
    return f'following {row["title_en"]} ({row["title_he"]}): {total} episodes, {new} queued. Next: kan-english run'


def _queue_new(show_row, eps: list[dict]) -> int:
    """Insert episodes not seen before as wanted; reads each new episode's page for its title and stream."""
    known = {r["id"] for r in db.episodes("show_id=?", (show_row["id"],))}
    new = 0
    for pos, e in enumerate(eps):
        if e["id"] in known:
            continue
        try:
            info = parse_episode(fetch(e["url"]))
        except Exception as ex:
            log.warning("episode page %s: %s", e["url"], ex)
            continue
        if not info["stream"]:
            log.warning("no stream on %s", e["url"])
            continue
        num = info["episode"] if info["episode"] is not None else pos + 1
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO episodes(id,show_id,season,episode,url,title_he,duration,stream,status,"
                      "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'wanted',?,?)",
                      (e["id"], show_row["id"], e["season"], num, e["url"], info["title_he"], info["duration"],
                       info["stream"], db.now(), db.now()))
        new += 1
    return new


def check_shows(force: bool = False) -> list[str]:
    """Read every active show's page for episodes not queued yet."""
    out = []
    for s in db.shows():
        if not s["active"] or (not force and db.now() - (s["checked_at"] or 0) < CHECK_HOURS * 3600):
            continue
        try:
            info = parse_show(s["url"], fetch(s["url"]))
            new = _queue_new(s, info["episodes"])
            with db.tx() as c:
                c.execute("UPDATE shows SET checked_at=? WHERE id=?", (db.now(), s["id"]))
            if new:
                out.append(f'{s["title_en"]}: {new} new episode{"s" if new > 1 else ""} queued')
        except Exception as ex:
            log.warning("check %s: %s", s["title_en"], ex)
    if out:
        write_page()
    return out


def next_episode():
    """The next episode to work on: wanted ones first (in show, season, episode order), then failed ones due a retry."""
    for r in db.episodes("status='wanted'"):
        return r
    for r in db.episodes("status='failed' AND attempts<? AND updated_at<?", (MAX_ATTEMPTS, db.now() - RETRY_HOURS * 3600)):
        return r
    return None


def recover() -> int:
    """Episodes cut off mid-work (Ctrl-C, a crash) go back to the queue."""
    with db.tx() as c:
        return c.execute("UPDATE episodes SET status='wanted', updated_at=? WHERE status IN "
                         "('downloading','reading','translating','embedding')", (db.now(),)).rowcount


def process(eid: str) -> str:
    """Download, read, translate, embed, list. Returns a summary line; raises after marking the episode failed."""
    ep = db.episode(eid)
    if ep is None:
        raise KeyError(eid)
    s = db.show(ep["show_id"])
    label = f'{s["title_en"]} S{ep["season"]}E{ep["episode"]}'
    with db.tx() as c:
        claimed = c.execute("UPDATE episodes SET status='downloading', attempts=attempts+1, last_error=NULL, updated_at=? "
                            "WHERE id=? AND status IN ('wanted','failed')", (db.now(), eid)).rowcount
    if not claimed:
        return f"{label} is already in work ({ep['status']})"
    work = DATA / "work" / eid
    work.mkdir(parents=True, exist_ok=True)
    usage = Usage()
    t0 = time.time()
    try:
        video = work / "video.mp4"
        if not video.exists() or video.stat().st_size < MIN_VIDEO_MB * 1024 * 1024:
            log.info("%s: downloading", label)
            download(ep["stream"], video)
        db.update(eid, status="reading")
        log.info("%s: looking for the captions in the picture", label)
        caps = burnin.extract(video)
        if not caps:
            raise RuntimeError("no burned-in captions found in the picture")
        log.info("%s: reading %d captions", label, len(caps))
        cues = read_captions(caps, usage)
        if not cues:
            raise RuntimeError(f"{len(caps)} captions detected but none read as subtitle text")
        db.update(eid, status="translating", cues=len(cues))
        log.info("%s: translating %d cues", label, len(cues))
        notes = notes_pass(s, ep, cues, usage)
        translate(cues, notes, usage)
        title_en = notes.get("episode_title_en", "").strip()
        (work / "he.srt").write_text(srt(cues, "he"), encoding="utf-8")
        (work / "en.srt").write_text(srt(cues, "en"), encoding="utf-8")
        (work / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
        lib = library_path(s["title_en"], ep["season"], ep["episode"], title_en)
        db.update(eid, status="embedding", title_en=title_en)
        embed(video, work / "en.srt", work / "he.srt", lib)
        shutil.copy(work / "en.srt", lib.with_suffix(".en.srt"))
        shutil.copy(work / "he.srt", lib.with_suffix(".he.srt"))
        lib.with_suffix(".en.vtt").write_text(vtt(cues), encoding="utf-8")
        note = f"{len(caps)} captions read, {len(cues)} cues, {usage.calls} calls, ${usage.cost():.2f}, {int(time.time() - t0)}s"
        db.update(eid, status="ready", file_path=str(lib), cues=len(cues), cost=usage.cost(), note=note)
        shutil.rmtree(work, ignore_errors=True)
        write_page()
        return f'{label} "{title_en}" ready: {lib} ({len(cues)} lines, ${usage.cost():.2f}, {int(time.time() - t0)}s)'
    except Exception as ex:
        log.exception("%s", label)
        db.update(eid, status="failed", last_error=str(ex)[:500], cost=usage.cost())
        raise


# ------------------------------------------------------------------ the page

TEMPLATE = Path(__file__).with_name("page.html")
PLAYER = Path(__file__).with_name("play.html")
STATE_LABEL = {"wanted": "Queued", "downloading": "Downloading", "reading": "Reading the Hebrew",
               "translating": "Translating", "embedding": "Finishing", "failed": "Failed"}


def _art(s) -> str:
    """The show's image from Kan, fetched once into art/."""
    if not s["image"]:
        return ""
    d = DATA / "art"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{s['id']}.jpg"
    if not p.exists():
        try:
            p.write_bytes(fetch_bytes(s["image"]))
        except Exception as ex:
            log.warning("art %s: %s", s["id"], ex)
            return ""
    return f"art/{p.name}"


def _media_url(path: str) -> str:
    return "/" + quote(str(Path(path).relative_to(DATA)), safe="/")


def render(shows_rows, eps_by_show: dict, art: dict) -> str:
    """Fill page.html."""
    parts = []
    for s in shows_rows:
        eps = eps_by_show.get(s["id"], [])
        items = []
        for e in eps:
            code = f'S{e["season"]} E{e["episode"]}'
            title = e["title_en"] or e["title_he"] or ""
            if e["status"] == "ready" and e["file_path"]:
                url = _media_url(e["file_path"])
                label = f'{s["title_en"]} {code} {title}'.strip()
                href = f'play.html?v={quote(url, safe="")}&t={quote(label, safe="")}'
                items.append(f'<li><a class="ep" href="{html.escape(href)}"><span class="code">{html.escape(code)}</span>'
                             f'<span class="t">{html.escape(title)}</span><span class="d">{html.escape(e["duration"] or "")}</span></a></li>')
            else:
                items.append(f'<li><span class="ep wait"><span class="code">{html.escape(code)}</span><span class="t">{html.escape(title)}</span>'
                             f'<span class="tag">{html.escape(STATE_LABEL.get(e["status"], e["status"]))}</span></span></li>')
        ready = sum(1 for e in eps if e["status"] == "ready")
        pic = art.get(s["id"], "")
        img = f'<span class="img" style="background-image:url(\'{html.escape(pic)}\')"></span>' if pic else '<span class="img none"></span>'
        parts.append(f'<section class="show"><div class="head">{img}<div class="txt"><h2>{html.escape(s["title_en"])}</h2>'
                     f'<p class="he">{html.escape(s["title_he"])}</p><p class="syn">{html.escape(s["synopsis_en"] or "")}</p>'
                     f'<p class="count">{ready} of {len(eps)} episodes ready</p></div></div><ul class="eps">{"".join(items)}</ul></section>')
    body = "\n".join(parts) or '<p class="empty">No shows yet. Add one with <code>kan-english add &lt;show url&gt;</code>.</p>'
    return TEMPLATE.read_text(encoding="utf-8").replace("{{SHOWS}}", body)


def write_page() -> None:
    rows = db.shows()
    by_show = {s["id"]: db.episodes("show_id=?", (s["id"],)) for s in rows}
    art = {s["id"]: _art(s) for s in rows}
    for name, content in (("index.html", render(rows, by_show, art)), ("play.html", PLAYER.read_text(encoding="utf-8"))):
        tmp = DATA / (name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(DATA / name)


def status_text() -> str:
    rows = db.shows()
    if not rows:
        return "no shows followed yet; kan-english add <show url>"
    out = []
    for s in rows:
        eps = db.episodes("show_id=?", (s["id"],))
        ready = [e for e in eps if e["status"] == "ready"]
        failed = [e for e in eps if e["status"] == "failed"]
        line = f'{s["title_en"]} ({s["title_he"]}, show {s["id"]}): {len(ready)}/{len(eps)} ready'
        if failed:
            line += f", {len(failed)} failed"
        out.append(line)
        for e in eps:
            out.append(f'  [{e["status"]:11}] {e["id"]} S{e["season"]:02d}E{e["episode"]:02d} {e["title_en"] or e["title_he"]}'
                       f'  {e["note"] or e["last_error"] or ""}')
    return "\n".join(out)
