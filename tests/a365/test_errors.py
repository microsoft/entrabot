"""The Agent 365 errors say what is wrong in words the agent can pass on."""

from __future__ import annotations

from entrabot.a365.errors import A365Error, A365ManifestNotFoundError


class TestManifestNotFound:
    """Seen on a real run: asked for a Word document, the agent got this error, read it as a
    missing file, and went off to build a .docx by hand. It means the Word, Excel and OneDrive
    tools were never switched on for this agent."""

    def _message(self) -> str:
        return str(A365ManifestNotFoundError(["C:/agent/ToolingManifest.json", "C:/agent/.a365"]))

    def test_it_says_the_tools_are_not_switched_on(self) -> None:
        message = self._message()
        assert message.startswith(
            "Word, Excel and OneDrive files are not switched on for this agent"
        )
        assert "These tools are not available until then." in message

    def test_it_says_what_to_do_on_each_platform(self) -> None:
        message = self._message()
        assert "scripts\\setup-windows.cmd" in message
        assert "answer yes to the Word, Excel and OneDrive question" in message
        assert "a365 develop add-mcp-servers mcp_WordServer" in message  # macOS and Linux
        assert "./scripts/setup.sh --configure-a365-work-iq" in message

    def test_it_says_a_microsoft_365_copilot_licence_is_needed(self) -> None:
        assert "Microsoft 365 Copilot licence" in self._message()

    def test_it_keeps_where_it_looked_and_the_override(self) -> None:
        error = A365ManifestNotFoundError(["C:/agent/ToolingManifest.json", "C:/agent/.a365"])
        assert error.searched_paths == ["C:/agent/ToolingManifest.json", "C:/agent/.a365"]
        assert str(error).endswith("Searched: C:/agent/ToolingManifest.json, C:/agent/.a365")
        assert "ENTRABOT_A365_TOOLING_MANIFEST" in str(error)

    def test_it_is_still_an_a365_error(self) -> None:
        assert isinstance(A365ManifestNotFoundError([]), A365Error)

    def test_it_is_plain_text(self) -> None:
        message = self._message()
        assert message.isascii() and "!" not in message
