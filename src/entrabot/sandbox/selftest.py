"""Check containment on this computer without starting the server.

``python -m entrabot.sandbox.selftest`` runs :mod:`entrabot.sandbox.probe` inside exactly the
sandbox and proxy the server gets, and prints what it found as JSON:

- the agent signs in (three-hop, its key read from the OS key store) and ``GET /me`` answers,
  which is what ``whoami`` reports;
- a file under the home directory outside the allowed paths cannot be read;
- the repository cannot be written;
- a host that is not on the list cannot be reached, through the proxy or directly.

It sends nothing and changes nothing in the tenant: two token requests and one Graph read.
Unlike starting the server it runs no background poll, no daily summary and no chat
discovery, so it can run beside a server that is already running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from entrabot.sandbox import launch


def main() -> int:
    from entrabot.config import get_config

    choice = launch.choose(platform=sys.platform, env=os.environ)
    if choice.backend is None:
        print(f'{{"contained": false, "reason": "{choice.reason}"}}')
        return 1
    home = Path.home()
    for name in (".zshrc", ".bashrc", ".profile", ".gitconfig", ".ssh/known_hosts"):
        if (home / name).is_file():
            os.environ["ENTRABOT_PROBE_OUTSIDE_FILE"] = str(home / name)
            break
    inner = [sys.executable, "-m", "entrabot.sandbox.probe"]
    return launch.run_contained(
        choice.backend, get_config(), home / ".entrabot", home, inner, record_status=False
    )


if __name__ == "__main__":
    sys.exit(main())
