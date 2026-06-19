"""Register THIS machine's Blueprint cert on the Blueprint app registration, using the
Azure-CLI token (Application.ReadWrite.All).

Why this exists: ``setup-windows.ps1`` only publishes the Blueprint cert when it *creates*
the Blueprint. When the Blueprint app already exists (reused from a prior provisioning or
another machine), the cert-publish step is skipped — so this machine's cert never lands on
the app, and the three-hop fails at hop 1 with::

    AADSTS700027: The certificate ... is not registered on application <blueprint-app-id>

``deploy-windows.ps1`` (cert rotation) can't fix this because it authenticates the Graph
PATCH *with* the Blueprint cert — the very thing that's missing (chicken-and-egg). This
script instead authenticates with the az-CLI provisioner token, so it can bootstrap.

Run once, from the repo root, with ``az login`` done::

    .\.venv\Scripts\python scripts\register_blueprint_cert.py

It exports the local cert (by SHA-1 from .env) from CurrentUser\\My, PATCHes its public key
onto the Blueprint app's keyCredentials (replacing, like deploy-windows.ps1 does), then
smoke-tests the three-hop token.
"""

from __future__ import annotations

import base64
import subprocess
import sys

import httpx

from entrabot.config import get_config

GRAPH = "https://graph.microsoft.com/v1.0"


def _az_graph_token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource",
         "https://graph.microsoft.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"az token failed (run `az login`): {out.stderr.strip()}")
    return out.stdout.strip()


def _export_cert_der(sha1: str) -> bytes:
    ps = f"[Convert]::ToBase64String((Get-Item Cert:\\CurrentUser\\My\\{sha1}).RawData)"
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"could not export cert {sha1} from Cert:\\CurrentUser\\My: {out.stderr.strip()}")
    return base64.b64decode(out.stdout.strip())


def main() -> int:
    cfg = get_config()
    app_id = cfg.blueprint_app_id
    sha1 = cfg.blueprint_cert_sha1
    if not app_id:
        sys.exit("ENTRABOT_BLUEPRINT_APP_ID missing from .env")
    if not sha1:
        sys.exit("ENTRABOT_BLUEPRINT_CERT_SHA1 missing from .env")

    token = _az_graph_token()
    headers = {"Authorization": f"Bearer {token}"}

    # Resolve the real object id from the app id (PATCH targets the object id).
    r = httpx.get(f"{GRAPH}/applications(appId='{app_id}')", headers=headers, timeout=20)
    r.raise_for_status()
    obj_id = r.json()["id"]

    der = _export_cert_der(sha1)
    body = {"keyCredentials": [{
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
        "key": base64.b64encode(der).decode(),
    }]}
    p = httpx.patch(f"{GRAPH}/applications/{obj_id}", headers=headers, json=body, timeout=30)
    if p.status_code not in (200, 204):
        sys.exit(f"PATCH /applications/{obj_id} failed {p.status_code}: {p.text[:400]}")
    print(f"Registered cert {sha1} on Blueprint app {app_id} (object {obj_id}).")

    # Smoke test: the three-hop should now succeed (allow a beat for AAD propagation).
    import time

    from entrabot.errors import TokenExchangeError
    from entrabot.tools.teams import acquire_agent_user_token

    for attempt in range(6):
        try:
            tok = acquire_agent_user_token(cfg)
            print(f"Three-hop token acquired OK (len {len(tok)}). Teams auth is now working.")
            return 0
        except TokenExchangeError as e:
            if attempt < 5:
                print(f"  not propagated yet (attempt {attempt + 1}/6), waiting 15s…")
                time.sleep(15)
            else:
                print(f"Cert registered, but token still failing after propagation wait: {e}")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
