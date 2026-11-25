import pytest

from notice_route import __version__
from notice_route.cli import main


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"notice-route {__version__}"


def test_no_arguments_returns_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--vers", "--ver", "--js"])
def test_abbreviated_flag_is_refused_with_exit_2(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([flag])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_unknown_flag_is_refused_with_exit_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-flag"])
    assert exc.value.code == 2
