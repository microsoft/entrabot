"""The Linux system-call filter, checked by running it: a small interpreter for the classic
BPF instructions the filter uses decides each call the way the kernel would."""

from __future__ import annotations

import struct

import pytest

from entrabot.sandbox import seccomp

ALLOW = 0x7FFF0000
KILL = 0x80000000


def errno(number: int) -> int:
    return 0x00050000 | number


def decide(machine: str, nr: int, *, arch: int | None = None, args: tuple[int, ...] = ()) -> int:
    """What the filter for *machine* returns for system call *nr* with *args*."""
    code = seccomp.program(machine)
    assert code is not None and len(code) % 8 == 0
    name = seccomp.machine_name(machine)
    assert name is not None
    padded = (*args, 0, 0, 0, 0, 0, 0)[:6]
    data = struct.pack("<iIQ6Q", nr, seccomp.AUDIT_ARCH[name] if arch is None else arch, 0, *padded)
    program = [struct.unpack_from("=HBBI", code, offset) for offset in range(0, len(code), 8)]
    accumulator, pc = 0, 0
    for _ in range(len(program) + 1):
        op, jump_true, jump_false, value = program[pc]
        pc += 1
        if op == 0x20:
            accumulator = struct.unpack_from("<I", data, value)[0]
        elif op == 0x15:
            pc += jump_true if accumulator == value else jump_false
        elif op == 0x35:
            pc += jump_true if accumulator >= value else jump_false
        elif op == 0x54:
            accumulator &= value
        elif op == 0x06:
            return value
        else:
            raise AssertionError(f"an instruction the filter should not use: {op:#x}")
    raise AssertionError("the filter did not return")


X86, ARM = "x86_64", "aarch64"
# From the kernel headers, written out again here on purpose: asm/unistd_64.h and
# asm-generic/unistd.h (linux-libc-dev and linux-libc-dev-arm64-cross on Ubuntu 24.04).
READ = {X86: 0, ARM: 63}
KEYCTL = {X86: 250, ARM: 219}
REQUEST_KEY = {X86: 249, ARM: 218}
UNSHARE = {X86: 272, ARM: 97}
MOUNT = {X86: 165, ARM: 40}
PTRACE = {X86: 101, ARM: 117}
CLONE = {X86: 56, ARM: 220}
IOCTL = {X86: 16, ARM: 29}
SOCKET = {X86: 41, ARM: 198}
CONNECT = {X86: 42, ARM: 203}
EXECVE = {X86: 59, ARM: 221}


@pytest.mark.parametrize("machine", [X86, ARM])
class TestFilter:
    def test_ordinary_calls_are_allowed(self, machine: str) -> None:
        # The server and the bridge inside need files, processes and sockets, Unix ones too.
        for call in (READ, SOCKET, CONNECT, EXECVE):
            assert decide(machine, call[machine]) == ALLOW

    def test_the_kernel_keyring_is_refused(self, machine: str) -> None:
        assert decide(machine, KEYCTL[machine]) == errno(seccomp.EPERM)
        assert decide(machine, REQUEST_KEY[machine]) == errno(seccomp.EPERM)

    def test_new_namespaces_and_mounts_are_refused(self, machine: str) -> None:
        assert decide(machine, UNSHARE[machine]) == errno(seccomp.EPERM)
        assert decide(machine, MOUNT[machine]) == errno(seccomp.EPERM)
        new_user = (seccomp.CLONE_NEWUSER | 0x11,)
        assert decide(machine, CLONE[machine], args=new_user) == errno(seccomp.EPERM)

    def test_starting_a_thread_or_a_process_is_allowed(self, machine: str) -> None:
        threads = (0x3D0F00,)  # what pthread_create asks for: no new namespace
        assert decide(machine, CLONE[machine], args=threads) == ALLOW

    def test_clone3_and_the_new_mount_calls_do_not_exist(self, machine: str) -> None:
        # "No such call", so the C library falls back to clone, whose flags can be read.
        for number in (435, 428, 429, 430, 431, 432, 433, 442, 425, 426, 427):
            assert decide(machine, number) == errno(seccomp.ENOSYS)

    def test_tracing_another_process_is_refused(self, machine: str) -> None:
        assert decide(machine, PTRACE[machine]) == errno(seccomp.EPERM)

    def test_typing_into_the_terminal_is_refused_and_other_ioctls_are_not(
        self, machine: str
    ) -> None:
        for request in (seccomp.TIOCSTI, seccomp.TIOCLINUX):
            assert decide(machine, IOCTL[machine], args=(0, request)) == errno(seccomp.EPERM)
            # The kernel reads the request as 32 bits: high bits must not get it through.
            sneaky = (0, request | (1 << 32))
            assert decide(machine, IOCTL[machine], args=sneaky) == errno(seccomp.EPERM)
        assert decide(machine, IOCTL[machine], args=(0, 0x5401)) == ALLOW  # TCGETS

    def test_a_process_of_another_architecture_is_killed(self, machine: str) -> None:
        assert decide(machine, 1, arch=0x40000003) == KILL  # 32-bit x86


def test_x32_calls_are_refused_on_x86_64() -> None:
    assert decide(X86, seccomp.X32_SYSCALL_BIT | 0) == errno(seccomp.ENOSYS)


def test_every_listed_call_is_refused_and_numbers_are_not_repeated() -> None:
    for machine, column in ((X86, 1), (ARM, 2)):
        numbers = [row[column] for row in seccomp.DENIED if row[column] is not None]
        numbers += [number for _, number in seccomp.UNAVAILABLE]
        assert len(numbers) == len(set(numbers)), machine
        for number in numbers:
            assert decide(machine, number) != ALLOW


def test_other_machines_have_no_filter() -> None:
    assert seccomp.program("riscv64") is None
    assert seccomp.program("AMD64") is not None and seccomp.program("arm64") is not None
