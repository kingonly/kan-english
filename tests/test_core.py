import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from kan_english import core as kan  # noqa: E402

SHOW_URL = "https://www.kan.org.il/content/kan/kan-11/p-984859/"
SHOW_PAGE = """<html><head><title>כל האימהות משקרות</title>
<meta name="description" content="&lt;p&gt;טלפון מהמחנכת של איתמר מאלץ את אימו&lt;/p&gt;">
<meta property="og:title" content="כל האימהות משקרות">
<meta property="og:image" content="https://www.kan.org.il/media/dsulz2rw/share.jpg?rmode=pad&amp;width=1200&amp;height=630">
</head><body>
<a href="https://www.kan.org.il/content/kan/kan-11/p-984859/s1/984861/" class="hero-vod-cta">לצפייה</a>
<a href="https://www.kan.org.il/content/kan/kan-11/p-984859/s1/984861/">פרק 1</a>
<a href="https://www.kan.org.il/content/kan/kan-11/p-984859/s1/984862/">פרק 2</a>
<a href="https://www.kan.org.il/content/kan/kan-11/p-999999/s1/111111/">another show</a>
</body></html>"""

EP_PAGE = """<html><head><title>כל האימהות משקרות | פרק 1 - ערב סרט</title></head><body>
<span class="duration">00:29:12</span>
<script type="application/json">{"src":"1_7s8v9hbe","sourceType":"hls","sourceUrls":null,"stripeUrl":"https:\\/\\/r.il.cdn-redge.media\\/file\\/x.jpg"}</script>
<script>var u = "https:\\/\\/r.il.cdn-redge.media\\/webcache\\/gorigin\\/hls\\/oil\\/kancdn\\/vod\\/2104131\\/LIBCODER_SMOOTH_1080_KAN\\/Manifest.ism\\/playlist.m3u8?fmp4";</script>
</body></html>"""

MASTER = """#EXTM3U\r
#EXT-X-VERSION:6\r
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="acc",DEFAULT="YES",AUTOSELECT="YES",NAME="HEB-aac",LANGUAGE="HEB",URI="playlist.m3u8?id=22&bitrate=192000&fmp4"\r
#EXT-X-STREAM-INF:BANDWIDTH=1992000,CODECS="avc1.64001f, mp4a.40.2",RESOLUTION=960x540,FRAME-RATE=25.000,AUDIO="acc"\r
playlist.m3u8?id=9&bitrate=1800000&fmp4\r
#EXT-X-STREAM-INF:BANDWIDTH=5692000,CODECS="avc1.64002a, mp4a.40.2",RESOLUTION=1920x1080,FRAME-RATE=25.000,AUDIO="acc"\r
playlist.m3u8?id=1&bitrate=5500000&fmp4\r
#EXT-X-STREAM-INF:BANDWIDTH=2992000,CODECS="avc1.640020, mp4a.40.2",RESOLUTION=1280x720,FRAME-RATE=25.000,AUDIO="acc"\r
playlist.m3u8?id=5&bitrate=2800000&fmp4\r
"""
BASE = "https://r.il.cdn-redge.media/webcache/gorigin/hls/oil/kancdn/vod/2104131/LIBCODER_SMOOTH_1080_KAN/Manifest.ism/playlist.m3u8?fmp4"


def test_parse_show_lists_this_shows_episodes_once_in_order():
    info = kan.parse_show(SHOW_URL, SHOW_PAGE)
    assert info["id"] == "984859"
    assert info["title_he"] == "כל האימהות משקרות"
    assert info["description"] == "טלפון מהמחנכת של איתמר מאלץ את אימו"
    assert info["image"].endswith("rmode=pad&width=1200&height=630")
    assert [(e["id"], e["season"]) for e in info["episodes"]] == [("984861", 1), ("984862", 1)]


def test_parse_show_strips_the_site_suffix_from_a_title():
    page = SHOW_PAGE.replace('<meta property="og:title" content="כל האימהות משקרות">', "").replace(
        "<title>כל האימהות משקרות</title>", '<title>שב"ס - פרקים מלאים לצפייה ישירה</title>')
    assert kan.parse_show("https://www.kan.org.il/content/kan/kan-11/p-13283/", page)["title_he"] == 'שב"ס'


def test_parse_episode_reads_title_number_stream_and_duration():
    info = kan.parse_episode(EP_PAGE)
    assert info["title_he"] == "פרק 1 - ערב סרט"
    assert info["episode"] == 1
    assert info["stream"] == BASE
    assert info["duration"] == "00:29:12"


def test_pick_variant_takes_the_tallest_and_its_audio():
    video, audio, height = kan.pick_variant(MASTER, BASE)
    assert height == 1080
    assert video.endswith("Manifest.ism/playlist.m3u8?id=1&bitrate=5500000&fmp4")
    assert audio.endswith("Manifest.ism/playlist.m3u8?id=22&bitrate=192000&fmp4")


def test_srt_and_vtt_formats():
    cues = [kan.Cue(1.2, 3.456, "שלום", "Hi.\n- And you?"), kan.Cue(61.0, 62.5, "כן.", "Yes.")]
    assert kan.srt(cues, "en").startswith("1\n00:00:01,200 --> 00:00:03,456\nHi.\n- And you?\n\n2\n00:01:01,000")
    assert kan.srt(cues, "he").split("\n")[2] == "שלום"
    v = kan.vtt(cues)
    assert v.startswith("WEBVTT\n\n00:00:01.200 --> 00:00:03.456 line:70%\nHi.\n- And you?\n\n")


def test_library_path_is_show_folder_and_kodi_style_name(tmp_path):
    kan.configure(tmp_path)
    p = kan.library_path("All Mothers Lie", 1, 3, 'Good Evening, Coach?')
    assert p == kan.kan_dir() / "All Mothers Lie" / "All Mothers Lie S01E03 - Good Evening, Coach.mp4"


def test_render_lists_ready_episodes_as_player_links_and_the_rest_with_their_state(tmp_path):
    kan.configure(tmp_path)
    shows = [{"id": "984859", "title_en": "All Mothers Lie", "title_he": "כל האימהות משקרות", "synopsis_en": "A single mother joins a team."}]
    eps = {"984859": [
        {"season": 1, "episode": 1, "title_en": "Movie Night", "title_he": "פרק 1", "status": "ready", "duration": "00:29:12",
         "file_path": str(kan.DATA / "Kan/All Mothers Lie/All Mothers Lie S01E01 - Movie Night.mp4")},
        {"season": 1, "episode": 2, "title_en": None, "title_he": "פרק 2 - ערב טוב", "status": "translating", "duration": "", "file_path": None},
    ]}
    page = kan.render(shows, eps, {"984859": "art/984859.jpg"})
    assert 'play.html?v=%2FKan%2FAll%2520Mothers%2520Lie%2FAll%2520Mothers%2520Lie%2520S01E01%2520-%2520Movie%2520Night.mp4&amp;t=All%20Mothers%20Lie%20S1%20E1%20Movie%20Night' in page
    assert "Translating" in page and "פרק 2 - ערב טוב" in page
    assert "1 of 2 episodes ready" in page
    assert "art/984859.jpg" in page


MEDIA = """#EXTM3U\r
#EXT-X-VERSION:6\r
#EXT-X-TARGETDURATION:2\r
#EXT-X-MAP:URI="fragment.mp4?id=1&bitrate=5500000&fmp4"\r
#EXTINF:2.000,\r
fragment.mp4?id=1&bitrate=5500000&start=0&fmp4\r
#EXTINF:2.000,\r
fragment.mp4?id=1&bitrate=5500000&start=20000000&fmp4\r
#EXT-X-ENDLIST\r
"""


def test_segments_resolve_init_and_fragments_against_the_playlist_url():
    url = BASE.replace("playlist.m3u8?fmp4", "playlist.m3u8?id=1&bitrate=5500000&fmp4")
    init, segs = kan.segments(MEDIA, url)
    assert init.endswith("Manifest.ism/fragment.mp4?id=1&bitrate=5500000&fmp4")
    assert [s.rsplit("start=", 1)[1] for s in segs] == ["0&fmp4", "20000000&fmp4"]
    assert all(s.startswith("https://r.il.cdn-redge.media/") for s in segs)
