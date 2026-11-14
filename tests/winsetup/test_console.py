"""The wizard's terminal: prompts, defaults, child processes, and what never gets printed."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from entrabot.winsetup.console import Cancelled, Choice, Console, RunRequest, redact
from tests.winsetup.fakes import FakeSpawn, Reply, make_console

CHOICES = [Choice("local", "On this computer"), Choice("cloud", "In Azure", hint="needs Azure")]


class TestPrompts:
    def test_enter_takes_the_default(self) -> None:
        console, _ = make_console(typed="\n\n\n")
        assert console.ask_text("Name", default="alex") == "alex"
        assert console.ask_choice("Memory", CHOICES, default="local") == "local"
        assert console.confirm("Go?", default=True) is True

    def test_a_choice_is_picked_by_number_or_by_name(self) -> None:
        console, _ = make_console(typed="2\nlocal\n")
        assert console.ask_choice("Memory", CHOICES, default="local") == "cloud"
        assert console.ask_choice("Memory", CHOICES, default="cloud") == "local"

    def test_an_unknown_choice_is_asked_again(self) -> None:
        console, out = make_console(typed="9\nnope\n1\n")
        assert console.ask_choice("Memory", CHOICES, default="cloud") == "local"
        assert out.getvalue().count("Memory") >= 3

    def test_an_invalid_text_answer_is_explained_and_asked_again(self) -> None:
        console, out = make_console(typed="Bad Name\ngood\n")

        def validate(value: str) -> str | None:
            return None if value.islower() else "Use lowercase letters."

        assert console.ask_text("Name", validate=validate) == "good"
        assert "Use lowercase letters." in out.getvalue()

    def test_text_without_a_default_cannot_be_skipped(self) -> None:
        console, _ = make_console(typed="\n\nvalue\n")
        assert console.ask_text("App ID") == "value"

    @pytest.mark.parametrize(
        ("typed", "expected"), [("y\n", True), ("N\n", False), ("yes\n", True)]
    )
    def test_confirm_reads_yes_and_no(self, typed: str, expected: bool) -> None:
        console, _ = make_console(typed=typed)
        assert console.confirm("Go?", default=not expected) is expected

    def test_end_of_input_cancels(self) -> None:
        console, _ = make_console(typed="")
        with pytest.raises(Cancelled):
            console.ask_text("Name", default="x")


class TestNonInteractive:
    def test_defaults_are_taken_without_reading_anything(self) -> None:
        console, out = make_console(assume_yes=True)
        assert console.ask_text("Name", default="alex") == "alex"
        assert console.ask_choice("Memory", CHOICES, default="cloud") == "cloud"
        assert console.confirm("Go?", default=False) is False
        assert console.missing == []
        assert "Name" not in out.getvalue()  # nothing is prompted

    def test_a_question_with_no_default_is_recorded_as_missing(self) -> None:
        console, _ = make_console(assume_yes=True)
        assert console.ask_text("Blueprint App ID", key="blueprint", flag="--use-blueprint") == ""
        assert [(item.key, item.flag) for item in console.missing] == [
            ("blueprint", "--use-blueprint")
        ]

    def test_answers_override_defaults(self) -> None:
        console, _ = make_console(answers={"memory": "cloud", "go": "no", "name": "sati"})
        assert console.ask_choice("Memory", CHOICES, default="local", key="memory") == "cloud"
        assert console.confirm("Go?", default=True, key="go") is False
        assert console.ask_text("Name", default="x", key="name") == "sati"

    def test_yes_is_not_interactive_even_on_a_terminal(self) -> None:
        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        console = Console(stdin=Tty("typed\n"), stdout=io.StringIO(), assume_yes=True)
        assert console.interactive is False
        console = Console(stdin=Tty("typed\n"), stdout=io.StringIO())
        assert console.interactive is True


class TestOutput:
    def test_plain_terminals_get_ascii_only(self) -> None:
        console, out = make_console()
        console.banner("Entrabot setup", ["One line."])
        console.step(1, 3, "Checking")
        console.ok("fine")
        console.warn("careful")
        console.fail("broken", fix="Do this.")
        console.info("note")
        console.detail("small print")
        text = out.getvalue()
        assert text.isascii()
        assert "[1/3] Checking" in text
        assert "[ok] fine" in text and "[!!] careful" in text and "[xx] broken" in text
        assert "Do this." in text

    def test_a_section_is_a_heading_without_a_step_number(self, tmp_path: Path) -> None:
        console, out = make_console(log_file=tmp_path / "setup.log")
        console.info("before")
        console.section("Licences for Microsoft 365")
        console.close()
        assert out.getvalue() == "  before\n\nLicences for Microsoft 365\n"
        assert "Licences for Microsoft 365" in (tmp_path / "setup.log").read_text("utf-8")

    def test_a_terminal_that_cannot_encode_a_character_never_raises(self) -> None:
        class Cp1252(io.TextIOWrapper):
            pass

        raw = io.BytesIO()
        stream = Cp1252(raw, encoding="cp1252", errors="strict")
        console = Console(stdin=io.StringIO(), stdout=stream, color=False, unicode=True)
        console.ok("café ✓ \U0001f3af")
        stream.flush()
        assert b"caf\xe9" in raw.getvalue()

    def test_thumbprints_tokens_and_private_keys_are_never_shown_or_logged(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "setup.log"
        spawn = FakeSpawn()
        thumbprint = "0123456789ABCDEF0123456789ABCDEF0123CAFE"
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZ2VudCJ9.c2lnbmF0dXJlLWJ5dGVz"
        spawn.default = Reply(
            stdout=f"thumb: {thumbprint}\n-----BEGIN PRIVATE KEY-----\nMIIsecret\n"
            "-----END PRIVATE KEY-----\nafter",
            stderr=f"Authorization: Bearer {token}",
        )
        console, out = make_console(spawn=spawn, log_file=tmp_path / "setup.log")
        console.run(["helper"])
        console.info(f"cert {thumbprint}")
        console.close()
        shown = out.getvalue() + log.read_text(encoding="utf-8")
        assert thumbprint not in shown and "...CAFE" in shown
        assert token not in shown and "MIIsecret" not in shown
        assert "after" in shown

    def test_sha256_thumbprints_in_hex_are_masked_too(self) -> None:
        digest = "9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08"
        text = redact(f"key {digest.lower()} not found")
        assert digest.lower() not in text and "...0A08" in text

    def test_a_labelled_base64url_thumbprint_is_masked(self) -> None:
        x5t = "n4bQgYhMfWWaL-qgxVrQFaO_TxsrC4Is0V1sFbDwCgg"  # 43 characters, as in x5t#S256
        for label in (
            f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={x5t}",
            f'"x5t#S256": "{x5t}"',
            f"x5t_s256={x5t}",
            f"Thumbprint of key used by client: '{x5t}'",
        ):
            text = redact(label)
            assert x5t not in text, label
            assert "...wCgg" in text, label

    def test_what_entra_echoes_back_in_an_error_is_masked(self) -> None:
        # The shape of AADSTS700027: Entra quotes the thumbprint of the key the client used.
        hex64 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        message = (
            "AADSTS700027: The certificate with identifier used to sign the client assertion "
            f"is not registered on application. [Reason - The key was not found., Thumbprint "
            f"of key used by client: '{hex64}', Please visit the Azure Portal]"
        )
        text = redact(message)
        assert hex64 not in text and "AADSTS700027" in text and "Azure Portal" in text

    def test_ordinary_long_words_are_left_alone(self) -> None:
        # Only a value that is labelled as a thumbprint is treated as one.
        url = "https://login.microsoftonline.com/common/discovery/v2.0/keys?appid=abcdefghijk"
        assert redact(url) == url
        blob = "n4bQgYhMfWWaL-qgxVrQFaO_TxsrC4Is0V1sFbDwCgg"
        assert redact(f"container {blob}") == f"container {blob}"

    def test_redact_leaves_guids_alone(self) -> None:
        guid = "0f3c1c7e-1111-2222-3333-444455556666"
        assert redact(f"app {guid}") == f"app {guid}"

    def test_a_padded_standard_base64_thumbprint_is_masked(self) -> None:
        # The same SHA-256 digest in ordinary base64: "+" and "/" and one "=" of padding.
        b64 = "HkiEpkW1e7ofnNj84USRyBK/nLVU4BZ/jjaNaae9ubw="
        text = redact(f"Certificate thumbprint (SHA-256, base64): {b64}")
        assert b64 not in text and b64[:-1] not in text and "...9ubw" in text

    @pytest.mark.parametrize(
        "value",
        [
            "/home/pat/projects/entrabot/certs/blueprint",  # a path
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT_PREVIOUS",  # a setting's name
            "entrabot-agent-sati-blueprint-certificate-g",  # a name made of words
            "0f3c1c7e-1111-2222-3333-444455556666-abcdef",  # an ID with a suffix
        ],
    )
    def test_a_43_character_word_after_a_label_is_not_mistaken_for_one(self, value: str) -> None:
        # Exactly the length of a base64 SHA-256 thumbprint, right after the word, and still
        # not one: a digest is random, mixes upper and lower case, and has no "/" in base64url.
        assert len(value) == 43
        for line in (f"Thumbprint saved to {value} today", f"x5t#S256 for {value}"):
            assert redact(line) == line

    def test_a_lookalike_first_does_not_shield_the_real_thumbprint_after_it(self) -> None:
        x5t = "n4bQgYhMfWWaL-qgxVrQFaO_TxsrC4Is0V1sFbDwCgg"
        line = f"thumbprint ENTRABOT_BLUEPRINT_CERT_THUMBPRINT_PREVIOUS = {x5t}"
        text = redact(line)
        assert x5t not in text and "...wCgg" in text
        assert "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT_PREVIOUS" in text

    def test_a_sha256_thumbprint_in_hex_is_masked_with_or_without_a_label(self) -> None:
        hex64 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        for line in (f"thumbprint={hex64}", f"x5t#S256: {hex64.upper()}", hex64):
            assert hex64 not in redact(line).lower(), line

    def test_paths_and_ordinary_text_pass_through_unchanged(self) -> None:
        for line in (
            r"Saved to C:\Users\pat\AppData\Local\entrabot\setup.log",
            "/Users/pat/Library/Application Support/entrabot/state.json",
            "The thumbprint was registered on the Blueprint.",
            "Certificate thumbprint ending 01D1",
        ):
            assert redact(line) == line

    def test_everything_shown_is_also_in_the_log(self, tmp_path: Path) -> None:
        log = tmp_path / "logs" / "setup.log"
        console, _ = make_console(log_file=log)
        console.step(2, 5, "Signing in")
        console.warn("careful")
        console.close()
        text = log.read_text(encoding="utf-8")
        assert "Signing in" in text and "careful" in text


class TestRun:
    def test_output_is_streamed_and_returned(self) -> None:
        spawn = FakeSpawn()
        spawn.default = Reply(returncode=3, stdout="KEY=value\n", stderr="progress\n")
        console, out = make_console(spawn=spawn)
        result = console.run(["tool", "--flag"], env={"A": "b"}, cwd=Path("/tmp"))
        assert result.returncode == 3
        assert result.stdout == "KEY=value\n" and result.stderr == "progress\n"
        assert "KEY=value" in out.getvalue() and "progress" in out.getvalue()
        request = spawn.calls[0]
        assert request.argv == ["tool", "--flag"] and request.env == {"A": "b"}

    def test_captured_stdout_is_data_and_is_not_displayed(self) -> None:
        spawn = FakeSpawn()
        spawn.default = Reply(stdout="BLOB_ENDPOINT=https://x\n", stderr="creating...\n")
        console, out = make_console(spawn=spawn)
        result = console.run(["tool"], capture=True)
        assert result.stdout == "BLOB_ENDPOINT=https://x\n"
        assert "BLOB_ENDPOINT" not in out.getvalue()
        assert "creating..." in out.getvalue()  # a child's stderr is never hidden

    def test_quiet_keeps_the_screen_clean_but_logs_stderr(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.default = Reply(returncode=1, stderr="Please run az login\n")
        log = tmp_path / "setup.log"
        console, out = make_console(spawn=spawn, log_file=log)
        result = console.run(["az", "account", "show"], capture=True, quiet=True)
        console.close()
        assert "Please run az login" not in out.getvalue()
        assert result.stderr == "Please run az login\n"
        assert "Please run az login" in log.read_text(encoding="utf-8")

    def test_sensitive_output_is_returned_but_never_logged(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.default = Reply(stdout='{"accessToken": "opaque-secret-value"}\n')
        log = tmp_path / "setup.log"
        console, out = make_console(spawn=spawn, log_file=log)
        result = console.run(["az", "account", "get-access-token"], capture=True, sensitive=True)
        console.close()
        assert "opaque-secret-value" in result.stdout
        assert "opaque-secret-value" not in out.getvalue()
        assert "opaque-secret-value" not in log.read_text(encoding="utf-8")


class TestRealChildProcess:
    """The default launcher, against this Python: no fake in the way."""

    def _console(self) -> tuple[Console, io.StringIO]:
        out = io.StringIO()
        return Console(stdin=io.StringIO(), stdout=out, color=False, unicode=False), out

    def test_stdout_stderr_and_exit_code_come_back(self) -> None:
        console, out = self._console()
        code = "import sys; print('to-out'); print('to-err', file=sys.stderr); sys.exit(4)"
        result = console.run([sys.executable, "-c", code], capture=True)
        assert result.returncode == 4
        assert result.stdout.strip() == "to-out" and result.stderr.strip() == "to-err"
        assert "to-err" in out.getvalue() and "to-out" not in out.getvalue()

    def test_undecodable_bytes_never_raise(self) -> None:
        console, _ = self._console()
        code = "import sys; sys.stdout.buffer.write(b'caf\\xe9 \\xff\\n')"
        result = console.run([sys.executable, "-c", code], capture=True)
        assert result.returncode == 0 and result.stdout.startswith("caf")

    def test_input_text_reaches_the_child(self) -> None:
        console, _ = self._console()
        code = "import sys; print(sys.stdin.read().upper())"
        result = console.run([sys.executable, "-c", code], capture=True, input_text="secret")
        assert result.stdout.strip() == "SECRET"

    def test_a_missing_program_is_a_failed_run_not_a_traceback(self) -> None:
        console, _ = self._console()
        result = console.run(["definitely-not-a-real-program-xyz"], capture=True)
        assert result.returncode != 0 and "definitely-not-a-real-program-xyz" in result.stderr

    def test_a_silent_child_gets_a_heartbeat(self) -> None:
        out = io.StringIO()
        console = Console(
            stdin=io.StringIO(), stdout=out, color=False, unicode=False, heartbeat_seconds=0.2
        )
        code = "import time; time.sleep(0.9); print('done')"
        console.run([sys.executable, "-c", code], heartbeat="Still creating your agent")
        assert "Still creating your agent" in out.getvalue()


def test_run_request_is_a_plain_record() -> None:
    request = RunRequest(argv=["a"], env=None, cwd=None)
    assert request.interactive is False and request.input_text is None


class TestSummaryOutput:
    """Helper scripts print for engineers. People get a summary; the log gets everything."""

    def _run(
        self, tmp_path: Path, *, verbose: bool, returncode: int = 0
    ) -> tuple[str, str, object]:
        spawn = FakeSpawn()
        spawn.default = Reply(
            returncode=returncode,
            stdout="".join(f"permission {n}\n" for n in range(1, 41)),
            stderr="ERROR: Insufficient privileges\n",
        )
        out = io.StringIO()
        log = tmp_path / "setup.log"
        console = Console(
            stdin=io.StringIO(),
            stdout=out,
            spawn=spawn,
            color=False,
            unicode=False,
            log_file=log,
            verbose=verbose,
        )
        result = console.run(["helper"], summary=True)
        console.close()
        return out.getvalue(), log.read_text(encoding="utf-8"), (console, result)

    def test_by_default_nothing_of_the_stream_is_shown_and_all_of_it_is_logged(
        self, tmp_path: Path
    ) -> None:
        shown, logged, _ = self._run(tmp_path, verbose=False)
        assert "permission" not in shown and "Insufficient" not in shown
        assert "permission 1" in logged and "permission 40" in logged
        assert "Insufficient privileges" in logged

    def test_verbose_shows_the_full_stream(self, tmp_path: Path) -> None:
        shown, _, _ = self._run(tmp_path, verbose=True)
        assert "permission 1" in shown and "permission 40" in shown

    def test_the_result_keeps_the_lines_in_the_order_they_arrived(self, tmp_path: Path) -> None:
        _, _, (_console, result) = self._run(tmp_path, verbose=False)
        lines = result.output.splitlines()  # type: ignore[attr-defined]
        assert lines[0] == "permission 1" and lines[-1] == "ERROR: Insufficient privileges"
        assert len(lines) == 41

    def test_on_failure_the_last_lines_and_the_log_path_can_be_shown(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.default = Reply(
            returncode=1,
            stdout="".join(f"line {n}\n" for n in range(1, 61)),
            stderr="ERROR: boom\n",
        )
        out = io.StringIO()
        log = tmp_path / "setup.log"
        console = Console(
            stdin=io.StringIO(), stdout=out, spawn=spawn, color=False, unicode=False, log_file=log
        )
        result = console.run(["helper"], summary=True)
        console.show_tail(result)
        shown = out.getvalue()
        assert "ERROR: boom" in shown and "line 60" in shown
        assert "line 30" not in shown  # the last 25 lines, not all 61
        assert str(log) in shown

    def test_a_tail_is_not_repeated_when_the_stream_was_already_on_screen(
        self, tmp_path: Path
    ) -> None:
        spawn = FakeSpawn()
        spawn.default = Reply(returncode=1, stderr="ERROR: boom\n")
        out = io.StringIO()
        console = Console(
            stdin=io.StringIO(), stdout=out, spawn=spawn, color=False, unicode=False, verbose=True
        )
        console.show_tail(console.run(["helper"], summary=True))
        assert out.getvalue().count("ERROR: boom") == 1

    def test_secrets_in_a_tail_are_still_hidden(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        thumbprint = "0123456789ABCDEF0123456789ABCDEF0123CAFE"
        spawn.default = Reply(returncode=1, stdout=f"Uploading cert (SHA-1 thumb: {thumbprint})\n")
        console, out = make_console(spawn=spawn)
        console.show_tail(console.run(["helper"], summary=True))
        assert thumbprint not in out.getvalue() and "...CAFE" in out.getvalue()


class TestResultMarkers:
    """A helper says how it ended with a line of its own, ``ENTRABOT_RESULT=<what>``. That line
    is for the wizard, which reads it from the result; people never need to see it. The setup
    log keeps it, as it keeps everything a helper prints."""

    MARKER = "ENTRABOT_RESULT=provisioner-could-not-check"

    def _console(self, tmp_path: Path, reply: Reply) -> tuple[Console, io.StringIO, Path]:
        spawn = FakeSpawn()
        spawn.default = reply
        out = io.StringIO()
        log = tmp_path / "setup.log"
        console = Console(
            stdin=io.StringIO(), stdout=out, spawn=spawn, color=False, unicode=False, log_file=log
        )
        return console, out, log

    def test_the_marker_is_the_one_the_helpers_print(self) -> None:
        from entrabot.winsetup import console as console_module
        from entrabot.winsetup.helpers import RESULT_MARKER

        assert console_module.RESULT_LINE == RESULT_MARKER

    def test_a_tail_leaves_marker_lines_out_and_the_log_keeps_them(self, tmp_path: Path) -> None:
        console, out, log = self._console(
            tmp_path,
            Reply(
                returncode=1,
                stdout=f"  Could not look up the app\n{self.MARKER}\nERROR: Could not look\n",
            ),
        )
        result = console.run(["helper"], summary=True)
        console.show_tail(result)
        console.close()
        shown = out.getvalue()
        assert "Could not look up the app" in shown and "ERROR: Could not look" in shown
        assert "ENTRABOT_RESULT" not in shown
        assert self.MARKER in log.read_text(encoding="utf-8")
        assert self.MARKER in result.stdout  # what the wizard reads it from

    def test_leaving_markers_out_costs_the_tail_no_real_line(self, tmp_path: Path) -> None:
        # 30 real lines with markers among them and after them: the tail is still the last
        # 25 real lines, all of them.
        lines = []
        for n in range(1, 31):
            lines.append(f"line {n:02d}")
            if n % 5 == 0:
                lines.append(f"ENTRABOT_RESULT=step-{n}")
        console, out, _ = self._console(
            tmp_path, Reply(returncode=1, stdout="\n".join(lines) + f"\n{self.MARKER}\n")
        )
        console.show_tail(console.run(["helper"], summary=True))
        shown = out.getvalue()
        assert all(f"line {n:02d}" in shown for n in range(6, 31))
        assert "line 05" not in shown
        assert "ENTRABOT_RESULT" not in shown

    def test_markers_on_stderr_are_left_out_too(self, tmp_path: Path) -> None:
        console, out, _ = self._console(
            tmp_path, Reply(returncode=1, stderr=f"{self.MARKER}\nERROR: boom\n")
        )
        console.show_tail(console.run(["helper"], summary=True))
        assert "ERROR: boom" in out.getvalue() and "ENTRABOT_RESULT" not in out.getvalue()

    def test_only_a_line_that_starts_with_the_marker_is_one(self, tmp_path: Path) -> None:
        console, out, _ = self._console(
            tmp_path, Reply(returncode=1, stdout=f"  echo {self.MARKER}\n")
        )
        console.show_tail(console.run(["helper"], summary=True))
        assert f"echo {self.MARKER}" in out.getvalue()

    @pytest.mark.parametrize("verbose", [False, True])
    def test_output_shown_as_it_arrives_leaves_marker_lines_out(
        self, tmp_path: Path, verbose: bool
    ) -> None:
        # A helper whose output is on screen while it runs (the certificate step's), or any
        # helper in verbose mode.
        console, out, log = self._console(
            tmp_path,
            Reply(stdout=f"working\n{self.MARKER}\n", stderr="ENTRABOT_RESULT=on-stderr\n"),
        )
        console.verbose = verbose
        result = console.run(["helper"], summary=verbose)
        console.close()
        assert "working" in out.getvalue()
        assert "ENTRABOT_RESULT" not in out.getvalue()
        logged = log.read_text(encoding="utf-8")
        assert self.MARKER in logged and "ENTRABOT_RESULT=on-stderr" in logged
        assert self.MARKER in result.stdout


def test_a_child_whose_output_is_hidden_still_gets_a_heartbeat() -> None:
    # The helper prints steadily, but none of it is on screen, so to the person watching it is
    # silent. The heartbeat follows what they can see.
    out = io.StringIO()
    console = Console(
        stdin=io.StringIO(), stdout=out, color=False, unicode=False, heartbeat_seconds=0.2
    )
    code = (
        "import sys, time\n"
        "for n in range(9):\n"
        "    print('chatter', n); sys.stdout.flush(); time.sleep(0.1)\n"
    )
    console.run([sys.executable, "-c", code], summary=True, heartbeat="Still creating your agent")
    assert "Still creating your agent" in out.getvalue()
    assert "chatter" not in out.getvalue()
