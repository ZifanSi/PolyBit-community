"""The backup scripts need docker to really run; here we check they parse and refuse misuse."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX sh")


@pytest.mark.parametrize("script", ["backup.sh", "restore.sh"])
def test_scripts_parse(script):
    assert subprocess.run([SH, "-n", str(ROOT / "scripts" / script)]).returncode == 0


@pytest.mark.parametrize("args", [[], ["x.dump"], ["x.dump", "yes"], ["x.dump", "--yes", "extra"]])
def test_restore_needs_explicit_yes(args):
    result = subprocess.run([SH, str(ROOT / "scripts" / "restore.sh"), *args], capture_output=True, text=True)
    assert result.returncode == 2 and "--yes" in result.stderr


def test_restore_refuses_missing_file(tmp_path):
    result = subprocess.run(
        [SH, str(ROOT / "scripts" / "restore.sh"), str(tmp_path / "nope.dump"), "--yes"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1 and "no such file" in result.stderr
