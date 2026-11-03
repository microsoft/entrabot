"""Print registered certs on the Blueprint app.

An operator tool for seeing which machines hold a certificate. Scripts can read
the count from stdout; human-readable detail lines go to stderr so they show up
even when stdout is captured.

Usage:
    python scripts/list_blueprint_certs.py <BLUEPRINT_OBJECT_ID>

Output:
    stdout: a single integer (count of registered keyCredentials)
    stderr: one line per cert — "    - <displayName>  expires <YYYY-MM-DD>"

Exit codes:
    0 — the list was read; the count on stdout is real (0 means really none)
    2 — usage error
    3 — could not read the list; nothing is printed on stdout, because a count
        that isn't known must never look like one that is
"""

from __future__ import annotations

import contextlib
import sys

from blueprint_certs import fetch_key_credentials
from entra_provisioning import get_existing_graph_token

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COULD_NOT_READ = 3


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: list_blueprint_certs.py <BLUEPRINT_OBJECT_ID>",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        # get_existing_graph_token is noisy on stdout; redirect so only the count
        # reaches a shell capture.
        with contextlib.redirect_stdout(sys.stderr):
            token = get_existing_graph_token()
        credentials = fetch_key_credentials(sys.argv[1], token)
    except Exception as exc:
        print(f"  Could not read the Blueprint's certificates: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_READ

    print(len(credentials))
    for credential in credentials:
        print(credential.summary(), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
