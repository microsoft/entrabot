"""Print registered certs on the Blueprint app.

Used by setup.sh to show the user what will be replaced before generating
a new cert. The shell reads the count from stdout; human-readable detail
lines go to stderr so setup.sh can surface them regardless of whether the
shell is capturing the count.

Usage:
    python scripts/list_blueprint_certs.py <BLUEPRINT_OBJECT_ID>

Output:
    stdout: a single integer (count of registered keyCredentials)
    stderr: one line per cert — "    - <displayName>  expires <YYYY-MM-DD>"
"""

from __future__ import annotations

import contextlib
import sys

import requests
from entra_provisioning import get_graph_token


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: list_blueprint_certs.py <BLUEPRINT_OBJECT_ID>",
            file=sys.stderr,
        )
        return 2
    blueprint_obj_id = sys.argv[1]

    # get_graph_token is noisy on stdout; redirect so only the count reaches
    # the shell capture.
    with contextlib.redirect_stdout(sys.stderr):
        token = get_graph_token(wait_for_propagation=False)

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/applications/{blueprint_obj_id}?$select=keyCredentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    creds = resp.json().get("keyCredentials", []) if resp.ok else []

    print(len(creds))
    for c in creds:
        name = c.get("displayName") or "?"
        end = (c.get("endDateTime") or "?")[:10]
        print(f"    - {name:50s}  expires {end}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
