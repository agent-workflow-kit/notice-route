import math
import subprocess

import pytest

from notice_route import gateway
from notice_route.gateway import FakeClock, FixedTokens, Gateway, SystemClock


def test_fake_clock_wall_jump_does_not_move_monotonic() -> None:
    clock = FakeClock(wall=1_000.0, monotonic=50.0)
    deadline = clock.monotonic() + 10.0
    clock.jump_wall(-86_400.0)
    assert clock.wall() == 1_000.0 - 86_400.0
    assert clock.monotonic() == 50.0
    assert clock.monotonic() < deadline


def test_fake_clock_monotonic_advance_does_not_move_wall() -> None:
    clock = FakeClock(wall=1_000.0, monotonic=50.0)
    clock.advance_monotonic(7.5)
    assert clock.monotonic() == 57.5
    assert clock.wall() == 1_000.0


def test_fake_clock_advance_moves_both_together() -> None:
    clock = FakeClock(wall=100.0, monotonic=5.0)
    clock.advance(2.0)
    assert (clock.wall(), clock.monotonic()) == (102.0, 7.0)


def test_fake_clock_rejects_monotonic_going_backwards() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance_monotonic(-1.0)
    with pytest.raises(ValueError):
        clock.advance(-1.0)


def test_system_clock_monotonic_never_decreases() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    assert clock.monotonic() >= first
    assert clock.wall() > 1_600_000_000.0


def test_fixed_tokens_yield_the_sequence_then_refuse() -> None:
    tokens = FixedTokens(["aa", "bb"])
    assert (tokens.new(), tokens.new()) == ("aa", "bb")
    with pytest.raises(RuntimeError):
        tokens.new()


def test_gateway_uses_injected_clock_and_tokens() -> None:
    clock = FakeClock(wall=42.0, monotonic=1.0)
    gw = Gateway(clock=clock, tokens=FixedTokens(["t1"]))
    assert gw.wall() == 42.0
    assert gw.monotonic() == 1.0
    assert gw.new_token() == "t1"


def test_default_gateway_token_is_random_hex() -> None:
    gw = Gateway()
    first, second = gw.new_token(), gw.new_token()
    assert first != second
    assert len(first) == 32
    int(first, 16)


def test_run_uses_no_shell_and_passes_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=["x"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    Gateway().run(["tool", "--flag"], stdin="a; echo $HOME", timeout=3.0)
    (args, kwargs) = calls[0]
    assert args == (["tool", "--flag"],)
    assert not kwargs.get("shell", False)
    assert kwargs["input"] == "a; echo $HOME"
    assert kwargs["timeout"] == 3.0


def test_gateway_module_holds_no_clock_instances() -> None:
    clocks = (FakeClock, SystemClock)
    held = [n for n, v in vars(gateway).items() if isinstance(v, clocks)]
    assert held == []


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_fake_clock_rejects_non_finite_values(bad: float) -> None:
    with pytest.raises(ValueError):
        FakeClock(wall=bad)
    with pytest.raises(ValueError):
        FakeClock(monotonic=bad)
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.jump_wall(bad)
    with pytest.raises(ValueError):
        clock.advance(bad)
    with pytest.raises(ValueError):
        clock.advance_monotonic(bad)
    assert (clock.wall(), clock.monotonic()) == (FakeClock().wall(), FakeClock().monotonic())


def test_fake_clock_rejects_huge_ints_and_overflowing_sums() -> None:
    with pytest.raises(ValueError):
        FakeClock(wall=10**400)  # type: ignore[arg-type]
    clock = FakeClock()
    for call in (clock.jump_wall, clock.advance, clock.advance_monotonic):
        with pytest.raises(ValueError):
            call(10**400)  # type: ignore[arg-type]
    before = (clock.wall(), clock.monotonic())
    clock.jump_wall(1e308)
    with pytest.raises(ValueError):
        clock.jump_wall(1e308)
    clock.advance_monotonic(1e308)
    with pytest.raises(ValueError):
        clock.advance_monotonic(1e308)
    with pytest.raises(ValueError):
        clock.advance(1e308)
    assert math.isfinite(clock.wall()) and math.isfinite(clock.monotonic())
    assert before != (clock.wall(), clock.monotonic())


def test_fixed_tokens_refuses_a_bare_string() -> None:
    with pytest.raises(TypeError):
        FixedTokens("abc")  # type: ignore[arg-type]


def test_fixed_tokens_refuses_duplicates() -> None:
    with pytest.raises(ValueError):
        FixedTokens(["aa", "bb", "aa"])
