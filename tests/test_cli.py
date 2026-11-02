import subprocess
import sys

from flashcards import __version__
from flashcards.cli import Deps, build_parser, main


def test_help_exits_zero(capsys):
    assert main(["--help"]) == 0
    assert "usage: flashcards" in capsys.readouterr().out


def test_version_exits_zero(capsys):
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help_and_exits_zero(capsys):
    assert main([]) == 0
    assert "usage: flashcards" in capsys.readouterr().out


def test_invalid_today_is_a_usage_error(capsys):
    assert main(["--today", "2026-13-01"]) == 2
    assert "--today" in capsys.readouterr().err


def test_today_is_parsed_as_a_date():
    args = build_parser().parse_args(["--today", "2026-11-02"])
    assert args.today.isoformat() == "2026-11-02"


def test_unknown_command_is_a_usage_error():
    assert main(["no-such-command"]) == 2


def test_deps_can_be_replaced():
    assert main([], deps=Deps()) == 0


def test_python_dash_m_flashcards_version():
    result = subprocess.run(
        [sys.executable, "-m", "flashcards", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert __version__ in result.stdout
