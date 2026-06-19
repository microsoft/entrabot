"""Agency-proxied MCP servers (port of Session/AgencyMcp.cs).

The ``agency`` CLI proxies a catalog of Microsoft MCP servers (ado, icm, calendar, …). An
agency MCP is installed into ``.mcp.json`` as ``{"command": "agency", "args": ["mcp", name, …]}``.
This module discovers the catalog + each server's parameters (by parsing ``agency … --help``)
and installs/uninstalls entries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

# On Windows `agency` is agency.exe/.cmd on PATH; cmd /c resolves it like az.
_AGENCY = ["cmd", "/c", "agency"] if os.name == "nt" else ["agency"]
_GENERIC = {"local", "npx", "remote", "help"}  # proxies, not named catalog servers
_PLUMBING = {"--help", "--transport", "--port", "--no-config-cache"}
_OPT = re.compile(r"^\s+(--[a-zA-Z][\w-]*)(?:\s+<([^>]+)>)?\s*$")
_CMD = re.compile(r"^\s{2,}(\S+)\s{2,}(.+)$")


def _run(*args: str) -> str:
    try:
        out = subprocess.run(_AGENCY + list(args), capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def available() -> bool:
    return bool(_run("mcp", "--help"))


def discover() -> List[Tuple[str, str]]:
    """[(name, description)] of agency MCP servers (excludes generic proxies)."""
    servers: List[Tuple[str, str]] = []
    in_cmds = False
    for line in _run("mcp", "--help").splitlines():
        if line.strip() == "Commands:":
            in_cmds = True
            continue
        if not in_cmds:
            continue
        m = _CMD.match(line)
        if m:
            name, desc = m.group(1), m.group(2).strip()
            if name not in _GENERIC:
                servers.append((name, desc))
        elif not line.strip() and servers:
            break
    return sorted(servers)


def discover_params(name: str) -> List[dict]:
    """Parse ``agency mcp <name> --help`` into editable param descriptors."""
    lines = _run("mcp", name, "--help").replace("\r\n", "\n").split("\n")
    required = set()
    for line in lines:
        if line.strip().lower().startswith("usage:"):
            for m in re.finditer(r"--[A-Za-z][\w-]*", line.replace("[OPTIONS]", "")):
                required.add(m.group(0))
            break

    params: List[dict] = []
    cur: Optional[dict] = None
    in_opts = False

    def flush():
        nonlocal cur
        if cur and cur["key"] not in _PLUMBING:
            params.append(cur)
        cur = None

    for raw in lines:
        if not in_opts:
            if raw.rstrip() == "Options:":
                in_opts = True
            continue
        m = _OPT.match(raw)
        if m:
            flush()
            flag, placeholder = m.group(1), m.group(2)
            cur = {
                "key": flag,
                "label": flag,
                "type": "bool" if placeholder is None else "text",
                "placeholder": placeholder or "",
                "description": "",
                "required": flag in required,
                "default": "",
            }
            continue
        if cur is None:
            continue
        t = raw.strip()
        if not t:
            continue
        if t.startswith("[default:"):
            cur["default"] = t[len("[default:"):].strip(" ]")
        elif t.startswith(("[env:", "[aliases:", "[possible", "- ")):
            continue
        elif not cur["description"]:
            cur["description"] = t
    flush()
    return params


def build_args(fields: List[dict], values: Dict[str, object]) -> List[str]:
    """Turn a filled form into agency CLI args (flag for bools, flag+value for text)."""
    args: List[str] = []
    for f in fields:
        v = values.get(f["key"])
        if f["type"] == "bool":
            if v:
                args.append(f["key"])
        elif v not in (None, ""):
            args += [f["key"], str(v)]
    return args


# ---- .mcp.json install/uninstall ----------------------------------------------------------
def _mcp_path(root: str) -> str:
    return os.path.join(root, ".mcp.json")


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: str, doc: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def installed(root: str) -> Dict[str, str]:
    """{mcp_name: key} for agency MCPs currently in .mcp.json."""
    servers = _load(_mcp_path(root)).get("mcpServers") or {}
    out: Dict[str, str] = {}
    for key, val in servers.items():
        if isinstance(val, dict) and val.get("command") == "agency":
            args = val.get("args") or []
            if len(args) >= 2 and args[0] == "mcp":
                out.setdefault(args[1], key)
    return out


def install(root: str, name: str, extra_args: Optional[List[str]] = None) -> None:
    path = _mcp_path(root)
    doc = _load(path)
    servers = doc.setdefault("mcpServers", {})
    existing = servers.get(name)
    if isinstance(existing, dict) and existing.get("command") != "agency":
        raise ValueError(f"'{name}' is already configured and is not an agency MCP.")
    servers[name] = {"command": "agency", "args": ["mcp", name] + list(extra_args or [])}
    _save(path, doc)


def uninstall(root: str, name: str) -> None:
    path = _mcp_path(root)
    doc = _load(path)
    servers = doc.get("mcpServers") or {}
    for key in [
        k
        for k, v in servers.items()
        if isinstance(v, dict) and v.get("command") == "agency" and (v.get("args") or [None, None])[1:2] == [name]
    ]:
        del servers[key]
    _save(path, doc)


def catalog(root: str) -> List[dict]:
    inst = installed(root)
    return [{"name": n, "description": d, "installed": n in inst} for n, d in discover()]
