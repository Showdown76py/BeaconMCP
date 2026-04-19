"""Unit tests for beaconmcp.utils (filter_fields, parse_since)."""

from __future__ import annotations

import time

import pytest

from beaconmcp.utils import filter_fields, parse_since


def test_filter_fields_none_returns_input() -> None:
    assert filter_fields({"a": 1, "b": 2}, None) == {"a": 1, "b": 2}


def test_filter_fields_empty_returns_input() -> None:
    assert filter_fields({"a": 1}, []) == {"a": 1}


def test_filter_fields_dict_trims_keys() -> None:
    assert filter_fields({"a": 1, "b": 2, "c": 3}, ["a", "c"]) == {"a": 1, "c": 3}


def test_filter_fields_dict_missing_keys_silently_dropped() -> None:
    assert filter_fields({"a": 1}, ["a", "missing"]) == {"a": 1}


def test_filter_fields_list_of_dicts() -> None:
    data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert filter_fields(data, ["a"]) == [{"a": 1}, {"a": 3}]


def test_filter_fields_non_dict_values_passthrough() -> None:
    data = [{"a": 1}, "not-a-dict", 42]
    assert filter_fields(data, ["a"]) == [{"a": 1}, "not-a-dict", 42]


def test_filter_fields_primitive_passthrough() -> None:
    assert filter_fields(42, ["anything"]) == 42
    assert filter_fields("str", ["anything"]) == "str"
    assert filter_fields(None, ["anything"]) is None


def test_parse_since_none_returns_none() -> None:
    assert parse_since(None) is None
    assert parse_since("") is None
    assert parse_since(0) is None


def test_parse_since_duration_units() -> None:
    now = 1_000_000.0
    assert parse_since("15m", now=now) == int(now - 900)
    assert parse_since("2h", now=now) == int(now - 7200)
    assert parse_since("1d", now=now) == int(now - 86400)
    assert parse_since("30s", now=now) == int(now - 30)


def test_parse_since_numeric_epoch() -> None:
    assert parse_since(1_700_000_000) == 1_700_000_000
    assert parse_since("1700000000") == 1_700_000_000


def test_parse_since_iso8601() -> None:
    # 2024-01-01T00:00:00Z == 1704067200
    assert parse_since("2024-01-01T00:00:00Z") == 1_704_067_200


def test_parse_since_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_since("not-a-duration")
