#!/usr/bin/env python3
"""Print a Graph API access token using the Provisioner app's cert.

Shell helper — replaces any curl-with-client_secret path. The private
key lives in macOS Keychain; this script shells out to
CertificateCredential (via entra_provisioning.get_graph_token) which
reads the key from Keychain in memory only.

Usage:
    python3 scripts/provisioner-token.py

Exits 0 on success (token on stdout). Exits 1 with the error on stderr.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import ProvisionerBootstrapError, get_graph_token


def main() -> int:
    try:
        # wait_for_propagation=False — the app+consent already exist by the
        # time a shell caller invokes this, so no need to re-sleep.
        #
        # Redirect get_graph_token's diagnostic prints to stderr so the
        # token is the ONLY thing on stdout. Shell callers rely on
        # `TOKEN=$(python3 scripts/provisioner-token.py)` capturing a
        # clean JWT; a diagnostic line leaking onto stdout corrupts
        # every downstream use.
        with contextlib.redirect_stdout(sys.stderr):
            token = get_graph_token(wait_for_propagation=False)
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
