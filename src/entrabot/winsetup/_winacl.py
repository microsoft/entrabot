"""Windows file permissions, in-process (advapi32 through ctypes). Windows only.

Why not ``icacls``: measured on Windows 11, a new file in a user's project folder carries
SYSTEM, Administrators and the user as EXPLICIT entries, not inherited ones. So
``icacls /inheritance:r /grant:r <user>:M`` exits 0 and leaves SYSTEM and Administrators with
full control: there was no inheritance to remove. The only thing that gives "this user and
nobody else" whatever was there before is to REPLACE the access list with exactly one entry,
which is what :func:`set_dacl` does, in one call.

Every function here declares its argument and return types. Without them ctypes passes and
returns 32-bit ints, which truncates pointers on 64-bit Windows.

Accounts are compared as SIDs, not as SDDL text: SDDL prints some accounts under two-letter
names (``LA`` for the built-in Administrator), and :func:`canonical_sid` turns such a name
back into ``S-1-...`` through Windows itself.

``tests/winsetup/test_winacl.py`` runs this code on any OS against fake DLLs: every function
called has its prototype declared, and everything Windows allocates is freed once, on the
error paths too. Needs confirmation on real Windows: that Windows behaves as those fakes do.
The win32-only tests in ``tests/winsetup/test_envfile.py`` cover that.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser
ERROR_INSUFFICIENT_BUFFER = 122


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


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
    advapi32.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(void_p)]
    advapi32.ConvertStringSidToSidW.restype = bool_

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
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        void_p,
        dword,
        dword,
        ctypes.POINTER(void_p),
        ctypes.POINTER(dword),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = bool_
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
        text = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(text)):
            raise _failed("ConvertSidToStringSidW")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def canonical_sid(text: str) -> str:  # pragma: no cover - Windows only
    """The SID, as ``S-1-...``, of an account named the way SDDL names it: either already a
    SID, or one of the two-letter names for well-known accounts (``SY``, ``BA``, ``LA`` for
    this machine's built-in Administrator, ...). Raises ``OSError`` for anything else.

    Needs confirmation on real Windows: added after the last run there. The win32-only
    tests check ``SY``, ``BA`` and the current user's own SID.
    """
    advapi32, kernel32 = _libraries()
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(text, ctypes.byref(sid)):
        raise _failed("ConvertStringSidToSidW")
    try:
        printed = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(printed)):
            raise _failed("ConvertSidToStringSidW")
        try:
            return ctypes.wstring_at(printed.value)
        finally:
            kernel32.LocalFree(printed)
    finally:
        kernel32.LocalFree(sid)


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


def read_dacl_sddl(path: Path) -> str:  # pragma: no cover - Windows only
    """The file's access list as SDDL text, for example ``D:P(A;;0x1301bf;;;S-1-5-21-...)``."""
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
        text = ctypes.c_void_p()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, SDDL_REVISION_1, DACL_SECURITY_INFORMATION, ctypes.byref(text), None
        ):
            raise _failed("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)
