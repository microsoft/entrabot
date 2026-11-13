"""``_winacl`` itself, on any OS: the real ctypes code, run against a fake advapi32/kernel32.

What this can show without Windows: every function the module calls has its argument and
return types declared (and is called with that many arguments), everything Windows would
allocate is handed back with ``LocalFree`` exactly once, on the error paths too, and nothing
Windows only lends (the access list inside a descriptor, an entry inside the list) is freed.

The fake lays the access list out in memory the way winnt.h documents it (an ``ACL`` header,
then ``ACCESS_ALLOWED_ACE``/``ACCESS_DENIED_ACE`` entries whose SID starts at byte 8, every
SID in its binary form), so the module really walks bytes: a wrong offset reads garbage and
the fake's SID decoder refuses it. What it can't show is that Windows behaves as the fake
does; ``tests/winsetup/test_envfile.py`` has the win32-only tests for that.
"""

from __future__ import annotations

import ctypes
import re
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from entrabot.winsetup import _winacl, envfile
from entrabot.winsetup._winacl import ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE, Ace, Dacl
from tests.winsetup.fakes import dacl_from_sddl

USER = "S-1-5-21-1-2-3-500"  # the built-in Administrator: SDDL would print it as LA
SYSTEM, ADMINISTRATORS, EVERYONE = "S-1-5-18", "S-1-5-32-544", "S-1-1-0"
MODIFY, FULL = 0x1301BF, 0x1F01FF
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


def _address(arg: Any) -> int:
    """The address a pointer argument points at, however the module spelled it."""
    if hasattr(arg, "_obj"):  # ctypes.byref(...)
        return ctypes.addressof(arg._obj)
    return int(_value(arg) or 0)


def _encode_sid(text: str) -> bytes:
    """A SID in its binary form (winnt.h ``SID``): revision, count, 48-bit big-endian
    authority, then each sub-authority as a little-endian DWORD."""
    parts = text.split("-")
    assert parts[0].upper() == "S" and parts[1] == "1", text
    authority, subs = int(parts[2]), [int(part) for part in parts[3:]]
    return (
        bytes([1, len(subs)])
        + authority.to_bytes(6, "big")
        + b"".join(struct.pack("<I", sub) for sub in subs)
    )


def _decode_sid(address: int) -> str:
    revision, count = ctypes.string_at(address, 2)
    assert revision == 1 and count <= 15, f"not a SID at {address}: {revision=} {count=}"
    raw = ctypes.string_at(address, 8 + 4 * count)
    authority = int.from_bytes(raw[2:8], "big")
    subs = struct.unpack(f"<{count}I", raw[8:])
    return "-".join(["S", "1", str(authority), *map(str, subs)])


SE_DACL_PRESENT, SE_SELF_RELATIVE = 0x0004, 0x8000
_DACL_OFFSET = 20  # a self-relative SECURITY_DESCRIPTOR header is 20 bytes; the DACL follows


class FakeWindows:
    """advapi32 and kernel32, in memory. ``live`` holds what Windows allocated and expects
    back through LocalFree; ``freed`` is every LocalFree call, in order."""

    def __init__(self) -> None:
        self.live: dict[int, Any] = {}
        self.freed: list[int] = []
        self.closed: list[int] = []
        self.last_error = 0
        self.fail: set[str] = set()
        self.sid_strings_before_failure: int | None = None  # ConvertSidToStringSidW then fails
        self.sid_strings = 0
        self.files: dict[str, str | Dacl] = {}  # each file's list: SDDL as written, or a Dacl
        self.applied: list[tuple[str, int]] = []
        self.ace_size_override: dict[int, int] = {}  # entry index -> AceSize to claim
        self._descriptor_at: dict[int, str] = {}
        self._keep: list[Any] = []
        user_sid = ctypes.create_string_buffer(_encode_sid(USER))
        self._keep.append(user_sid)
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

    def ConvertSidToStringSidW(self, sid: Any, text: Any) -> int:  # noqa: N802
        if self._failing("ConvertSidToStringSidW"):
            return 0
        if (
            self.sid_strings_before_failure is not None
            and self.sid_strings >= self.sid_strings_before_failure
        ):
            self.last_error = 1337
            return 0
        self.sid_strings += 1
        _out(text, self._alloc_text(_decode_sid(_address(sid))))
        return 1

    def IsValidSid(self, sid: Any) -> int:  # noqa: N802
        revision, count = ctypes.string_at(_address(sid), 2)
        return int(revision == 1 and count <= 15)

    def GetLengthSid(self, sid: Any) -> int:  # noqa: N802
        return 8 + 4 * ctypes.string_at(_address(sid), 2)[1]

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
        """A self-relative descriptor with the DACL laid out after its header, in one block
        Windows allocated, so only the descriptor itself goes back through LocalFree."""
        assert kind == _winacl.SE_FILE_OBJECT
        assert information == _winacl.DACL_SECURITY_INFORMATION
        if path not in self.files:
            return 2  # ERROR_FILE_NOT_FOUND
        stored = self.files[path]
        parsed = stored if isinstance(stored, Dacl) else dacl_from_sddl(stored, user=USER)
        control = SE_SELF_RELATIVE | (SE_DACL_PRESENT if parsed.present else 0)
        if parsed.protected:
            control |= _winacl.SE_DACL_PROTECTED
        acl = self._acl_bytes(parsed.entries) if parsed.present else b""
        header = struct.pack("<BBHIIII", 1, 0, control, 0, 0, 0, _DACL_OFFSET if acl else 0)
        address = self._alloc(len(header) + len(acl) + 8)
        ctypes.memmove(address, header + acl, len(header) + len(acl))
        _out(descriptor, address)
        _out(dacl, address + _DACL_OFFSET if acl else 0)  # NULL: no access list at all
        return 0

    def _acl_bytes(self, entries: tuple[Ace, ...]) -> bytes:
        body = b""
        for index, entry in enumerate(entries):
            if entry.sid is None:  # some other kind: a GUID-sized body nobody should parse
                rest = struct.pack("<I", entry.mask) + b"\xee" * 16
            else:
                rest = struct.pack("<I", entry.mask) + _encode_sid(entry.sid)
            rest += b"\0" * (-len(rest) % 4)  # entries are DWORD-aligned
            size = self.ace_size_override.get(index, 4 + len(rest))
            body += struct.pack("<BBH", entry.type, entry.flags, size) + rest
        return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(entries), 0) + body

    def GetSecurityDescriptorControl(  # noqa: N802
        self, descriptor: Any, control: Any, revision: Any
    ) -> int:
        if self._failing("GetSecurityDescriptorControl"):
            return 0
        address = _value(descriptor)
        assert address in self.live, "GetSecurityDescriptorControl on something else"
        revision_byte, _, word = struct.unpack("<BBH", ctypes.string_at(address, 4))
        control._obj.value = word
        revision._obj.value = revision_byte
        return 1

    def GetAclInformation(  # noqa: N802
        self, acl: Any, information: Any, length: int, cls: int
    ) -> int:
        if self._failing("GetAclInformation"):
            return 0
        assert cls == 2, "AclSizeInformation is class 2"
        assert length == ctypes.sizeof(_winacl._AclSizeInformation) == 12
        _, _, size, count, _ = struct.unpack("<BBHHH", ctypes.string_at(_value(acl), 8))
        ctypes.memmove(_address(information), struct.pack("<III", count, size, 0), 12)
        return 1

    def GetAce(self, acl: Any, index: int, ace: Any) -> int:  # noqa: N802
        if self._failing("GetAce"):
            return 0
        start = _value(acl)
        _, _, _, count, _ = struct.unpack("<BBHHH", ctypes.string_at(start, 8))
        if not 0 <= index < count:
            self.last_error = 87  # ERROR_INVALID_PARAMETER
            return 0
        address = start + 8
        for _ in range(index):
            address += struct.unpack("<H", ctypes.string_at(address + 2, 2))[0]
        _out(ace, address)
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

    def test_the_read_back_never_turns_the_list_into_text(self) -> None:
        # SDDL text is where accounts get their two-letter names; the read-back avoids it.
        source = Path(_winacl.__file__).read_text(encoding="utf-8")
        assert "ConvertSecurityDescriptorToStringSecurityDescriptor" not in source

    def test_the_structures_have_the_documented_layout(self) -> None:
        # winnt.h: ACE_HEADER is BYTE, BYTE, WORD; ACCESS_ALLOWED_ACE/ACCESS_DENIED_ACE are
        # the header, a DWORD mask, then the SID from byte 8; ACL_SIZE_INFORMATION is three
        # DWORDs. None holds a pointer, so the layout is the same on 32- and 64-bit Windows.
        assert ctypes.sizeof(_winacl._AceHeader) == 4
        assert _winacl._AccessAce.Mask.offset == 4
        assert _winacl._AccessAce.SidStart.offset == 8
        assert ctypes.sizeof(_winacl._AclSizeInformation) == 12
        assert ctypes.sizeof(_winacl._TokenUser) == 2 * ctypes.sizeof(ctypes.c_void_p)


class TestCurrentUserSid:
    def test_the_sid_comes_from_the_process_token(self, windows: FakeWindows) -> None:
        assert _winacl.current_user_sid() == USER
        assert windows.closed == [4242] and windows.leaked() == set()

    def test_the_token_is_closed_when_reading_it_fails(self, windows: FakeWindows) -> None:
        windows.fail.add("ConvertSidToStringSidW")
        with pytest.raises(OSError):
            _winacl.current_user_sid()
        assert windows.closed == [4242] and windows.leaked() == set()


class TestSetDacl:
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


class TestReadDacl:
    def _read(self, windows: FakeWindows, tmp_path: Path, stored: str | Dacl) -> Dacl:
        path = tmp_path / ".env"
        windows.files[str(path)] = stored
        return _winacl.read_dacl(path)

    def test_a_locked_file_reads_back_as_one_entry_with_its_numeric_sid(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        # Written as LA, read back as S-1-5-21-...-500: no alias ever reaches the comparison.
        dacl = self._read(windows, tmp_path, "D:P(A;;0x1301bf;;;LA)")
        assert dacl == Dacl(
            present=True,
            protected=True,
            entries=(Ace(type=ACCESS_ALLOWED_ACE_TYPE, flags=0, mask=MODIFY, sid=USER),),
        )
        # The descriptor and the one SID string were freed; the list and entry inside the
        # descriptor were not (LocalFree would have refused them).
        assert len(windows.live) == 2 and windows.leaked() == set()

    def test_every_entry_is_read_in_order_with_its_kind_flags_and_rights(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        dacl = self._read(windows, tmp_path, "D:AI(D;;FA;;;WD)(A;ID;FA;;;SY)(A;;FR;;;BA)")
        assert dacl.protected is False
        assert dacl.entries == (
            Ace(type=ACCESS_DENIED_ACE_TYPE, flags=0, mask=FULL, sid=EVERYONE),
            Ace(type=ACCESS_ALLOWED_ACE_TYPE, flags=0x10, mask=FULL, sid=SYSTEM),
            Ace(type=ACCESS_ALLOWED_ACE_TYPE, flags=0, mask=0x120089, sid=ADMINISTRATORS),
        )
        assert windows.sid_strings == 3 and windows.leaked() == set()

    @pytest.mark.parametrize("kind", [5, 9, 0x0B, 0x11])
    def test_an_entry_of_another_kind_comes_back_with_no_account(
        self, windows: FakeWindows, tmp_path: Path, kind: int
    ) -> None:
        # Object (5), callback (9, 0x0B) and other kinds lay out their bodies differently:
        # reading a SID at byte 8 of them would be a guess. They come back unread.
        stored = Dacl(
            present=True,
            protected=True,
            entries=(Ace(type=0, mask=MODIFY, sid=USER), Ace(type=kind, mask=FULL, sid=None)),
        )
        dacl = self._read(windows, tmp_path, stored)
        assert dacl.entries[1] == Ace(type=kind, flags=0, mask=FULL, sid=None)
        assert windows.sid_strings == 1 and windows.leaked() == set()

    def test_an_entry_too_short_to_hold_its_sid_is_not_read_as_an_account(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        windows.ace_size_override[0] = 12  # header + mask + 4 bytes: no room for any SID
        dacl = self._read(windows, tmp_path, f"D:P(A;;0x1301bf;;;{USER})")
        assert dacl.entries[0].sid is None
        assert windows.leaked() == set()

    def test_no_access_list_at_all_is_reported_as_such(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        dacl = self._read(windows, tmp_path, Dacl(present=False, protected=False))
        assert dacl == Dacl(present=False, protected=False, entries=())
        assert windows.leaked() == set()

    def test_an_empty_list_is_present_with_no_entries(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        dacl = self._read(windows, tmp_path, "D:P")
        assert dacl == Dacl(present=True, protected=True, entries=())

    @pytest.mark.parametrize(
        "failure", ["GetSecurityDescriptorControl", "GetAclInformation", "GetAce"]
    )
    def test_a_step_that_fails_is_an_error_and_the_descriptor_is_still_freed(
        self, windows: FakeWindows, tmp_path: Path, failure: str
    ) -> None:
        windows.fail.add(failure)
        with pytest.raises(OSError, match=failure):
            self._read(windows, tmp_path, f"D:P(A;;0x1301bf;;;{USER})")
        assert windows.leaked() == set()

    def test_a_sid_that_cannot_be_printed_frees_the_ones_already_printed(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        windows.sid_strings_before_failure = 1
        with pytest.raises(OSError, match="ConvertSidToStringSidW"):
            self._read(windows, tmp_path, f"D:P(A;;FA;;;SY)(A;;0x1301bf;;;{USER})")
        assert windows.sid_strings == 1 and len(windows.live) == 2
        assert windows.leaked() == set()

    def test_a_file_that_is_not_there_is_an_error_and_leaks_nothing(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        with pytest.raises(OSError, match="GetNamedSecurityInfoW"):
            _winacl.read_dacl(tmp_path / "missing.env")
        assert windows.leaked() == set()


class TestThroughEnvfile:
    """``envfile`` driving the real ``_winacl`` code, against the fake DLLs."""

    @pytest.fixture(autouse=True)
    def _as_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(envfile, "_is_windows", lambda: True)

    def test_a_file_locked_to_the_built_in_administrator_is_recognised(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        # The account SDDL prints as LA. Compared as text, setup warned that "LA" had access
        # and the user had none, on a correctly locked file (seen on a CI runner).
        path = tmp_path / ".env"
        envfile.lock_to_current_user(path)
        assert windows.files[str(path)] == f"D:P(A;;0x1301bf;;;{USER})"
        assert envfile.lock_problem(path) is None
        windows.files[str(path)] = "D:P(A;;0x1301bf;;;LA)"  # however it came to be written
        assert envfile.lock_problem(path) is None
        assert windows.leaked() == set()

    def test_another_account_next_to_the_user_is_still_reported(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        path = tmp_path / ".env"
        windows.files[str(path)] = "D:P(A;;FA;;;BA)(A;;0x1301bf;;;LA)"
        problem = envfile.lock_problem(path)
        assert problem == "these also have access: Administrators"

    def test_an_unknown_kind_of_entry_is_reported_not_passed(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        path = tmp_path / ".env"
        windows.files[str(path)] = Dacl(
            present=True,
            protected=True,
            entries=(Ace(type=0, mask=MODIFY, sid=USER), Ace(type=9, mask=FULL, sid=None)),
        )
        problem = envfile.lock_problem(path)
        assert problem is not None and "cannot check" in problem

    def test_an_inherited_list_is_reported(self, windows: FakeWindows, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        windows.files[str(path)] = f"D:AI(A;ID;0x1301bf;;;{USER})"
        assert envfile.lock_problem(path) == "it inherits permissions from its folder"

    def test_an_access_list_that_cannot_be_read_is_never_a_pass(
        self, windows: FakeWindows, tmp_path: Path
    ) -> None:
        problem = envfile.lock_problem(tmp_path / "missing.env")
        assert problem is not None and "could not be read" in problem
