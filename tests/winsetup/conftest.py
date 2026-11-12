"""Safety net for the setup wizard tests: nothing here may touch the real home folder.

The wizard writes a per-user Copilot CLI config and logs under the user's data folder. Tests
pass their own paths, but a missed one would land in the developer's real home, so HOME,
USERPROFILE and COPILOT_HOME all point at the test's temp directory.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    home = tmp_path_factory.mktemp("home")  # beside, not inside, the test's own tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("COPILOT_HOME", str(home / ".copilot"))
