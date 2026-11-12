"""Windows file permissions, in-process (advapi32 through ctypes). Windows only.

Why not ``icacls``: measured on Windows 11, a new file in a user's project folder carries
SYSTEM, Administrators and the user as EXPLICIT entries, not inherited ones. So
``icacls /inheritance:r /grant:r <user>:M`` exits 0 and leaves SYSTEM and Administrators with
full control: there was no inheritance to remove. The only thing that gives "this user and
nobody else" whatever was there before is to REPLACE the access list with exactly one entry,
which is what :func:`set_dacl` does, in one call.

Every function here declares its argument and return types. Without them ctypes passes and
returns 32-bit ints, which truncates pointers on 64-bit Windows.

Reading back is never done through SDDL text. SDDL prints some accounts under two-letter
names (``LA`` for the built-in Administrator, which is the account a CI runner uses), so text
can spell the very same account differently from the SID it is compared with.
:func:`read_dacl` walks the binary access list instead (``GetAclInformation``, ``GetAce``)
and turns every entry's SID into ``S-1-...`` with ``ConvertSidToStringSidW``, which prints
numbers, never names. The layouts are the ones winnt.h documents: ``ACE_HEADER`` is
BYTE/BYTE/WORD, and ``ACCESS_ALLOWED_ACE``/``ACCESS_DENIED_ACE`` are that header, a DWORD
mask, and the SID from byte 8. They hold no pointers, so they are the same on 32- and 64-bit
Windows. Any other kind of entry is handed back with no account: it is never guessed at.

``tests/winsetup/test_winacl.py`` runs this code on any OS against fake DLLs that lay the
list out byte for byte: every function called has its prototype declared, and everything
Windows allocates is freed once, on the error paths too. Needs confirmation on real Windows:
that Windows behaves as those fakes do. The win32-only tests in
``tests/winsetup/test_envfile.py`` cover that.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_DACL_PROTECTED = 0x1000  # SECURITY_DESCRIPTOR_CONTROL: inheritance is switched off
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser
ACL_SIZE_INFORMATION_CLASS = 2  # ACL_INFORMATION_CLASS.AclSizeInformation
ERROR_INSUFFICIENT_BUFFER = 122
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
_MIN_SID_BYTES = 8  # revision, sub-authority count, 6-byte authority: a SID with no subs


@dataclass(frozen=True)
class Ace:
    """One entry of an access list, as Windows stores it.

    *sid* is the account as ``S-1-...``, or None for an entry whose account was not read: a
    kind other than plain allow/deny (object and callback entries are laid out differently),
    or one too short or malformed to hold a SID. Such an entry can't be vouched for.
    """

    type: int
    flags: int = 0
    mask: int = 0
    sid: str | None = None


@dataclass(frozen=True)
class Dacl:
    """A file's access list. *present* False means it has none at all (a NULL DACL), which
    lets everyone in. *protected* means it does not inherit from the folder."""

    present: bool
    protected: bool
    entries: tuple[Ace, ...] = ()


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _AceHeader(ctypes.Structure):  # winnt.h ACE_HEADER
    _fields_ = [
        ("AceType", ctypes.c_uint8),
        ("AceFlags", ctypes.c_uint8),
        ("AceSize", ctypes.c_uint16),
    ]


class _AccessAce(ctypes.Structure):  # winnt.h ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE
    _fields_ = [("Header", _AceHeader), ("Mask", ctypes.c_uint32), ("SidStart", ctypes.c_uint32)]


class _AclSizeInformation(ctypes.Structure):  # winnt.h ACL_SIZE_INFORMATION
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


def _libraries() -> tuple[Any, Any]:  # pragma: no cover - Windows only
    """advapi32 and kernel32 with every prototype spelled out."""
    void_p, dword, bool_ = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = void_p
    kernel32.CloseHandle.argtypes = [void_p]
    kernel32.CloseHandle.restype = bool_
    kernel32.LocalFree.argtypes = [void_p]
    kernel32.LocalFree.restype = void_p

    advapi32.OpenProcessToken.argtypes = [void_p, dword, ctypes.POINTER(void_p)]
    advapi32.OpenProcessToken.restype = bool_
    advapi32.GetTokenInformation.argtypes = [
        void_p,
        ctypes.c_int,
        void_p,
        dword,
        ctypes.POINTER(dword),
    ]
    advapi32.GetTokenInformation.restype = bool_
    advapi32.ConvertSidToStringSidW.argtypes = [void_p, ctypes.POINTER(void_p)]
    advapi32.ConvertSidToStringSidW.restype = bool_
    advapi32.IsValidSid.argtypes = [void_p]
    advapi32.IsValidSid.restype = bool_
    advapi32.GetLengthSid.argtypes = [void_p]
    advapi32.GetLengthSid.restype = dword

    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        dword,
        ctypes.POINTER(void_p),
        ctypes.POINTER(dword),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = bool_
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        void_p,
        ctypes.POINTER(bool_),
        ctypes.POINTER(void_p),
        ctypes.POINTER(bool_),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = bool_
    advapi32.SetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        dword,
        void_p,
        void_p,
        void_p,
        void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = dword  # a Win32 error code; 0 is success

    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        dword,
        ctypes.POINTER(void_p),
        ctypes.POINTER(void_p),
        ctypes.POINTER(void_p),
        ctypes.POINTER(void_p),
        ctypes.POINTER(void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = dword
    advapi32.GetSecurityDescriptorControl.argtypes = [
        void_p,
        ctypes.POINTER(ctypes.c_uint16),  # SECURITY_DESCRIPTOR_CONTROL is a WORD
        ctypes.POINTER(dword),
    ]
    advapi32.GetSecurityDescriptorControl.restype = bool_
    advapi32.GetAclInformation.argtypes = [void_p, void_p, dword, ctypes.c_int]
    advapi32.GetAclInformation.restype = bool_
    advapi32.GetAce.argtypes = [void_p, dword, ctypes.POINTER(void_p)]
    advapi32.GetAce.restype = bool_
    return advapi32, kernel32


def _failed(what: str, code: int | None = None) -> OSError:  # pragma: no cover - Windows only
    code = ctypes.get_last_error() if code is None else code  # type: ignore[attr-defined]
    return OSError(f"{what} failed (Windows error {code}: {ctypes.FormatError(code).strip()})")  # type: ignore[attr-defined]


def current_user_sid() -> str:  # pragma: no cover - Windows only
    """The SID of the account this process runs as, from its own access token."""
    advapi32, kernel32 = _libraries()
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise _failed("OpenProcessToken")
    try:
        needed = ctypes.c_uint32(0)
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not needed.value:  # type: ignore[attr-defined]
            raise _failed("GetTokenInformation (size)")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER_CLASS, buffer, needed.value, ctypes.byref(needed)
        ):
            raise _failed("GetTokenInformation")
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_text(advapi32, kernel32, user.User.Sid)
    finally:
        kernel32.CloseHandle(token)


def _sid_text(advapi32: Any, kernel32: Any, sid: int) -> str:  # pragma: no cover - Windows
    """``S-1-...`` for the binary SID at *sid*. Always numbers, never an SDDL short name."""
    text = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _failed("ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


def set_dacl(path: Path, sddl: str) -> None:  # pragma: no cover - Windows only
    """Replace the file's access list with exactly what *sddl* says (for example
    ``D:P(A;;0x1301bf;;;<SID>)``), whatever was there before. A ``P`` flag also switches
    inheritance off."""
    advapi32, kernel32 = _libraries()
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise _failed("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present, defaulted = ctypes.c_int(0), ctypes.c_int(0)
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise _failed("GetSecurityDescriptorDacl")
        if not present.value or not dacl.value:
            raise OSError("The security descriptor has no access list to apply.")
        information = DACL_SECURITY_INFORMATION
        if "D:P" in sddl:
            information |= PROTECTED_DACL_SECURITY_INFORMATION
        code = advapi32.SetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT, information, None, None, dacl, None
        )
        if code != 0:
            raise _failed("SetNamedSecurityInfoW", code)
    finally:
        kernel32.LocalFree(descriptor)


def _read_ace(advapi32: Any, kernel32: Any, address: int) -> Ace:  # pragma: no cover
    """The entry at *address* (inside the list: Windows lends it, nothing to free)."""
    header = _AceHeader.from_address(address)
    kind, flags, size = header.AceType, header.AceFlags, header.AceSize
    if kind not in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
        # Object and callback entries put other fields before the SID: never guess at it.
        mask = _AccessAce.from_address(address).Mask if size >= _AccessAce.SidStart.offset else 0
        return Ace(type=kind, flags=flags, mask=mask, sid=None)
    ace = _AccessAce.from_address(address)
    room = size - _AccessAce.SidStart.offset
    if room < _MIN_SID_BYTES:
        return Ace(type=kind, flags=flags, mask=ace.Mask, sid=None)
    sid = address + _AccessAce.SidStart.offset
    if not advapi32.IsValidSid(sid) or advapi32.GetLengthSid(sid) > room:
        return Ace(type=kind, flags=flags, mask=ace.Mask, sid=None)
    return Ace(type=kind, flags=flags, mask=ace.Mask, sid=_sid_text(advapi32, kernel32, sid))


def read_dacl(path: Path) -> Dacl:  # pragma: no cover - Windows only
    """The file's access list, entry by entry, every account as a numeric ``S-1-...`` SID,
    and whether it is protected from inheritance. Raises ``OSError`` when any part of it
    can't be read: a half-read list is never handed back."""
    advapi32, kernel32 = _libraries()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    code = advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code != 0:
        raise _failed("GetNamedSecurityInfoW", code)
    try:
        control, revision = ctypes.c_uint16(0), ctypes.c_uint32(0)
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise _failed("GetSecurityDescriptorControl")
        protected = bool(control.value & SE_DACL_PROTECTED)
        if not dacl.value:  # no DACL at all: Windows lets everyone in
            return Dacl(present=False, protected=protected)
        size = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(size), ctypes.sizeof(size), ACL_SIZE_INFORMATION_CLASS
        ):
            raise _failed("GetAclInformation")
        entries: list[Ace] = []
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise _failed("GetAce")
            entries.append(_read_ace(advapi32, kernel32, ace.value))
        return Dacl(present=True, protected=protected, entries=tuple(entries))
    finally:
        kernel32.LocalFree(descriptor)
