# Linux Platform APIs

This page is the platform reference for how Entrabot uses Linux system services:
where it stores secrets, how it signs the Blueprint certificate assertion, how
delegated tokens are cached, and how to prepare a Linux host for setup.

## Credential storage: Secret Service / KWallet

Entrabot's `CredentialStore` on Linux is `LinuxCredentialStore`
(`platform/linux.py`), a thin wrapper over the `keyring` library that maps to the
desktop secret store — GNOME Keyring / Secret Service, KWallet, or libsecret. It
exposes the OS-agnostic `store()` / `retrieve()` / `delete()` contract used
across the codebase for small string secrets such as the active client ID and the
Blueprint private key.

**Backend allowlist, fail closed.** At construction, `LinuxCredentialStore`
calls `assert_allowed_keyring_backend("Linux")` (`platform/keyring_backend.py`),
which accepts only these backends:

- `keyring.backends.SecretService.Keyring`
- `keyring.backends.kwallet.DBusKeyring`
- `keyring.backends.libsecret.Keyring`

If the active `keyring` backend is anything else — for example a plaintext or
in-memory alternative, which `PYTHON_KEYRING_BACKEND` or a `keyring`
configuration file can select — or cannot be inspected, it raises
`InsecureKeyringBackendError` and refuses to proceed rather than writing secrets
to an insecure store. A headless server with no Secret Service provider installed,
or a session with no D-Bus available to reach one, therefore fails closed here
rather than silently degrading. Run Entrabot in a session where a supported
secret-store provider is present and unlocked; see
[Preparing a Linux host](#preparing-a-linux-host).

Setup checks this in its first step, "Checking this computer", before it creates
anything, and stops with "Setup can't use this account's key store, where the
agent's sign-in key is kept (the Linux keyring). Nothing was changed." when the
keyring cannot be used. Setup never falls back to keeping the key in a file.

## Blueprint certificate signing

`platform/keycert.py` keeps the Blueprint certificate's lifecycle (create, find,
test-sign, delete) on top of the secret store, with the same functions
`platform/wincert.py` has on Windows, so setup, certificate rotation, teardown,
the health checks and the runtime treat a certificate the same way on every
system.

- **One secret-store item per certificate.** Service `entrabot-blueprint-cert`,
  account the certificate's SHA-1 thumbprint (40 hexadecimal characters),
  holding the RSA-2048 private key and the certificate together, as PEM. An
  `index` item under the same service lists the thumbprints. `.env` names the
  agent's certificate in `ENTRABOT_BLUEPRINT_CERT_SHA1` (and
  `ENTRABOT_BLUEPRINT_KSP=keyring`), so two agents set up under one Linux
  account are two items, and neither overwrites the other's key.
- **A key is only used once it has signed.** Setup writes the item, reads it
  back, signs test data with the key and verifies the signature against the
  certificate before it registers anything. Deleting a certificate deletes its
  key: they are one item.
- **Fail closed.** A keyring that cannot be read stops setup before anything is
  created, and stops the agent's sign-in. Nothing is ever written to a file
  instead.

When Entrabot signs the Hop 1 client assertion with `ENTRABOT_BLUEPRINT_CERT_SHA1`
set, it reads the key stored with that certificate into the process, checks that
the certificate is in date (logging a warning when 30 days or fewer remain), and
signs the JWT in-process with `cryptography` and PyJWT
(`auth/certificate.py::build_client_assertion`, passed `private_key_pem`). The
assertion header carries the base64url SHA-256 (`x5t#S256`) of the DER
certificate and has a 10-minute lifetime.

Setup's own Provisioner certificate is kept the same way, under service
`entrabot-provisioner-cert`, one item per tenant (account: the tenant ID).

**A computer set up before the wizard.** Earlier versions of `setup.sh` stored
the Blueprint private key on its own, as a PEM string under service `entrabot`
and account `blueprint-private-key`, with no SHA-1 thumbprint in `.env`. Without
`ENTRABOT_BLUEPRINT_CERT_SHA1` the agent still signs with that key, so such a
computer keeps working. The next `./scripts/setup.sh` keeps the key and, when its
certificate is on the agent's Blueprint, stores the two together as the agent's
own item (see
[Computers set up before the wizard](../reference/scripts/setup/setup-sh.md#computers-set-up-before-the-wizard)).
The older item is left in place; teardown deletes it only when it holds the same
key. While `.env` names a certificate, the agent never reads the older item: it
may be another agent's.

As on macOS, this path is **not** hardware-backed and the key is **not**
non-exportable: anything that can read the secret store under the agent's account
can retrieve the PEM. See
[Security Boundaries](../architecture/security-boundaries.md) for the threat-model
comparison and
[Windows and Platform Support](../architecture/windows-and-platforms.md) for the
per-OS certificate matrix. Downstream of the signed assertion, the token flow is
identical across platforms — see
[Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Delegated-mode token cache

In delegated mode, MSAL persists its token cache through msal-extensions'
`build_encrypted_persistence`, which on Linux encrypts the cache via the Secret
Service when one is available (`auth/delegated.py`). The cache file lives under
the per-user cache directory resolved by `platformdirs`
(`platformdirs.user_cache_dir("entrabot")`), and its parent directory is created
with `0o700` permissions. If encrypted persistence cannot be built — for example
on a host with no Secret Service provider — Entrabot logs the failure and falls
back to an in-memory cache for that run rather than writing an unencrypted cache
to disk.

## Preparing a Linux host

There is no dedicated Linux prerequisites installer. Setup itself needs only:

- **Python 3.12 or newer**, with the module that creates virtual environments.
  On Debian and Ubuntu: `sudo apt install python3.12 python3.12-venv`. On Fedora
  and RHEL: `sudo dnf install python3.12`.
- **A keyring provider that is running and unlocked** in a D-Bus session that
  setup, and later the agent, can reach: GNOME Keyring (Secret Service),
  KWallet, or libsecret.

Git and the Azure CLI are optional: download the repository as a ZIP if there is
no Git, and setup offers to install a private Azure CLI under
`~/.entrabot/tools/azure-cli`, with `pip`, when none is on `PATH`.

**On a desktop**, the desktop session normally starts the keyring and unlocks it
when you sign in. Run setup, and the agent, from a terminal in that session.

**On a server with no desktop**, or over SSH, there is usually no keyring
running, or none this session can reach. Start a shell with its own D-Bus
session and unlock GNOME Keyring in it. On Debian or Ubuntu:

```bash
sudo apt install gnome-keyring dbus-user-session libsecret-tools
dbus-run-session -- bash                 # a shell with its own D-Bus session
```

Then, in that shell, unlock the keyring (it is created with this password the
first time) and run setup. `eval` matters: the daemon prints the variables the
rest of the session needs to find it.

```bash
read -rs -p "Keyring password: " KEYRING_PASSWORD; echo
eval "$(printf '%s' "$KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets)"
unset KEYRING_PASSWORD
./scripts/setup.sh
```

To run one command unattended, the same in one line:

```bash
dbus-run-session -- bash -c 'eval "$(printf %s "$KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets)"; ./scripts/setup.sh --diagnose'
```

Start the agent (`./start-entrabot.sh`) from a session unlocked the same way:
the agent reads its key from the keyring each time it signs in, and a new
session starts with the keyring locked. This recipe was run on Ubuntu 24.04
(GNOME Keyring, bash 5.2) against a real tenant, with the setup wizard and with
the `setup.sh` from before it.

If setup or the end-to-end test says the Linux keyring cannot be used, see
[macOS and Linux troubleshooting](../troubleshooting/macos-linux.md).

Then run **`./scripts/setup.sh`**. It builds `.venv` and starts the setup
wizard, the same one macOS and Windows run: it signs in (with a code instead of
the browser when neither `DISPLAY` nor `WAYLAND_DISPLAY` is set, or over SSH), provisions
the Blueprint → Agent Identity → Agent User chain, creates or reuses the
Blueprint certificate in the keyring, writes `.env`, registers the Entrabot MCP
server, and runs an end-to-end test. Operational storage stays local by default;
pass `--use-cloud-memory` to opt into Azure Blob-backed storage.
`./scripts/setup.sh --rotate-cert` replaces the certificate,
`./scripts/setup.sh --diagnose` checks the setup, and `./scripts/teardown.sh`
disconnects this computer. Setup has not yet been run on Linux at all; see
[What has been tested](../getting-started/macos-linux.md#what-has-been-tested).

For step-by-step setup, see [macOS and Linux setup](../getting-started/macos-linux.md)
and [Prerequisites](../getting-started/prerequisites.md).

## Default paths

- **Operational data, logs, and audit** default to `~/.entrabot/` (for example
  `~/.entrabot/data`, `~/.entrabot/logs`, `~/.entrabot/audit`), each overridable
  by its corresponding `ENTRABOT_*_DIR` environment variable (`config.py`).
- **The MSAL delegated token cache** lives under the `platformdirs` per-user
  cache directory for `entrabot`, as described above.

## See also

- [Windows and Platform Support](../architecture/windows-and-platforms.md) — the
  cross-platform `CredentialStore` abstraction and per-OS certificate matrix.
- [Security Boundaries](../architecture/security-boundaries.md) — why the
  PEM-in-process model differs in strength from the Windows CNG path.
- [Identity and Token Flow](../architecture/identity-and-token-flow.md) — the
  three-hop flow the signed assertion feeds into.
- [macOS Platform APIs](platform-macos.md) and
  [Windows Platform APIs](platform-windows.md) — the other two platforms.
