#!/usr/bin/env python3
"""Upload a Windows-generated public certificate to an existing Blueprint."""

from __future__ import annotations

import argparse
import base64
import socket
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import requests
from cryptography import x509

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import get_existing_graph_token  # noqa: E402

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT_SECONDS = 15


class ResponseLike(Protocol):
    status_code: int
    text: str

    def json(self) -> dict[str, Any]: ...


RequestFn = Callable[..., ResponseLike]


class BlueprintCertUploadError(RuntimeError):
    """Raised when a Blueprint certificate cannot be registered."""


def _graph_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def upload_blueprint_certificate(
    *,
    token: str,
    blueprint_object_id: str,
    der_bytes: bytes,
    display_name: str | None = None,
    request: RequestFn = requests.request,
) -> None:
    """Register ``der_bytes`` idempotently, preserving certificates from other machines."""
    if not token.strip():
        raise BlueprintCertUploadError("Graph token is required")
    if not blueprint_object_id.strip():
        raise BlueprintCertUploadError("Blueprint object ID is required")
    if not der_bytes:
        raise BlueprintCertUploadError("Certificate DER bytes are required")

    try:
        cert = x509.load_der_x509_certificate(der_bytes)
    except ValueError as exc:
        raise BlueprintCertUploadError("Certificate DER is malformed") from exc

    name = display_name or f"EntraBot Device Certificate - {socket.gethostname().split('.')[0]}"
    credential = {
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
        "key": base64.b64encode(der_bytes).decode(),
        "displayName": name,
        "startDateTime": _graph_datetime(cert.not_valid_before_utc),
        "endDateTime": _graph_datetime(cert.not_valid_after_utc),
    }
    url = f"{GRAPH_BASE}/applications/{blueprint_object_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        existing_response = request(
            "GET", url + "?$select=keyCredentials",
            headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if existing_response.status_code != 200:
            raise BlueprintCertUploadError(
                f"Cannot read existing Blueprint certificates ({existing_response.status_code}); "
                "no certificates were changed."
            )
        try:
            data = existing_response.json()
        except ValueError as exc:
            raise BlueprintCertUploadError(
                "Cannot read existing Blueprint certificate data"
            ) from exc
        existing = data.get("keyCredentials") if isinstance(data, dict) else None
        if not isinstance(existing, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]
            for item in existing
        ):
            raise BlueprintCertUploadError(
                "Cannot preserve existing Blueprint certificates without their public keys"
            )
        if any(item["key"] == credential["key"] for item in existing):
            return
        response = request(
            "PATCH", url,
            headers=headers,
            json={"keyCredentials": [*existing, credential]},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise BlueprintCertUploadError("Graph request failed while uploading certificate") from exc
    if response.status_code not in (200, 204):
        raise BlueprintCertUploadError(
            f"Blueprint certificate upload failed ({response.status_code}): "
            f"{response.text[:300]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blueprint-object-id", required=True)
    parser.add_argument("--der-path", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        der_bytes = args.der_path.read_bytes()
        token = get_existing_graph_token()
        upload_blueprint_certificate(
            token=token,
            blueprint_object_id=args.blueprint_object_id,
            der_bytes=der_bytes,
        )
    except (OSError, BlueprintCertUploadError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Blueprint public certificate registered in Entra.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
