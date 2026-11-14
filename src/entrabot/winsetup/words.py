"""What things are called on each operating system: the commands and flags setup tells people
to run, and the few nouns that differ.

Setup is one wizard on Windows, macOS and Linux, started by ``scripts\\setup-windows.cmd`` or
``./scripts/setup.sh``. Whenever it says "run this", the command has to be the one that exists
on the computer it is running on, spelled the way that launcher takes it
(``-UseBlueprint APP_ID`` on Windows, ``--use-blueprint=APP_ID`` elsewhere). Every such string
comes from here, through ``console.words``, so no message names another system's command.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

_WINDOWS_FLAGS: Mapping[str, str] = {
    "new": "-NewChain",
    "use-blueprint": "-UseBlueprint",
    "upn-suffix": "-UpnSuffix",
    "agent-user-upn": "-AgentUserUpn",
    "sponsor": "-Sponsor",
    "tenant": "-Tenant",
    "yes": "-Yes",
    "skip-smoke": "-SkipSmoke",
    "cloud-memory": "-UseCloudMemory",
    "create-new-storage": "-CreateNewStorage",
    "storage-account": "-WithStorageAccount",
    "diagnose": "-Diagnose",
    "delete-agent": "-DeleteAgent",
    "purge-data": "-PurgeData",
    "keep-setup-credential": "-KeepSetupCredential",
}

_POSIX_FLAGS: Mapping[str, str] = {
    "new": "--new",
    "use-blueprint": "--use-blueprint",
    "upn-suffix": "--with-upn-suffix",
    "agent-user-upn": "--agent-user-upn",
    "sponsor": "--sponsor",
    "tenant": "--tenant",
    "yes": "--yes",
    "skip-smoke": "--skip-smoke",
    "cloud-memory": "--use-cloud-memory",
    "create-new-storage": "--create-new-storage",
    "storage-account": "--with-storage-account",
    "diagnose": "--diagnose",
    "delete-agent": "--delete-agent",
    "purge-data": "--purge-data",
    "keep-setup-credential": "--keep-setup-credential",
}


@dataclass(frozen=True)
class Words:
    """The commands and nouns for one operating system."""

    platform: str  # sys.platform's value: "win32", "darwin", "linux"
    setup: str  # the command that starts setup, from the project folder
    rotate: str  # ...that replaces this computer's certificate
    teardown: str  # ...that disconnects this computer
    start_script: str  # the launcher in the project folder that starts the agent
    computer: str  # "PC", "Mac" or "computer", after "this"
    terminal: str  # where a command is typed: "window" or "terminal"
    key_store: str  # where this computer keeps the agent's private key
    az_install_url: str
    _flags: Mapping[str, str] = field(repr=False, default_factory=dict)

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @property
    def diagnose(self) -> str:
        """The health check: it changes nothing."""
        return f"{self.setup} {self.flag('diagnose')}"

    @property
    def yes_hint(self) -> str:
        """The flag that answers yes, as a person is told to add it."""
        return "-Yes (--yes)" if self.windows else "--yes"

    def flag(self, name: str, value: str = "") -> str:
        """The flag called *name* as this system's launcher takes it, with *value* when it
        has one: ``-UseBlueprint APP_ID`` on Windows, ``--use-blueprint=APP_ID`` elsewhere."""
        spelled = self._flags[name]
        if not value:
            return spelled
        return f"{spelled} {value}" if self.windows else f"{spelled}={value}"

    def set_variable(self, name: str, value: str) -> list[str]:
        """How to set an environment variable for the next command, one line per shell."""
        if self.windows:
            return [
                f'  cmd.exe:     set "{name}={value}"',
                f"  PowerShell:  $env:{name} = '{value}'",
            ]
        return [f"  export {name}='{value}'"]

    def clear_variable(self, name: str) -> list[str]:
        if self.windows:
            return [
                f'  cmd.exe:     set "{name}="',
                f"  PowerShell:  Remove-Item Env:{name}",
            ]
        return [f"  unset {name}"]


_WINDOWS = Words(
    platform="win32",
    setup=r"scripts\setup-windows.cmd",
    rotate=r"powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1",
    teardown=r"powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1",
    start_script="start-entrabot.cmd",
    computer="PC",
    terminal="window",
    key_store="Windows key store",
    az_install_url="https://aka.ms/installazurecliwindows",
    _flags=_WINDOWS_FLAGS,
)

_MAC = Words(
    platform="darwin",
    setup="./scripts/setup.sh",
    rotate="./scripts/setup.sh --rotate-cert",
    teardown="./scripts/teardown.sh",
    start_script="start-entrabot.sh",
    computer="Mac",
    terminal="terminal",
    key_store="macOS Keychain",
    az_install_url="https://aka.ms/install-az",
    _flags=_POSIX_FLAGS,
)

_LINUX = Words(
    platform="linux",
    setup="./scripts/setup.sh",
    rotate="./scripts/setup.sh --rotate-cert",
    teardown="./scripts/teardown.sh",
    start_script="start-entrabot.sh",
    computer="computer",
    terminal="terminal",
    key_store="Linux keyring",
    az_install_url="https://aka.ms/install-az",
    _flags=_POSIX_FLAGS,
)


def words_for(platform: str | None = None) -> Words:
    """The words for *platform* (``sys.platform`` when not given). Anything that is not
    Windows or macOS is treated as Linux."""
    platform = platform or sys.platform
    if platform == "win32":
        return _WINDOWS
    if platform == "darwin":
        return _MAC
    return _LINUX
