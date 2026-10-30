"""kan-english: English subtitles for Kan 11 shows. See README.md."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from . import core, db

USAGE = """\
kan-english add <show url>      follow a show, queue its episodes   (https://www.kan.org.il/content/kan/kan-11/p-<id>/)
kan-english run [episode id]    make the English subtitles for the next queued episode (or this one), then the next...
kan-english run --one           just one episode
kan-english watch               keep running: new episodes are picked up every 6 hours
kan-english list                every show and episode with its state
kan-english serve [--host H] [--port P]   the page with the player at http://127.0.0.1:8765/
kan-english retry <episode id>  queue a failed episode again
kan-english check               look for new episodes of every followed show now
kan-english drop <show id>      stop checking a show for new episodes (files stay)
kan-english page                rewrite index.html
kan-english captions <video>    debug: the burned-in captions of a local video as Hebrew SRT

Data folder: ~/KanEnglish (or --data / KAN_ENGLISH_DATA). Needs ffmpeg on PATH and ANTHROPIC_API_KEY set.
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kan-english", usage=USAGE, add_help=True)
    p.add_argument("--data", default=None, help="data folder (default ~/KanEnglish or $KAN_ENGLISH_DATA)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("command", nargs="?", default="list")
    p.add_argument("arg", nargs="?", default=None)
    p.add_argument("--one", action="store_true", help="run: stop after one episode")
    p.add_argument("--host", default="127.0.0.1", help="serve: 0.0.0.0 to reach it from a TV or phone on the same network")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    core.configure(Path(a.data) if a.data else core.DATA)
    cmd = a.command

    if cmd in ("run", "watch", "add", "captions") and not shutil.which("ffmpeg"):
        print("ffmpeg is not on PATH; install it first (https://ffmpeg.org/download.html)")
        return 2

    if cmd == "add":
        if not a.arg:
            print("usage: kan-english add <show url>")
            return 2
        print(core.add(a.arg))
    elif cmd == "run":
        core.recover()
        if a.arg:
            print(core.process(a.arg))
            return 0
        n = 0
        while True:
            r = core.next_episode()
            if r is None:
                print("nothing queued" if n == 0 else f"done, {n} episode(s)")
                break
            try:
                print(core.process(r["id"]))
            except Exception as ex:
                print(f"failed: {ex}")
            n += 1
            if a.one:
                break
    elif cmd == "watch":
        core.recover()
        print(f"watching; new episodes are checked every {core.CHECK_HOURS} h (Ctrl-C stops)")
        try:
            while True:
                for line in core.check_shows():
                    print(line)
                r = core.next_episode()
                if r is None:
                    time.sleep(300)
                    continue
                try:
                    print(core.process(r["id"]))
                except Exception as ex:
                    print(f"failed: {ex}")
        except KeyboardInterrupt:
            core.recover()
    elif cmd == "list":
        print(core.status_text())
    elif cmd == "serve":
        from . import server
        core.write_page()
        server.serve(core.DATA, a.host, a.port)
    elif cmd == "retry":
        if not a.arg:
            print("usage: kan-english retry <episode id>")
            return 2
        db.update(a.arg, status="wanted", attempts=0, last_error=None)
        print(f"{a.arg} queued again")
    elif cmd == "check":
        print("\n".join(core.check_shows(force=True)) or "no new episodes")
    elif cmd == "drop":
        if not a.arg:
            print("usage: kan-english drop <show id>")
            return 2
        with db.tx() as c:
            c.execute("UPDATE shows SET active=0 WHERE id=?", (a.arg,))
        print(f"show {a.arg} is no longer checked for new episodes (files stay)")
    elif cmd == "page":
        core.write_page()
        print(f"written {core.DATA / 'index.html'}")
    elif cmd == "captions":
        if not a.arg:
            print("usage: kan-english captions <video>")
            return 2
        from . import burnin
        caps = burnin.extract(Path(a.arg))
        print(core.srt(core.read_captions(caps, core.Usage()), "he"))
    else:
        print(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
