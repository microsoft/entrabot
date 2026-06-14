"""Fail-closed validation for the active ``keyring`` backend."""

from __future__ import annotations

import importlib
import platform
from functools import cache
from typing import Any

import keyring

from entrabot.errors import InsecureKeyringBackendError

_ALLOWED_BACKENDS: dict[str, tuple[str, ...]] = {
    "Darwin": ("keyring.backends.macOS.Keyring",),
    "Linux": (
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
        # libsecret is shipped by `keyring` itself (not keyrings.alt) and is
        # the recommended default on Fedora / openSUSE. It talks to the same
        # Secret Service collection as the SecretService backend via the
        # libsecret C library through GI, just with a different Python path.
        "keyring.backends.libsecret.Keyring",
    ),
    "Windows": ("keyring.backends.Windows.WinVaultKeyring",),
}


def backend_class_name(backend: object) -> str:
    """Return ``module.Class`` for a keyring backend instance."""
    cls = backend.__class__
    return f"{cls.__module__}.{cls.__name__}"


@cache
def _load_backend_class(path: str) -> type[Any] | None:
    module_name, class_name = path.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(module, class_name, None)


def _allowed_backend_classes(system: str) -> tuple[type[Any], ...]:
    return tuple(
        cls
        for path in _ALLOWED_BACKENDS.get(system, ())
        if (cls := _load_backend_class(path)) is not None
    )


def assert_allowed_keyring_backend(system: str | None = None) -> str:
    """Return the active backend name if it is approved for ``system``.

    Raises:
        InsecureKeyringBackendError: the active backend is not the OS keystore.
    """
    system = system or platform.system()
    expected = _ALLOWED_BACKENDS.get(system, ())
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # pragma: no cover - depends on host keyring config
        raise InsecureKeyringBackendError(
            f"uninspectable keyring backend ({exc!r})",
            expected,
        ) from exc

    backend_name = backend_class_name(backend)
    if not isinstance(backend, _allowed_backend_classes(system)):
        raise InsecureKeyringBackendError(backend_name, expected)
    return backend_name
