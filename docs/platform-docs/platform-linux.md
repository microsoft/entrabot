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
- **bubblewrap and xdg-dbus-proxy**, for process containment:
  `sudo apt install bubblewrap xdg-dbus-proxy` or
  `sudo dnf install bubblewrap xdg-dbus-proxy`. Without either one the server
  runs uncontained and says so. On Ubuntu 24.04 and later, `bwrap` also needs
  an AppArmor profile before the system lets it work; see "Process
  containment" below.

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
disconnects this computer. Setup has been run against a real tenant on Ubuntu 24.04 (before containment existed); see
[What has been tested](../getting-started/macos-linux.md#what-has-been-tested).

For step-by-step setup, see [macOS and Linux setup](../getting-started/macos-linux.md)
and [Prerequisites](../getting-started/prerequisites.md).

## Process containment

When bubblewrap (`bwrap`) and `xdg-dbus-proxy` are installed, the MCP server runs inside a
bubblewrap sandbox by default.

- **Only named folders exist inside.** The computer's root folder is never mounted. The
  sandbox has the system folders a Python process needs (`/usr`, `/etc`, and `/bin`, `/sbin`
  and `/lib*`) read-only, the repository and its virtual environment read-only, and
  Entrabot's state and sign-in cache read-write. `/tmp` is private and thrown away when the
  server stops. There is no `/run`, `/home` (beyond the way to those folders), `/mnt`,
  `/media`, `/var`, `/root` or `/sys` inside: no other user's files, no other disk, and none
  of the Unix sockets under `/run` (the session and system D-Bus, systemd, the SSH and GPG
  agents, Docker, snapd).
- **Network.** The server has its own network namespace whose only exit is the launcher's
  allowlisting proxy.
- **The key store, and nothing else on the session bus.** The agent's key is in the keyring,
  which is reached over the D-Bus session bus. That bus is never given to the sandbox,
  because the user's systemd is on it too. The launcher runs `xdg-dbus-proxy` outside the
  sandbox, set to let the server talk to the key store (`org.freedesktop.secrets`, and
  KWallet's names) and to nothing else, and only that proxy's socket is inside. The proxy
  connects to the bus from outside, so the kind of socket the bus uses does not matter.
- **Environment.** The server gets a named part of the launcher's environment (its own
  `ENTRABOT_*` settings, `PATH`, the language settings and a few more), not all of it. A
  token for another service in the shell that started Entrabot stays outside. Name anything
  else a token command needs in `ENTRABOT_SANDBOX_EXTRA_ENV`.
- **No way to rebuild the view from inside.** The server holds no capabilities, cannot make
  namespaces of its own (`--disable-userns` on bubblewrap 0.8.0 and later), and runs under
  a system-call filter that refuses new namespaces and mounts, the kernel keyring, tracing
  other processes and typing into the terminal. The filter exists for x86-64 and arm64; on
  another kind of machine the Linux sandbox is not used.

What this does not narrow: through the key store the server can read and change every secret
the keyring holds for your account, not only Entrabot's own. `/etc` is readable in full, as is
everything under `/usr`.

**Ubuntu 24.04 and later** restrict unprivileged user namespaces through AppArmor
(`kernel.apparmor_restrict_unprivileged_userns = 1`), and the `bubblewrap` package ships no
AppArmor profile, so `bwrap` exits with "setting up uid map: Permission denied". Entrabot
tries `bwrap` before it uses it: where the system refuses it, the server runs uncontained and
says so, and with `ENTRABOT_CONTAINMENT=required` it refuses to start. To let `bwrap` work,
an administrator adds an AppArmor profile for it; see [Containment
troubleshooting](../troubleshooting/containment.md#linux-bwrap-is-installed-but-the-server-is-not-contained).

Status: **run for real on Ubuntu 24.04.5 with a throwaway agent** (2026-11-20, x86-64,
`bubblewrap` 0.9.0 with that AppArmor profile, `xdg-dbus-proxy`, GNOME Keyring unlocked in a
`dbus-run-session` shell as in "Preparing a Linux host" above). The agent signed in inside
the sandbox, its key read from the keyring through the filtering proxy. The server started
contained, answered `initialize` and `whoami` over stdio as the agent, ran its polls through
the proxy with nothing refused, and stopped cleanly. The sandbox was then attacked from
inside, and each attempt failed; the first version of this sandbox did not survive the same
attack. A second run, with an agent that has a licence, sent and read a Teams message, sent
mail, listed recent files and ran the Teams and mail polls from inside the sandbox, with
nothing refused; inbound Teams messages and a mail from the sponsor were pushed to the host.
**Not run:** reading a mail or a file's
contents, a desktop session's keyring, KWallet, arm64, and any other distribution. See the
[Verification Record](../project/verification.md). Run
`python -m entrabot.sandbox.selftest` to check it on yours. Settings and how to turn it off:
[Process Containment](../guides/containment.md).

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
