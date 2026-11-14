"""Entrabot setup wizard, for Windows, macOS and Linux.

It was written for Windows first, hence the package's name. ``scripts/setup-windows.ps1``
(Windows) and ``scripts/setup.sh`` (macOS, Linux) only get Python and a virtual environment
onto the machine.
Everything after that (questions, sign-in, provisioning, the certificate, ``.env``, memory,
registering the MCP server, the smoke test) lives here, where it can be unit-tested on any
OS. Run it with ``python -m entrabot.winsetup setup --project-root <repo>``.

Nothing in this package imports ``entrabot.config``: importing it loads ``.env`` into the
process environment, and a wizard that rewrites ``.env`` would then hand the old values to
every child it starts.
"""
