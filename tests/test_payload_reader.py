"""The strict JSON reader (docs/interface.md, Input shapes). One module, tested once."""

from __future__ import annotations

import time
from typing import Any

import pytest

from notice_route.payload import INPUT_MAX, StrictJsonError, parse_json_object


def ok(text: str | bytes) -> dict[str, Any]:
    data = text.encode() if isinstance(text, str) else text
    return parse_json_object(data)


def bad(text: str | bytes) -> StrictJsonError:
    data = text.encode() if isinstance(text, str) else text
    with pytest.raises(StrictJsonError) as info:
        parse_json_object(data)
    return info.value


def test_plain_object_parses() -> None:
    assert ok('{"a": 1, "b": [true, null, 1.5, "x"], "c": {"d": {}}}') == {
        "a": 1,
        "b": [True, None, 1.5, "x"],
        "c": {"d": {}},
    }


def test_surrounding_json_whitespace_is_allowed() -> None:
    assert ok(' \t\r\n{"a": 1}\n\n') == {"a": 1}


# --- size -----------------------------------------------------------------------------------


def test_limit_is_1048576_bytes() -> None:
    assert INPUT_MAX == 1048576


def test_exactly_the_limit_parses_and_one_more_is_refused() -> None:
    head = b'{"a": "'
    tail = b'"}'
    body = head + b"x" * (INPUT_MAX - len(head) - len(tail)) + tail
    assert len(body) == INPUT_MAX
    assert len(ok(body)["a"]) > 1000
    bad(body + b" ")


# --- bytes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b'{"a": "\xff"}',  # invalid byte
        b'{"a": "\xc3"}',  # truncated sequence
        b'{"a": "\xed\xa0\x80"}',  # a UTF-8 encoded surrogate
        b'{"a": "\xc0\xaf"}',  # overlong
        b'{"a": "\xf4\x90\x80\x80"}',  # past U+10FFFF
        b'\xff\xfe{\x00"\x00a\x00"\x00:\x001\x00}\x00',  # UTF-16 with its BOM
    ],
)
def test_invalid_utf8_is_refused(data: bytes) -> None:
    bad(data)


def test_byte_order_mark_is_refused() -> None:
    bad(b'\xef\xbb\xbf{"a": 1}')


def test_bom_character_inside_a_string_is_ordinary_text() -> None:
    assert ok('{"a": "﻿"}') == {"a": "﻿"}


def test_empty_and_whitespace_only_are_refused() -> None:
    bad(b"")
    bad(b"  \n ")


def test_raw_nul_byte_is_refused_but_an_escaped_nul_is_text() -> None:
    bad(b'{"a": "x\x00y"}')
    bad(b'{"a": 1}\x00')
    assert ok('{"a": "x\\u0000y"}') == {"a": "x\x00y"}


@pytest.mark.parametrize("ch", ["\x01", "\n", "\t", "\x1f"])
def test_raw_control_characters_in_a_string_are_refused(ch: str) -> None:
    bad('{"a": "x' + ch + 'y"}')


# --- structure ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["[]", "[{}]", '"x"', "1", "true", "null", "1.5", '[{"a": 1}]'],
)
def test_top_level_must_be_an_object(text: str) -> None:
    bad(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1} x',
        '{"a": 1}{"b": 2}',
        '{"a": 1}}',
        '{"a": 1},',
        '{"a": 1} // c',
        '{"a": 1,}',
        "{'a': 1}",
        '{"a" 1}',
        '{"a": }',
        "{a: 1}",
        '{"a": 1',
        '{"a": "x',
        '{"a": tru}',
        '{"a": 01}',
        '{"a": +1}',
        '{"a": .5}',
        '{"a": 1.}',
        '{"a": "\\x"}',
        '{"a": "\\u12"}',
    ],
)
def test_malformed_json_is_refused(text: str) -> None:
    bad(text)


# --- duplicate keys -------------------------------------------------------------------------


def test_duplicate_key_is_refused() -> None:
    bad('{"a": 1, "a": 2}')


def test_duplicate_key_at_depth_is_refused() -> None:
    bad('{"a": {"b": [{"c": 1, "c": 1}]}}')


def test_duplicate_key_after_unescaping_is_refused() -> None:
    bad('{"a": 1, "\\u0061": 2}')
    bad('{"\\u00e9": 1, "é": 2}')


def test_distinct_keys_that_differ_in_case_or_form_are_fine() -> None:
    assert len(ok('{"a": 1, "A": 2, "\\u00e9": 3, "e\\u0301": 4}')) == 4


def test_lone_surrogate_keys_that_collide_after_replacement_are_refused() -> None:
    bad('{"\\ud800": 1, "\\ud801": 2}')
    bad('{"\\ud800": 1, "\\ufffd": 2}')


# --- numbers --------------------------------------------------------------------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_constants_are_refused(literal: str) -> None:
    bad('{"a": ' + literal + "}")
    bad('{"a": [' + literal + "]}")


@pytest.mark.parametrize("literal", ["1e999", "-1e999", "1E400", "1e99999999999", "9" * 400 + ".0"])
def test_floats_that_do_not_fit_a_double_are_refused(literal: str) -> None:
    bad('{"a": ' + literal + "}")


def test_floats_that_fit_are_accepted() -> None:
    assert ok('{"a": 1.7976931348623157e308}') == {"a": 1.7976931348623157e308}
    assert ok('{"a": 1e-999}') == {"a": 0.0}
    assert ok('{"a": -0.0}') == {"a": -0.0}


def test_integer_range_is_signed_64_bit() -> None:
    assert ok('{"a": 9223372036854775807}') == {"a": 2**63 - 1}
    assert ok('{"a": -9223372036854775808}') == {"a": -(2**63)}
    bad('{"a": 9223372036854775808}')
    bad('{"a": -9223372036854775809}')


def test_huge_integers_are_refused_without_slow_parsing() -> None:
    started = time.monotonic()
    bad('{"a": ' + "9" * 200_000 + "}")
    bad('{"a": -' + "9" * 200_000 + "}")
    bad('{"a": 1' + "0" * 5000 + "}")
    assert time.monotonic() - started < 2


def test_integers_in_nested_places_are_range_checked() -> None:
    bad('{"a": [{"b": 18446744073709551616}]}')


def test_exponent_literals_are_floats_not_integers() -> None:
    value = ok('{"a": 1e2}')["a"]
    assert isinstance(value, float)


def test_negative_zero_integer_is_zero() -> None:
    assert ok('{"a": -0}') == {"a": 0}


# --- depth ----------------------------------------------------------------------------------


def _nest(depth: int) -> str:
    """An object of the given depth (the top-level object is depth 1)."""
    return '{"a":' * (depth - 1) + "{}" + "}" * (depth - 1)


def test_depth_16_is_accepted_and_17_is_refused() -> None:
    assert ok(_nest(16))
    bad(_nest(17))


def test_arrays_count_toward_depth() -> None:
    assert ok('{"a":' + "[" * 15 + "]" * 15 + "}")
    bad('{"a":' + "[" * 16 + "]" * 16 + "}")


def test_brackets_inside_strings_do_not_count() -> None:
    assert ok('{"a": "' + "[" * 100 + '", "b": "\\"' + "{" * 100 + '"}')


def test_extreme_depth_is_refused_quickly_and_without_recursion_errors() -> None:
    started = time.monotonic()
    bad("[" * 500_000)
    bad('{"a":' * 100_000)
    bad("[" * 200_000 + "]" * 200_000)
    assert time.monotonic() - started < 5


def test_unbalanced_closers_do_not_hide_depth() -> None:
    bad('{"a":' + "[" * 17 + "]" * 17 + "}")


def test_many_quotes_and_backslashes_scan_in_linear_time() -> None:
    started = time.monotonic()
    bad(b'{"a": "' + b"\\" * 500_000)
    bad(b'"' * 500_000)
    bad(b'{"a": "' + b'\\"' * 300_000)
    assert time.monotonic() - started < 5


# --- lone surrogates ------------------------------------------------------------------------


def test_lone_surrogate_in_a_value_becomes_replacement_character() -> None:
    assert ok('{"a": "x\\ud800y"}') == {"a": "x�y"}
    assert ok('{"a": "\\udfff"}') == {"a": "�"}


def test_lone_surrogate_deep_in_a_structure_is_replaced() -> None:
    assert ok('{"a": [{"b": ["\\ud800"]}]}') == {"a": [{"b": ["�"]}]}


def test_lone_surrogate_in_a_key_is_replaced() -> None:
    assert ok('{"\\ud800": 1}') == {"�": 1}


def test_valid_surrogate_pair_is_one_astral_character() -> None:
    assert ok('{"a": "\\ud83d\\ude00"}') == {"a": "\U0001f600"}


def test_reversed_pair_is_two_lone_surrogates() -> None:
    assert ok('{"a": "\\ude00\\ud83d"}') == {"a": "��"}


# --- errors never carry input ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"marker_xyz": 1, "marker_xyz": 2}',
        '{"a": marker_xyz}',
        '{"a": NaN, "marker_xyz": 1}',
        '[{"marker_xyz": 1}]',
        '{"marker_xyz": 1e999}',
        '{"marker_xyz": 99999999999999999999}',
        '{"marker_xyz": "\xff"}',
    ],
)
def test_error_text_and_chain_never_carry_input(text: str) -> None:
    error = bad(text.encode("latin-1") if "\xff" in text else text)
    assert "marker_xyz" not in str(error)
    assert "marker_xyz" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.args == (str(error),)


def test_error_is_a_value_error_with_a_fixed_message() -> None:
    assert isinstance(bad("[]"), ValueError)
    assert str(bad("[]")) == str(bad('{"a": NaN}'))
