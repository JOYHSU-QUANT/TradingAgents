"""The fixed split: three windows that tile a span, and the lock on the newest."""

from __future__ import annotations

import pytest

from contrib.autoresearch.split import Segment, SegmentName, Split, SplitError

from .conftest import ANCHOR_MS

_STEP = 4 * 60 * 60_000
_DAY = 6 * _STEP


def _split(bars: int = 100, **kwargs) -> Split:
    return Split.by_shares("4h", start_ms=ANCHOR_MS, end_ms=ANCHOR_MS + bars * _STEP, **kwargs)


# -- shape ----------------------------------------------------------------


def test_by_shares_cuts_sixty_twenty_twenty_with_the_holdout_newest():
    split = _split(100)
    assert split.train == Segment(SegmentName.TRAIN, ANCHOR_MS, ANCHOR_MS + 60 * _STEP)
    assert split.validation == Segment(
        SegmentName.VALIDATION, ANCHOR_MS + 60 * _STEP, ANCHOR_MS + 80 * _STEP
    )
    assert split.holdout == Segment(
        SegmentName.HOLDOUT, ANCHOR_MS + 80 * _STEP, ANCHOR_MS + 100 * _STEP
    )
    assert split.holdout.start_ms > split.validation.start_ms > split.train.start_ms


def test_a_share_that_is_not_exactly_representable_still_cuts_at_the_bar_it_names():
    """``0.7`` of 100 bars is bar 70, not the 69 that flooring ``span * 0.7`` gives."""
    split = _split(100, train_share=0.7, validation_share=0.15)
    assert split.train.end_ms == ANCHOR_MS + 70 * _STEP
    assert split.validation.end_ms == ANCHOR_MS + 85 * _STEP


def test_the_end_is_snapped_too_so_the_holdout_has_no_partial_tail():
    split = Split.by_shares("4h", start_ms=ANCHOR_MS, end_ms=ANCHOR_MS + 100 * _STEP + 1)
    assert split.holdout.end_ms == ANCHOR_MS + 100 * _STEP


def test_an_edge_outside_the_epoch_range_is_refused_as_a_split_error():
    """Not as the decoder's OverflowError from inside the refusal's own sentence."""
    with pytest.raises(SplitError, match="not a venue instant"):
        Segment(SegmentName.TRAIN, ANCHOR_MS, 10**20)
    with pytest.raises(SplitError, match="at or before it starts"):
        Segment(SegmentName.TRAIN, 10**20, 5)


def test_boundaries_are_snapped_onto_the_bar_grid():
    """A boundary inside a bar would let a bar belong to two windows depending on the stamp read."""
    split = _split(101)  # 60.6 and 80.8 bars in, before snapping to the nearest bar
    assert (split.train.end_ms - ANCHOR_MS) % _STEP == 0
    assert (split.validation.end_ms - ANCHOR_MS) % _STEP == 0
    assert split.train.end_ms == ANCHOR_MS + 61 * _STEP
    assert split.validation.end_ms == ANCHOR_MS + 81 * _STEP


def test_a_bar_belongs_to_the_window_it_opens_in():
    split = _split(10)
    open_times = [ANCHOR_MS + i * _STEP for i in range(10)]
    assert split.train.bar_range(open_times) == (0, 6)
    assert split.validation.bar_range(open_times) == (6, 8)
    assert split.holdout.bar_range(open_times) == (8, 10)
    # Half-open: the bar opening exactly on a boundary is the LATER window's.
    assert split.validation.bar_range(open_times)[0] == open_times.index(split.validation.start_ms)


def test_the_three_windows_must_tile_the_span():
    train = Segment(SegmentName.TRAIN, ANCHOR_MS, ANCHOR_MS + 6 * _STEP)
    validation = Segment(SegmentName.VALIDATION, ANCHOR_MS + 7 * _STEP, ANCHOR_MS + 8 * _STEP)
    holdout = Segment(SegmentName.HOLDOUT, ANCHOR_MS + 8 * _STEP, ANCHOR_MS + 10 * _STEP)
    with pytest.raises(SplitError, match="validation must begin where train ends"):
        Split("4h", train, validation, holdout)


def test_segments_are_held_to_their_own_names_and_order():
    train = Segment(SegmentName.TRAIN, ANCHOR_MS, ANCHOR_MS + 6 * _STEP)
    validation = Segment(SegmentName.VALIDATION, ANCHOR_MS + 6 * _STEP, ANCHOR_MS + 8 * _STEP)
    holdout = Segment(SegmentName.HOLDOUT, ANCHOR_MS + 8 * _STEP, ANCHOR_MS + 10 * _STEP)
    with pytest.raises(SplitError, match="the train segment is named 'validation'"):
        Split("4h", validation, train, holdout)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"train_share": 0}, "train_share is a share"),
        ({"validation_share": 1.5}, "validation_share is a share"),
        ({"train_share": 0.8, "validation_share": 0.2}, "leave room for a holdout"),
    ],
)
def test_shares_that_leave_no_room_for_three_windows_are_refused(kwargs, message):
    with pytest.raises(SplitError, match=message):
        _split(100, **kwargs)


def test_an_empty_or_backwards_segment_is_refused():
    with pytest.raises(SplitError, match="ends .* at or before it starts"):
        Segment(SegmentName.TRAIN, ANCHOR_MS, ANCHOR_MS)
    with pytest.raises(SplitError, match="a segment edge is epoch ms"):
        Segment(SegmentName.TRAIN, ANCHOR_MS, 1.5)
    with pytest.raises(SplitError, match="split segment"):
        Segment("trian", ANCHOR_MS, ANCHOR_MS + _STEP)


def test_the_interval_is_the_package_s_own_two_and_nothing_else():
    """``1h`` is a venue interval and not a studied one; the CLI refuses it and so must a split."""
    with pytest.raises(SplitError, match=r"studies \['4h', '1d'\] candles, not '1h'"):
        Split.by_shares("1h", start_ms=ANCHOR_MS, end_ms=ANCHOR_MS + _DAY)
    with pytest.raises(SplitError, match="not '1h'"):
        Split.from_dict({**_split(100).to_dict(), "interval": "1h"})
    with pytest.raises(SplitError, match="interval: "):
        Split.from_dict({**_split(100).to_dict(), "interval": None})
    assert _split(100).interval == "4h"
    daily = Split.by_shares("1d", start_ms=ANCHOR_MS, end_ms=ANCHOR_MS + 10 * _DAY)
    assert daily.interval == "1d"
    assert daily.train.end_ms == ANCHOR_MS + 6 * _DAY


# -- the lock ---------------------------------------------------------------


def test_the_holdout_is_only_handed_out_when_asked_for_by_name():
    split = _split(100)
    assert [segment.name for segment in split.segments()] == [
        SegmentName.TRAIN,
        SegmentName.VALIDATION,
    ]
    assert [segment.name for segment in split.segments(holdout=True)] == list(SegmentName)


def test_loadable_until_stops_one_millisecond_before_the_holdout_opens():
    """The last bar an unpromoted trial needs is the one that OPENS before the holdout.

    A position still open at that bar's close is flattened there, so no bar
    of the holdout is read — and the store's inclusive ``until_ms`` therefore
    stops just short of the holdout's first ``open_time``.
    """
    split = _split(100)
    assert split.loadable_until() == split.holdout.start_ms - 1
    assert split.loadable_until(holdout=True) == split.holdout.end_ms - 1


# -- the record ---------------------------------------------------------------


def test_the_record_round_trips():
    split = _split(100)
    record = split.to_dict()
    assert record["interval"] == "4h"
    assert record["holdout"] == {
        "start_ms": split.holdout.start_ms,
        "end_ms": split.holdout.end_ms,
    }
    assert Split.from_dict(record) == split


def test_a_record_with_the_wrong_keys_is_refused():
    record = _split(100).to_dict()
    with pytest.raises(SplitError, match="exactly the keys"):
        Split.from_dict({**record, "test": {}})
    with pytest.raises(SplitError, match="validation: expected"):
        Split.from_dict({**record, "validation": {"start_ms": 1}})


def test_the_description_names_each_window_in_order():
    lines = _split(100).describe()
    assert len(lines) == 3
    assert lines[0].startswith("split (4h): train: ")
    assert lines[2].startswith("split (4h): holdout: ")
