"""A system-call filter for the Linux sandbox (``bwrap --seccomp``).

The mounts and namespaces of :mod:`entrabot.sandbox.bwrap` decide what the contained server
can see. This filter takes away the kernel calls it could use to change that view or to
reach around it: new namespaces and mounts, the kernel keyring (which is not namespaced, and
whose ``request_key`` can make the host run a helper), tracing other processes, typing into
the controlling terminal, and a few calls with no use in a server. The list follows Flatpak's
(``setup_seccomp`` in its ``flatpak-run.c``), with ``io_uring``, ``process_vm_*``, ``bpf``,
``userfaultfd`` and the module and ``kexec`` calls added.

The program is classic BPF, assembled here so that nothing has to be installed for it: an
array of ``struct sock_filter`` (``u16 code, u8 jt, u8 jf, u32 k``), which is what ``bwrap``
reads from the file descriptor. Everything not listed is allowed.

System-call numbers differ by machine. They are listed for x86-64 and arm64, taken from the
kernel headers (``asm/unistd_64.h`` and ``asm-generic/unistd.h``). A process of another
architecture inside the sandbox (32-bit x86 on an x86-64 kernel) is killed, and the x32 ABI's
calls are refused: neither could be filtered by these numbers.
"""

from __future__ import annotations

import struct

EPERM = 1
ENOSYS = 38

_ALLOW = 0x7FFF0000
_ERRNO = 0x00050000
_KILL_PROCESS = 0x80000000
_LD_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_AND = 0x54  # BPF_ALU | BPF_AND | BPF_K
_RET = 0x06  # BPF_RET | BPF_K
# struct seccomp_data: int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6].
_NR, _ARCH = 0, 4
_ARG0_LOW, _ARG1_LOW = 16, 24  # little-endian: the low half of each argument comes first

X32_SYSCALL_BIT = 0x40000000
CLONE_NEWUSER = 0x10000000
TIOCSTI = 0x5412
TIOCLINUX = 0x541C

AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}

# Refused with EPERM: (name, x86-64 number, arm64 number). None: that machine has no such call.
DENIED: tuple[tuple[str, int | None, int | None], ...] = (
    # The kernel keyring is shared with the host, and request_key can start a host helper.
    ("add_key", 248, 217),
    ("request_key", 249, 218),
    ("keyctl", 250, 219),
    # New namespaces and mounts: a way to undo the read-only view from inside.
    ("unshare", 272, 97),
    ("setns", 308, 268),
    ("mount", 165, 40),
    ("umount2", 166, 39),
    ("pivot_root", 155, 41),
    ("chroot", 161, 51),
    # Reading or controlling another process.
    ("ptrace", 101, 117),
    ("process_vm_readv", 310, 270),
    ("process_vm_writev", 311, 271),
    ("perf_event_open", 298, 241),
    ("bpf", 321, 280),
    ("userfaultfd", 323, 282),
    # No use in a server, and each has been a source of kernel bugs or leaks.
    ("syslog", 103, 116),
    ("acct", 163, 89),
    ("quotactl", 179, 60),
    ("move_pages", 279, 239),
    ("mbind", 237, 235),
    ("get_mempolicy", 239, 236),
    ("set_mempolicy", 238, 237),
    ("migrate_pages", 256, 238),
    ("kexec_load", 246, 104),
    ("kexec_file_load", 320, 294),
    ("swapon", 167, 224),
    ("swapoff", 168, 225),
    ("reboot", 169, 142),
    ("init_module", 175, 105),
    ("finit_module", 313, 273),
    ("delete_module", 176, 106),
    ("uselib", 134, None),
    ("modify_ldt", 154, None),
)
# Refused with ENOSYS, "no such call", so that a program falls back to an older one. clone3
# hides its flags in a struct the filter cannot read, so it goes and clone is checked instead.
# These numbers are the same on every machine (all were added after Linux 5.1).
UNAVAILABLE: tuple[tuple[str, int], ...] = (
    ("clone3", 435),
    ("open_tree", 428),
    ("move_mount", 429),
    ("fsopen", 430),
    ("fsconfig", 431),
    ("fsmount", 432),
    ("fspick", 433),
    ("mount_setattr", 442),
    ("io_uring_setup", 425),
    ("io_uring_enter", 426),
    ("io_uring_register", 427),
)
CLONE = {"x86_64": 56, "aarch64": 220}  # flags are the first argument on both
IOCTL = {"x86_64": 16, "aarch64": 29}


def machine_name(machine: str) -> str | None:
    """``platform.machine()`` as a machine this filter is written for, or None."""
    return MACHINES.get(machine.strip().lower())


def _instruction(code: int, jump_true: int, jump_false: int, value: int) -> bytes:
    return struct.pack("=HBBI", code, jump_true, jump_false, value & 0xFFFFFFFF)


def program(machine: str) -> bytes | None:
    """The filter for *machine* (``platform.machine()``), or None when there is none for it."""
    name = machine_name(machine)
    if name is None:
        return None
    column = 1 if name == "x86_64" else 2
    out = [
        _instruction(_LD_ABS, 0, 0, _ARCH),
        _instruction(_JEQ, 1, 0, AUDIT_ARCH[name]),
        _instruction(_RET, 0, 0, _KILL_PROCESS),
        _instruction(_LD_ABS, 0, 0, _NR),
    ]
    if name == "x86_64":
        out += [
            _instruction(_JGE, 0, 1, X32_SYSCALL_BIT),
            _instruction(_RET, 0, 0, _ERRNO | ENOSYS),
        ]
    for row in DENIED:
        number = row[column]
        if number is not None:
            out += [_instruction(_JEQ, 0, 1, number), _instruction(_RET, 0, 0, _ERRNO | EPERM)]
    for _, number in UNAVAILABLE:
        out += [_instruction(_JEQ, 0, 1, number), _instruction(_RET, 0, 0, _ERRNO | ENOSYS)]
    out += [
        # clone that asks for a new user namespace
        _instruction(_JEQ, 0, 5, CLONE[name]),
        _instruction(_LD_ABS, 0, 0, _ARG0_LOW),
        _instruction(_AND, 0, 0, CLONE_NEWUSER),
        _instruction(_JEQ, 1, 0, 0),
        _instruction(_RET, 0, 0, _ERRNO | EPERM),
        _instruction(_LD_ABS, 0, 0, _NR),
        # ioctl that types into the controlling terminal (CVE-2017-5226, CVE-2023-28100)
        _instruction(_JEQ, 0, 4, IOCTL[name]),
        _instruction(_LD_ABS, 0, 0, _ARG1_LOW),
        _instruction(_JEQ, 1, 0, TIOCSTI),
        _instruction(_JEQ, 0, 1, TIOCLINUX),
        _instruction(_RET, 0, 0, _ERRNO | EPERM),
        _instruction(_RET, 0, 0, _ALLOW),
    ]
    return b"".join(out)
