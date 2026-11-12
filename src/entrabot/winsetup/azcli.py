"""Find, install and drive the Azure CLI.

Three facts shape this module:

* On Windows ``az`` is ``az.cmd``, a batch file. Its arguments pass through ``cmd.exe``, where
  ``& | ( ) ^ %`` mean something. Where the interpreter that ``az.cmd`` would start can be
  found, it is started directly; otherwise an argument ``cmd.exe`` would reinterpret is refused.
* A machine may have no admin rights and no ``winget``. ``pip install azure-cli`` into a
  private virtual environment needs neither, so it is the fallback that always works.
* ``az`` output is parsed as JSON only. Its stderr is never hidden: it goes to the setup log,
  and to the screen when a command fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from entrabot.winsetup.console import Console

# Microsoft Entra built-in role template IDs (the same in every tenant).
GLOBAL_ADMIN = "62e90394-69f5-4237-9190-012177145e10"
PRIVILEGED_ROLE_ADMIN = "e8611ab8-c189-46e8-94e1-60213ab1f814"
APPLICATION_ADMIN = "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3"
CLOUD_APPLICATION_ADMIN = "158c047a-c907-4556-b7ef-446551a6b5f7"

_GRAPH = "https://graph.microsoft.com/v1.0"
# The private copy of the Azure CLI is never "whatever is newest". The lower bound is a
# release that exists on PyPI (2.92.0, checked 2026-11-11); the upper bound keeps a future
# major version, with whatever it changes, from arriving unannounced. "az version" is still
# asked afterwards (see _verified): what was installed is only trusted once it answers.
AZURE_CLI_REQUIREMENT = "azure-cli>=2.92.0,<3"
# Only what setup uses is asked for: less of the directory travels, and less lands in the log.
_USER_FIELDS = "id,displayName,userPrincipalName,mail,userType,otherMails"
# The step after the direct lookup, as the person is told about it (what was tried, or why
# more than one person matched).
_SIGN_IN_SEARCH = "a search for their sign-in name"
_ROLE_FIELDS = "id,displayName,roleTemplateId"
_ROLES_ONLY = (
    "{value: value[?roleTemplateId].{id: id, displayName: displayName, "
    'roleTemplateId: roleTemplateId}, nextLink: "@odata.nextLink"}'
)
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CMD_UNSAFE = re.compile(r'[&|<>^%()!"\r\n]')
_TENANT = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_BATCH_SUFFIXES = (".cmd", ".bat")


class AzError(RuntimeError):
    """An ``az`` command failed, or the Azure CLI could not be installed."""


@dataclass(frozen=True)
class AzLocation:
    """How to start the Azure CLI, and the folder child processes need on PATH to find it."""

    command: tuple[str, ...]
    bin_dir: Path
    source: Literal["path", "standard", "tools-venv"]


@dataclass(frozen=True)
class Account:
    user: str
    user_type: str
    tenant_id: str
    tenant_name: str
    subscription_id: str
    subscription_name: str
    tenant_domain: str = ""


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str
    tenant_id: str
    is_default: bool


@dataclass(frozen=True)
class DirectoryUser:
    object_id: str
    upn: str
    mail: str
    user_type: str
    display_name: str = ""


@dataclass(frozen=True)
class UserSearch:
    """The result of looking a person up, and what was looked at, in plain words."""

    user: DirectoryUser | None
    tried: tuple[str, ...]


class AmbiguousUser(AzError):
    """More than one person in the directory matches what was typed."""

    def __init__(self, identifier: str, candidates: Sequence[DirectoryUser], how: str) -> None:
        super().__init__(f"More than one user matches {identifier!r} by {how}.")
        self.identifier = identifier
        self.candidates = list(candidates)
        self.how = how


@dataclass(frozen=True)
class Privileges:
    level: Literal["enough", "partial", "none", "unknown"]
    message: str


def validate_tenant(value: str) -> str | None:
    if _TENANT.match(value.strip()):
        return None
    return "A tenant is a domain (contoso.com) or a tenant ID (a GUID)."


def has_interactive_desktop(env: Mapping[str, str], *, platform: str | None = None) -> bool:
    """Can a sign-in window be shown to the person running setup?

    Measured on Windows 11: in an SSH session ``SESSIONNAME`` is empty and ``SSH_CONNECTION`` is
    set; at the console it is ``Console`` and over Remote Desktop ``RDP-Tcp#N``. When there is
    no telling, the answer is no: a device code works everywhere, and a sign-in window nobody
    can see hangs setup.
    """
    if env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or env.get("SSH_TTY"):
        return False
    if (platform or sys.platform) == "win32":
        return bool(env.get("SESSIONNAME", "").strip())
    if (platform or sys.platform) == "darwin":
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


# ── Finding az ───────────────────────────────────────────────────────────────


def _tools_venv(data_dir: Path) -> Path:
    return data_dir / "tools" / "azure-cli"


def _venv_bin(venv: Path, platform: str) -> Path:
    return venv / ("Scripts" if platform == "win32" else "bin")


def _venv_python(venv: Path, platform: str) -> Path:
    return _venv_bin(venv, platform) / ("python.exe" if platform == "win32" else "python")


def _venv_has_az(venv: Path, platform: str) -> bool:
    names = ("az.bat", "az.cmd", "az.exe", "az") if platform == "win32" else ("az",)
    return any((_venv_bin(venv, platform) / name).is_file() for name in names)


def _tools_venv_location(data_dir: Path, platform: str) -> AzLocation | None:
    venv = _tools_venv(data_dir)
    python = _venv_python(venv, platform)
    if python.is_file() and _venv_has_az(venv, platform):
        return AzLocation(
            command=(str(python), "-m", "azure.cli"),
            bin_dir=_venv_bin(venv, platform),
            source="tools-venv",
        )
    return None


def _from_launcher(
    launcher: Path, platform: str, source: Literal["path", "standard"]
) -> AzLocation:
    """Start what a Windows ``az.cmd`` would start, without going through ``cmd.exe``.

    The MSI's ``wbin\\az.cmd`` runs ``..\\python.exe -IBm azure.cli``; a pip install's
    ``Scripts\\az.bat`` runs ``python.exe -m azure.cli`` beside it.
    Needs confirmation on real Windows: the bundled interpreter's location.
    """
    if platform == "win32" and launcher.suffix.lower() in _BATCH_SUFFIXES:
        bundled = launcher.parent.parent / "python.exe"
        if bundled.is_file():
            return AzLocation((str(bundled), "-IBm", "azure.cli"), launcher.parent, source)
        beside = launcher.parent / "python.exe"
        if beside.is_file():
            return AzLocation((str(beside), "-m", "azure.cli"), launcher.parent, source)
    return AzLocation((str(launcher),), launcher.parent, source)


def find_az(
    *,
    env: Mapping[str, str],
    data_dir: Path,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> AzLocation | None:
    """Locate the Azure CLI: PATH, then the standard install folders, then the private venv."""
    platform = platform or sys.platform
    found = which("az")
    if found:
        return _from_launcher(Path(found), platform, "path")
    if platform == "win32":
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            root = env.get(variable)
            if not root:
                continue
            launcher = Path(root) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd"
            if launcher.is_file():
                return _from_launcher(launcher, platform, "standard")
    return _tools_venv_location(data_dir, platform)


# ── Installing az ────────────────────────────────────────────────────────────


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _verified(location: AzLocation, console: Console) -> str:
    """The installed version, proving the CLI actually starts. Raises :class:`AzError`."""
    result = console.run(
        [*location.command, "version", "--output", "json"], capture=True, quiet=True
    )
    if result.returncode != 0:
        raise AzError(f"The Azure CLI is installed but does not start:\n{_tail(result.stderr)}")
    try:
        return str(json.loads(result.stdout)["azure-cli"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AzError("The Azure CLI is installed but 'az version' gave no version.") from exc


def install_az(
    console: Console,
    *,
    data_dir: Path,
    python: Path,
    env: Mapping[str, str],
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    use_winget: bool | None = None,
) -> AzLocation:
    """Install the Azure CLI and return where it is. Raises :class:`AzError`.

    ``winget`` is tried when it exists (and *use_winget* allows it): that is the standard,
    self-updating install, but it needs admin rights. Otherwise, or when it fails, the CLI is
    pip-installed into a private virtual environment under the Entrabot data folder, which
    needs neither admin rights nor ``winget``. A venv that is already complete is reused; one
    that an interrupted run left half-built is finished.
    """
    platform = platform or sys.platform

    existing = _tools_venv_location(data_dir, platform)
    if existing is not None:
        console.ok(f"Azure CLI {_verified(existing, console)} ready (private copy for Entrabot)")
        return existing

    winget = which("winget") if platform == "win32" else None
    if winget and use_winget is not False:
        console.info("Installing the Azure CLI with winget.")
        console.detail("Windows may ask for permission to install it. Choose Yes.")
        result = console.run(
            [
                winget,
                "install",
                "--id",
                "Microsoft.AzureCLI",
                "--exact",
                "--source",
                "winget",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            interactive=True,
        )
        location = find_az(env=env, data_dir=data_dir, platform=platform, which=which)
        if location is not None:
            console.ok(f"Azure CLI {_verified(location, console)} installed")
            return location
        console.warn(
            f"winget did not install the Azure CLI (exit {result.returncode}). "
            "Installing a private copy instead; this needs no admin rights."
        )

    venv = _tools_venv(data_dir)
    venv_python = _venv_python(venv, platform)
    console.info("Installing a private copy of the Azure CLI for Entrabot.")
    console.detail("No admin rights needed. This is a large download and takes a few minutes.")
    if not venv_python.is_file():
        venv.parent.mkdir(parents=True, exist_ok=True)
        result = console.run([str(python), "-m", "venv", str(venv)], capture=True, quiet=True)
        if result.returncode != 0:
            raise AzError(f"Could not create a folder for the Azure CLI:\n{_tail(result.stderr)}")
    # Prebuilt packages are preferred (a source package runs its build script here), not
    # required: "--only-binary :all:" would refuse the whole install wherever one of the many
    # dependencies has no prebuilt package for this Python and processor, and nothing here
    # can show that never happens. Where every dependency has one, nothing is built either way.
    result = console.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--prefer-binary",
            AZURE_CLI_REQUIREMENT,
        ],
        capture=True,
        quiet=True,
        heartbeat="Still installing the Azure CLI",
    )
    if result.returncode != 0:
        raise AzError(
            "Installing the Azure CLI failed:\n"
            f"{_tail(result.stderr or result.stdout)}\n"
            "Check the internet connection (a company proxy can block pypi.org), then run "
            "setup again. It continues from here."
        )
    location = _tools_venv_location(data_dir, platform)
    if location is None:
        raise AzError("pip reported success but the Azure CLI is not where it should be.")
    console.ok(f"Azure CLI {_verified(location, console)} installed (private copy for Entrabot)")
    return location


def is_local_admin(console: Console, env: Mapping[str, str]) -> bool:
    """Is the user in the local Administrators group (so a UAC prompt can be answered)?

    A standard user who is shown a UAC prompt is asked for someone else's password, which is
    a dead end; for them the no-admin install path is taken straight away.
    Needs confirmation on real Windows: ``whoami /groups`` output.
    """
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
    result = console.run(
        [str(Path(root) / "System32" / "whoami.exe"), "/groups", "/fo", "csv", "/nh"],
        capture=True,
        quiet=True,
    )
    return result.returncode == 0 and "S-1-5-32-544" in result.stdout


# ── Driving az ───────────────────────────────────────────────────────────────


class AzCli:
    """A located Azure CLI. Every call goes through the wizard's console."""

    def __init__(self, location: AzLocation, console: Console) -> None:
        self.location = location
        self._console = console

    def child_env(self, base: Mapping[str, str]) -> dict[str, str]:
        """*base* with the CLI's folder first on PATH, so helper scripts find the same ``az``."""
        env = dict(base)
        key = next((name for name in env if name.upper() == "PATH"), "PATH")
        current = env.get(key, "")
        env[key] = (
            f"{self.location.bin_dir}{os.pathsep}{current}"
            if current
            else str(self.location.bin_dir)
        )
        return env

    def _argv(self, args: Sequence[str]) -> list[str]:
        if self.location.command[0].lower().endswith(_BATCH_SUFFIXES):
            for arg in args:
                if _CMD_UNSAFE.search(arg):
                    raise AzError(
                        f"The value {arg!r} can't be passed to the Azure CLI on this machine "
                        "(it contains a character the Windows command processor would "
                        "reinterpret)."
                    )
        return [*self.location.command, *args]

    def run(
        self,
        args: Sequence[str],
        *,
        interactive: bool = False,
        sensitive: bool = False,
        quiet: bool = True,
        heartbeat: str | None = None,
    ) -> Any:
        """Run ``az <args>`` and return the CompletedProcess."""
        if interactive:
            return self._console.run(self._argv(args), interactive=True)
        return self._console.run(
            self._argv(args), capture=True, quiet=quiet, sensitive=sensitive, heartbeat=heartbeat
        )

    def json(self, args: Sequence[str], *, sensitive: bool = False) -> Any:
        """Run ``az <args> --output json`` and parse it. Raises :class:`AzError`."""
        result = self.run([*args, "--output", "json"], sensitive=sensitive)
        if result.returncode != 0:
            raise AzError(_tail(result.stderr) or f"az {' '.join(args[:3])} failed")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise AzError(f"az {' '.join(args[:3])} did not return JSON.") from exc

    def try_json(self, args: Sequence[str]) -> Any:
        """Like :meth:`json`, but None when the command fails (the reason is in the log)."""
        try:
            return self.json(args)
        except AzError:
            return None

    def account(self) -> Account | None:
        """The signed-in account, or None when nobody is signed in."""
        data = self.try_json(["account", "show"])
        if not isinstance(data, dict) or not data.get("tenantId"):
            return None
        user = data.get("user") or {}
        return Account(
            user=str(user.get("name") or ""),
            user_type=str(user.get("type") or ""),
            tenant_id=str(data["tenantId"]),
            tenant_name=str(data.get("tenantDisplayName") or data.get("tenantDefaultDomain") or ""),
            subscription_id=str(data.get("id") or ""),
            subscription_name=str(data.get("name") or ""),
            tenant_domain=str(data.get("tenantDefaultDomain") or ""),
        )

    def login(self, *, tenant: str | None, device_code: bool) -> bool:
        """Sign in. ``--allow-no-subscriptions`` because most tenants a person administers
        have no Azure subscription, and plain ``az login`` refuses those."""
        args = ["login", "--allow-no-subscriptions", "--output", "none"]
        if tenant:
            args += ["--tenant", tenant]
        if device_code:
            args.append("--use-device-code")
        return self.run(args, interactive=True).returncode == 0

    def subscriptions(self, tenant_id: str) -> list[Subscription]:
        """Enabled Azure subscriptions in *tenant_id* (tenant-level placeholders excluded)."""
        data = self.try_json(["account", "list"])
        found: list[Subscription] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or item.get("state") != "Enabled":
                continue
            if str(item.get("tenantId")) != tenant_id or str(item.get("id")) == tenant_id:
                continue
            if str(item.get("name") or "").startswith("N/A("):
                continue
            found.append(
                Subscription(
                    id=str(item["id"]),
                    name=str(item.get("name") or item["id"]),
                    tenant_id=tenant_id,
                    is_default=bool(item.get("isDefault")),
                )
            )
        return found

    def me(self) -> DirectoryUser | None:
        data = self.try_json(["ad", "signed-in-user", "show"])
        return _directory_user(data)

    @property
    def _through_cmd(self) -> bool:
        """True when ``az`` can only be started through its batch file. ``cmd.exe`` would then
        reinterpret ``% & ( )`` in an argument, so encoded URLs can't be passed."""
        return self.location.command[0].lower().endswith(_BATCH_SUFFIXES)

    def _graph(
        self,
        path_or_url: str,
        query: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        jmespath: str | None = None,
        sensitive: bool = False,
    ) -> Any:
        """GET from Microsoft Graph with the signed-in user's token, or None on any failure
        (the reason is in the log). Never used for Agent Identity APIs.

        Every query value is percent-encoded here, completely, so nothing in it can be read
        as URL syntax: a raw ``#`` would start a fragment and silently cut the request short,
        a raw ``+`` would arrive as a space.
        """
        url = path_or_url if path_or_url.startswith("https://") else f"{_GRAPH}{path_or_url}"
        if query:
            url += "?" + "&".join(f"{key}={quote(value, safe='')}" for key, value in query.items())
        args = ["rest", "--method", "get", "--url", url]
        if headers:
            args += ["--headers", *(f"{key}={value}" for key, value in headers.items())]
        if jmespath:
            args += ["--query", jmespath]
        try:
            return self.json(args, sensitive=sensitive)
        except AzError:
            return None

    def _users(self, condition: str) -> list[DirectoryUser]:
        """Users matching an OData filter. A filter Graph rejects finds nobody."""
        data = self._graph("/users", {"$filter": condition, "$select": _USER_FIELDS, "$top": "10"})
        found = [_directory_user(item) for item in (data or {}).get("value") or []]
        return [user for user in found if user is not None]

    def search_user(self, identifier: str) -> UserSearch:
        """Find one person from what someone typed: an address, a sign-in name, or an object ID.

        A person's address is not always their sign-in name. A guest, or a personal Microsoft
        account that owns the tenant, signs in as ``name_domain.com#EXT#@tenant.onmicrosoft.com``
        with no ``mail`` at all; the address they are known by is in ``otherMails``, among
        their email aliases, or only inside that sign-in name. So, in order, stopping at the
        first step that finds exactly one person:

        1. the sign-in name or object ID, exactly;
        2. a search for the sign-in name (``userPrincipalName eq``), for a name the first
           lookup can miss: Graph may not take an apostrophe (o'brien@contoso.com) in a path;
        3. the primary email (``mail``);
        4. other email addresses (``otherMails``);
        5. email aliases (``proxyAddresses``, ``SMTP:`` and ``smtp:``);
        6. an external sign-in name made from the address (``@`` becomes ``_``).

        An object ID is only ever looked up by step 1. Raises :class:`AmbiguousUser` when a step
        finds more than one person.
        """
        identifier = identifier.strip()
        if self._through_cmd:
            return self._search_user_through_cmd(identifier)

        is_object_id = bool(_GUID.match(identifier))
        tried = ["that object ID" if is_object_id else "their sign-in name"]
        exact = _directory_user(
            self._graph(f"/users/{quote(identifier, safe='')}", {"$select": _USER_FIELDS})
        )
        if exact is not None or is_object_id:
            return UserSearch(exact, tuple(tried))

        literal = identifier.replace("'", "''")  # OData doubles a quote inside a string
        steps = [
            (_SIGN_IN_SEARCH, [f"userPrincipalName eq '{literal}'"]),
            ("their primary email", [f"mail eq '{literal}'"]),
            ("their other email addresses", [f"otherMails/any(x:x eq '{literal}')"]),
            (
                "their email aliases",
                [
                    f"proxyAddresses/any(x:x eq 'SMTP:{literal}')",
                    f"proxyAddresses/any(x:x eq 'smtp:{literal}')",
                ],
            ),
        ]
        for label, conditions in steps:
            tried.append(label)
            found: dict[str, DirectoryUser] = {}
            for condition in conditions:
                for user in self._users(condition):
                    found[user.object_id] = user
            if len(found) > 1:
                raise AmbiguousUser(identifier, list(found.values()), label)
            if found:
                return UserSearch(next(iter(found.values())), tuple(tried))

        local, _, domain = identifier.partition("@")
        if local and domain:
            label = "a guest or personal account made from that address"
            tried.append(label)
            # No "#" in the filter at all: the directory is asked for names that start with
            # name_domain, and the "#EXT#@" that must follow is checked here.
            prefix = f"{local}_{domain}".lower()
            external = [
                user
                for user in self._users(
                    "startswith(userPrincipalName,'" + prefix.replace("'", "''") + "')"
                )
                if user.upn.lower().startswith(f"{prefix}#ext#@")
            ]
            if len(external) > 1:
                raise AmbiguousUser(identifier, external, label)
            if external:
                return UserSearch(external[0], tuple(tried))
        return UserSearch(None, tuple(tried))

    def _search_user_through_cmd(self, identifier: str) -> UserSearch:
        """The lookups that need no character ``cmd.exe`` would reinterpret."""
        tried = ["their sign-in name or object ID"]
        user = _directory_user(self.try_json(["ad", "user", "show", "--id", identifier]))
        if user is None and "@" in identifier:  # never for an object ID
            literal = identifier.replace("'", "''")  # OData doubles a quote inside a string
            for label, condition in (
                (_SIGN_IN_SEARCH, f"userPrincipalName eq '{literal}'"),
                ("their primary email", f"mail eq '{literal}'"),
            ):
                tried.append(label)
                matches = self.try_json(["ad", "user", "list", "--filter", condition])
                found = [_directory_user(item) for item in matches or []]
                users = [item for item in found if item is not None]
                if len(users) > 1:
                    raise AmbiguousUser(identifier, users, label)
                if users:
                    user = users[0]
                    break
        if user is None:
            tried.append("(other lookups are not possible with this Azure CLI install)")
        return UserSearch(user, tuple(tried))

    def find_user(self, identifier: str) -> DirectoryUser | None:
        """The one person *identifier* names, or None. See :meth:`search_user`."""
        return self.search_user(identifier).user

    def graph_token(self) -> str | None:
        """A Graph token for the signed-in user. Used only to read licence counts; never for
        Agent Identity APIs (those need the provisioner's token)."""
        try:
            data = self.json(
                ["account", "get-access-token", "--resource", "https://graph.microsoft.com"],
                sensitive=True,
            )
        except AzError:
            return None
        token = data.get("accessToken") if isinstance(data, dict) else None
        return str(token) if token else None

    def directory_role_template_ids(self) -> set[str] | None:
        """Role template IDs of the signed-in user's ACTIVE directory roles, or None when they
        can't be read. Roles that are only eligible (PIM) are not included.

        Only directory roles are requested, and three fields of each, so the setup log does
        not fill with every group the person belongs to. Casting ``transitiveMemberOf`` to
        ``directoryRole`` is an "advanced query" in Graph: it needs ``$count=true`` and the
        ``ConsistencyLevel: eventual`` header. A tenant that refuses it gets the plain
        request with az's own ``--query`` dropping everything that is not a role.
        """
        if self._through_cmd:
            # "&" and quotes can't pass through cmd.exe: plain URL, response kept out of the log.
            return self._collect_roles("/me/transitiveMemberOf", sensitive=True)
        roles = self._collect_roles(
            "/me/transitiveMemberOf/microsoft.graph.directoryRole",
            query={"$select": _ROLE_FIELDS, "$count": "true"},
            headers={"ConsistencyLevel": "eventual"},
        )
        if roles is None:
            roles = self._collect_roles("/me/transitiveMemberOf", jmespath=_ROLES_ONLY)
        return roles

    def _collect_roles(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        jmespath: str | None = None,
        sensitive: bool = False,
    ) -> set[str] | None:
        roles: set[str] = set()
        target: str = path
        for _page in range(20):
            data = self._graph(
                target, query, headers=headers, jmespath=jmespath, sensitive=sensitive
            )
            if not isinstance(data, dict):
                return None
            for item in data.get("value") or []:
                if isinstance(item, dict) and item.get("roleTemplateId"):
                    roles.add(str(item["roleTemplateId"]))
            next_link = data.get("@odata.nextLink") or data.get("nextLink")
            if not next_link:
                return roles
            if self._through_cmd and _CMD_UNSAFE.search(str(next_link)):
                return None
            target, query = str(next_link), None  # the link already carries its query
        return None


def _directory_user(data: Any) -> DirectoryUser | None:
    if not isinstance(data, dict) or not data.get("id"):
        return None
    upn = str(data.get("userPrincipalName") or "")
    user_type = str(data.get("userType") or "") or ("Guest" if "#EXT#" in upn else "Member")
    other = data.get("otherMails")
    first_other = str(other[0]) if isinstance(other, list) and other else ""
    return DirectoryUser(
        object_id=str(data["id"]),
        upn=upn,
        mail=str(data.get("mail") or first_other),
        user_type=user_type,
        display_name=str(data.get("displayName") or ""),
    )


def assess_privileges(
    role_template_ids: set[str] | None, *, mode: Literal["new", "join", "continue"]
) -> Privileges:
    """Does the signed-in account look able to finish setup?

    What setup needs, from ``scripts/entra_provisioning.py``:

    * A first run in a tenant creates the "EntraBot Agent ID Provisioner" app registration and
      grants it Microsoft Graph application permissions. Granting Graph application permissions
      takes a Global Administrator, or a Privileged Role Administrator (who also needs to be able to
      manage the app: Application or Cloud Application Administrator).
    * Every machine uploads its own certificate to that app, which takes an Application or
      Cloud Application Administrator, or being an owner of the app.

    This only warns. Roles activated through PIM, custom roles and app ownership can't be seen
    from here, so the answer is advice, not a verdict.
    """
    if role_template_ids is None:
        return Privileges("unknown", "Your admin roles in this tenant could not be read.")
    app_admin = bool(role_template_ids & {APPLICATION_ADMIN, CLOUD_APPLICATION_ADMIN})
    if GLOBAL_ADMIN in role_template_ids or (
        PRIVILEGED_ROLE_ADMIN in role_template_ids and app_admin
    ):
        return Privileges("enough", "Your account has the admin rights setup needs.")
    if mode in ("join", "continue"):
        if app_admin:
            return Privileges("enough", "Your account can add this machine to the agent.")
        return Privileges(
            "none",
            "Your account has no admin role in this tenant. Adding this machine to an existing "
            "agent works only if an administrator has made you an owner of the "
            "'EntraBot Agent ID Provisioner' app registration, or does this step for you.",
        )
    if app_admin:
        return Privileges(
            "partial",
            "Your account can create app registrations but cannot grant admin consent for "
            "Microsoft Graph permissions. If Entrabot has never been set up in this tenant, "
            "that step will fail. Ask a Global Administrator to run setup once, or to activate "
            "that role for you.",
        )
    return Privileges(
        "none",
        "Your account has no admin role in this tenant. Creating a new agent needs a "
        "Global Administrator (or a Privileged Role Administrator who is also an Application "
        "Administrator). Ask one to run setup, or to give you the agent's Blueprint App ID so "
        "you can join it.",
    )
