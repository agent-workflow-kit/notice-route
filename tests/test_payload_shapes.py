"""Shape discrimination, source rules, field validation, labels and ids (Input shapes)."""

from __future__ import annotations

from typing import Any

import pytest

from notice_route.payload import (
    Notification,
    PayloadError,
    classify,
    notice_from_flags,
)

MARK = "marker_xyz"


def hook(**extra: Any) -> dict[str, Any]:
    return {"tool": "agent-spawn", "hook": "post-merge", **extra}


def refused(
    obj: dict[str, Any],
    *,
    source: str | None = None,
    labels: tuple[str, ...] = (),
) -> PayloadError:
    with pytest.raises(PayloadError) as info:
        classify(obj, source=source, labels=labels)
    return info.value


def check(error: PayloadError, reason: str, pointer: str | None) -> None:
    assert error.reason == reason
    assert error.pointer == pointer
    assert error.exit_code == 20


# --- shapes ---------------------------------------------------------------------------------


def test_hook_payload_shape() -> None:
    note = classify(hook(repo="h/o/r", extra={"a": 1}), source=None, labels=())
    assert (note.shape, note.source, note.hook) == ("hook", "agent-spawn", "post-merge")
    assert note.repo == "h/o/r"
    assert note.payload["extra"] == {"a": 1}
    assert note.title is None and note.message is None and note.level is None


def test_channel_notice_shape_with_default_source() -> None:
    note = classify({"message": "hi"}, source=None, labels=())
    assert (note.shape, note.source, note.hook) == ("notice", "direct", None)
    assert note.message == "hi"
    assert note.title is None and note.level is None and note.link is None


def test_channel_notice_with_every_field_and_a_source_flag() -> None:
    obj = {"title": "t", "message": "m", "level": "warn", "link": "https://example.org/x"}
    note = classify(obj, source="my-script", labels=())
    assert note.source == "my-script"
    assert (note.title, note.message, note.level, note.link) == (
        "t",
        "m",
        "warn",
        "https://example.org/x",
    )


@pytest.mark.parametrize(
    "obj",
    [
        {},
        {"title": "t"},
        {"title": "t", "level": "info"},
        {"message": "m", "extra": 1},
        {"message": "m", "tool": "x"},
        {"tool": "x"},
        {"hook": "x"},
        {"tool": 1, "hook": "x"},
        {"tool": "x", "hook": 1},
        {"tool": None, "hook": None},
        {"tool": ["x"], "hook": {"a": 1}},
        {"tool": "x", "message": "m"},
        {"Message": "m"},
        {"message": "m", "labels": ["a=b"]},
    ],
)
def test_anything_else_is_unrecognized(obj: dict[str, Any]) -> None:
    check(refused(obj), "payload_unrecognized", None)


def test_hook_payload_wins_over_notice_keys() -> None:
    note = classify(hook(message="m", title="t", level="bogus"), source=None, labels=())
    assert note.shape == "hook"


def test_unrecognized_error_does_not_echo_keys() -> None:
    error = refused({MARK: 1})
    assert MARK not in str(error) and MARK not in repr(error)
    assert error.to_error() == {
        "error": {
            "code": "refused",
            "message": error.message,
            "reason": "payload_unrecognized",
            "details": None,
        }
    }


# --- tool, hook, source ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["", "A", "-x", "_x", "x" * 65, "x y", "x/y", "é", "x\n", "x.y", "notice route", "\x00"],
)
def test_bad_tool_is_invalid_at_tool(tool: str) -> None:
    check(refused(hook(tool=tool)), "payload_invalid", "/tool")


@pytest.mark.parametrize("tool", ["a", "0", "9x", "a-b_c", "x" * 64, "agent-spawn"])
def test_good_tools(tool: str) -> None:
    assert classify(hook(tool=tool), source=None, labels=()).source == tool


@pytest.mark.parametrize(
    "name",
    ["", "A", "1x", "-x", "x" * 65, "x y", "x.y", "x\n", "é", "x/y"],
)
def test_bad_hook_is_invalid_at_hook(name: str) -> None:
    check(refused(hook(hook=name)), "payload_invalid", "/hook")


@pytest.mark.parametrize("name", ["a", "post-merge", "a_b-c9", "x" * 64])
def test_good_hooks(name: str) -> None:
    assert classify(hook(hook=name), source=None, labels=()).hook == name


def test_order_tool_before_hook_before_source() -> None:
    check(refused(hook(tool="A", hook="A"), source="B"), "payload_invalid", "/tool")
    check(refused(hook(hook="A"), source="B"), "payload_invalid", "/hook")
    check(refused(hook(), source="B"), "payload_invalid", "--source")


@pytest.mark.parametrize("source", ["", "A", "-x", "x" * 65, "x y", "x\n", "é"])
def test_bad_source_flag_is_invalid_at_source_for_both_shapes(source: str) -> None:
    check(refused(hook(), source=source), "payload_invalid", "--source")
    check(refused({"message": "m"}, source=source), "payload_invalid", "--source")


def test_source_flag_may_repeat_the_tool() -> None:
    assert classify(hook(), source="agent-spawn", labels=()).source == "agent-spawn"


def test_source_flag_that_differs_from_the_tool_is_a_mismatch() -> None:
    check(refused(hook(), source="other"), "source_mismatch", None)


def test_reserved_source_is_refused_for_both_shapes() -> None:
    check(refused(hook(tool="notice-route")), "source_mismatch", None)
    check(refused(hook(tool="notice-route"), source="notice-route"), "source_mismatch", None)
    check(refused({"message": "m"}, source="notice-route"), "source_mismatch", None)


def test_source_mismatch_comes_before_notice_fields_and_labels() -> None:
    check(refused(hook(), source="other", labels=("bad",)), "source_mismatch", None)
    check(
        refused({"message": 5}, source="notice-route", labels=("bad",)),
        "source_mismatch",
        None,
    )


# --- notice fields --------------------------------------------------------------------------


def test_title_rules() -> None:
    assert classify({"message": "m", "title": ""}, source=None, labels=()).title == ""
    assert (
        len(classify({"message": "m", "title": "x" * 100000}, source=None, labels=()).title or "")
        == 100000
    )
    check(refused({"message": "m", "title": "x" * 100001}), "payload_invalid", "/title")
    for bad in (None, 1, True, ["t"], {"a": 1}):
        check(refused({"message": "m", "title": bad}), "payload_invalid", "/title")


def test_message_rules() -> None:
    assert classify({"message": "x" * 100000}, source=None, labels=()).message == "x" * 100000
    check(refused({"message": "x" * 100001}), "payload_invalid", "/message")
    check(refused({"message": ""}), "payload_invalid", "/message")
    for bad in (None, 1, False, ["m"], {"a": 1}):
        check(refused({"message": bad}), "payload_invalid", "/message")


def test_length_counts_code_points_not_bytes() -> None:
    assert classify({"message": "é" * 100000}, source=None, labels=()).message
    assert classify({"message": "\U0001f600" * 100000}, source=None, labels=()).message
    check(refused({"message": "\U0001f600" * 100001}), "payload_invalid", "/message")


@pytest.mark.parametrize("level", ["info", "warn", "action", "error"])
def test_levels(level: str) -> None:
    assert classify({"message": "m", "level": level}, source=None, labels=()).level == level


@pytest.mark.parametrize("level", ["INFO", "debug", "", None, 1, True, ["info"], "info "])
def test_bad_level(level: object) -> None:
    check(refused({"message": "m", "level": level}), "payload_invalid", "/level")


@pytest.mark.parametrize(
    "link",
    ["http://example.org", "https://example.org/a?b=c#d", "https://" + "x" * 1016],
)
def test_good_links(link: str) -> None:
    assert classify({"message": "m", "link": link}, source=None, labels=()).link == link


@pytest.mark.parametrize(
    "link",
    [
        "",
        "HTTPS://example.org",
        "ftp://example.org",
        "javascript:alert(1)",
        " https://example.org",
        "https://example.org/a b",
        "https://example.org/\n",
        "https://example.org/é",
        "https://example.org/\x7f",
        "https://",
        "http://",
        "https://" + "x" * 1017,
        None,
        5,
        ["https://example.org"],
    ],
)
def test_bad_links(link: object) -> None:
    check(refused({"message": "m", "link": link}), "payload_invalid", "/link")


def test_notice_fields_are_checked_in_order() -> None:
    bad = {"title": 1, "message": 1, "level": 1, "link": 1}
    check(refused(bad), "payload_invalid", "/title")
    check(refused({**bad, "title": "t"}), "payload_invalid", "/message")
    check(refused({**bad, "title": "t", "message": "m"}), "payload_invalid", "/level")
    check(
        refused({**bad, "title": "t", "message": "m", "level": "info"}),
        "payload_invalid",
        "/link",
    )


def test_notice_fields_come_before_label_flags() -> None:
    check(refused({"message": 1}, labels=("bad",)), "payload_invalid", "/message")


def test_lone_surrogate_text_counts_as_text() -> None:
    # The reader already replaced it; a flag value can still carry one (argv).
    note = notice_from_flags(title="t\udc80", message="m\udc80", source=None, labels=())
    assert note.title == "t�" and note.message == "m�"


def test_invalid_field_errors_never_echo_values() -> None:
    for obj in (
        {"message": MARK * 100000},
        {"message": "m", "level": MARK},
        {"message": "m", "link": MARK},
        {"message": "m", "title": [MARK]},
    ):
        error = refused(obj)
        assert MARK not in str(error) and MARK not in repr(error)
        assert MARK not in repr(error.to_error())
        assert error.__cause__ is None and error.__context__ is None


# --- flags build a notice -------------------------------------------------------------------


def test_flag_notice_defaults_and_source() -> None:
    note = notice_from_flags(title=None, message="hello", source=None, labels=())
    assert (note.shape, note.source, note.message, note.title) == (
        "notice",
        "direct",
        "hello",
        None,
    )
    note = notice_from_flags(title="T", message="hello", source="cron-job", labels=())
    assert (note.source, note.title) == ("cron-job", "T")


def test_flag_notice_is_validated_like_json() -> None:
    with pytest.raises(PayloadError) as info:
        notice_from_flags(title=None, message="", source=None, labels=())
    check(info.value, "payload_invalid", "/message")
    with pytest.raises(PayloadError) as info:
        notice_from_flags(title="x" * 100001, message="m", source=None, labels=())
    check(info.value, "payload_invalid", "/title")
    with pytest.raises(PayloadError) as info:
        notice_from_flags(title=None, message="m", source="notice-route", labels=())
    check(info.value, "source_mismatch", None)
    with pytest.raises(PayloadError) as info:
        notice_from_flags(title=None, message="m", source="Bad", labels=())
    check(info.value, "payload_invalid", "--source")


# --- labels ---------------------------------------------------------------------------------


def labels_of(obj: dict[str, Any], flags: tuple[str, ...] = ()) -> Notification:
    return classify(obj, source=None, labels=flags)


def test_payload_labels_split_at_the_first_equals() -> None:
    note = labels_of(hook(labels=["a=b", "k=v=w", "e="]))
    assert note.labels == (("a", "b"), ("k", "v=w"), ("e", ""))
    assert note.labels_ignored == 0


def test_label_flags_add_labels_to_both_shapes_merged_in_order() -> None:
    note = labels_of(hook(labels=["a=1"]), ("b=2",))
    assert note.labels == (("a", "1"), ("b", "2"))
    note = labels_of({"message": "m"}, ("b=2", "c=3"))
    assert note.labels == (("b", "2"), ("c", "3"))


def test_a_label_pair_given_twice_counts_once_and_ignores_nothing() -> None:
    note = labels_of(hook(labels=["a=1", "a=1"]), ("a=1", "b=1"))
    assert note.labels == (("a", "1"), ("b", "1"))
    assert note.labels_ignored == 0


def test_same_key_with_different_values_keeps_both() -> None:
    assert labels_of(hook(labels=["a=1", "a=2"])).labels == (("a", "1"), ("a", "2"))


@pytest.mark.parametrize("value", [None, "a=b", {"a": "b"}, 5, True])
def test_labels_that_is_not_an_array_is_ignored_once(value: object) -> None:
    note = labels_of(hook(labels=value))
    assert note.labels == () and note.labels_ignored == 1


def test_absent_labels_ignore_nothing() -> None:
    assert labels_of(hook()).labels_ignored == 0


@pytest.mark.parametrize(
    "element",
    [
        5,
        None,
        ["a=b"],
        {"a": "b"},
        True,
        "",
        "noequals",
        "=v",
        "A=v",
        "-a=v",
        "a b=v",
        "a=v v",
        "a=é",
        "a=v\n",
        "a=" + "x" * 257,
        "a" * 65 + "=v",
        "a=<b>",
    ],
)
def test_bad_label_elements_add_one_each(element: object) -> None:
    note = labels_of(hook(labels=["ok=1", element, element]))
    assert note.labels == (("ok", "1"),)
    assert note.labels_ignored == 2


def test_label_grammar_boundaries() -> None:
    good = ["a" * 64 + "=" + "x" * 256, "0.a_b-c=A.b_c:d/e#f@g+h=i,j-k", "a="]
    note = labels_of(hook(labels=good))
    assert note.labels_ignored == 0 and len(note.labels) == 3


def test_only_the_first_64_elements_are_examined() -> None:
    elements = [f"k{i}=v" for i in range(70)]
    note = labels_of(hook(labels=elements))
    assert len(note.labels) == 64
    assert note.labels_ignored == 6


def test_elements_beyond_64_count_even_when_valid_duplicates_or_invalid() -> None:
    elements = ["a=1"] * 64 + ["a=1", 5, "x"]
    note = labels_of(hook(labels=elements))
    assert note.labels == (("a", "1"),)
    assert note.labels_ignored == 3


def test_huge_label_array_is_cheap() -> None:
    note = labels_of(hook(labels=["a=1"] * 200000))
    assert note.labels_ignored == 200000 - 64


def test_labels_ignored_never_hides_flag_labels() -> None:
    note = labels_of(hook(labels=5), ("a=1",))
    assert note.labels == (("a", "1"),) and note.labels_ignored == 1


@pytest.mark.parametrize("flag", ["", "noequals", "=v", "A=v", "a=é", "a=v v", "a=" + "x" * 257])
def test_malformed_label_flag_is_invalid_at_label(flag: str) -> None:
    check(refused(hook(), labels=("ok=1", flag)), "payload_invalid", "--label")
    check(refused({"message": "m"}, labels=(flag,)), "payload_invalid", "--label")


def test_label_flag_error_never_echoes_the_flag() -> None:
    error = refused(hook(), labels=(MARK,))
    assert MARK not in str(error) and MARK not in repr(error.to_error())


# --- event_id and object_id -----------------------------------------------------------------


def ids(**extra: Any) -> Notification:
    return classify(hook(**extra), source=None, labels=())


def test_integer_and_string_event_ids_differ() -> None:
    a = ids(event_id=17).event_id
    b = ids(event_id="17").event_id
    assert a == 17 and type(a) is int
    assert b == "17" and type(b) is str


@pytest.mark.parametrize(
    "value", [True, False, 1.5, 17.0, None, [1], {"a": 1}, "", "-x", "a b", "é"]
)
def test_other_event_ids_are_ignored(value: object) -> None:
    assert ids(event_id=value).event_id is None


def test_boolean_event_id_is_ignored_not_read_as_an_integer() -> None:
    assert ids(event_id=True).event_id is None
    assert ids(event_id=False).event_id is None


@pytest.mark.parametrize("value", ["a", "A1", "0", "a.b/c#d-e_f", "x" * 256])
def test_plain_string_ids(value: str) -> None:
    assert ids(event_id=value).event_id == value
    assert ids(object_id=value).object_id == value


def test_plain_string_boundary() -> None:
    assert ids(event_id="x" * 256).event_id == "x" * 256
    assert ids(event_id="x" * 257).event_id is None


def test_integer_ids_including_negative_and_zero() -> None:
    assert ids(event_id=0).event_id == 0
    assert ids(object_id=-5).object_id == -5
    assert ids(object_id=2**63 - 1).object_id == 2**63 - 1


@pytest.mark.parametrize("value", [True, 1.5, None, "a b", [1], {}])
def test_object_id_other_values_are_null(value: object) -> None:
    assert ids(object_id=value).object_id is None


def test_absent_ids_are_none() -> None:
    note = ids()
    assert note.event_id is None and note.object_id is None


def test_notice_has_no_ids() -> None:
    note = classify({"message": "m"}, source=None, labels=())
    assert note.event_id is None and note.object_id is None and note.repo is None


@pytest.mark.parametrize("repo, expected", [("a/b", "a/b"), (None, None), (5, None), ([], None)])
def test_repo_is_a_string_or_none(repo: object, expected: str | None) -> None:
    assert ids(repo=repo).repo == expected
