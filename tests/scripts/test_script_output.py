"""``scripts/script_output.py``: a script run on its own never fails on a character its output
cannot encode.

Live on Windows 11, ``ensure_a365_work_iq_permissions.py`` with its output piped did its work
and then died printing "✅": a redirected stdout there uses the ANSI code page (cp1252), and
the script exited 1. The setup wizard runs the scripts with ``PYTHONIOENCODING=utf-8``.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location("script_output", SCRIPTS / "script_output.py")
assert _spec is not None and _spec.loader is not None
script_output = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script_output)
make_output_safe = script_output.make_output_safe


def _stream(encoding: str, errors: str = "strict") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors, newline="\n")


def _written(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()  # type: ignore[attr-defined]


def test_a_cp1252_stream_fails_on_a_check_mark_without_it() -> None:
    stream = _stream("cp1252")
    with pytest.raises(UnicodeEncodeError):
        print("  ✅ ready", file=stream)


def test_a_cp1252_stream_gets_a_replacement_instead_of_an_error() -> None:
    stream = _stream("cp1252")
    make_output_safe(stream)
    print("  ✅ ready — done…", file=stream)
    assert _written(stream) == b"  ? ready \x97 done\x85\n"
    assert stream.encoding == "cp1252"


def test_a_utf8_stream_prints_exactly_what_it_did() -> None:
    before, after = _stream("utf-8"), _stream("utf-8")
    make_output_safe(after)
    for stream in (before, after):
        print("  ✅ ready — done…", file=stream)
    assert _written(after) == _written(before) == "  ✅ ready — done…\n".encode()
    assert after.encoding == "utf-8"


def test_a_stream_that_already_copes_is_left_alone() -> None:
    # Python's own stderr uses backslashreplace; that is kept.
    stream = _stream("cp1252", errors="backslashreplace")
    make_output_safe(stream)
    assert stream.errors == "backslashreplace"


def test_a_stream_that_cannot_be_reconfigured_is_no_error() -> None:
    make_output_safe(io.StringIO())


def test_with_no_arguments_it_covers_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _stream("cp1252"), _stream("cp1252")
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    make_output_safe()
    print("✅", file=out)
    print("✅", file=err)
    assert _written(out) == _written(err) == b"?\n"


# Run without the entrabot package: these scripts never needed it, and must not now.
_NO_ENTRABOT = """
import runpy, sys
sys.modules["entrabot"] = None  # any import of the package fails
script = sys.argv[1]
sys.argv = [script, "--help"]
runpy.run_path(script, run_name="__main__")
"""


@pytest.mark.parametrize("name", ["provision_blob_storage.py", "deprovision_blob_storage.py"])
def test_a_script_that_never_needed_entrabot_still_starts_without_it(name: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _NO_ENTRABOT, str(SCRIPTS / name)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "entrabot" not in result.stderr
