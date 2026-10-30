# kan-english

English subtitles for the shows of Kan 11, Israel's public broadcaster, for someone who watches them with a partner who does not read Hebrew.

Kan's own site has no English subtitles and no subtitle track at all: the Hebrew subtitles are burned into the picture. This tool downloads an episode from Kan's site, finds every Hebrew caption in the picture, has Claude read it and translate it, and gives you the episode as an MP4 with an English subtitle track, plus a page with a player that shows the English above the Hebrew.

## What you need

- A computer with Python 3.11 or newer and [ffmpeg](https://ffmpeg.org/download.html) on the PATH (macOS: `brew install ffmpeg`; Windows: the gyan.dev build, add its `bin` folder to PATH; Ubuntu: `apt install ffmpeg`).
- An Anthropic API key from https://console.anthropic.com/ (the reading and translation run on Claude; an episode of half an hour costs about $1.50).
- Kan's video servers are meant for Israel; from abroad use a VPN with an Israeli exit.
- Disk: an episode is about 1.3 GB at 1080p.

## Install

```
pip install git+https://github.com/kingonly/kan-english
export ANTHROPIC_API_KEY=sk-ant-...        # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
```

## Use

Find the show on https://www.kan.org.il (Kan 11 section) and copy the URL of its page, which looks like `https://www.kan.org.il/content/kan/kan-11/p-984859/`. Then:

```
kan-english add https://www.kan.org.il/content/kan/kan-11/p-984859/
kan-english run
```

`add` reads the show's page, gives it an English title and queues every episode. `run` works through the queue, one episode at a time: download (a minute or so), finding the captions in the picture (a few minutes, it decodes the whole episode), reading and translating them with Claude (about three minutes), and writing the file. An episode takes 6 to 10 minutes on an ordinary laptop. The result lands in `~/KanEnglish/Kan/<Show>/<Show> S01E01 - <Episode title>.mp4`, with the English track embedded as the default subtitle and the Hebrew as a second track, and `.en.srt`, `.he.srt` and `.en.vtt` files next to it. Any player opens the MP4 with the English on (VLC, a TV's own player from a USB stick or a share, Plex, Infuse).

To watch in a browser instead:

```
kan-english serve
```

opens the page at http://127.0.0.1:8765/: each show with its episodes, a tap plays the episode in the browser with the English placed above the burned-in Hebrew. `kan-english serve --host 0.0.0.0` makes it reachable from a phone or a TV browser on the same Wi-Fi (at `http://<this computer's IP>:8765/`). The page remembers where you left each episode.

More:

```
kan-english list                 every show and episode with its state, note or error
kan-english run --one            just the next episode
kan-english run <episode id>     a particular episode (the id is in `list`)
kan-english watch                keep running; new episodes of followed shows are picked up every 6 hours
kan-english check                look for new episodes now
kan-english retry <episode id>   queue a failed episode again
kan-english drop <show id>       stop following a show (files stay)
kan-english --data /somewhere    use another data folder (or set KAN_ENGLISH_DATA)
```

A failed episode (network, a Claude hiccup) is retried on the next `run` or `watch`, three times, with the error shown in `list`. Ctrl-C mid-episode is fine; the episode goes back to the queue.

## How it works

1. **The site.** Kan's pages sit behind a Cloudflare check that blocks plain scripts; the tool fetches them with `curl_cffi` impersonating Chrome. The episode page carries the URL of the HLS stream (plain HLS, no DRM, hosted by Kan's CDN). The tallest variant (1080p) and its audio rendition are downloaded segment by segment and remuxed by ffmpeg into an MP4 without re-encoding.
2. **The captions.** `burnin.py` scans the bottom 22% of every frame five times a second for white strokes next to dark pixels, which is what a subtitle with its outline looks like and what a bright wall or a shirt is not. Consecutive frames whose masks overlap are one caption, so every caption comes out with its exact on-screen time, and a crop of it from its clearest frame.
3. **Reading.** Claude (Opus 5.5) reads the Hebrew off images of 12 numbered crops at a time. Credits and graphics come back empty and are dropped.
4. **Translating.** One pass over the whole episode gives it an English title, every character one Latin spelling and style notes; then the cues are translated 100 at a time with context on both sides, in parallel. The translator is told the Hebrew came from OCR and reads through the occasional misread letter.
5. **The files.** English and Hebrew SRT, a WebVTT with every cue positioned above the Hebrew band, and the MP4 with both tracks inside.

Timing is as exact as the frame clock (0.2 s). What can go wrong: a show whose captions are not white-on-dark in the bottom band (none seen so far on Kan 11), opening credits read as a subtitle (the reader is told to skip them), an OCR slip the translator did not catch. The Hebrew the tool read is kept next to the file, so a doubtful line can be checked.

## Development

```
git clone https://github.com/kingonly/kan-english && cd kan-english
uv venv && uv pip install -e . pytest
.venv/bin/pytest
```

`tests/test_core.py` pins the page parsing on saved markup, `tests/test_burnin.py` the caption grouping. The same code runs inside the author's home VOD box; this is the standalone cut.

MIT.
