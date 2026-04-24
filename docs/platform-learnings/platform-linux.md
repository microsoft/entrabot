# Linux Platform APIs

## Overview

Linux lacks a single, unified credential management API like macOS Keychain or Windows DPAPI. Instead, it offers a **layered ecosystem** of complementary mechanisms:

| Layer | Mechanism | Scope | GUI Required? |
|-------|-----------|-------|---------------|
| **User-space (desktop)** | Secret Service API (D-Bus) | Per-user, per-session | Typically yes (unlock prompts) |
| **User-space (library)** | Python `keyring` / `secretstorage` | Per-user | Depends on backend |
| **Kernel** | KEYS subsystem (`keyctl`) | Per-process/session/user | No |
| **Init system** | systemd credentials (`systemd-creds`) | Per-service | No |
| **Authorization** | polkit (PolicyKit) | System-wide | Agent-dependent |
| **Authentication** | PAM | System-wide | No |

The Linux security model is based on **DAC (Discretionary Access Control)** via UNIX permissions (UID/GID), supplemented by capabilities, namespaces, cgroups, seccomp, and optionally MAC frameworks (SELinux, AppArmor). Every process carries credential metadata (real/effective UID, GID, supplementary groups) tracked in `/proc/[pid]/status`.

**Key architectural difference from macOS/Windows:** There is no single OS vendor controlling the desktop stack. GNOME, KDE, and other DEs each implement the Secret Service spec differently, and headless servers may have *none* of them installed.

---

## Secret Service API (D-Bus)

### What It Is

The [freedesktop.org Secret Service API](https://specifications.freedesktop.org/secret-service/latest/) is a **D-Bus interface specification** (`org.freedesktop.Secrets`) that standardizes how applications store and retrieve secrets on Linux desktops. It is the closest Linux equivalent to macOS Keychain's `Security.framework`.

### Architecture

```
┌──────────────────────┐
│   Application        │
│  (Python, C, etc.)   │
└──────┬───────────────┘
       │ D-Bus (session bus)
       ▼
┌──────────────────────┐
│  Secret Service       │
│  Provider              │
│  ┌──────────────────┐ │
│  │ GNOME Keyring    │ │  ← or KWallet, KeePassXC, pass-secret-service
│  └──────────────────┘ │
└──────┬───────────────┘
       │ Encrypted storage
       ▼
  ~/.local/share/keyrings/  (GNOME)
  ~/.local/share/kwalletd/  (KDE)
```

### D-Bus Interface Details

The service lives at bus name `org.freedesktop.secrets` with objects under `/org/freedesktop/secrets/`.

**Core interfaces and methods:**

| Interface | Key Methods | Purpose |
|-----------|-------------|---------|
| `org.freedesktop.Secret.Service` | `OpenSession`, `CreateCollection`, `SearchItems`, `Unlock`, `Lock`, `GetSecrets`, `ReadAlias`, `SetAlias` | Entry point; session & collection management |
| `org.freedesktop.Secret.Collection` | `CreateItem`, `Delete`, `SearchItems` | Manage groups of secrets (keyrings) |
| `org.freedesktop.Secret.Item` | `Delete`, properties: `Label`, `Attributes`, `Locked` | Individual secret CRUD |
| `org.freedesktop.Secret.Session` | `Close` | Secure communication session lifecycle |

**Typical workflow:**
1. `OpenSession` — negotiate encryption (DH or plaintext)
2. `Unlock` — unlock the target collection (may trigger GUI prompt)
3. `SearchItems` / `CreateItem` — find or store secrets by attribute dict
4. `GetSecrets` — retrieve secret values using the session
5. `Close` — end session

### Providers

| Provider | Desktop | Notes |
|----------|---------|-------|
| **GNOME Keyring** (`gnome-keyring-daemon`) | GNOME | Default on Ubuntu, Fedora GNOME. Most widely deployed. |
| **KDE KWallet** (`kwalletd5/6`) | KDE Plasma | Default on Kubuntu, Fedora KDE. Has Secret Service compatibility mode. |
| **KeePassXC** | Any | Can expose its database via Secret Service D-Bus interface. |
| **pass-secret-service** | Any | Bridges `pass` (GPG-based) to Secret Service API. |

### CLI Tool: `secret-tool`

```bash
# Store a secret
secret-tool store --label="Entraclaw Agent Token" service entraclaw account agent-001

# Lookup a secret
secret-tool lookup service entraclaw account agent-001

# Clear a secret
secret-tool clear service entraclaw account agent-001
```

### Python Access via `secretstorage`

The [`secretstorage`](https://pypi.org/project/SecretStorage/) library provides direct Python bindings to the D-Bus Secret Service API:

```python
import secretstorage

# Connect to D-Bus session
connection = secretstorage.dbus_init()

# Get the default collection ("login" keyring on GNOME)
collection = secretstorage.get_default_collection(connection)

# Unlock if needed (may trigger GUI prompt)
if collection.is_locked():
    collection.unlock()

# Store a credential
attributes = {
    'application': 'entraclaw',
    'agent_id': 'agent-001',
    'credential_type': 'oauth_token'
}
collection.create_item(
    label='Entraclaw Agent Token',
    attributes=attributes,
    secret=b'eyJhbGciOiJSUzI1NiIs...',
    replace=True  # update if exists
)

# Retrieve a credential
items = list(collection.search_items({
    'application': 'entraclaw',
    'agent_id': 'agent-001'
}))
if items:
    token = items[0].get_secret().decode('utf-8')
    print(f"Token: {token[:20]}...")

# Delete a credential
for item in items:
    item.delete()
```

**Dependencies:**
```bash
pip install SecretStorage   # pulls in jeepney (pure-Python D-Bus)
# System: apt install gnome-keyring dbus  (or equivalent)
```

### Python Access via `keyring` (Higher-Level)

The [`keyring`](https://pypi.org/project/keyring/) library provides a cross-platform abstraction:

```python
import keyring

# Store — maps to SecretService on Linux
keyring.set_password("entraclaw", "agent-001", "eyJhbGciOiJSUzI1NiIs...")

# Retrieve
token = keyring.get_password("entraclaw", "agent-001")

# Delete
keyring.delete_password("entraclaw", "agent-001")

# Check which backend is active
print(keyring.get_keyring())
# → keyring.backends.SecretService.Keyring (priority: 5)
```

**Backend priority on Linux:**
1. `SecretService.Keyring` (if D-Bus + provider available)
2. `KWallet.Keyring` (if KDE + dbus-python available)
3. `chainer.ChainerBackend` (tries multiple)
4. Falls back to plaintext file (insecure!) if nothing else works

**Override backend:**
```bash
export PYTHON_KEYRING_BACKEND=keyring.backends.SecretService.Keyring
# or in ~/.config/python_keyring/keyringrc.cfg:
# [backend]
# default-keyring=keyring.backends.SecretService.Keyring
```

---

## Kernel Keyring (KEYS Subsystem)

### What It Is

The Linux kernel has a built-in [Key Retention Service](https://docs.kernel.org/security/keys/core.html) — an in-kernel credential storage facility accessed via `keyctl(2)`, `add_key(2)`, and `request_key(2)` syscalls. This is a **completely separate system** from the D-Bus Secret Service API.

### Key Concepts

```
┌─────────────────────────────────────────┐
│              Kernel Space                │
│  ┌─────────────────────────────────────┐ │
│  │  @s (session keyring)               │ │  ← per login session
│  │  @u (user keyring)                  │ │  ← per UID, persists
│  │  @us (user-session keyring)         │ │  ← per UID, per session
│  │  @p (process keyring)               │ │  ← per process, dies with it
│  │  @t (thread keyring)                │ │  ← per thread
│  └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

**Key types:**
| Type | Readable by userspace? | Use case |
|------|----------------------|----------|
| `user` | Yes | General secrets that apps need to read back |
| `logon` | **No** (kernel-only) | Filesystem encryption keys, NFS tokens — never exposed to user-space |
| `keyring` | N/A | Container that holds references to other keys |
| `big_key` | Yes | Large payloads (stored in shmem or tmpfs) |

**Access control:** Each key has POSIX-like permissions with four categories: possessor, user, group, other. Each category can have: view, read, write, search, link, setattr.

### CLI Usage (`keyctl`)

```bash
# Add a key to the user keyring
keyctl add user entraclaw-token "my-secret-token" @u

# List the user keyring
keyctl list @u

# Read a key by ID
keyctl read <key-id>
keyctl pipe <key-id>        # raw bytes

# Set a timeout (auto-expire after 3600 seconds)
keyctl timeout <key-id> 3600

# Revoke a key
keyctl revoke <key-id>

# Remove a key from a keyring
keyctl unlink <key-id> @u
```

### Python Integration

**Option 1: `python-keyutils` bindings (preferred)**

```python
import keyutils

# Add a key to the session keyring
key_id = keyutils.add_key(
    'user',                              # key type
    'entraclaw:agent-001:token',          # description (acts as key name)
    b'eyJhbGciOiJSUzI1NiIs...',          # payload
    keyutils.KEY_SPEC_USER_KEYRING       # destination keyring (@u)
)

# Read it back
data = keyutils.read_key(key_id)
print(f"Token: {data.decode()[:20]}...")

# Set a timeout (1 hour)
keyutils.set_timeout(key_id, 3600)

# Search for a key by description
found_id = keyutils.search(
    keyutils.KEY_SPEC_USER_KEYRING,
    'user',
    'entraclaw:agent-001:token'
)
```

```bash
pip install keyutils   # requires libkeyutils-dev on the system
# apt install libkeyutils-dev  (Debian/Ubuntu)
# dnf install keyutils-libs-devel  (Fedora/RHEL)
```

**Option 2: subprocess wrapper**

```python
import subprocess

def kernel_keyring_store(name: str, secret: str, keyring: str = "@u") -> int:
    """Store a secret in the kernel keyring. Returns key serial number."""
    result = subprocess.run(
        ['keyctl', 'add', 'user', name, secret, keyring],
        capture_output=True, text=True, check=True
    )
    return int(result.stdout.strip())

def kernel_keyring_read(name: str, keyring: str = "@u") -> str:
    """Read a secret from the kernel keyring."""
    # First search for the key
    result = subprocess.run(
        ['keyctl', 'search', keyring, 'user', name],
        capture_output=True, text=True, check=True
    )
    key_id = result.stdout.strip()
    # Then read it
    result = subprocess.run(
        ['keyctl', 'pipe', key_id],
        capture_output=True, text=True, check=True
    )
    return result.stdout
```

### When to Use Kernel Keyring vs Secret Service

| Criterion | Kernel Keyring | Secret Service |
|-----------|---------------|----------------|
| **Headless/server** | ✅ Always available | ❌ Requires D-Bus + provider |
| **Persistence** | ⚠️ Keys in `@s` die with session; `@u` persists until reboot | ✅ Persistent on disk |
| **Survives reboot** | ❌ No (RAM only) | ✅ Yes (encrypted on disk) |
| **GUI prompts** | ❌ None | ✅ Can prompt for unlock |
| **Cross-process sharing** | ✅ Via `@u` keyring | ✅ Via D-Bus |
| **Max payload** | ~32KB (`user`), larger with `big_key` | Unlimited |
| **Attack surface** | Kernel memory (swap-protected) | User-space daemon memory |

**Recommendation for Entraclaw:** Use kernel keyring for **short-lived session tokens** (OAuth access tokens with TTL). Use Secret Service for **long-lived credentials** (refresh tokens, Agent ID keys) that must survive reboots.

---

## systemd User Services

### Why systemd User Services

For running Entraclaw as a **user-level background agent** (no root required), systemd user services are the standard mechanism. They provide:

- Process lifecycle management (start, stop, restart on failure)
- Logging via `journald`
- Dependency ordering
- Resource limits via cgroups
- Credential injection (`LoadCredential`)
- Socket activation
- Automatic start at boot (with linger)

### Service File Location

```
~/.config/systemd/user/entraclaw-agent.service
```

### Full Service File Example

```ini
[Unit]
Description=Entraclaw Autonomous Agent
Documentation=https://entraclaw.dev/docs
# Ensure D-Bus session is available (needed for Secret Service access)
Wants=dbus.socket
After=dbus.socket

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m entraclaw.agent --config %h/.config/entraclaw/agent.toml
ExecReload=/bin/kill -HUP $MAINPID

# Restart policy
Restart=on-failure
RestartSec=5
RestartMaxDelaySec=300
StartLimitIntervalSec=600
StartLimitBurst=5

# Environment
Environment=PYTHONUNBUFFERED=1
Environment=ENTRACLAW_LOG_LEVEL=info
EnvironmentFile=-%h/.config/entraclaw/env

# Working directory
WorkingDirectory=%h

# Logging — all stdout/stderr goes to journal
StandardOutput=journal
StandardError=journal
SyslogIdentifier=entraclaw-agent

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/entraclaw %h/.cache/entraclaw
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes

# Resource limits
MemoryMax=512M
CPUQuota=50%
TasksMax=64

# Credential injection (systemd v250+)
# LoadCredential=agent-key:%h/.config/entraclaw/agent-key.cred
# LoadCredentialEncrypted=api-token:/etc/entraclaw/api-token.cred

[Install]
WantedBy=default.target
```

### Lifecycle Management

```bash
# Reload unit files after editing
systemctl --user daemon-reload

# Enable (auto-start) and start
systemctl --user enable --now entraclaw-agent.service

# Status, logs, restart
systemctl --user status entraclaw-agent
journalctl --user -u entraclaw-agent -f          # follow logs
journalctl --user -u entraclaw-agent --since today
systemctl --user restart entraclaw-agent

# Stop and disable
systemctl --user stop entraclaw-agent
systemctl --user disable entraclaw-agent
```

### Lingering (Run Without Login Session)

By default, systemd kills all user services when the user's last session ends. **Linger** keeps the user manager alive at boot:

```bash
# Enable linger (requires root or polkit authorization)
sudo loginctl enable-linger $USER

# Verify
loginctl show-user $USER --property=Linger
# Linger=yes

# Or check the file directly
ls /var/lib/systemd/linger/
```

**With linger enabled:**
- `systemd --user` starts at boot (not at login)
- User services with `WantedBy=default.target` auto-start
- Services survive logout

### systemd Credentials (`systemd-creds`)

Modern systemd (v250+) provides a **service-scoped credential injection** mechanism — secrets are decrypted at service start and placed in a temporary, permissions-restricted directory:

```bash
# Check TPM2 availability
systemd-analyze has-tpm2

# Encrypt a credential (uses TPM2 + host key if available)
echo -n "my-secret-api-key" | systemd-creds encrypt - agent-api-key.cred

# Use in service file:
# [Service]
# LoadCredentialEncrypted=api-key:/path/to/agent-api-key.cred
```

**In the service process:**
```python
import os
from pathlib import Path

creds_dir = os.environ.get('CREDENTIALS_DIRECTORY')
if creds_dir:
    api_key = Path(creds_dir, 'api-key').read_text()
```

**Security properties:**
- Credentials live in unswappable memory
- Only the target service can read them
- Encrypted credentials can be stored world-readable (only the host/TPM can decrypt)
- Not inherited by child processes
- Cleaned up when the service stops

### Process Identity and Tracking

systemd places every service in its own **cgroup**, providing:

```bash
# See the cgroup tree
systemd-cgls --user

# See resource usage
systemd-cgtop

# Check a process's cgroup
cat /proc/<pid>/cgroup

# systemd tracks: PID, cgroup, start time, invocation ID
systemctl --user show entraclaw-agent --property=MainPID,InvocationID
```

Each service invocation gets a unique **InvocationID** (UUID), useful for audit correlation:
```ini
# Access in the service
Environment=INVOCATION_ID=%i
# Or read from: /proc/self/cgroup, sd_id128_get_invocation()
```

---

## PAM Integration

### What PAM Is

**Pluggable Authentication Modules** provide a framework for authentication, account management, session setup, and password changes. PAM configuration lives in `/etc/pam.d/` with per-service stack files.

### PAM Module Types

| Type | Purpose | Relevant for Entraclaw? |
|------|---------|----------------------|
| `auth` | Verify identity (password, biometric, token) | Maybe — could verify agent identity |
| `account` | Access restrictions (time, group, etc.) | Maybe — could restrict which agents can run |
| `session` | Session setup/teardown | Yes — unlock keyring at login |
| `password` | Password change management | No |

### PAM for GNOME Keyring Unlock

The most relevant PAM use for Entraclaw is **auto-unlocking the keyring at login** so the agent can access stored credentials without GUI prompts:

```
# /etc/pam.d/login (and /etc/pam.d/sshd)

# At end of auth section:
auth        optional    pam_gnome_keyring.so

# At end of session section:
session     optional    pam_gnome_keyring.so auto_start
```

This passes the login password to `gnome-keyring-daemon` to unlock the "login" keyring automatically.

### Could PAM Be Used for Agent Consent?

**Theoretically yes, practically not recommended.** Here's why:

| Aspect | Assessment |
|--------|-----------|
| **Mechanism** | A custom PAM module (`pam_entraclaw.so`) could intercept auth and present consent prompts |
| **Implementation** | Requires writing a C shared library implementing `pam_sm_authenticate` |
| **Conversation API** | PAM provides `pam_conv` for user prompts — supports `PAM_PROMPT_ECHO_ON`, `PAM_TEXT_INFO`, etc. |
| **Problem 1** | PAM is designed for **login flows**, not arbitrary application consent |
| **Problem 2** | Modifying PAM stacks is risky — misconfiguration can lock users out |
| **Problem 3** | PAM runs as root; agent consent should not require privilege escalation |
| **Verdict** | ❌ Use **polkit** instead for agent consent flows |

### Custom PAM Module Skeleton (For Reference)

```c
#include <security/pam_modules.h>
#include <security/pam_ext.h>
#include <string.h>
#include <stdlib.h>

PAM_EXTERN int pam_sm_authenticate(
    pam_handle_t *pamh, int flags, int argc, const char **argv
) {
    struct pam_conv *conv;
    struct pam_message msg = { PAM_PROMPT_ECHO_ON,
        "Entraclaw agent requests access. Approve? (yes/no): " };
    const struct pam_message *msgp = &msg;
    struct pam_response *resp = NULL;

    pam_get_item(pamh, PAM_CONV, (const void **)&conv);
    if (conv->conv(1, &msgp, &resp, conv->appdata_ptr) != PAM_SUCCESS)
        return PAM_AUTH_ERR;

    int result = (resp && resp->resp && strcmp(resp->resp, "yes") == 0)
        ? PAM_SUCCESS : PAM_AUTH_ERR;

    if (resp) { free(resp->resp); free(resp); }
    return result;
}
```

---

## polkit (PolicyKit)

### What It Is

[polkit](https://www.freedesktop.org/software/polkit/docs/latest/polkit.8.html) is a **system-wide authorization framework** for controlling access to privileged operations. Unlike PAM (which handles authentication — "who are you?"), polkit handles authorization — "are you allowed to do this?"

### Architecture

```
┌──────────────────┐     ┌──────────────────┐
│  Entraclaw Agent   │     │  User (Desktop)   │
│  (unprivileged)   │     │                   │
└────────┬─────────┘     └────────┬──────────┘
         │ D-Bus                  │
         ▼                        ▼
┌──────────────────────────────────────────┐
│        polkit Authority                    │
│   (org.freedesktop.PolicyKit1)            │
│                                            │
│   Checks: action + subject → result       │
│   Results: YES / NO / AUTH_REQUIRED       │
└────────────────────┬─────────────────────┘
                     │ If AUTH_REQUIRED
                     ▼
         ┌──────────────────────┐
         │  Authentication Agent │
         │  (GUI or TTY)         │
         │  polkit-gnome / pkttyagent │
         └──────────────────────┘
```

### Creating a Custom polkit Policy for Entraclaw

**Step 1: Define the action** — `/usr/share/polkit-1/actions/dev.entraclaw.agent.policy`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "https://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>

  <vendor>Entraclaw</vendor>
  <vendor_url>https://entraclaw.dev</vendor_url>

  <action id="dev.entraclaw.agent.authorize-action">
    <description>Allow Entraclaw agent to perform an action on your behalf</description>
    <message>Entraclaw agent "$(agent_id)" wants to: $(action_description)</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_self</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.owner">unix-user:$(uid)</annotate>
  </action>

  <action id="dev.entraclaw.agent.manage-credentials">
    <description>Allow Entraclaw to manage stored credentials</description>
    <message>Entraclaw wants to access stored credentials for agent "$(agent_id)"</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_self</allow_active>
    </defaults>
  </action>

</policyconfig>
```

**Authorization levels:**
| Value | Meaning |
|-------|---------|
| `no` | Never allowed |
| `yes` | Always allowed without auth |
| `auth_self` | User must authenticate as themselves |
| `auth_admin` | User must authenticate as an admin |
| `auth_self_keep` | Like `auth_self` but caches for a short time |

**Step 2: Custom rules** — `/etc/polkit-1/rules.d/50-entraclaw.rules`

```javascript
// Allow members of 'entraclaw-agents' group to manage credentials
// without repeated authentication prompts
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("dev.entraclaw.agent.") === 0 &&
        subject.isInGroup("entraclaw-agents")) {
        // Cache auth for 5 minutes
        return polkit.Result.AUTH_SELF_KEEP;
    }
});

// Auto-approve low-risk actions for the agent's own user
polkit.addRule(function(action, subject) {
    if (action.id === "dev.entraclaw.agent.authorize-action" &&
        subject.user === action.lookup("agent_owner")) {
        return polkit.Result.YES;
    }
});
```

### Python: Checking polkit Authorization

```python
import dbus

def check_polkit_authorization(action_id: str, details: dict = None) -> bool:
    """Check if the current process is authorized for a polkit action."""
    bus = dbus.SystemBus()
    proxy = bus.get_object(
        'org.freedesktop.PolicyKit1',
        '/org/freedesktop/PolicyKit1/Authority'
    )
    authority = dbus.Interface(proxy, 'org.freedesktop.PolicyKit1.Authority')

    subject = (
        'unix-process',
        {
            'pid': dbus.UInt32(os.getpid()),
            'start-time': dbus.UInt64(0)  # 0 = look up automatically
        }
    )

    result = authority.CheckAuthorization(
        subject,
        action_id,
        details or {},
        dbus.UInt32(1),   # AllowUserInteraction flag
        ''                # cancellation_id
    )

    is_authorized = result[0]  # Boolean
    return bool(is_authorized)


# Usage
if check_polkit_authorization('dev.entraclaw.agent.authorize-action',
                               {'agent_id': 'agent-001',
                                'action_description': 'send email'}):
    print("Authorized — proceeding")
else:
    print("Denied by user or policy")
```

### Authentication Agents

polkit requires a running **authentication agent** to present prompts:

| Agent | Environment | Notes |
|-------|-------------|-------|
| `polkit-gnome-authentication-agent-1` | GNOME/GTK | GUI dialog |
| `polkit-kde-authentication-agent-1` | KDE | GUI dialog |
| `pkttyagent` | TTY/Headless | Terminal-based password prompt |
| Custom | Any | Can write your own via `PolkitAgentListener` |

**For headless/SSH sessions:**
```bash
# Start a TTY agent in background
pkttyagent --notify-fd 5 --fallback &
```

### Consent UX for Entraclaw

polkit is the **recommended mechanism** for agent consent prompts because:
1. It's designed for exactly this purpose (authorization decisions)
2. It works on both GUI and TTY
3. It integrates with desktop auth agents
4. Rules can be customized per-user, per-group, per-action
5. It supports caching decisions (`auth_self_keep`)

---

## Integration Patterns

### Recommended Architecture for Entraclaw on Linux

```
┌─────────────────────────────────────────────────────────┐
│                     User Session                          │
│                                                           │
│  ┌──────────────────────────────────────────────────┐   │
│  │  systemd --user                                    │   │
│  │                                                    │   │
│  │  ┌────────────────────────────────────┐           │   │
│  │  │  entraclaw-agent.service             │           │   │
│  │  │                                     │           │   │
│  │  │  ┌─────────────────────────┐       │           │   │
│  │  │  │  Credential Layer        │       │           │   │
│  │  │  │  • keyring (long-lived)  │       │           │   │
│  │  │  │  • kernel keyring (temp) │       │           │   │
│  │  │  │  • systemd-creds (boot)  │       │           │   │
│  │  │  └─────────────────────────┘       │           │   │
│  │  │                                     │           │   │
│  │  │  ┌─────────────────────────┐       │           │   │
│  │  │  │  Consent Layer           │       │           │   │
│  │  │  │  • polkit (auth checks)  │       │           │   │
│  │  │  │  • D-Bus notifications   │       │           │   │
│  │  │  └─────────────────────────┘       │           │   │
│  │  └────────────────────────────────────┘           │   │
│  └──────────────────────────────────────────────────┘   │
│                                                           │
│  cgroup: user.slice/user-1000.slice/user@1000.service/    │
│          app.slice/entraclaw-agent.service                  │
└─────────────────────────────────────────────────────────┘
```

### Credential Storage Strategy

```python
"""
Credential storage strategy for Entraclaw on Linux.
Adapts to available backends with graceful fallback.
"""
import os
import sys
from pathlib import Path
from typing import Optional

class LinuxCredentialStore:
    """Multi-tier credential storage with fallback."""

    def __init__(self, app_id: str = "entraclaw"):
        self.app_id = app_id
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        """Detect the best available credential backend."""
        # Tier 1: systemd credentials (if running as a service)
        creds_dir = os.environ.get('CREDENTIALS_DIRECTORY')
        if creds_dir and Path(creds_dir).is_dir():
            return 'systemd-creds'

        # Tier 2: Secret Service (D-Bus)
        try:
            import secretstorage
            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            return 'secret-service'
        except Exception:
            pass

        # Tier 3: Kernel keyring
        try:
            import keyutils
            return 'kernel-keyring'
        except ImportError:
            pass

        # Tier 4: Encrypted file (last resort)
        return 'encrypted-file'

    def store(self, key: str, value: str) -> None:
        if self._backend == 'secret-service':
            self._store_secret_service(key, value)
        elif self._backend == 'kernel-keyring':
            self._store_kernel_keyring(key, value)
        elif self._backend == 'encrypted-file':
            self._store_encrypted_file(key, value)
        # systemd-creds are read-only (injected at service start)

    def retrieve(self, key: str) -> Optional[str]:
        if self._backend == 'systemd-creds':
            return self._read_systemd_cred(key)
        elif self._backend == 'secret-service':
            return self._read_secret_service(key)
        elif self._backend == 'kernel-keyring':
            return self._read_kernel_keyring(key)
        elif self._backend == 'encrypted-file':
            return self._read_encrypted_file(key)
        return None

    def _read_systemd_cred(self, key: str) -> Optional[str]:
        creds_dir = os.environ.get('CREDENTIALS_DIRECTORY', '')
        cred_path = Path(creds_dir) / key
        if cred_path.exists():
            return cred_path.read_text().strip()
        return None

    def _store_secret_service(self, key: str, value: str) -> None:
        import secretstorage
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()
        collection.create_item(
            label=f'Entraclaw: {key}',
            attributes={'application': self.app_id, 'key': key},
            secret=value.encode(),
            replace=True
        )

    def _read_secret_service(self, key: str) -> Optional[str]:
        import secretstorage
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()
        items = list(collection.search_items(
            {'application': self.app_id, 'key': key}
        ))
        if items:
            return items[0].get_secret().decode()
        return None

    def _store_kernel_keyring(self, key: str, value: str) -> None:
        import keyutils
        keyutils.add_key(
            'user',
            f'{self.app_id}:{key}',
            value.encode(),
            keyutils.KEY_SPEC_USER_KEYRING
        )

    def _read_kernel_keyring(self, key: str) -> Optional[str]:
        import keyutils
        try:
            key_id = keyutils.search(
                keyutils.KEY_SPEC_USER_KEYRING,
                'user',
                f'{self.app_id}:{key}'
            )
            return keyutils.read_key(key_id).decode()
        except keyutils.Error:
            return None

    def _store_encrypted_file(self, key: str, value: str) -> None:
        """Fallback: AES-encrypted file with restricted permissions."""
        from cryptography.fernet import Fernet
        store_dir = Path.home() / '.local' / 'share' / self.app_id / 'secrets'
        store_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(store_dir, 0o700)

        key_file = store_dir / '.key'
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
            os.chmod(key_file, 0o600)

        fernet = Fernet(key_file.read_bytes())
        secret_file = store_dir / f'{key}.enc'
        secret_file.write_bytes(fernet.encrypt(value.encode()))
        os.chmod(secret_file, 0o600)

    def _read_encrypted_file(self, key: str) -> Optional[str]:
        from cryptography.fernet import Fernet
        store_dir = Path.home() / '.local' / 'share' / self.app_id / 'secrets'
        key_file = store_dir / '.key'
        secret_file = store_dir / f'{key}.enc'

        if not key_file.exists() or not secret_file.exists():
            return None

        fernet = Fernet(key_file.read_bytes())
        return fernet.decrypt(secret_file.read_bytes()).decode()
```

### Service Deployment Script

```bash
#!/usr/bin/env bash
# deploy-entraclaw-linux.sh — Install Entraclaw as a systemd user service
set -euo pipefail

INSTALL_DIR="$HOME/.local/lib/entraclaw"
CONFIG_DIR="$HOME/.config/entraclaw"
DATA_DIR="$HOME/.local/share/entraclaw"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "=== Installing Entraclaw Agent ==="

# Create directories following XDG Base Directory spec
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR" "$SERVICE_DIR"

# Install Python package
python3 -m pip install --user entraclaw-agent

# Write default config if not present
if [ ! -f "$CONFIG_DIR/agent.toml" ]; then
    cat > "$CONFIG_DIR/agent.toml" <<'EOF'
[agent]
id = ""  # Will be set during registration
log_level = "info"

[credentials]
backend = "auto"  # auto | secret-service | kernel-keyring | encrypted-file

[service]
idle_timeout = 3600
EOF
fi

# Install systemd service
cp "$INSTALL_DIR/share/entraclaw-agent.service" "$SERVICE_DIR/"

# Reload, enable, start
systemctl --user daemon-reload
systemctl --user enable --now entraclaw-agent.service

# Enable linger if possible (may need sudo)
if command -v loginctl &>/dev/null; then
    echo "Enabling linger for $USER (may require sudo)..."
    sudo loginctl enable-linger "$USER" 2>/dev/null || \
        echo "  ⚠ Could not enable linger. Service will stop on logout."
fi

echo "=== Entraclaw Agent installed ==="
echo "  Status: systemctl --user status entraclaw-agent"
echo "  Logs:   journalctl --user -u entraclaw-agent -f"
```

---

## Community Learnings & Gotchas

### Secret Service Quirks

1. **Headless unlock is painful.** GNOME Keyring expects a GUI to display unlock prompts. On headless servers, you must:
   - Configure PAM to auto-unlock at login (`pam_gnome_keyring.so`)
   - Manually start D-Bus and the keyring daemon in shell init scripts
   - Or use `dbus-run-session` to wrap your process
   ```bash
   dbus-run-session -- gnome-keyring-daemon --start --components=secrets
   ```

2. **"Login" keyring password must match login password.** If they diverge (e.g., password changed via `passwd` without updating keyring), the keyring won't auto-unlock. Users see "Enter password to unlock your login keyring" prompts.

3. **D-Bus session bus scoping.** The Secret Service API uses the **session bus**, not the system bus. Each user session has its own bus. A systemd user service has access to the session bus only if `DBUS_SESSION_BUS_ADDRESS` is set (usually automatic with `Type=simple` and linger).

4. **Multiple providers can conflict.** If both GNOME Keyring and KWallet are installed, applications may connect to the wrong one. Check with:
   ```bash
   # Which service owns the Secret Service bus name?
   dbus-send --session --print-reply --dest=org.freedesktop.DBus \
     /org/freedesktop/DBus org.freedesktop.DBus.GetNameOwner \
     string:"org.freedesktop.secrets"
   ```

5. **Attribute search is exact-match only.** The Secret Service spec does not support wildcards or partial matching in `SearchItems`. Design your attribute schema carefully.

6. **`keyring` library silent fallback.** The Python `keyring` library may silently fall back to a **plaintext file backend** (`PlaintextKeyring`) if no Secret Service provider is found. Always check the active backend:
   ```python
   import keyring
   backend = keyring.get_keyring()
   if 'plaintext' in type(backend).__name__.lower():
       raise RuntimeError("Refusing to store secrets in plaintext backend")
   ```

### systemd Pitfalls

1. **`XDG_RUNTIME_DIR` not set.** When accessing user services via `sudo -iu <user>`, `XDG_RUNTIME_DIR` is often unset, causing `systemctl --user` to fail:
   ```bash
   # Fix:
   export XDG_RUNTIME_DIR=/run/user/$(id -u)
   ```

2. **Linger enables everything.** Enabling linger starts **all** enabled user services at boot, not just the one you care about. Audit enabled services with:
   ```bash
   systemctl --user list-unit-files --state=enabled
   ```

3. **Service environment is minimal.** systemd user services don't source `.bashrc` / `.profile`. Explicitly set needed environment variables in the unit file or via `EnvironmentFile=`.

4. **`Restart=always` vs `Restart=on-failure`.** Use `on-failure` for agents — `always` restarts even on clean exit (exit code 0), which can cause restart loops during intentional shutdowns.

5. **Journal persistence.** User journal logs may not persist across reboots unless `/var/log/journal/` exists and has correct permissions. Create it:
   ```bash
   sudo mkdir -p /var/log/journal
   sudo systemd-tmpfiles --create --prefix /var/log/journal
   ```

### Headless Considerations

1. **No Secret Service provider on servers.** Minimal server installs (Ubuntu Server, Alpine, etc.) don't include GNOME Keyring or KWallet. Options:
   - Install `gnome-keyring` (pulls minimal deps, ~5MB)
   - Use kernel keyring only
   - Use `systemd-creds` for service credentials
   - Use encrypted file backend as fallback

2. **D-Bus availability.** Some container environments and minimal installs don't have D-Bus. Check with:
   ```bash
   dbus-send --session --print-reply --dest=org.freedesktop.DBus \
     /org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null
   ```

3. **Wayland vs X11 vs TTY.** polkit authentication agents differ by session type:
   - Wayland/X11: GUI dialogs
   - TTY: `pkttyagent` (requires a controlling terminal)
   - No terminal: Must pre-authorize or use polkit rules for auto-approval

---

## Open Questions

### For the Entraclaw Scenario

1. **Which Secret Service provider to require/recommend?** GNOME Keyring is most common, but should we support headless-only deployments (kernel keyring + encrypted file)?

2. **Agent ID as a systemd credential?** The Agent ID keypair could be injected via `LoadCredentialEncrypted` — tied to the machine via TPM. Is this the right abstraction?

3. **Consent UX on headless servers.** polkit's `pkttyagent` requires a terminal. For SSH-only servers running autonomous agents, what's the consent flow? Options:
   - Pre-approved polkit rules per agent
   - Web-based consent redirect (like OAuth device flow)
   - Email/notification-based approval

4. **Cross-desktop consistency.** Can we abstract away GNOME vs KDE differences entirely, or do we need DE-specific code paths?

5. **Container deployments.** Docker/Podman containers typically lack D-Bus and systemd. Do we need a separate strategy for containerized agents?

6. **Kernel keyring key limits.** The default per-user key quota is 200 keys and 20,000 bytes. For agents managing many credentials, may need to adjust `/proc/sys/kernel/keys/maxkeys` and `/proc/sys/kernel/keys/maxbytes`.

7. **Multi-user agent isolation.** If multiple users run Entraclaw agents, how do we ensure credential isolation? The kernel keyring and Secret Service both scope to the user — this is good. But what about system-level agent services?

8. **Secret rotation coordination.** When a token is rotated, the agent service needs to pick up the new credential. Options:
   - `ExecReload` + `SIGHUP` handler
   - Inotify watch on credential file
   - D-Bus signal from credential manager
   - systemd `LoadCredential` re-exec

---

## Sources

| Source | URL | Notes |
|--------|-----|-------|
| **Secret Service API Spec** | https://specifications.freedesktop.org/secret-service/latest/ | D-Bus interface specification (v0.2 draft) |
| **libsecret Documentation** | https://gnome.pages.gitlab.gnome.org/libsecret/ | Official C library for Secret Service |
| **SecretStorage (Python)** | https://secretstorage.readthedocs.io/ | Pure-Python D-Bus bindings to Secret Service |
| **Python keyring** | https://keyring.readthedocs.io/ | Cross-platform credential storage abstraction |
| **GNOME Keyring — ArchWiki** | https://wiki.archlinux.org/title/GNOME/Keyring | Configuration, PAM integration, troubleshooting |
| **Kernel Key Retention Service** | https://docs.kernel.org/security/keys/core.html | Kernel documentation for KEYS subsystem |
| **keyrings(7) man page** | https://man7.org/linux/man-pages/man7/keyrings.7.html | Keyring types, access control, lifecycle |
| **Cloudflare: Kernel Key Retention** | https://blog.cloudflare.com/the-linux-kernel-key-retention-service-and-why-you-should-use-it-in-your-next-application/ | Practical guide with real-world usage |
| **systemd Credentials** | https://systemd.io/CREDENTIALS/ | Official credential injection documentation |
| **systemd-creds — ArchWiki** | https://wiki.archlinux.org/title/Systemd-creds | Practical tutorial with TPM2 |
| **systemd-creds — Smallstep** | https://smallstep.com/blog/systemd-creds-hardware-protected-secrets/ | "The magic of systemd-creds" tutorial |
| **polkit Reference Manual** | https://www.freedesktop.org/software/polkit/docs/latest/polkit.8.html | Authorization framework documentation |
| **polkit — ArchWiki** | https://wiki.archlinux.org/title/Polkit | Rules, agents, configuration |
| **polkit Python example** | https://github.com/ayasa520/example-policykit | D-Bus service + polkit in Python |
| **python-slip polkit** | https://github.com/nphilipp/python-slip/blob/master/slip/dbus/polkit.py | Python decorator helpers for polkit |
| **Writing polkit apps** | https://www.freedesktop.org/software/polkit/docs/master/polkit-apps.html | Official guide for application developers |
| **PAM Guide** | https://linuxvox.com/blog/pluggable-authentication-module-linux/ | Comprehensive PAM tutorial |
| **systemd User Services** | https://wiki.archlinux.org/title/Systemd/User | User-level service management |
| **python-systemd-tutorial** | https://github.com/torfsen/python-systemd-tutorial | Writing systemd services in Python |
| **Zowe: Headless Credential Storage** | https://docs.zowe.org/stable/user-guide/cli-configure-scs-on-headless-linux-os/ | Practical guide for headless GNOME Keyring |
| **OWASP Secrets Management** | https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html | Industry best practices |
| **credentials(7) man page** | https://linux.die.net/man/7/credentials | Process identity and credential tracking |
| **nsjail** | https://github.com/google/nsjail | Lightweight process isolation tool |
| **Secret Service — NixOS Wiki** | https://wiki.nixos.org/wiki/Secret_Service | Provider comparison and configuration |
| **GitGuardian: Python Secrets** | https://blog.gitguardian.com/how-to-handle-secrets-in-python/ | Python secrets management best practices |
