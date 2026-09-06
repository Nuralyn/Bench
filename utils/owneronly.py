"""Create a file that only its owner can read, on every platform Bench runs on.

The viewer page embeds every diff body the chain holds, so it must be
readable by the user who generated it and nobody else, from its first
byte. On POSIX that is exclusive creation with mode 0600. Windows honours
only the read-only bit of a mode, and a file created there inherits its
directory's ACL (SYSTEM, Administrators, and more), so restricting it after
the write would leave a window in which another principal could open it
and keep a readable handle. The file is therefore created through
``CreateFileW`` with a security descriptor already in hand: a protected
DACL (no inherited entries) holding exactly one entry, full control for
the SID of the user running Bench. Administrators keep no entry; one can
still take ownership, which Windows audits, but cannot simply read.

The Win32 security API is reached via ctypes rather than by spawning
``icacls``, so no process is started and no path becomes an argument to
one. Creation is exclusive on both platforms (``O_EXCL``, ``CREATE_NEW``):
a file that appears at the path between the caller's cleanup and the
create is an error, never something written into.
"""

import os
import sys
from pathlib import Path

# Win32 constants (winnt.h, accctrl.h, fileapi.h). Named here so the calls
# below read as the documented API rather than as magic numbers.
_FILE_ALL_ACCESS: int = 0x001F01FF
_GENERIC_WRITE: int = 0x40000000
_CREATE_NEW: int = 1
_FILE_ATTRIBUTE_NORMAL: int = 0x00000080
_ACL_REVISION: int = 2
_SECURITY_DESCRIPTOR_REVISION: int = 1
_TOKEN_QUERY: int = 0x0008
_TOKEN_USER: int = 1
# sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) - sizeof(DWORD placeholder for the
# SID's first DWORD), per the AddAccessAllowedAce documentation.
_ACL_FIXED_BYTES: int = 8 + 12 - 4


def open_owner_only(path: Path) -> int:
    """Create ``path`` readable and writable by the current user only, and
    return a file descriptor open for writing.

    The file must not exist: creation is exclusive so that nothing already
    at the path, and nothing that appears there concurrently, is written
    into. POSIX: ``O_CREAT | O_EXCL`` with mode 0600. Windows: ``CreateFileW``
    with ``CREATE_NEW`` and a security descriptor holding a one-entry DACL
    for the current user. Raises OSError when the platform refuses, so the
    caller can decide what to do about a page it could not protect; nothing
    is retried or silently skipped.
    """
    if os.name == "nt":
        return _create_windows(path)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _create_windows(path: Path) -> int:
    """CreateFileW with a protected one-entry DACL, returned as a C runtime
    descriptor so the caller can wrap it with os.fdopen like any other."""
    if sys.platform != "win32":  # pragma: no cover: guarded by the caller
        raise OSError("Windows ACLs can only be applied on Windows")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _SecurityDescriptor(ctypes.Structure):
        _fields_ = [
            ("Revision", wintypes.BYTE),
            ("Sbz1", wintypes.BYTE),
            ("Control", wintypes.WORD),
            ("Owner", ctypes.c_void_p),
            ("Group", ctypes.c_void_p),
            ("Sacl", ctypes.c_void_p),
            ("Dacl", ctypes.c_void_p),
        ]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
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
    advapi32.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
    advapi32.SetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p, wintypes.BOOL,
    ]
    advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL

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
        # AddAccessAllowedAce copies the SID into the ACL, so the ACL is
        # self-contained from here on.
        if not advapi32.AddAccessAllowedAce(acl, _ACL_REVISION, _FILE_ALL_ACCESS, sid):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(token)

    descriptor = _SecurityDescriptor()
    if not advapi32.InitializeSecurityDescriptor(
        ctypes.byref(descriptor), _SECURITY_DESCRIPTOR_REVISION
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    # bDaclPresent TRUE with our ACL; bDaclDefaulted FALSE. A descriptor
    # passed to CreateFileW is applied as-is with no inheritance from the
    # directory, which is what makes the file owner-only from creation.
    if not advapi32.SetSecurityDescriptorDacl(ctypes.byref(descriptor), True, acl, False):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), ctypes.addressof(descriptor), False
    )
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_WRITE,
        0,  # no sharing while the page is being written
        ctypes.byref(attributes),
        _CREATE_NEW,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
