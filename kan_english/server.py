"""A small static file server for the data folder: the show page, the player, the videos and the subtitle files.

Python's http.server cannot answer Range requests, and a <video> element seeks with them, so this one does."""
from __future__ import annotations

import logging
import mimetypes
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("application/x-subrip", ".srt")
mimetypes.add_type("video/mp4", ".mp4")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, directory: str, **kw):
        super().__init__(*a, directory=directory, **kw)

    def log_message(self, fmt, *args):   # quiet
        log.debug(fmt, *args)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        if self.path.endswith((".html", "/")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = self.translate_path(self.path)
        rng = self.headers.get("Range")
        if not rng or not os.path.isfile(path):
            return super().do_GET()
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        size = os.path.getsize(path)
        if not m:
            return super().do_GET()
        start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2) or 0))
        end = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


def serve(directory: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), lambda *a, **kw: Handler(*a, directory=str(directory), **kw))
    print(f"Serving {directory} at http://{host}:{port}/  (Ctrl-C stops)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
