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


def test_run_uses_no_shell_and_passes_stdin() -> None:
    result = Gateway().run(["cat"], stdin="a; echo $HOME")
    assert result.returncode == 0
    assert result.stdout == "a; echo $HOME"


def test_module_exposes_no_global_clock_state() -> None:
    assert not hasattr(gateway, "_clock")
