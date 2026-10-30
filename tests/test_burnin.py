import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from kan_english.burnin import segment, text_mask  # noqa: E402

FPS = 5


def mask(cols: range, rows: range = range(10, 20), shape=(40, 200)) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    m[rows.start:rows.stop, cols.start:cols.stop] = True
    return m


def empty() -> np.ndarray:
    return np.zeros((40, 200), dtype=bool)


def frames(*masks):
    return [(i / FPS, m) for i, m in enumerate(masks)]


def test_one_caption_has_frame_clock_times_and_clearest_frame():
    a = mask(range(20, 120))
    caps = segment(frames(empty(), a, a, a, a, empty()), min_px=100, fps=FPS)
    assert len(caps) == 1
    start, end, k = caps[0]
    assert abs(start - (1 / FPS - 0.1)) < 1e-9 and abs(end - (4 / FPS + 0.1)) < 1e-9
    assert k == 1


def test_two_captions_back_to_back_split_without_a_gap_frame():
    a, b = mask(range(20, 120)), mask(range(90, 190), rows=range(24, 34))
    caps = segment(frames(a, a, a, b, b, b), min_px=100, fps=FPS)
    assert [(c[2]) for c in caps] == [0, 3]
    assert abs(caps[0][1] - caps[1][0]) < 1e-9     # the first ends where the second begins


def test_single_frame_flash_is_dropped():
    a = mask(range(20, 120))
    assert segment(frames(empty(), a, empty()), min_px=100, fps=FPS) == []


def test_fade_in_stays_one_caption_and_the_fullest_frame_is_chosen():
    small, mid, full = mask(range(60, 80)), mask(range(40, 100)), mask(range(20, 120))
    caps = segment(frames(small, mid, full, full, mid), min_px=100, fps=FPS)
    assert len(caps) == 1
    assert caps[0][2] in (2, 3)


def test_text_mask_wants_white_next_to_dark_not_a_bright_wall():
    rgb = np.full((30, 60, 3), 230, dtype=np.uint8)          # a bright wall
    assert int(text_mask(rgb).sum()) == 0
    rgb[10:20, 20:40] = 20                                     # dark outline block
    rgb[13:17, 23:37] = 255                                    # white strokes inside it
    m = text_mask(rgb)
    assert m[13:17, 23:37].all()
    assert not m[0:5, :].any()
