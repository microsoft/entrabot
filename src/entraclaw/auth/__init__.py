"""Authentication building blocks for EntraClaw Agent Identity."""

from entraclaw.auth.certificate import build_client_assertion, compute_cert_thumbprint

__all__ = ["build_client_assertion", "compute_cert_thumbprint"]
