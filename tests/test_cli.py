from importlib import metadata

import pytest

from notice_route.cli import main


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"notice-route {metadata.version('notice-route')}"


def test_no_arguments_returns_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--vers", "--ver", "--js"])
def test_abbreviated_flag_is_refused_with_exit_2(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([flag]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "notice-route: usage: the command line cannot be used\n"


def test_unknown_flag_is_refused_with_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-such-flag"]) == 2
    assert capsys.readouterr().err == "notice-route: usage: the command line cannot be used\n"
