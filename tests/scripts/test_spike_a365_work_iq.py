from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "spike_a365_work_iq.py"


def load_module():
    spec = importlib.util.spec_from_file_location("spike_a365_work_iq", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def server(name: str, **overrides: Any) -> dict[str, Any]:
    return {
        "mcpServerName": name,
        "mcpServerUniqueName": name,
        "url": f"https://contoso.example/{name}",
        "scope": "Tools.ListInvoke.All",
        "audience": "00000000-0000-0000-0000-000000000000",
        **overrides,
    }


def run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    servers: list[dict[str, Any]],
) -> int:
    manifest = tmp_path / "ToolingManifest.json"
    manifest.write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setenv("ENTRABOT_A365_TOOLING_MANIFEST", str(manifest))
    monkeypatch.setattr(sys, "argv", ["spike_a365_work_iq.py"])
    return load_module().main()


def test_passes_when_word_and_excel_are_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_main(
        tmp_path,
        monkeypatch,
        [server("mcp_WordServer"), server("mcp_ExcelServer")],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "mcp_WordServer configured" in out
    assert "mcp_ExcelServer configured" in out


def test_fails_when_excel_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_main(tmp_path, monkeypatch, [server("mcp_WordServer")])

    assert code == 3
    assert "mcp_ExcelServer is not configured" in capsys.readouterr().err


def test_fails_when_word_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_main(tmp_path, monkeypatch, [server("mcp_ExcelServer")])

    assert code == 3
    assert "mcp_WordServer is not configured" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["scope", "audience"])
def test_fails_when_excel_entry_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    code = run_main(
        tmp_path,
        monkeypatch,
        [server("mcp_WordServer"), server("mcp_ExcelServer", **{field: ""})],
    )

    err = capsys.readouterr().err
    assert code != 0
    assert "mcp_ExcelServer" in err
    assert field in err


def test_other_servers_are_not_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = run_main(
        tmp_path,
        monkeypatch,
        [
            server("mcp_WordServer"),
            server("mcp_ExcelServer"),
            server("mcp_ODSPRemoteServer", scope="", audience=""),
        ],
    )

    assert code == 0
