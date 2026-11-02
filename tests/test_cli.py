import subprocess
import sys
from pathlib import Path

import pytest

from flashcards import __version__, cli
from flashcards.cli import Deps, build_parser, main
from flashcards.errors import IngestError


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


def test_db_option_is_parsed_as_a_path():
    assert build_parser().parse_args(["--db", "some/dir/cards.db"]).db == Path("some/dir/cards.db")


def test_handled_error_is_one_line_on_stderr_with_exit_code_1(monkeypatch, capsys):
    def fail(args, parser, deps):
        raise IngestError("cannot read notes file x.md")

    monkeypatch.setattr(cli, "_run", fail)
    assert main(["--today", "2026-11-02"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: cannot read notes file x.md\n"
    assert captured.out == ""


def test_unexpected_errors_are_not_swallowed(monkeypatch):
    def crash(args, parser, deps):
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "_run", crash)
    with pytest.raises(RuntimeError):
        main([])
