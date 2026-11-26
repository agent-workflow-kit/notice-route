"""The ``fallback`` key of a destination (#35, docs/design.md C-63): validation at load. A name
that is unknown, a self reference, a cycle and a chain of more than 3 hops are ``config_invalid``,
keyed by the destination whose ``fallback`` is wrong; a config without the key is unchanged."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from notice_route.config import ConfigError
from notice_route.gateway import Gateway
from notice_route.validate import Config, validate_config

ROOT = Path(__file__).resolve().parent.parent


def dests(**fallbacks: Any) -> dict[str, Any]:
    """Destinations named by the keys, each with the given ``fallback`` (``None``: no key)."""
    out: dict[str, Any] = {}
    for name, fb in fallbacks.items():
        body: dict[str, Any] = {"kind": "generic", "cmd": ["/bin/true"]}
        if fb is not None:
            body["fallback"] = fb
        out[name] = body
    return out


def check(**fallbacks: Any) -> Config:
    return validate_config({"destinations": dests(**fallbacks)}, Gateway())


def key_of(**fallbacks: Any) -> str:
    with pytest.raises(ConfigError) as info:
        check(**fallbacks)
    assert info.value.code == "config_invalid" and info.value.exit_code == 2
    return str(info.value.key)


def test_a_config_without_the_key_has_no_fallback() -> None:
    config = check(a=None, b=None)
    assert [d.fallback for d in config.destinations.values()] == [None, None]


def test_a_fallback_is_kept_on_its_destination() -> None:
    config = check(a="b", b=None)
    assert config.destinations["a"].fallback == "b"
    assert config.destinations["b"].fallback is None


def test_a_chain_of_three_hops_is_accepted() -> None:
    config = check(a="b", b="c", c="d", d=None)
    assert [d.fallback for d in config.destinations.values()] == ["b", "c", "d", None]


def test_a_chain_of_four_hops_is_refused_at_its_first_destination() -> None:
    assert key_of(a="b", b="c", c="d", d="e", e=None) == "destinations.a.fallback"


def test_an_unknown_destination_is_refused() -> None:
    assert key_of(a="nowhere") == "destinations.a.fallback"


def test_a_destination_that_is_its_own_fallback_is_refused() -> None:
    assert key_of(a="a") == "destinations.a.fallback"


def test_a_two_destination_cycle_is_refused() -> None:
    assert key_of(a="b", b="a") == "destinations.a.fallback"


def test_a_three_destination_cycle_is_refused() -> None:
    assert key_of(a="b", b="c", c="a") == "destinations.a.fallback"


def test_a_chain_that_leads_into_a_cycle_is_refused() -> None:
    assert key_of(x="a", a="b", b="a") in {"destinations.x.fallback", "destinations.a.fallback"}


@pytest.mark.parametrize("value", [1, True, [], {}, "", "Upper", "a b", "x" * 33, "../a"])
def test_a_bad_value_is_refused(value: Any) -> None:
    assert key_of(a=value, b=None) == "destinations.a.fallback"


def test_a_null_value_is_refused() -> None:
    body = dests(a=None, b=None)
    body["a"]["fallback"] = None
    with pytest.raises(ConfigError) as info:
        validate_config({"destinations": body}, Gateway())
    assert info.value.key == "destinations.a.fallback"


def test_a_disabled_fallback_is_allowed_in_the_config() -> None:
    body = dests(a="b", b=None)
    body["b"]["enabled"] = False
    assert validate_config({"destinations": body}, Gateway()).destinations["a"].fallback == "b"


def test_the_config_schema_knows_the_key() -> None:
    schema = json.loads((ROOT / "schemas" / "config.json").read_text())
    ok = {"destinations": dests(a="b", b=None)}
    jsonschema.Draft202012Validator(schema).validate(ok)
    bad = {"destinations": dests(a="B", b=None)}
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(bad))
