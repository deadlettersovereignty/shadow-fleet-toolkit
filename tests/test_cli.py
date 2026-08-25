"""CLI dispatch."""
from __future__ import annotations

import subprocess
import sys

import pytest

from shadowfleet.cli import COMMANDS, main


def test_usage_lists_every_command(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


def test_unknown_command_exits_nonzero(capsys):
    assert main(["nonsense"]) == 2


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_every_command_has_working_help(command):
    """Catches a subcommand whose main() does not accept argv."""
    r = subprocess.run([sys.executable, "-m", "shadowfleet.cli", command, "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "usage: shadowfleet" in r.stdout


def test_output_survives_a_closed_pipe(tmp_path):
    """`shadowfleet gaps | head` must not print a BrokenPipeError traceback."""
    db = str(tmp_path / "p.db")
    subprocess.run([sys.executable, "-m", "shadowfleet.cli", "testdata", "--db", db],
                   capture_output=True, check=True)
    proc = subprocess.run(
        f"{sys.executable} -m shadowfleet.cli gaps --db {db} --min-hours 4 | head -2",
        shell=True, capture_output=True, text=True)
    assert "BrokenPipeError" not in proc.stderr
    assert "Traceback" not in proc.stderr
