"""``.env`` is read the way the runtime reads it, and rewritten without leaving stale lines."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from entrabot.winsetup import envfile
from entrabot.winsetup._winacl import ACCESS_ALLOWED_ACE_TYPE, Ace, Dacl
from entrabot.winsetup.envfile import (
    EnvGuard,
    EnvLockError,
    dacl_lock_problem,
    lock_problem,
    lock_to_current_user,
    private_sddl,
    read_env,
    update_env,
)
from tests.winsetup.fakes import FakeSpawn, FakeWindowsAcl, dacl_from_sddl, make_console


def _write(path: Path, lines: list[str], *, newline: str = "\n", bom: bool = False) -> None:
    text = newline.join(lines) + newline
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class TestReadEnv:
    def test_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_env(tmp_path / ".env") == {}

    def test_keys_and_values_are_trimmed_and_comments_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["# a comment", "", "  KEY = value  ", "no-equals-sign", "#OFF=1", "URL=a=b"])
        assert read_env(path) == {"KEY": "value", "URL": "a=b"}

    def test_the_first_value_of_a_repeated_key_wins(self, tmp_path: Path) -> None:
        # config.py never overwrites a key it has already set, so a later line is dead.
        path = tmp_path / ".env"
        _write(path, ["KEY=first", "KEY=second", " KEY = third"])
        assert read_env(path) == {"KEY": "first"}

    def test_a_byte_order_mark_does_not_become_part_of_the_first_key(self, tmp_path: Path) -> None:
        # Notepad and Windows PowerShell 5.1 both add one.
        path = tmp_path / ".env"
        _write(path, ["ENTRABOT_TENANT_ID=tenant"], bom=True)
        assert read_env(path) == {"ENTRABOT_TENANT_ID": "tenant"}

    def test_crlf_and_non_ascii_values_survive(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["UPN=josé@contoso.com", "OTHER=x"], newline="\r\n")
        assert read_env(path) == {"UPN": "josé@contoso.com", "OTHER": "x"}

    def test_a_file_saved_in_the_ansi_code_page_is_still_read(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_bytes("KEY=café\nOTHER=x\n".encode("cp1252"))
        values = read_env(path)
        assert values["OTHER"] == "x" and values["KEY"].startswith("caf")


class TestUpdateEnv:
    def test_a_new_file_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        update_env(path, {"A": "1", "B": "2"})
        assert _lines(path) == ["A=1", "B=2"]

    def test_every_earlier_line_for_a_key_is_replaced_not_appended_to(self, tmp_path: Path) -> None:
        # A stale line left above the new one would win at runtime.
        path = tmp_path / ".env"
        _write(path, ["ENTRABOT_AGENT_ID=stale", "OTHER=kept", " ENTRABOT_AGENT_ID = staler "])
        update_env(path, {"ENTRABOT_AGENT_ID": "agent-app"})
        assert _lines(path) == ["OTHER=kept", "ENTRABOT_AGENT_ID=agent-app"]
        assert read_env(path)["ENTRABOT_AGENT_ID"] == "agent-app"

    def test_comments_blank_lines_and_other_keys_keep_their_order(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["# header", "", "KEEP=1", "# ENTRABOT_AGENT_ID=a note", "ALSO=2"])
        update_env(path, {"ENTRABOT_AGENT_ID": "x"})
        assert _lines(path) == [
            "# header",
            "",
            "KEEP=1",
            "# ENTRABOT_AGENT_ID=a note",
            "ALSO=2",
            "ENTRABOT_AGENT_ID=x",
        ]

    def test_removed_keys_are_dropped_however_they_are_padded(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["  GONE = 1", "KEEP=2", "GONE=3"])
        update_env(path, {}, remove=["GONE"])
        assert _lines(path) == ["KEEP=2"]

    def test_a_key_that_is_both_set_and_removed_is_set(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["KEY=old"])
        update_env(path, {"KEY": "new"}, remove=["KEY"])
        assert _lines(path) == ["KEY=new"]

    def test_an_empty_value_is_written_as_empty(self, tmp_path: Path) -> None:
        # A new sponsor must not inherit the old sponsor's tenant IDs.
        path = tmp_path / ".env"
        _write(path, ["ENTRABOT_HUMAN_USER_TENANT_IDS=old-tenant"])
        update_env(path, {"ENTRABOT_HUMAN_USER_TENANT_IDS": ""})
        assert _lines(path) == ["ENTRABOT_HUMAN_USER_TENANT_IDS="]

    def test_marked_comment_lines_are_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["# ADR-005: keep agent memory local (skip cloud sync)", "# mine", "A=1"])
        update_env(path, {"B": "2"}, drop_comment_prefixes=["# ADR-005:"])
        assert _lines(path) == ["# mine", "A=1", "B=2"]

    def test_the_file_is_utf8_without_a_bom_and_with_lf_endings(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["OLD=1", "UPN=josé@contoso.com"], newline="\r\n", bom=True)
        update_env(path, {"NEW": "2"})
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert raw.decode("utf-8").splitlines() == ["OLD=1", "UPN=josé@contoso.com", "NEW=2"]

    def test_a_failed_write_leaves_the_old_file_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / ".env"
        _write(path, ["KEEP=1"])

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(envfile.os, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            update_env(path, {"NEW": "2"})
        assert _lines(path) == ["KEEP=1"]
        assert [item.name for item in tmp_path.iterdir()] == [".env"]  # no temp file left

    def test_updating_twice_is_stable(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        update_env(path, {"A": "1"})
        update_env(path, {"A": "1"})
        assert _lines(path) == ["A=1"]


class TestLock:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_posix_locks_with_chmod_600(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        os.chmod(path, 0o644)
        lock_to_current_user(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_windows_replaces_the_whole_access_list_with_one_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not "remove inheritance and grant": a new file's SYSTEM and Administrators entries
        # are explicit, so there is no inheritance to remove and they would simply stay.
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        before = acl.read_dacl(path)  # the starting point, as measured
        assert "S-1-5-18" in [entry.sid for entry in before.entries]
        lock_to_current_user(path)
        assert acl.acls[str(path)] == f"D:P(A;;0x1301bf;;;{acl.sid})"
        # Modify, not read-only: the next setup run and certificate rotation update the file.
        assert private_sddl(acl.sid) == acl.acls[str(path)]
        assert dacl_lock_problem(acl.read_dacl(path), acl.sid) is None

    def test_windows_raises_when_the_permissions_cannot_be_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl(
            fail_set="SetNamedSecurityInfoW failed (Windows error 5: Access is denied.)"
        )
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        with pytest.raises(EnvLockError, match="Access is denied"):
            lock_to_current_user(path)

    def test_the_arguments_older_callers_pass_are_still_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real operating system decides how a file is locked; these are kept so callers
        # written against the first version keep working.
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        console, _ = make_console()
        lock_to_current_user(path, console=console, platform="win32", env={})
        assert lock_problem(path, console=console, platform="win32", env={}) is None

    def test_nothing_is_run_as_a_separate_program(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        spawn = FakeSpawn()
        console, _ = make_console(spawn=spawn)
        path = tmp_path / ".env"
        update_env(path, {"A": "1"}, guard=EnvGuard(console=console))
        assert lock_problem(path, console=console) is None
        assert spawn.calls == []


SID = "S-1-5-21-1-2-3-1001"
SYSTEM, ADMINISTRATORS, USERS, EVERYONE = "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545", "S-1-1-0"
MODIFY, FULL, READ = 0x1301BF, 0x1F01FF, 0x120089
INHERITED_ACE = 0x10


def _allow(sid: str, mask: int = MODIFY, flags: int = 0) -> Ace:
    return Ace(type=ACCESS_ALLOWED_ACE_TYPE, flags=flags, mask=mask, sid=sid)


def _deny(sid: str, mask: int = FULL) -> Ace:
    return Ace(type=1, flags=0, mask=mask, sid=sid)


def _dacl(*entries: Ace, protected: bool = True) -> Dacl:
    return Dacl(present=True, protected=protected, entries=entries)


LOCKED = _dacl(_allow(SID))
# What a new file in a user's project folder looks like on Windows 11 (measured): SYSTEM,
# Administrators and the user, all EXPLICIT entries.
EXPLICIT = _dacl(
    _allow(SYSTEM, FULL), _allow(ADMINISTRATORS, FULL), _allow(SID, FULL), protected=False
)
# The same accounts as inherited entries, which is what the first fix assumed.
INHERITED = _dacl(
    *(_allow(sid, FULL, INHERITED_ACE) for sid in (SYSTEM, ADMINISTRATORS, SID)), protected=False
)
# What "icacls /inheritance:r /grant:r <user>:M" leaves behind on such a file (measured).
HALF_LOCKED = _dacl(_allow(SYSTEM, FULL), _allow(ADMINISTRATORS, FULL), _allow(SID))


class TestEveryWriteIsLocked:
    """Protection is a property of the write, not a step someone has to remember afterwards.

    Two defects, both measured on Windows 11, shaped this:

    * Setup locked ``.env``, then a later step rewrote it through a temp file and
      ``os.replace``, which swapped in a brand-new, unlocked file, while setup had already
      printed "only your account can read it". So the lock goes on the temp file BEFORE it
      replaces ``.env``.
    * The lock itself (``icacls /inheritance:r /grant:r``) left SYSTEM and Administrators with
      full control, because on a new file those entries are explicit, not inherited. So the
      lock REPLACES the access list with exactly one entry.
    """

    def test_the_temp_file_is_locked_before_it_replaces_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        _write(path, ["OLD=1"])
        seen: list[tuple[str, str, int]] = []
        real_set = acl.set_dacl

        def spy(target: Path, sddl: str) -> None:
            seen.append(
                (str(target), path.read_text(encoding="utf-8"), Path(target).stat().st_size)
            )
            real_set(target, sddl)

        monkeypatch.setattr(acl, "set_dacl", spy)
        update_env(path, {"NEW": "2"})

        ((locked_path, env_at_that_moment, temp_size_when_locked),) = seen
        assert locked_path != str(path) and Path(locked_path).parent == tmp_path
        assert env_at_that_moment == "OLD=1\n"  # .env itself was still the old file
        # The temp file is locked while it is still EMPTY: the settings are never, even for
        # a moment, in a file that carries the folder's permissions.
        assert temp_size_when_locked == 0
        assert _lines(path) == ["OLD=1", "NEW=2"]  # ...and written afterwards
        assert acl.events == [f"lock {Path(locked_path).name}", "replace .env"]
        assert acl.acls == {str(path): private_sddl(acl.sid)}  # the lock came along
        assert lock_problem(path) is None

    def test_nothing_is_written_to_a_temp_file_that_could_not_be_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl(fail_set="Access is denied.")
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        _write(path, ["KEEP=1"])
        sizes: list[int] = []
        real_unlink = os.unlink

        def unlink(target: object, **kwargs: object) -> None:
            sizes.append(Path(str(target)).stat().st_size)
            real_unlink(target, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(envfile.os, "unlink", unlink)
        with pytest.raises(EnvLockError):
            update_env(path, {"SECRET_LOOKING": "value"})
        assert sizes == [0]  # the unlocked temp file never held the content

    def test_every_single_write_ends_with_exactly_one_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        for change in ({"A": "1"}, {"B": "2"}, {"A": "3"}):
            update_env(path, change)
            assert acl.acls[str(path)].count("(") == 1
            assert lock_problem(path) is None
        update_env(path, {}, remove=["A"])
        assert lock_problem(path) is None
        assert len([event for event in acl.events if event.startswith("lock")]) == 4
        assert _lines(path) == ["B=2"]

    def test_a_write_that_skipped_the_lock_would_be_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard on this test itself: with the lock call taken out, the file that lands is
        # an ordinary new file, and reading its permissions back says so.
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        monkeypatch.setattr(envfile, "lock_to_current_user", lambda *args, **kwargs: None)
        path = tmp_path / ".env"
        update_env(path, {"A": "1"})
        problem = lock_problem(path)
        assert problem is not None and "SYSTEM" in problem and "Administrators" in problem

    def test_a_lock_that_fails_stops_the_write_and_leaves_the_old_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default is to fail closed: a caller that does not say otherwise never ends up
        # with an unprotected file it believes is protected.
        FakeWindowsAcl(fail_set="Access is denied.").install(monkeypatch)
        path = tmp_path / ".env"
        _write(path, ["KEEP=1"])
        with pytest.raises(EnvLockError, match="Access is denied"):
            update_env(path, {"NEW": "2"})
        assert _lines(path) == ["KEEP=1"]
        assert [item.name for item in tmp_path.iterdir()] == [".env"]

    def test_a_caller_can_choose_to_write_anyway_and_is_told_what_went_wrong(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeWindowsAcl(fail_set="Access is denied.").install(monkeypatch)
        path = tmp_path / ".env"
        guard = EnvGuard(tolerate_failure=True)
        update_env(path, {"NEW": "2"}, guard=guard)
        assert _lines(path) == ["NEW=2"]
        assert len(guard.problems) == 1
        assert "Access is denied" in guard.problems[0] and ".env " in guard.problems[0]
        assert ".tmp" not in guard.problems[0]  # named as the file the person knows

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_posix_result_is_private_even_when_the_old_file_was_not(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        _write(path, ["OLD=1"])
        os.chmod(path, 0o644)
        update_env(path, {"NEW": "2"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert lock_problem(path) is None


@pytest.mark.skipif(sys.platform != "win32", reason="sets and reads real Windows permissions")
class TestRealWindowsPermissions:
    """No fakes: the real access list of a real file, set in-process and read back.

    Everything is checked through the product's own read-back (:func:`envfile.read_dacl`),
    which hands back every account as a numeric SID. Nothing here matches SDDL text: Windows
    prints some accounts under two-letter names (the built-in Administrator, which a CI
    runner may well be, as ``LA``), so text would depend on which account runs the tests.
    """

    def _with_explicit_extra_entries(self, path: Path, *, protected: bool = False) -> str:
        """Give *path* explicit entries for SYSTEM and Administrators as well as the user:
        the state a new file in a project folder is in (measured), made deliberately so the
        test does not depend on what this machine's temp folder happens to hand out.

        *protected* also switches inheritance off, which is exactly what the old
        ``icacls /inheritance:r /grant:r`` lock left behind.
        """
        sid = envfile.current_user_sid()
        path.write_text("A=1\n", encoding="utf-8")
        flags = "P" if protected else ""
        envfile._winacl.set_dacl(
            path, f"D:{flags}(A;;FA;;;{SYSTEM})(A;;FA;;;{ADMINISTRATORS})(A;;FA;;;{sid})"
        )
        before = dacl_lock_problem(envfile.read_dacl(path), sid)
        # The starting point is NOT locked: the other two accounts really are in the list.
        assert before is not None and "SYSTEM" in before and "Administrators" in before, before
        return sid

    def _assert_locked_to(self, path: Path, sid: str) -> None:
        dacl = envfile.read_dacl(path)
        assert dacl.present and dacl.protected, dacl  # a real list, inheritance off...
        (entry,) = dacl.entries  # ...exactly one entry...
        assert entry.type == ACCESS_ALLOWED_ACE_TYPE, dacl
        assert (entry.sid or "").upper() == sid.upper(), dacl  # ...for this very account...
        assert entry.mask & MODIFY == MODIFY, dacl  # ...which can still change the file
        assert dacl_lock_problem(dacl, sid) is None
        assert lock_problem(path) is None

    def test_the_current_user_has_a_real_sid(self) -> None:
        assert envfile.current_user_sid().startswith("S-1-5-")

    def test_accounts_come_back_as_numeric_sids_even_when_sddl_has_a_name_for_them(
        self, tmp_path: Path
    ) -> None:
        # Written with the SDDL short names, read back as S-1-...: the comparison never
        # depends on how Windows would print an account.
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        envfile._winacl.set_dacl(path, "D:P(A;;FA;;;SY)(A;;FR;;;BA)")
        dacl = envfile.read_dacl(path)
        assert [entry.sid for entry in dacl.entries] == [SYSTEM, ADMINISTRATORS], dacl
        assert dacl.protected

    @pytest.mark.parametrize("protected", [False, True], ids=["as-created", "half-locked"])
    def test_locking_a_file_with_explicit_extra_entries_leaves_exactly_one(
        self, tmp_path: Path, protected: bool
    ) -> None:
        path = tmp_path / ".env"
        sid = self._with_explicit_extra_entries(path, protected=protected)
        lock_to_current_user(path)
        self._assert_locked_to(path, sid)

    def test_the_user_can_still_read_and_rewrite_the_locked_file(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        self._with_explicit_extra_entries(path)
        lock_to_current_user(path)
        assert path.read_text(encoding="utf-8") == "A=1\n"
        update_env(path, {"B": "2"})
        update_env(path, {}, remove=["A"])
        assert _lines(path) == ["B=2"]
        assert lock_problem(path) is None

    def test_a_rewrite_over_an_unlocked_env_ends_locked(self, tmp_path: Path) -> None:
        # The whole path setup takes: an existing .env with extra explicit entries, rewritten
        # several times through a temp file and os.replace.
        path = tmp_path / ".env"
        sid = self._with_explicit_extra_entries(path)
        for change in ({"B": "2"}, {"C": "3"}):
            update_env(path, change)
            self._assert_locked_to(path, sid)

    def test_an_ordinary_new_file_is_reported_as_not_locked(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.env"
        path.write_text("A=1\n", encoding="utf-8")
        assert lock_problem(path) is not None


class TestTheBuiltInAdministrator:
    """SDDL text does not always spell an account as S-1-...: for well-known accounts Windows
    prints a two-letter name, and the built-in Administrator (the account whose SID ends in
    -500) appears as ``LA``. Comparing text said the user had no entry and that some other
    account, "LA", had access, on a file that was in fact locked to that very user (seen on a
    CI runner). The check reads the binary list instead, where every account is a SID."""

    ADMINISTRATOR = "S-1-5-21-1-2-3-500"

    def test_the_lock_check_passes_for_the_built_in_administrator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl(sid=self.ADMINISTRATOR)
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        update_env(path, {"A": "1"})
        acl.acls[str(path)] = "D:P(A;;0x1301bf;;;LA)"  # however it was written...
        assert acl.read_dacl(path).entries[0].sid == self.ADMINISTRATOR  # ...read back as a SID
        assert lock_problem(path) is None

    def test_the_lock_check_still_fails_when_someone_else_is_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl(sid=self.ADMINISTRATOR)
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        acl.acls[str(path)] = "D:P(A;;FA;;;SY)(A;;0x1301bf;;;LA)"
        assert lock_problem(path) == "these also have access: SYSTEM"


class TestDaclLockProblem:
    def test_a_locked_file_has_no_problem(self) -> None:
        assert dacl_lock_problem(LOCKED, SID) is None
        assert dacl_lock_problem(_dacl(_allow(SID, FULL)), SID.lower()) is None
        assert dacl_lock_problem(dacl_from_sddl(private_sddl(SID)), SID) is None

    def test_generic_all_counts_as_being_able_to_change_the_file(self) -> None:
        assert dacl_lock_problem(_dacl(_allow(SID, 0x10000000)), SID) is None

    def test_explicit_entries_for_other_accounts_are_named(self) -> None:
        problem = dacl_lock_problem(EXPLICIT, SID)
        assert problem is not None
        assert "SYSTEM" in problem and "Administrators" in problem

    def test_the_half_lock_the_old_tool_produced_is_a_problem(self) -> None:
        # Inheritance off and the user at "modify", yet SYSTEM and Administrators still in.
        problem = dacl_lock_problem(HALF_LOCKED, SID)
        assert problem is not None and "SYSTEM" in problem and "Administrators" in problem
        assert "inherits" not in problem

    def test_inherited_permissions_are_named_precisely(self) -> None:
        problem = dacl_lock_problem(INHERITED, SID)
        assert problem is not None
        assert "SYSTEM" in problem and "Administrators" in problem
        assert "inherits" in problem

    def test_a_list_that_is_not_protected_is_a_problem_even_with_only_the_user(self) -> None:
        problem = dacl_lock_problem(_dacl(_allow(SID), protected=False), SID)
        assert problem == "it inherits permissions from its folder"

    def test_another_account_with_access_is_a_problem_even_without_inheritance(self) -> None:
        other = "S-1-5-21-9-9-9-500"
        problem = dacl_lock_problem(_dacl(_allow(SID), _allow(other, READ)), SID)
        assert problem is not None and other in problem

    def test_no_entry_for_the_user_is_a_problem(self) -> None:
        problem = dacl_lock_problem(_dacl(_allow(SYSTEM, FULL)), SID)
        assert problem is not None and "your account" in problem

    def test_an_entry_that_only_lets_the_user_read_is_a_problem(self) -> None:
        # Setup and certificate rotation rewrite the file: read-only would break them.
        problem = dacl_lock_problem(_dacl(_allow(SID, READ)), SID)
        assert problem is not None and "change" in problem

    def test_a_deny_entry_for_someone_else_is_not_access(self) -> None:
        assert dacl_lock_problem(_dacl(_deny(EVERYONE), _allow(SID)), SID) is None

    @pytest.mark.parametrize("kind", [5, 9, 0x0B, 0x11, 0xFF])
    def test_an_entry_of_a_kind_setup_does_not_read_is_a_problem(self, kind: int) -> None:
        # Object, callback and every other kind: their layout is not ACCESS_ALLOWED_ACE's,
        # so who they let in is not known. Unknown is never a pass.
        unknown = Ace(type=kind, flags=0, mask=FULL, sid=None)
        problem = dacl_lock_problem(_dacl(_allow(SID), unknown), SID)
        assert problem is not None and "cannot check" in problem

    def test_an_allow_entry_whose_account_was_not_read_is_a_problem(self) -> None:
        problem = dacl_lock_problem(_dacl(_allow(SID), Ace(type=0, mask=FULL, sid=None)), SID)
        assert problem is not None and "cannot check" in problem

    def test_no_access_list_at_all_means_everyone_is_in(self) -> None:
        problem = dacl_lock_problem(Dacl(present=False, protected=True), SID)
        assert problem is not None and "Everyone" in problem

    def test_an_empty_protected_list_is_not_a_lock_the_user_can_use(self) -> None:
        problem = dacl_lock_problem(_dacl(), SID)
        assert problem is not None and "your account" in problem

    def test_everyone_and_users_are_named_in_words(self) -> None:
        problem = dacl_lock_problem(_dacl(_allow(SID), _allow(USERS, READ), _allow(EVERYONE)), SID)
        assert problem == "these also have access: Users, Everyone"


class TestLockProblem:
    def test_windows_reads_the_real_access_list_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        acl.acls[str(path)] = LOCKED
        assert lock_problem(path) is None
        assert acl.events == ["read .env"]

    @pytest.mark.parametrize("dacl", [EXPLICIT, HALF_LOCKED, INHERITED])
    def test_windows_reports_what_is_wrong(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dacl: Dacl
    ) -> None:
        acl = FakeWindowsAcl()
        acl.install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        acl.acls[str(path)] = dacl
        problem = lock_problem(path)
        assert problem is not None and "Administrators" in problem

    def test_an_access_list_that_cannot_be_read_is_a_problem_not_a_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeWindowsAcl(fail_read="GetNamedSecurityInfoW failed").install(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        problem = lock_problem(path)
        assert problem is not None and "could not be read" in problem

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_posix_checks_the_mode(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        os.chmod(path, 0o644)
        assert lock_problem(path) is not None
        os.chmod(path, 0o600)
        assert lock_problem(path) is None
