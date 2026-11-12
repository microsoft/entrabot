"""``_winacl`` itself, on any OS: the real ctypes code, run against a fake advapi32/kernel32.

What this can show without Windows: every function the module calls has its argument and
return types declared (and is called with that many arguments), everything Windows would
allocate is handed back with ``LocalFree`` exactly once, on the error paths too, and an
account SDDL prints as a two-letter name is compared as its real SID. What it can't show is
that Windows behaves as the fake does; ``tests/winsetup/test_envfile.py`` has the win32-only
tests for that.
"""

from __future__ import annotations

import ctypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from entrabot.winsetup import _winacl, envfile

USER = "S-1-5-21-1-2-3-500"  # the built-in Administrator: SDDL prints it as LA
_UNSET = object()


class _Function:
    """One DLL export. It refuses to run until its prototype has been declared."""

    def __init__(self, name: str, impl: Callable[..., Any] | None) -> None:
        self.name = name
        self.impl = impl
        self.argtypes: list[Any] | None = None
        self.restype: Any = _UNSET

    def __call__(self, *args: Any) -> Any:
        assert self.argtypes is not None, f"{self.name}: argtypes not declared"
        assert self.restype is not _UNSET, f"{self.name}: restype not declared"
        assert len(args) == len(self.argtypes), f"{self.name}: called with {len(args)} args"
        assert self.impl is not None, f"{self.name}: not expected to be called"
        return self.impl(*args)


class _Dll:
    def __init__(self, impls: dict[str, Callable[..., Any]]) -> None:
        self._impls = impls
        self._functions: dict[str, _Function] = {}

    def __getattr__(self, name: str) -> _Function:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._functions:
            self._functions[name] = _Function(name, self._impls.get(name))
        return self._functions[name]


def _value(arg: Any) -> int | None:
    return arg.value if hasattr(arg, "value") else arg


def _out(ref: Any, value: int) -> None:
    ref._obj.value = value  # what Windows does with a pointer it was handed by reference


class FakeWindows:
    """advapi32 and kernel32, in memory. ``live`` holds what Windows allocated and expects
    back through LocalFree; ``freed`` is every LocalFree call, in order."""

    ALIASES = {"SY": "S-1-5-18", "BA": "S-1-5-32-544", "LA": USER}

    def __init__(self) -> None:
        self.live: dict[int, Any] = {}
        self.freed: list[int] = []
        self.closed: list[int] = []
        self.last_error = 0
        self.fail: set[str] = set()
        self.files: dict[str, str] = {}
        self.applied: list[tuple[str, int]] = []
        self._sid_at: dict[int, str] = {}
        self._descriptor_at: dict[int, str] = {}
        self._keep: list[Any] = []
        user_sid = ctypes.create_string_buffer(16)
        self._keep.append(user_sid)
        self._sid_at[ctypes.addressof(user_sid)] = USER
        self.user_sid_address = ctypes.addressof(user_sid)

    # memory Windows hands out
    def _alloc(self, size: int = 16) -> int:
        buffer = ctypes.create_string_buffer(size)
        address = ctypes.addressof(buffer)
        self.live[address] = buffer
        return address

    def _alloc_text(self, text: str) -> int:
        buffer = ctypes.create_unicode_buffer(text)
        address = ctypes.addressof(buffer)
        self.live[address] = buffer
        return address

    def _failing(self, name: str, code: int = 5) -> bool:
        if name in self.fail:
            self.last_error = code
            return True
        return False

    # kernel32
    def LocalFree(self, pointer: Any) -> None:  # noqa: N802 - the Windows name
        address = _value(pointer)
        assert address in self.live, f"LocalFree of memory Windows did not allocate: {address}"
        assert address not in self.freed, "LocalFree called twice on the same memory"
        self.freed.append(address)
        return None

    def CloseHandle(self, handle: Any) -> int:  # noqa: N802
        self.closed.append(_value(handle) or 0)
        return 1

    # advapi32
    def OpenProcessToken(self, process: Any, access: int, token: Any) -> int:  # noqa: N802
        if self._failing("OpenProcessToken"):
            return 0
        _out(token, 4242)
        return 1

    def GetTokenInformation(  # noqa: N802
        self, token: Any, cls: int, buffer: Any, size: int, needed: Any
    ) -> int:
        if buffer is None or size == 0:
            needed._obj.value = ctypes.sizeof(_winacl._TokenUser)
            self.last_error = _winacl.ERROR_INSUFFICIENT_BUFFER
            return 0
        _winacl._TokenUser.from_buffer(buffer).User.Sid = self.user_sid_address
        return 1

    def ConvertStringSidToSidW(self, text: str, sid: Any) -> int:  # noqa: N802
        resolved = self.ALIASES.get(text.upper(), text if text.upper().startswith("S-1-") else "")
        if not resolved or self._failing("ConvertStringSidToSidW", 1337):
            self.last_error = 1337
            return 0
        address = self._alloc()
        self._sid_at[address] = resolved
        _out(sid, address)
        return 1

    def ConvertSidToStringSidW(self, sid: Any, text: Any) -> int:  # noqa: N802
        if self._failing("ConvertSidToStringSidW"):
            return 0
        _out(text, self._alloc_text(self._sid_at[_value(sid) or 0]))
        return 1

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(  # noqa: N802
        self, sddl: str, revision: int, descriptor: Any, size: Any
    ) -> int:
        if self._failing("ConvertStringSecurityDescriptorToSecurityDescriptorW", 1336):
            return 0
        address = self._alloc()
        self._descriptor_at[address] = sddl
        _out(descriptor, address)
        return 1

    def GetSecurityDescriptorDacl(  # noqa: N802
        self, descriptor: Any, present: Any, dacl: Any, defaulted: Any
    ) -> int:
        present._obj.value = 1
        _out(dacl, _value(descriptor) + 8)  # inside the descriptor: never freed on its own
        return 1

    def SetNamedSecurityInfoW(  # noqa: N802
        self,
        path: str,
        kind: int,
        information: int,
        owner: Any,
        group: Any,
        dacl: Any,
        sacl: Any,
    ) -> int:
        if "SetNamedSecurityInfoW" in self.fail:
            return 5
        self.files[path] = self._descriptor_at[_value(dacl) - 8]
        self.applied.append((path, information))
        return 0

    def GetNamedSecurityInfoW(  # noqa: N802
        self,
        path: str,
        kind: int,
        information: int,
        owner: Any,
        group: Any,
        dacl: Any,
        sacl: Any,
        descriptor: Any,
    ) -> int:
        if path not in self.files:
            return 2  # ERROR_FILE_NOT_FOUND
        address = self._alloc()
        self._descriptor_at[address] = self.files[path]
        _out(descriptor, address)
        _out(dacl, address + 8)
        return 0

    def ConvertSecurityDescriptorToStringSecurityDescriptorW(  # noqa: N802
        self, descriptor: Any, revision: int, information: int, text: Any, size: Any
    ) -> int:
        if self._failing("ConvertSecurityDescriptorToStringSecurityDescriptorW"):
            return 0
        _out(text, self._alloc_text(self._descriptor_at[_value(descriptor) or 0]))
        return 1

    def GetCurrentProcess(self) -> int:  # noqa: N802
        return -1

    def install(self, monkeypatch: pytest.MonkeyPatch) -> tuple[_Dll, _Dll]:
        names = [name for name in dir(self) if name[:1].isupper()]
        impls = {name: getattr(self, name) for name in names}
        advapi32, kernel32 = _Dll(impls), _Dll(impls)
        dlls = {"advapi32": advapi32, "kernel32": kernel32}
        monkeypatch.setattr(
            ctypes, "WinDLL", lambda name, use_last_error=False: dlls[name], raising=False
        )
        monkeypatch.setattr(ctypes, "get_last_error", lambda: self.last_error, raising=False)
        monkeypatch.setattr(ctypes, "FormatError", lambda code: f"code {code}", raising=False)
        return advapi32, kernel32

    def leaked(self) -> set[int]:
        return set(self.live) - set(self.freed)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> FakeWindows:
    fake = FakeWindows()
    fake.install(monkeypatch)
    return fake


class TestPrototypes:
    def test_every_function_the_module_calls_has_its_types_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without them ctypes passes and returns 32-bit ints, truncating 64-bit pointers.
        advapi32, kernel32 = FakeWindows().install(monkeypatch)
        _winacl._libraries()
        source = Path(_winacl.__file__).read_text(encoding="utf-8")
        called = set(re.findall(r"\b(advapi32|kernel32)\.([A-Za-z]+)\(", source))
        assert called, "the scan found no calls at all"
        for dll_name, function in sorted(called):
            declared = (advapi32 if dll_name == "advapi32" else kernel32)._functions.get(function)
            assert declared is not None, f"{dll_name}.{function} has no prototype"
            assert declared.argtypes is not None, f"{dll_name}.{function}.argtypes"
            assert declared.restype is not _UNSET, f"{dll_name}.{function}.restype"


class TestCanonicalSid:
    def test_a_two_letter_name_becomes_its_sid_and_both_allocations_are_freed(
        self, windows: FakeWindows
    ) -> None:
        assert _winacl.canonical_sid("LA") == USER
        assert _winacl.canonical_sid("sy") == "S-1-5-18"
        assert len(windows.live) == 4 and windows.leaked() == set()

    def test_a_name_that_is_no_account_is_an_error_and_nothing_leaks(
        self, windows: FakeWindows
    ) -> None:
        with pytest.raises(OSError, match="ConvertStringSidToSidW"):
            _winacl.canonical_sid("ZZ")
        assert windows.leaked() == set()

    def test_the_sid_is_freed_even_when_printing_it_fails(self, windows: FakeWindows) -> None:
        windows.fail.add("ConvertSidToStringSidW")
        with pytest.raises(OSError, match="ConvertSidToStringSidW"):
            _winacl.canonical_sid("BA")
        assert len(windows.live) == 1 and windows.leaked() == set()


class TestCurrentUserSid:
    def test_the_sid_comes_from_the_process_token(self, windows: FakeWindows) -> None:
        assert _winacl.current_user_sid() == USER
        assert windows.closed == [4242] and windows.leaked() == set()

    def test_the_token_is_closed_when_reading_it_fails(self, windows: FakeWindows) -> None:
        windows.fail.add("ConvertSidToStringSidW")
        with pytest.raises(OSError):
            _winacl.current_user_sid()
        assert windows.closed == [4242] and windows.leaked() == set()


class TestSetAndRead:
    def test_a_lock_is_applied_protected_and_the_descriptor_is_freed(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        path = tmp_path / ".env"
        _winacl.set_dacl(path, f"D:P(A;;0x1301bf;;;{USER})")
        ((applied_to, information),) = windows.applied
        assert applied_to == str(path)
        assert information & _winacl.PROTECTED_DACL_SECURITY_INFORMATION
        assert information & _winacl.DACL_SECURITY_INFORMATION
        assert windows.leaked() == set()

    def test_a_refused_lock_is_an_error_and_still_frees(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        windows.fail.add("SetNamedSecurityInfoW")
        with pytest.raises(OSError, match="SetNamedSecurityInfoW"):
            _winacl.set_dacl(tmp_path / ".env", f"D:P(A;;0x1301bf;;;{USER})")
        assert windows.leaked() == set()

    def test_reading_back_frees_the_descriptor_and_the_text(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        path = tmp_path / ".env"
        windows.files[str(path)] = "D:P(A;;0x1301bf;;;LA)"
        assert _winacl.read_dacl_sddl(path) == "D:P(A;;0x1301bf;;;LA)"
        assert len(windows.live) == 2 and windows.leaked() == set()

    @pytest.mark.parametrize("failure", ["missing", "convert"])
    def test_a_read_that_fails_is_an_error_and_leaks_nothing(
        self, windows: FakeWindows, tmp_path: Path, failure: str
    ) -> None:
        path = tmp_path / ".env"
        if failure == "convert":
            windows.files[str(path)] = "D:P(A;;FA;;;SY)"
            windows.fail.add("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        with pytest.raises(OSError):
            _winacl.read_dacl_sddl(path)
        assert windows.leaked() == set()


class TestThroughEnvfile:
    """``envfile`` driving the real ``_winacl`` code, against the fake DLLs."""

    @pytest.fixture(autouse=True)
    def _as_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(envfile, "_is_windows", lambda: True)

    def test_a_file_locked_to_the_built_in_administrator_is_recognised(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        # Windows prints this account as LA, not as its SID. Compared as text, setup would
        # warn that "LA" has access and the user has none, on a correctly locked file.
        path = tmp_path / ".env"
        envfile.lock_to_current_user(path)
        assert windows.files[str(path)] == f"D:P(A;;0x1301bf;;;{USER})"
        windows.files[str(path)] = "D:P(A;;0x1301bf;;;LA)"  # as Windows reads it back
        assert envfile.lock_problem(path) is None
        assert windows.leaked() == set()

    def test_another_account_next_to_the_user_is_still_reported(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        path = tmp_path / ".env"
        windows.files[str(path)] = "D:P(A;;FA;;;BA)(A;;0x1301bf;;;LA)"
        problem = envfile.lock_problem(path)
        assert problem == "these also have access: Administrators"

    def test_an_access_list_that_cannot_be_read_is_never_a_pass(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        problem = envfile.lock_problem(tmp_path / "missing.env")
        assert problem is not None and "could not be read" in problem
