"""scripts/check_attribution.sh: the commit-message / PR-description guard."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_attribution.sh"
BASH = shutil.which("bash") or "/bin/bash"  # absolute path, not whatever "bash" PATH finds


def check(tmp_path: Path, *messages: str) -> subprocess.CompletedProcess[str]:
    files = []
    for i, message in enumerate(messages):
        f = tmp_path / f"msg{i}.txt"
        f.write_text(message)
        files.append(str(f))
    return subprocess.run(  # noqa: S603 - fixed script path, test-controlled arguments
        [BASH, str(SCRIPT), *files], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    "message",
    [
        "feat: add rate limiter\n\nToken bucket in Redis.\n",
        "fix: handle claudette edge case\n",  # substring only, not the word
        "docs: explain the co-authoring workflow\n",  # no "co-authored-by"
    ],
)
def test_clean_messages_pass(tmp_path: Path, message: str) -> None:
    result = check(tmp_path, message)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "message",
    [
        "feat: x\n\nCo-Authored-By: Someone <a@b.c>\n",
        "feat: x\n\nco-authored-by: someone\n",
        "feat: x\n\nWritten with Claude.\n",
        "chore: update CLAUDE.md\n",
        "feat: x\n\nThanks, Anthropic.\n",
        "feat: x\n\nGenerated with some tool\n",
    ],
)
def test_attribution_is_rejected(tmp_path: Path, message: str) -> None:
    result = check(tmp_path, message)
    assert result.returncode == 1
    assert "not allowed" in result.stderr


def test_git_comment_lines_are_ignored(tmp_path: Path) -> None:
    message = "feat: x\n\n# Please enter the commit message. Claude\n# Co-authored-by: nobody\n"
    assert check(tmp_path, message).returncode == 0


def test_text_below_git_scissors_is_ignored(tmp_path: Path) -> None:
    scissors = "# " + "-" * 24 + " >8 " + "-" * 24
    message = f"feat: x\n\n{scissors}\ndiff --git a/CLAUDE.md b/CLAUDE.md\n+Co-authored-by: x\n"
    assert check(tmp_path, message).returncode == 0


def test_any_bad_file_fails_the_whole_check(tmp_path: Path) -> None:
    result = check(tmp_path, "feat: fine\n", "feat: x\n\nCo-authored-by: x\n", "fix: fine\n")
    assert result.returncode == 1


def test_reports_the_offending_line(tmp_path: Path) -> None:
    result = check(tmp_path, "feat: x\n\nbody\nCo-authored-by: someone\n")
    assert "line 4:" in result.stderr


def test_requires_at_least_one_file() -> None:
    result = subprocess.run(  # noqa: S603
        [BASH, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
