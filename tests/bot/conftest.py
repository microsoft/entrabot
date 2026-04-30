"""Skip Bot Gateway tests on Windows.

The Bot Gateway (M365 Agents SDK server + JSONL IPC) uses POSIX
``fcntl.flock`` for advisory file locking. Bot Gateway is not yet
part of the Windows port (the Windows port covers the ``agent_user``
three-hop flow + Teams Graph API). Skip cleanly on win32 so the
Windows CI gate stays useful for the agent_user surface.
"""

from __future__ import annotations

import sys

collect_ignore_glob = ["*.py"] if sys.platform == "win32" else []
