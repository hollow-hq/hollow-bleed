"""Unit tests for duration helpers in cogs.moderation.modsys (no Discord I/O)."""

import pytest

from cogs.moderation.modsys import (
    _MAX_DURATION,
    _MAX_TIMEOUT,
    humanize_seconds,
    join_attachments,
    parse_duration,
    split_attachments,
)


def test_parse_simple_units():
    assert parse_duration("10s") == 10
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("1w") == 604800


def test_parse_combined_and_spaced():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2 days") == 2 * 86400
    assert parse_duration("1.5h") == 5400


def test_parse_bare_seconds_and_clock():
    assert parse_duration("30") == 30
    assert parse_duration("1:30") == 90
    assert parse_duration("1:02:03") == 3723


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_duration("")
    with pytest.raises(ValueError):
        parse_duration("banana")
    with pytest.raises(ValueError):
        parse_duration("10 parsecs")
    with pytest.raises(ValueError):
        parse_duration("0s")
    with pytest.raises(ValueError):
        parse_duration("-5m")


def test_parse_maximum_clamp():
    with pytest.raises(ValueError):
        parse_duration("9999d", maximum=100)
    with pytest.raises(ValueError):
        parse_duration("29d", maximum=_MAX_TIMEOUT)
    # Within timeout bounds is fine.
    assert parse_duration("27d", maximum=_MAX_TIMEOUT) == 27 * 86400


def test_humanize_seconds():
    assert humanize_seconds(None) == "N/A"
    assert humanize_seconds(0) == "0s"
    assert humanize_seconds(45) == "45s"
    assert humanize_seconds(90) == "1m"
    assert humanize_seconds(5400) == "1h 30m"
    assert humanize_seconds(90000) == "1d 1h"


def test_attachments_roundtrip():
    assert split_attachments(None) == []
    assert split_attachments("") == []
    assert split_attachments("a,b , c") == ["a", "b", "c"]
    assert join_attachments(["a", "a ", "b", ""]) == "a,b"
    assert join_attachments([]) == ""
