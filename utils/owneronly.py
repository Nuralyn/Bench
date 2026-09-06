"""Restrict a file to its owner on every platform Bench runs on.

The viewer page embeds every diff body the chain holds, so it must be
readable by the user who generated it and nobody else. On POSIX that is a
mode of 0600. Windows honours only the read-only bit of a mode, so there a
chmod protects nothing; the file's discretionary ACL has to say it instead.

``restrict_to_owner`` gives the Windows file a protected DACL (inherited
entries dropped) holding exactly one entry: full control for the SID of
the user running Bench. Administrators and SYSTEM keep no entry; an
administrator can still take ownership, which is audited by Windows, but
cannot simply read the file. The work is done through the Win32 security
API via ctypes rather than by spawning ``icacls``, so no process is
started and no path becomes an argument to one.
"""

import os
import sys
from pathlib import Path

# Win32 constants (winnt.h, accctrl.h). Named here so the calls below read
# as the documented API rather than as magic numbers.
_FILE_ALL_ACCESS: int = 0x001F01FF
_ACL_REVISION: int = 2
_SE_FILE_OBJECT: int = 1
_DACL_SECURITY_INFORMATION: int = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION: int = 0x80000000
_TOKEN_QUERY: int = 0x0008
_TOKEN_USER: int = 1
# sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) - sizeof(DWORD placeholder for the
# SID's first DWORD), per the AddAccessAllowedAce documentation.
_ACL_FIXED_BYTES: int = 8 + 12 - 4


def restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable and writable by the current user only.

    POSIX: ``chmod 0600``. Windows: a protected DACL with one full-control
    entry for the current user's SID. Raises OSError when the platform
    refuses, so the caller can decide what to do with a file it could not
    protect; nothing is retried or silently skipped.
    """
    if os.name == "nt":
        _restrict_windows(path)
        return
    os.chmod(path, 0o600)


def _restrict_windows(path: Path) -> None:
    """Replace the file's DACL with a single full-control entry for the
    process's user, and stop it inheriting entries from its directory."""
    if sys.platform != "win32":  # pragma: no cover: guarded by the caller
        raise OSError("Windows ACLs can only be applied on Windows")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    advapi32.AddAccessAllowedAce.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Two calls: the first reports the buffer size the TOKEN_USER needs.
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, token_user, needed, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is
        # the PSID; the SID bytes live inside the same buffer.
        sid = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not sid:
            raise OSError("the process token carries no user SID")
        acl_size: int = _ACL_FIXED_BYTES + int(advapi32.GetLengthSid(sid))
        acl = ctypes.create_string_buffer(acl_size)
        if not advapi32.InitializeAcl(acl, acl_size, _ACL_REVISION):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi32.AddAccessAllowedAce(acl, _ACL_REVISION, _FILE_ALL_ACCESS, sid):
            raise ctypes.WinError(ctypes.get_last_error())
        status: int = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
        if status != 0:
            raise ctypes.WinError(status)
    finally:
        kernel32.CloseHandle(token)
