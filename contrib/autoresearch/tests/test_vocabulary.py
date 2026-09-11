"""The vocabulary is closed, self-describing, and refuses by name.

Every test here is about one of those three claims. They are the properties
the hypothesis loop rests on: a generator can only propose what this module
admits, an operator and the model are shown the same listing, and a refusal
says which edit would fix it rather than only that something was wrong.
"""

from __future__ import annotations

import pytest

from contrib.autoresearch.vocabulary import (
    MAX_OFFSET_BARS,
    FeatureKind,
    FeatureRef,
    FeatureUnit,
    SeriesSource,
    SpecError,
    describe_vocabulary,
    feature_names,
    parse_feature_name,
    periods_for,
    spec_of,
)


def test_every_legal_name_parses_back_to_the_reference_that_spells_it():
    """A round trip over the WHOLE vocabulary, not over a sample of it."""
    for name in feature_names():
        kind, period = parse_feature_name(name)
        assert FeatureRef(kind, period).name == name


def test_the_vocabulary_is_not_empty_and_covers_every_kind():
    """Guard the round trip above: an empty listing would satisfy it vacuously."""
    assert feature_names()
    for kind in FeatureKind:
        spelled = [
            name
            for name in feature_names()
            if name == kind.value or name.startswith(f"{kind.value}_")
        ]
        assert spelled, f"{kind.value} has no legal spelling"


def test_a_longer_stem_wins_over_a_shorter_one_that_prefixes_it():
    """``close_1d`` is a kind, not ``close`` wearing a period.

    Asserted on the REFUSALS, because the accepted names cannot show it: every
    legal spelling is answered by the name table before the stem search runs,
    so ordering the stems shortest-first leaves all three lookups below
    unchanged. Where it shows is the sentence an illegal name gets — and a
    ``sma_1d_9`` told that ``sma`` has no period ``1d_9`` has been refused for
    a reason with nothing to do with what was written.
    """
    assert parse_feature_name("close_1d") == (FeatureKind.CLOSE_1D, None)
    assert parse_feature_name("sma_1d_50") == (FeatureKind.SMA_1D, 50)
    assert parse_feature_name("sma_50") == (FeatureKind.SMA, 50)

    with pytest.raises(SpecError) as daily_period:
        parse_feature_name("sma_1d_9")
    assert "'sma_1d' has no period '9'" in str(daily_period.value)
    assert "[20, 50, 200] days" in str(daily_period.value)

    with pytest.raises(SpecError) as daily_close:
        parse_feature_name("close_1d_20")
    assert "'close_1d' takes no period" in str(daily_close.value)


def test_an_impossible_period_on_a_real_stem_is_told_which_periods_exist():
    with pytest.raises(SpecError) as caught:
        parse_feature_name("ema_9")
    message = str(caught.value)
    assert "'ema' has no period '9'" in message
    assert "[20, 50] bars" in message


def test_a_period_on_a_stem_that_takes_none_says_how_to_write_it():
    with pytest.raises(SpecError) as caught:
        parse_feature_name("regime_20")
    assert "'regime' takes no period" in str(caught.value)


@pytest.mark.parametrize(
    ("bare", "wanted"),
    [("ema", "ema_20"), ("sma_1d", "sma_1d_20"), ("atr_pct", "atr_pct_14")],
)
def test_a_parameterised_stem_written_bare_is_told_it_needs_a_period(bare, wanted):
    """And it is told about ITS OWN stem, including when that stem contains one.

    ``sma_1d`` does not start with ``sma_1d_`` but does start with ``sma_``, so
    a bare one was refused as "sma has no period '1d'" — which steers the
    author (a model, next round) towards ``sma_20``: a legal name, for a
    twenty-BAR mean. The hypothesis then scored is not the one written.
    """
    with pytest.raises(SpecError) as caught:
        parse_feature_name(bare)
    message = str(caught.value)
    assert f"{bare!r} needs a period" in message
    assert wanted in message


def test_an_unknown_name_is_told_the_vocabulary_is_closed_and_where_to_look():
    with pytest.raises(SpecError) as caught:
        parse_feature_name("bollinger_20")
    message = str(caught.value)
    assert "The vocabulary is closed" in message
    assert "contrib.autoresearch vocab" in message


def test_a_near_miss_is_offered_the_nearest_legal_name():
    """A misspelling has no stem to report a period against, so it gets a suggestion.

    ``rsi14`` is the shape this catches: a name with the underscore left out
    matches no stem at all, so without the suggestion the author would be
    told only that the vocabulary is closed — true, and no help.
    """
    with pytest.raises(SpecError) as caught:
        parse_feature_name("rsi14")
    assert "Did you mean 'rsi_14'?" in str(caught.value)


def test_a_feature_name_that_is_not_a_string_is_refused_without_a_type_error():
    with pytest.raises(SpecError) as caught:
        parse_feature_name(20, path="spec.entry.long[0].left")
    assert "spec.entry.long[0].left: a feature is named by a string" in str(caught.value)


def test_the_path_a_caller_names_is_carried_into_the_refusal():
    with pytest.raises(SpecError) as caught:
        parse_feature_name("ema_9", path="spec.filters[2].right")
    assert str(caught.value).startswith("spec.filters[2].right: ")


# -- references ------------------------------------------------------------


def test_a_reference_built_in_code_is_checked_against_the_same_table():
    """The parser is not the only way to make one, so it is not the only guard."""
    with pytest.raises(SpecError):
        FeatureRef(FeatureKind.EMA, 9)
    with pytest.raises(SpecError):
        FeatureRef(FeatureKind.REGIME, 20)
    with pytest.raises(SpecError):
        FeatureRef(FeatureKind.SMA)  # a parameterised kind with no period


def test_a_negative_offset_is_refused_as_a_bar_that_has_not_closed():
    """Plan §3.6(a): the leakage guard, stated where every reference passes."""
    with pytest.raises(SpecError) as caught:
        FeatureRef(FeatureKind.CLOSE, None, -1)
    message = str(caught.value)
    assert "has not closed" in message
    assert "0 is this bar, 1 the one before it" in message


def test_an_offset_reaching_further_back_than_the_bound_is_refused():
    assert FeatureRef(FeatureKind.CLOSE, None, MAX_OFFSET_BARS).offset == MAX_OFFSET_BARS
    with pytest.raises(SpecError) as caught:
        FeatureRef(FeatureKind.CLOSE, None, MAX_OFFSET_BARS + 1)
    assert f"further back than {MAX_OFFSET_BARS} bars" in str(caught.value)


def test_a_boolean_offset_is_not_a_whole_number_of_bars():
    """``isinstance(True, int)`` is true, so ``offset=True`` would silently mean 1."""
    with pytest.raises(SpecError) as caught:
        FeatureRef(FeatureKind.CLOSE, None, True)
    assert "whole number of bars" in str(caught.value)


@pytest.mark.parametrize("period", [True, 20.0], ids=["bool", "float"])
def test_a_period_that_is_not_a_whole_number_is_refused_even_when_it_compares_equal(period):
    """``True == 1`` and ``20.0 == 20``, so membership alone lets both through.

    What they break is the round trip: they render as ``ret_True`` and
    ``sma_20.0``, names ``parse_feature_name`` refuses — so a spec the ledger
    wrote back out could not be read in again.
    """
    with pytest.raises(SpecError) as caught:
        FeatureRef(FeatureKind.SMA if period == 20.0 else FeatureKind.RET, period)
    assert "a period is a whole number" in str(caught.value)


def test_a_level_and_a_distance_are_not_the_same_unit():
    """``atr_14`` is quote currency, and is not a price a close can be above.

    The first cut of this table gave it ``price``, which re-admitted the
    comparison the units exist to refuse: ``close > atr_14`` is true at
    essentially every bar.
    """
    assert FeatureRef(FeatureKind.CLOSE).unit is FeatureUnit.PRICE
    assert FeatureRef(FeatureKind.ATR, 14).unit is FeatureUnit.PRICE_SPAN
    assert FeatureRef(FeatureKind.DONCHIAN_HIGH, 20).unit is FeatureUnit.PRICE


def test_one_settlement_and_a_sum_of_settlements_are_not_the_same_unit():
    assert FeatureRef(FeatureKind.FUNDING_RATE).unit is FeatureUnit.RATE
    assert FeatureRef(FeatureKind.FUNDING_CUM, 24).unit is FeatureUnit.RATE_SUM


def test_a_reference_reads_back_the_way_a_report_prints_it():
    assert str(FeatureRef(FeatureKind.CLOSE)) == "close"
    assert str(FeatureRef(FeatureKind.CLOSE, None, 2)) == "close[2]"
    assert str(FeatureRef(FeatureKind.SMA, 50)) == "sma_50"


@pytest.mark.parametrize(
    ("name", "unit", "source"),
    [
        ("close", FeatureUnit.PRICE, SeriesSource.BARS),
        ("rsi_14", FeatureUnit.OSCILLATOR, SeriesSource.BARS),
        ("ret_6", FeatureUnit.RETURN, SeriesSource.BARS),
        ("close_1d", FeatureUnit.PRICE, SeriesSource.DAILY),
        ("funding_rate", FeatureUnit.RATE, SeriesSource.FUNDING),
        ("funding_zscore_30", FeatureUnit.SCORE, SeriesSource.FUNDING),
        ("regime", FeatureUnit.REGIME, SeriesSource.BARS),
    ],
)
def test_a_feature_carries_what_it_measures_and_where_it_comes_from(name, unit, source):
    """Both are load-bearing: the unit gates comparisons, the source gates refusals."""
    kind, period = parse_feature_name(name)
    ref = FeatureRef(kind, period)
    assert ref.unit is unit
    assert ref.source is source


# -- the listing -----------------------------------------------------------


def test_the_listing_covers_every_kind_and_every_period_it_accepts():
    """Plan §4: the model is shown a listing GENERATED from the parser's table.

    Each claim is checked against that kind's OWN row, not against the whole
    blob. Matched loosely, a row that had lost ``100, 200`` from ``sma_N``
    still passed, because ``100`` appears in the RSI row's "0..100" and
    ``200`` in the daily row's own periods — a listing missing exactly what
    this test is for, reading as complete.
    """
    rows = {line.split("  ", 1)[0]: line for line in describe_vocabulary()}
    for kind in FeatureKind:
        periods = periods_for(kind)
        name = f"{kind.value}_N" if periods else kind.value
        assert name in rows, f"{kind.value} has no row in the listing"
        row = rows[name]
        assert spec_of(kind).unit.value in row
        assert spec_of(kind).source.value in row
        if periods:
            noun = spec_of(kind).period_noun
            listed = row.rsplit("N: ", 1)[1]
            assert listed.endswith(f"{noun}."), row  # the periods say what they COUNT
            numbers = listed.removesuffix(f"{noun}.").strip().split(", ")
            assert numbers == [str(period) for period in periods], row


def test_the_listing_explains_the_offset_syntax_and_the_unit_rule():
    """The two rules a spec author cannot infer from the names alone."""
    trailer = describe_vocabulary()[-1]
    assert '"offset"' in trailer
    assert str(MAX_OFFSET_BARS) in trailer
    assert "same unit" in trailer
