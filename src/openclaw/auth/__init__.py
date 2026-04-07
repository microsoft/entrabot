"""Authentication building blocks for Openclaw Agent Identity."""

from openclaw.auth.certificate import build_client_assertion, compute_cert_thumbprint

__all__ = ["build_client_assertion", "compute_cert_thumbprint"]
