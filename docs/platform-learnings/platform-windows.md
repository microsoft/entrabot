# Windows Platform APIs

## Overview

Windows provides a layered security model for agent identity and credential management,
built around three core pillars:

1. **Credential Manager + DPAPI** — Secure storage of secrets (tokens, passwords, keys)
   tied to user or machine identity via the Win32 Credential APIs and the Data Protection API.
2. **Windows Services + Task Scheduler** — Background execution as distinct identities
   (LocalSystem, NetworkService, LocalService, or custom service accounts).
3. **UAC + Windows Hello** — Consent prompts and biometric authentication for
   human-in-the-loop authorization flows.

All of these are accessible from Python via `pywin32` (which wraps most Win32 APIs),
`keyring` (cross-platform abstraction over Credential Manager), and `ctypes` (for
anything not directly wrapped).

### Key Python Packages

| Package | Purpose | Install |
|---------|---------|---------|
| `pywin32` | Full Win32 API bindings (`win32cred`, `win32crypt`, `win32security`, `win32service`) | `pip install pywin32` |
| `pywin32-ctypes` | Lightweight ctypes-based subset (no C extensions) | `pip install pywin32-ctypes` |
| `keyring` | Cross-platform credential storage (uses WinCred backend on Windows) | `pip install keyring` |
| `fido2` | FIDO2/WebAuthn support (Windows Hello integration) | `pip install fido2` |

---

## Credential Manager

### Architecture

Windows Credential Manager (`wincred.h`) provides a per-user encrypted vault for storing
credentials. It is the OS-native way to persist secrets like tokens, passwords, and
API keys. Under the hood, credential blobs are encrypted with DPAPI, binding them to
the user's login credentials.

### Credential Types

| Constant | Value | Use Case |
|----------|-------|----------|
| `CRED_TYPE_GENERIC` | 1 | Application-defined secrets (tokens, API keys) — **this is what Entrabot should use** |
| `CRED_TYPE_DOMAIN_PASSWORD` | 2 | Windows domain authentication (NTLM/Kerberos) |
| `CRED_TYPE_DOMAIN_CERTIFICATE` | 3 | Certificate-based domain auth |
| `CRED_TYPE_DOMAIN_VISIBLE_PASSWORD` | 4 | Passport/Live ID |

### Persistence Levels

| Constant | Behavior |
|----------|----------|
| `CRED_PERSIST_SESSION` | Deleted on logoff — good for ephemeral tokens |
| `CRED_PERSIST_LOCAL_MACHINE` | Survives reboots, bound to this machine |
| `CRED_PERSIST_ENTERPRISE` | Roams with domain profile (requires AD roaming profiles) |

Use `CredGetSessionTypes()` to query which persistence levels are available on a given
system for each credential type.

### Win32 API (C)

```c
#include <windows.h>
#include <wincred.h>
#pragma comment(lib, "Advapi32.lib")

// --- Store a credential ---
void StoreCredential(void) {
    CREDENTIALW cred = {0};
    cred.Type = CRED_TYPE_GENERIC;
    cred.TargetName = L"Entrabot/AgentToken";
    cred.UserName = L"agent-abc123";
    const char* token = "eyJhbGciOiJSUzI1NiIs...";
    cred.CredentialBlobSize = (DWORD)strlen(token);
    cred.CredentialBlob = (LPBYTE)token;
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE;

    if (!CredWriteW(&cred, 0)) {
        printf("CredWrite failed: %d\n", GetLastError());
    }
}

// --- Read a credential ---
void ReadCredential(void) {
    PCREDENTIALW pCred = NULL;
    if (CredReadW(L"Entrabot/AgentToken", CRED_TYPE_GENERIC, 0, &pCred)) {
        printf("User: %ws\n", pCred->UserName);
        printf("Token: %.*s\n", pCred->CredentialBlobSize, pCred->CredentialBlob);
        CredFree(pCred);
    }
}

// --- Delete a credential ---
void DeleteCredential(void) {
    CredDeleteW(L"Entrabot/AgentToken", CRED_TYPE_GENERIC, 0);
}
```

### Python via `win32cred` (pywin32)

```python
import win32cred

# --- Store ---
target = "Entrabot/AgentToken"
token = "eyJhbGciOiJSUzI1NiIs..."

credential = {
    'Type': win32cred.CRED_TYPE_GENERIC,
    'TargetName': target,
    'UserName': 'agent-abc123',
    'CredentialBlob': token,
    'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
    'Comment': 'Entrabot agent OBO token',
}
win32cred.CredWrite(credential, 0)

# --- Read ---
cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
token = cred['CredentialBlob'].decode('utf-16-le')
print(f"Token: {token}")

# --- Delete ---
win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)

# --- Enumerate all generic credentials ---
creds = win32cred.CredEnumerate(Filter=None, Flags=0)
for c in creds:
    if c['Type'] == win32cred.CRED_TYPE_GENERIC:
        print(f"  {c['TargetName']}: {c['UserName']}")
```

### Python via `pywin32-ctypes` (lighter weight, no C extensions)

```python
import win32ctypes.pywin32.win32cred as win32cred

target = "Entrabot/AgentToken"
token = "eyJhbGciOiJSUzI1NiIs..."

# Store — note: CredentialBlob must be bytes in UTF-16LE
credential = {
    'Type': win32cred.CRED_TYPE_GENERIC,
    'TargetName': target,
    'UserName': 'agent-abc123',
    'CredentialBlob': token.encode('utf-16-le'),
    'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
}
win32cred.CredWrite(credential, 0)

# Read
cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
retrieved = cred['CredentialBlob'].decode('utf-16-le')
```

### Python via `keyring` (cross-platform — recommended for portable code)

```python
import keyring

# Store
keyring.set_password("entrabot", "agent-abc123", "eyJhbGciOiJSUzI1NiIs...")

# Retrieve
token = keyring.get_password("entrabot", "agent-abc123")

# Delete
keyring.delete_password("entrabot", "agent-abc123")
```

On Windows, `keyring` uses `WinVaultKeyring` backend by default, which maps to the
same Credential Manager APIs. Service name → TargetName, username → UserName.

**Tradeoff:** `keyring` is the simplest and most portable, but `win32cred` gives access
to metadata fields (Comment, Attributes, LastWritten) and persistence control that
`keyring` abstracts away.

---

## DPAPI (Data Protection API)

### Architecture

DPAPI provides symmetric encryption tied to Windows user credentials. It is the
mechanism that Credential Manager itself uses under the hood. Direct DPAPI usage is
useful when you need to encrypt arbitrary data (config files, local caches, etc.)
beyond what fits in a credential blob.

### Scopes

| Scope | Flag | Who Can Decrypt |
|-------|------|-----------------|
| **CurrentUser** | `0` (default) | Only the same Windows user on the same machine |
| **LocalMachine** | `CRYPTPROTECT_LOCAL_MACHINE` (0x4) | Any user/process on the same machine |

For Entrabot agents, **CurrentUser scope is strongly preferred** — it ensures that
even a local admin on the machine cannot trivially decrypt the agent's secrets without
impersonating the specific user.

### How It Works

1. Each user has a **master key** derived from their Windows password, stored in
   `%APPDATA%\Microsoft\Protect\{SID}\`.
2. `CryptProtectData()` generates a session key, encrypts data, and wraps the session
   key with the master key.
3. `CryptUnprotectData()` reverses the process — requires the same user context.
4. In Active Directory environments, a **domain backup key** on the DC can recover
   any user's master key. If this backup key is compromised, all DPAPI secrets in the
   domain are at risk.

### Win32 API (C)

```c
#include <windows.h>
#include <dpapi.h>
#pragma comment(lib, "Crypt32.lib")

BOOL EncryptSecret(const BYTE* data, DWORD dataLen, DATA_BLOB* out) {
    DATA_BLOB input = { dataLen, (BYTE*)data };
    DATA_BLOB entropy = { 8, (BYTE*)"entrabot" };  // optional extra entropy

    return CryptProtectData(
        &input,
        L"Entrabot Agent Secret",  // description (stored in cleartext!)
        &entropy,                   // optional entropy
        NULL,                       // reserved
        NULL,                       // prompt struct (NULL = no UI)
        0,                          // flags (0 = CurrentUser scope)
        out                         // output blob
    );
}

BOOL DecryptSecret(DATA_BLOB* encrypted, DATA_BLOB* out) {
    DATA_BLOB entropy = { 8, (BYTE*)"entrabot" };
    LPWSTR description = NULL;

    BOOL result = CryptUnprotectData(
        encrypted, &description, &entropy,
        NULL, NULL, 0, out
    );
    if (description) LocalFree(description);
    return result;
}
```

### Python via `win32crypt` (pywin32)

```python
import win32crypt

# --- Encrypt (CurrentUser scope) ---
plaintext = b'{"access_token": "eyJ...", "refresh_token": "dGhpcyBpcyBh..."}'
entropy = b"entrabot-agent"

# CryptProtectData returns (description, encrypted_bytes)
desc, encrypted = win32crypt.CryptProtectData(
    plaintext,
    "Entrabot Agent Config",  # description (stored in cleartext)
    entropy,                   # optional entropy
    None,                      # reserved
    None,                      # prompt struct
    0                          # flags: 0 = CurrentUser scope
)

# --- Decrypt ---
desc, decrypted = win32crypt.CryptUnprotectData(
    encrypted,
    entropy,   # must match what was used for encryption
    None,      # reserved
    None,      # prompt struct
    0          # flags
)
print(decrypted.decode('utf-8'))

# --- Machine-scope encryption (any user on this machine can decrypt) ---
CRYPTPROTECT_LOCAL_MACHINE = 0x4
desc, encrypted_machine = win32crypt.CryptProtectData(
    plaintext, "Shared Secret", None, None, None,
    CRYPTPROTECT_LOCAL_MACHINE
)
```

### DPAPI + File Storage Pattern

For data too large for Credential Manager (e.g., cached tokens, agent config):

```python
import win32crypt
import os

CONFIG_PATH = os.path.join(os.environ['LOCALAPPDATA'], 'Entrabot', 'agent_config.enc')

def save_encrypted(data: bytes, path: str):
    _, encrypted = win32crypt.CryptProtectData(data, "Entrabot", b"entropy", None, None, 0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(encrypted)

def load_decrypted(path: str) -> bytes:
    with open(path, 'rb') as f:
        encrypted = f.read()
    _, decrypted = win32crypt.CryptUnprotectData(encrypted, b"entropy", None, None, 0)
    return decrypted
```

---

## Windows Services

### Service Identity Options

| Identity | Local Privilege | Network Auth | DPAPI Scope | Best For |
|----------|----------------|-------------|-------------|----------|
| **LocalSystem** | Highest (root-equivalent) | Machine credentials | Machine-scope keys (weak protection) | Avoid unless absolutely necessary |
| **NetworkService** | Low | Machine credentials (COMPUTERNAME$) | Has own profile, but limited | Network-facing services |
| **LocalService** | Lowest | Anonymous | Has own profile, limited | Local-only services |
| **Custom user** | Configurable | User's credentials | Full user-scope DPAPI | **Recommended for Entrabot** |

**Recommendation for Entrabot:** Create a dedicated `entrabot-agent` user account with
minimal privileges. This gives the agent its own DPAPI master key, its own Credential
Manager vault, and proper identity isolation.

### Python Windows Service with pywin32

```python
import win32serviceutil
import win32service
import win32event
import servicemanager
import time
import os

class EntrabotAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = 'EntrabotAgent'
    _svc_display_name_ = 'Entrabot Autonomous Agent'
    _svc_description_ = 'Background service for the Entrabot autonomous agent.'

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.running = True

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        self.running = False

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        self.main()

    def main(self):
        """Main agent loop."""
        log_dir = os.path.join(os.environ.get('LOCALAPPDATA', 'C:\\'), 'Entrabot')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'agent.log')

        while self.running:
            # Agent work happens here
            with open(log_path, 'a') as f:
                f.write(f'Agent heartbeat: {time.ctime()}\n')

            # Wait for stop signal or timeout (10 seconds)
            result = win32event.WaitForSingleObject(self.stop_event, 10000)
            if result == win32event.WAIT_OBJECT_0:
                break

if __name__ == '__main__':
    win32serviceutil.HandleCommandLine(EntrabotAgentService)
```

### Service Lifecycle Commands

```powershell
# Install the service
python entrabot_service.py install

# Set to run as a specific user (recommended)
sc.exe config EntrabotAgent obj= ".\entrabot-agent" password= "SecurePassword"

# Start / Stop / Remove
python entrabot_service.py start
python entrabot_service.py stop
python entrabot_service.py remove

# Debug mode (runs in console for development)
python entrabot_service.py debug
```

### Alternative: NSSM (Non-Sucking Service Manager)

NSSM wraps any executable (including Python scripts) as a Windows service without
requiring pywin32 service code. This is simpler but less integrated.

```powershell
# Install (from admin shell)
nssm install EntrabotAgent "C:\path\to\venv\Scripts\python.exe" "C:\path\to\agent.py"

# Configure working directory
nssm set EntrabotAgent AppDirectory "C:\path\to\project"

# Configure logging
nssm set EntrabotAgent AppStdout "C:\path\to\logs\stdout.log"
nssm set EntrabotAgent AppStderr "C:\path\to\logs\stderr.log"

# Set service account
nssm set EntrabotAgent ObjectName ".\entrabot-agent"

# Auto-start on boot
nssm set EntrabotAgent Start SERVICE_AUTO_START

# Set environment variables
nssm set EntrabotAgent AppEnvironmentExtra ENTRABOT_ENV=production

# Control
nssm start EntrabotAgent
nssm stop EntrabotAgent
nssm restart EntrabotAgent
nssm edit EntrabotAgent     # Opens GUI for configuration
nssm remove EntrabotAgent
```

**NSSM advantages:** Auto-restart on failure, stdout/stderr capture, GUI editor,
no code changes needed. **Disadvantages:** External dependency, less control over
service events (pause, custom commands).

---

## Task Scheduler

### When to Use Task Scheduler vs. Services

| Aspect | Windows Service | Task Scheduler |
|--------|----------------|----------------|
| **Execution model** | Always running (daemon) | Triggered by schedule/event |
| **User session** | Runs without user login | Can run without login (with config) |
| **Credential access** | Service account's vault | Logged-in user's vault (or specified user) |
| **Restart on failure** | Built-in recovery options | Limited retry options |
| **Best for** | Persistent agents, message queues | Periodic tasks, token refresh, cleanup |

For Entrabot, a **hybrid approach** works well:
- **Service** for the main agent loop (always running, processing tasks)
- **Scheduled task** for periodic maintenance (token refresh, log rotation)

### Creating Tasks via schtasks.exe

```powershell
# Run a Python script every hour under the current user
schtasks /create /tn "Entrabot\TokenRefresh" /tr "C:\path\to\venv\Scripts\python.exe C:\path\to\refresh_token.py" /sc hourly /ru "%USERNAME%" /rl HIGHEST

# Run at system startup (requires admin)
schtasks /create /tn "Entrabot\AgentStart" /tr "C:\path\to\start_agent.py" /sc onstart /ru "entrabot-agent" /rp "Password" /rl HIGHEST

# Run when user logs on
schtasks /create /tn "Entrabot\AgentUserStart" /tr "C:\path\to\agent.py" /sc onlogon

# Query task status
schtasks /query /tn "Entrabot\TokenRefresh" /fo LIST /v

# Delete a task
schtasks /delete /tn "Entrabot\TokenRefresh" /f
```

### Creating Tasks via PowerShell (more control)

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\path\to\venv\Scripts\python.exe" `
    -Argument "C:\path\to\refresh_token.py" `
    -WorkingDirectory "C:\path\to\project"

$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours 1) -Once -At (Get-Date)

$principal = New-ScheduledTaskPrincipal `
    -UserId "entrabot-agent" `
    -LogonType ServiceAccount `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName "Entrabot\TokenRefresh" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Periodic token refresh for Entrabot agent"
```

### Python Task Scheduler via COM API

```python
import win32com.client

scheduler = win32com.client.Dispatch('Schedule.Service')
scheduler.Connect()
root = scheduler.GetFolder('\\')

# Create a new task
task_def = scheduler.NewTask(0)
task_def.RegistrationInfo.Description = 'Entrabot token refresh'

# Create trigger (daily at 3 AM)
trigger = task_def.Triggers.Create(2)  # 2 = TASK_TRIGGER_DAILY
trigger.StartBoundary = '2025-01-01T03:00:00'
trigger.DaysInterval = 1

# Create action
action = task_def.Actions.Create(0)  # 0 = TASK_ACTION_EXEC
action.Path = r'C:\path\to\venv\Scripts\python.exe'
action.Arguments = r'C:\path\to\refresh_token.py'
action.WorkingDirectory = r'C:\path\to\project'

# Register (TASK_CREATE_OR_UPDATE=6, TASK_LOGON_PASSWORD=1)
root.RegisterTaskDefinition(
    'Entrabot\\TokenRefresh',
    task_def,
    6,                    # TASK_CREATE_OR_UPDATE
    'entrabot-agent',     # user
    'password',           # password
    1                     # TASK_LOGON_PASSWORD
)
```

---

## SSPI (Security Support Provider Interface)

### Architecture

SSPI is Microsoft's implementation of GSSAPI — a pluggable authentication framework
that abstracts protocol details (Kerberos, NTLM, etc.) behind a common API. It is the
mechanism Windows uses for all integrated authentication.

### Security Support Providers (SSPs)

| SSP | Protocol | When Used | Strength |
|-----|----------|-----------|----------|
| **Kerberos** | Kerberos v5 (RFC 4120) | Domain-joined machines, Active Directory | Strong: mutual auth, delegation, tickets |
| **NTLM** | Challenge-response | Legacy/workgroup, fallback | Moderate: no mutual auth, replay-vulnerable |
| **Negotiate** | SPNEGO (RFC 2478) | Default in most Windows auth | Auto-selects Kerberos → NTLM fallback |
| **Schannel** | TLS/SSL | HTTPS, encrypted channels | Transport-level security |
| **CredSSP** | Kerberos + TLS | RDP, WinRM, PowerShell remoting | Delegation with encryption |

### How Negotiate Works

1. Application requests authentication using the "Negotiate" package.
2. SSPI examines the target (SPN, network context) and tries Kerberos first.
3. If Kerberos is unavailable (no domain, no SPN, workgroup), falls back to NTLM.
4. The application never needs to know which protocol was selected.

### Relevance to Entrabot

SSPI is primarily relevant for:
- **Agent-to-agent authentication** in enterprise/AD environments
- **Service-to-service auth** where Entrabot agents need to call Windows-authenticated APIs
- **OBO (On-Behalf-Of) token flows** — Kerberos delegation (`S4U2Proxy`) allows a
  service to act on behalf of a user

### Python SSPI Access

```python
import sspi
import sspicon

# Create a client security context (Negotiate = auto Kerberos/NTLM)
client_auth = sspi.ClientAuth("Negotiate", targetspn="HTTP/server.domain.com")

# Generate the initial auth token
err, buffers = client_auth.authorize(None)
token = buffers[0].Buffer  # Send this to the server

# Server side
server_auth = sspi.ServerAuth("Negotiate")
err, buffers = server_auth.authorize(token)
# After completion, server_auth.ctxt contains the authenticated identity
```

The `sspi` module is part of `pywin32`. For HTTP-based auth, libraries like `requests-negotiate-sspi`
or `requests-kerberos` handle SSPI integration automatically.

---

## UAC & Consent

### Architecture

User Account Control (UAC) creates a split-token model: even administrator accounts
run with standard-user privileges by default. Elevation to full admin requires an
explicit consent prompt.

### Elevation Flow

```
Application requests admin privileges
    → CreateProcess returns ERROR_ELEVATION_REQUIRED
    → ShellExecuteEx with "runas" verb triggers AppInfo service
    → AppInfo displays consent/credential prompt on Secure Desktop
    → User consents → new process created with full admin token
    → User denies → ERROR_CANCELLED returned
```

### Consent Prompt Behaviors (Group Policy)

| Setting | Effect | Security |
|---------|--------|----------|
| Prompt for consent on secure desktop | Dimmed screen, UAC dialog only | **Recommended** (default) |
| Prompt for credentials on secure desktop | Must enter admin password | Highest security |
| Prompt for consent (no secure desktop) | Normal dialog box | Lower (malware can click) |
| Elevate without prompting | No UI at all | **Dangerous** — never use |

Registry: `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\ConsentPromptBehaviorAdmin`

### Programmatic Elevation (Python)

```python
import ctypes
import sys
import subprocess

def is_elevated() -> bool:
    """Check if the current process has admin privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def run_elevated(script_path: str, args: str = ""):
    """Re-launch a Python script with elevation (triggers UAC prompt)."""
    # ShellExecuteEx with "runas" verb
    ctypes.windll.shell32.ShellExecuteW(
        None,          # parent window
        "runas",       # verb — triggers UAC
        sys.executable,  # application
        f'"{script_path}" {args}',  # arguments
        None,          # working directory
        1              # SW_SHOWNORMAL
    )

# Usage pattern for Entrabot consent
if not is_elevated():
    print("Agent needs elevated privileges for initial setup.")
    run_elevated(__file__, "--setup")
    sys.exit(0)
else:
    print("Running with admin privileges — performing setup...")
```

### Application Manifest for Auto-Elevation

For compiled executables, embed a manifest requesting elevation:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <!-- Options: asInvoker | highestAvailable | requireAdministrator -->
        <requestedExecutionLevel level="highestAvailable" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
</assembly>
```

### Entrabot Consent Model

UAC is **not** suitable for ongoing agent consent (it's designed for one-shot elevation).
For Entrabot, the consent flow should be:

1. **Installation** — One-time UAC elevation to install the service and create the
   service account.
2. **Ongoing consent** — Use a custom UI (tray app, toast notification) rather than
   UAC. Windows Toast Notifications API can present approve/deny actions.
3. **Sensitive operations** — Trigger Windows Hello biometric prompt (see below).

---

## Windows Hello

### Architecture

Windows Hello provides biometric (fingerprint, face) and PIN-based authentication,
exposed through the WebAuthn/FIDO2 APIs. It acts as a **platform authenticator** —
the biometric check happens locally, and the result is a cryptographic assertion.

### Integration Model

Windows Hello is accessed through the **WebAuthn API**, not directly. The flow is:

```
Agent requests user consent
    → Agent's local web UI calls navigator.credentials.get() (browser)
    → OR agent calls WebAuthNAuthenticatorGetAssertion (Win32 native)
    → Windows Hello prompt appears (face/fingerprint/PIN)
    → Returns signed assertion (not biometric data)
    → Agent verifies assertion server-side
```

### Native Win32 WebAuthn API

Windows 10 1903+ provides `webauthn.h`:

```c
#include <webauthn.h>
#pragma comment(lib, "WebAuthn.lib")

// Check if WebAuthn is available
DWORD apiVersion = WebAuthNGetApiVersionNumber();
BOOL available = WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable(&pbAvailable);

// Request assertion (simplified)
WEBAUTHN_AUTHENTICATOR_GET_ASSERTION_OPTIONS options = {
    .dwVersion = WEBAUTHN_AUTHENTICATOR_GET_ASSERTION_OPTIONS_VERSION_1,
    .dwTimeoutMilliseconds = 30000,
    .dwUserVerificationRequirement = WEBAUTHN_USER_VERIFICATION_REQUIREMENT_REQUIRED
};
```

### Python Integration via FIDO2

```python
from fido2.server import Fido2Server
from fido2.webauthn import (
    PublicKeyCredentialRpEntity,
    UserVerificationRequirement,
)

rp = PublicKeyCredentialRpEntity(name="Entrabot Agent", id="localhost")
server = Fido2Server(rp)

# Registration (one-time setup)
registration_data, state = server.register_begin(
    user={"id": b"agent-abc123", "name": "entrabot-agent", "displayName": "Entrabot Agent"},
    credentials=[],
    user_verification=UserVerificationRequirement.REQUIRED,
)
# Client (browser) handles the Windows Hello prompt and returns attestation

# Authentication (each consent request)
auth_data, state = server.authenticate_begin(
    credentials=registered_credentials,
    user_verification=UserVerificationRequirement.REQUIRED,
)
# Client triggers Windows Hello → returns assertion → server.authenticate_complete()
```

### Practical Considerations for Entrabot

- Windows Hello requires a **browser or native WebAuthn caller** — it cannot be
  triggered from a pure CLI/headless Python process.
- For a headless agent, the pattern is:
  1. Agent runs as a background service.
  2. When consent is needed, agent notifies a **tray application** (user-session process).
  3. Tray app opens a local web page that triggers WebAuthn.
  4. Result is passed back to the agent service via IPC (named pipe, localhost HTTP).
- Alternative: Use the Windows Security Center API for simpler PIN verification
  without the full WebAuthn ceremony.

---

## Windows Security Tokens & Process Identity

### Access Tokens

Every Windows process has a **primary access token** that defines its security identity:

- **User SID** — The security identifier of the user/account
- **Group SIDs** — All groups the user belongs to
- **Privileges** — Specific rights (e.g., `SeBackupPrivilege`, `SeDebugPrivilege`)
- **Default DACL** — Permissions applied to objects created by this process
- **Token type** — Primary (process) or Impersonation (thread)

### Impersonation

A thread can temporarily assume another user's identity via an **impersonation token**.
This is how services act on behalf of users:

```python
import win32security
import win32con
import win32api

# Log on as a specific user
handle = win32security.LogonUser(
    'entrabot-agent',                    # username
    '.',                                  # domain (. = local)
    'password',                           # password
    win32con.LOGON32_LOGON_INTERACTIVE,   # logon type
    win32con.LOGON32_PROVIDER_DEFAULT     # logon provider
)

# Impersonate that user (current thread now runs as entrabot-agent)
win32security.ImpersonateLoggedOnUser(handle)
print(f"Now running as: {win32api.GetUserName()}")

# Do work that requires entrabot-agent's credentials/DPAPI keys...

# Revert to original identity
win32security.RevertToSelf()
handle.Close()
```

### Token Queries

```python
import win32security
import win32api
import win32process

# Get current process token
token = win32security.OpenProcessToken(
    win32api.GetCurrentProcess(),
    win32con.TOKEN_QUERY
)

# Get the user SID from the token
user_sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
account, domain, type = win32security.LookupAccountSid(None, user_sid)
print(f"Running as: {domain}\\{account}")

# Get privileges
privileges = win32security.GetTokenInformation(token, win32security.TokenPrivileges)
for priv_luid, priv_attr in privileges:
    name = win32security.LookupPrivilegeName(None, priv_luid)
    enabled = bool(priv_attr & win32security.SE_PRIVILEGE_ENABLED)
    print(f"  {name}: {'enabled' if enabled else 'disabled'}")
```

---

## Integration Patterns

### Recommended Entrabot Deployment on Windows

```
┌─────────────────────────────────────────────────────┐
│                   Windows Machine                    │
│                                                      │
│  ┌──────────────────────┐  ┌──────────────────────┐ │
│  │   Entrabot Service   │  │   Entrabot Tray App  │ │
│  │   (Background)       │  │   (User Session)     │ │
│  │                      │  │                      │ │
│  │  • Runs as dedicated │  │  • Shows status icon │ │
│  │    service account   │  │  • Displays consent  │ │
│  │  • Processes tasks   │  │    prompts           │ │
│  │  • Manages agent ID  │  │  • Triggers Windows  │ │
│  │                      │  │    Hello for auth    │ │
│  │  Credentials:        │  │  • Toast notifs for  │ │
│  │  • Credential Mgr    │  │    approvals         │ │
│  │  • DPAPI-encrypted   │  │                      │ │
│  │    config files      │  │  IPC: Named Pipes    │ │
│  │                      │  │  or localhost HTTP   │ │
│  └──────────┬───────────┘  └──────────┬───────────┘ │
│             │                         │              │
│             └─────────┬───────────────┘              │
│                       │                              │
│              ┌────────▼────────┐                     │
│              │ Credential Mgr  │                     │
│              │ (WinCred API)   │                     │
│              │                 │                     │
│              │ Agent tokens,   │                     │
│              │ OBO tokens,     │                     │
│              │ refresh tokens  │                     │
│              └─────────────────┘                     │
└─────────────────────────────────────────────────────┘
```

### Implementation Checklist

#### 1. Credential Storage Layer

```python
# credential_store_windows.py — Entrabot credential abstraction

import platform
if platform.system() != 'Windows':
    raise ImportError("This module is Windows-only")

import win32cred
import win32crypt
import json
import os

CREDENTIAL_PREFIX = "Entrabot"

class WindowsCredentialStore:
    """Windows-native credential storage using Credential Manager + DPAPI."""

    def store_token(self, agent_id: str, token_data: dict) -> None:
        """Store an agent token in Credential Manager."""
        target = f"{CREDENTIAL_PREFIX}/{agent_id}/token"
        blob = json.dumps(token_data)
        credential = {
            'Type': win32cred.CRED_TYPE_GENERIC,
            'TargetName': target,
            'UserName': agent_id,
            'CredentialBlob': blob,
            'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
            'Comment': f'Entrabot agent token for {agent_id}',
        }
        win32cred.CredWrite(credential, 0)

    def get_token(self, agent_id: str) -> dict | None:
        """Retrieve an agent token from Credential Manager."""
        target = f"{CREDENTIAL_PREFIX}/{agent_id}/token"
        try:
            cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
            blob = cred['CredentialBlob'].decode('utf-16-le')
            return json.loads(blob)
        except Exception:
            return None

    def delete_token(self, agent_id: str) -> None:
        """Remove an agent token from Credential Manager."""
        target = f"{CREDENTIAL_PREFIX}/{agent_id}/token"
        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception:
            pass

    def store_encrypted_config(self, agent_id: str, config: dict) -> None:
        """Store larger config data as DPAPI-encrypted file."""
        config_dir = os.path.join(
            os.environ.get('LOCALAPPDATA', ''),
            'Entrabot', agent_id
        )
        os.makedirs(config_dir, exist_ok=True)
        path = os.path.join(config_dir, 'config.enc')

        plaintext = json.dumps(config).encode('utf-8')
        _, encrypted = win32crypt.CryptProtectData(
            plaintext, f"Entrabot:{agent_id}", None, None, None, 0
        )
        with open(path, 'wb') as f:
            f.write(encrypted)

    def load_encrypted_config(self, agent_id: str) -> dict | None:
        """Load DPAPI-encrypted config."""
        path = os.path.join(
            os.environ.get('LOCALAPPDATA', ''),
            'Entrabot', agent_id, 'config.enc'
        )
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            encrypted = f.read()
        _, decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        return json.loads(decrypted.decode('utf-8'))
```

#### 2. Service Installation Script

```python
# install_service.py — One-time setup requiring UAC elevation

import ctypes
import sys
import subprocess
import os

def is_admin():
    return ctypes.windll.shell32.IsUserAnAdmin() != 0

def create_service_account():
    """Create a dedicated Windows user for the agent service."""
    import secrets
    password = secrets.token_urlsafe(32)

    # Create user
    subprocess.run([
        'net', 'user', 'entrabot-agent', password,
        '/add', '/comment:"Entrabot Agent Service Account"',
        '/passwordchg:no', '/expires:never'
    ], check=True)

    # Grant "Log on as a service" right
    # (Requires ntrights.exe or secedit — simplified here)
    print(f"Service account created. Password stored in Credential Manager.")
    return password

def install():
    if not is_admin():
        # Re-launch with elevation
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{__file__}"', None, 1
        )
        return

    password = create_service_account()

    # Install the service
    subprocess.run([
        sys.executable, 'entrabot_service.py', 'install',
        '--username', '.\\entrabot-agent',
        '--password', password,
        '--startup', 'auto',
    ], check=True)

    print("Entrabot agent service installed successfully.")

if __name__ == '__main__':
    install()
```

#### 3. Tray Application for Consent

```python
# tray_app.py — System tray application for user consent
# Requires: pip install pystray pillow

import pystray
from PIL import Image, ImageDraw
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class ConsentHandler(BaseHTTPRequestHandler):
    """Local HTTP server for consent requests from the agent service."""

    def do_POST(self):
        if self.path == '/consent':
            length = int(self.headers['Content-Length'])
            body = json.loads(self.rfile.read(length))

            # Show consent dialog (Windows toast notification or dialog)
            approved = show_consent_dialog(
                action=body['action'],
                resource=body['resource'],
                agent_id=body['agent_id'],
            )

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'approved': approved}).encode())

def show_consent_dialog(action: str, resource: str, agent_id: str) -> bool:
    """Show a Windows dialog for user consent."""
    import ctypes
    result = ctypes.windll.user32.MessageBoxW(
        0,
        f"Entrabot agent '{agent_id}' wants to:\n\n"
        f"Action: {action}\n"
        f"Resource: {resource}\n\n"
        f"Allow this action?",
        "Entrabot Agent Consent",
        0x00000004 | 0x00000030  # MB_YESNO | MB_ICONWARNING
    )
    return result == 6  # IDYES

def create_tray_icon():
    # Create a simple icon
    img = Image.new('RGB', (64, 64), color='blue')
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, 48, 48], fill='white')

    icon = pystray.Icon(
        "entrabot",
        img,
        "Entrabot Agent",
        menu=pystray.Menu(
            pystray.MenuItem("Status: Running", lambda: None),
            pystray.MenuItem("View Logs", lambda: None),
            pystray.MenuItem("Quit", lambda icon, _: icon.stop()),
        )
    )
    return icon

if __name__ == '__main__':
    # Start consent HTTP server in background
    server = HTTPServer(('127.0.0.1', 19876), ConsentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Run tray icon (blocks)
    icon = create_tray_icon()
    icon.run()
```

---

## Community Learnings & Gotchas

### DPAPI Scope Issues

- **Problem:** DPAPI secrets are bound to the user's Windows password. If an admin
  resets the user's password (vs. the user changing it themselves), the DPAPI master
  key is lost and all encrypted data becomes unrecoverable.
- **Mitigation:** In AD environments, the domain backup key can recover master keys.
  For local accounts, there is no recovery. Always maintain a separate backup of
  critical secrets.

- **Problem:** Service accounts (SYSTEM, NetworkService) have DPAPI keys, but they're
  derived from machine secrets rather than passwords, making them weaker.
- **Mitigation:** Use a dedicated user account for the agent service, not a built-in
  service account.

### Credential Manager Limits

- **CredentialBlob size:** Maximum is **512 bytes** for `CRED_TYPE_GENERIC` credentials
  on older Windows versions (2560 bytes on newer). Long tokens may need to be split
  or stored via DPAPI files instead.
- **TargetName uniqueness:** TargetName + Type must be unique per user. Use a
  consistent naming convention: `Entrabot/{agent_id}/{purpose}`.

### Service Account Quirks

- Services running as `LocalSystem` cannot access the interactive user's Credential
  Manager vault — they have their own (mostly empty) vault.
- Services don't get a desktop session by default. Any UI (dialogs, console output)
  requires "Allow service to interact with desktop" (deprecated) or a separate
  user-session process.
- The service's `%USERPROFILE%` and `%APPDATA%` point to `C:\Windows\system32\config\systemprofile`
  when running as SYSTEM, not the user's actual profile.

### Credential Isolation

- Each Windows user has their own isolated Credential Manager vault. A credential
  stored by User A is invisible to User B, even if B is an administrator.
- **Exception:** LocalMachine-scope DPAPI allows any local process to decrypt. Use
  CurrentUser scope for agent secrets.

### Attack Surface

- **Mimikatz** and similar tools can extract DPAPI master keys with local admin access.
  DPAPI is defense-in-depth, not an absolute barrier.
- **Credential Guard** (Windows 10 Enterprise) isolates DPAPI keys in a VSM (Virtual
  Secure Mode) enclave, significantly hardening against extraction.
- The AD domain backup key is a **single point of compromise** — if leaked, all
  domain-joined DPAPI secrets are exposed. Microsoft recommends abandoning the domain
  in this scenario (no supported key rotation).

### Python-Specific Issues

- `pywin32` requires a matching Python architecture (x86 vs x64) and version.
- `pywin32-ctypes` is lighter but doesn't wrap all APIs (no `win32service`,
  `win32security`, `sspi`).
- The `keyring` library may use `WinVaultKeyring` or `Windows Credential Locker`
  depending on the Windows version — test on target platforms.
- When encoding credential blobs: `pywin32`'s `CredWrite` handles strings directly,
  but `pywin32-ctypes` requires explicit UTF-16LE encoding.

---

## Open Questions

1. **Service vs. scheduled task for token refresh?** A service is always-on but
   consumes resources. A scheduled task is lighter but has delayed startup. For an
   autonomous agent, the service model is likely required, with periodic token refresh
   on a timer within the service.

2. **Credential Manager vs. DPAPI file for OBO tokens?** Credential Manager is
   simpler but has size limits. DPAPI files handle arbitrary data. Consider: tokens
   in Credential Manager (small, high-value), config/cache in DPAPI files.

3. **How to handle Windows password resets?** If the domain admin resets the service
   account's password, DPAPI master keys are lost. Options: (a) store a backup key
   in the cloud, (b) use machine-scope DPAPI for the most critical bootstrap secret,
   (c) implement a re-enrollment flow.

4. **Consent UX without a browser?** Windows Hello requires WebAuthn (browser-based)
   or the native `webauthn.dll` API. For a pure-desktop consent flow, consider using
   `MessageBoxW` for simple approve/deny, with Windows Hello reserved for
   high-security operations where a browser component is acceptable.

5. **Cross-platform credential abstraction?** The `keyring` library provides the
   best cross-platform API (macOS Keychain on Mac, SecretService on Linux, WinCred
   on Windows), but loses access to platform-specific features. Consider `keyring`
   as the default with platform-specific backends for advanced features.

6. **Credential Guard compatibility?** If the target machine has Credential Guard
   enabled, does it affect DPAPI access for service accounts? Need to test on
   Enterprise SKUs.

7. **MSA/gMSA for service identity?** Group Managed Service Accounts (gMSA)
   provide automatic password rotation and are the enterprise best practice for
   service identity in AD environments. Investigate whether the Entrabot agent service
   can run as a gMSA.

---

## Sources

### Microsoft Documentation
- [Credential Manager API (wincred.h)](https://learn.microsoft.com/en-us/windows/win32/api/wincred/) — Complete Win32 credential API reference
- [CredWriteW function](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew) — Write credentials to the vault
- [CredReadW function](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw) — Read credentials from the vault
- [CREDENTIAL structure](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentiala) — Credential data structure definition
- [CryptProtectData (DPAPI)](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) — Encrypt data with DPAPI
- [CryptUnprotectData (DPAPI)](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata) — Decrypt DPAPI-protected data
- [SSPI Architecture](https://learn.microsoft.com/en-us/windows-server/security/windows-authentication/security-support-provider-interface-architecture) — Security Support Provider Interface overview
- [Microsoft Negotiate](https://learn.microsoft.com/en-us/windows/win32/secauthn/microsoft-negotiate) — Negotiate SSP documentation
- [UAC Architecture](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/user-account-control/architecture) — User Account Control internals
- [Access Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/access-tokens) — Windows access token reference
- [Impersonation Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/impersonation-tokens) — Thread impersonation
- [LocalSystem Account](https://learn.microsoft.com/en-us/windows/win32/services/localsystem-account) — Service identity: LocalSystem
- [LocalService Account](https://learn.microsoft.com/en-us/windows/win32/services/localservice-account) — Service identity: LocalService
- [WebAuthn APIs for Windows Hello](https://learn.microsoft.com/en-us/windows/security/identity-protection/hello-for-business/webauthn-apis) — Windows Hello integration

### Python Libraries
- [pywin32 (GitHub)](https://github.com/mhammond/pywin32) — Comprehensive Win32 API bindings for Python
- [pywin32 win32cred docs](https://timgolden.me.uk/pywin32-docs/win32cred.html) — Credential Manager bindings
- [pywin32 win32crypt docs](https://timgolden.me.uk/pywin32-docs/win32crypt.html) — DPAPI bindings
- [pywin32 win32security docs](https://timgolden.me.uk/pywin32-docs/win32security.html) — Security/token bindings
- [pywin32-ctypes (ReadTheDocs)](https://pywin32-ctypes.readthedocs.io/en/stable/api/win32ctypes.pywin32.win32cred.html) — Lightweight alternative
- [keyring (PyPI)](https://pypi.org/project/keyring/) — Cross-platform credential storage
- [python-fido2 (Yubico)](https://developers.yubico.com/python-fido2/) — FIDO2/WebAuthn library
- [py_webauthn (Duo Labs)](https://github.com/duo-labs/py_webauthn) — WebAuthn server library

### Tools
- [NSSM (nssm.cc)](https://nssm.cc/) — Non-Sucking Service Manager for wrapping executables as services
- [Credential Manager P/Invoke gist](https://gist.github.com/meziantou/10311113) — C# examples of CredRead/CredWrite

### Security Research
- [DPAPI Best Practices](https://comcomponent.com/en/blog/2026/03/16/000-windows-app-secret-storage-best-practices-dpapi/) — Keeping secrets out of plaintext config
- [DPAPI Internals (SwissKyRepo)](https://swisskyrepo.github.io/InternalAllTheThings/redteam/evasion/windows-dpapi/) — Red team perspective on DPAPI
- [DPAPI: Decline of a Top Secret Weapon (Sygnia)](https://www.sygnia.co/blog/the-downfall-of-dpapis-top-secret-weapon/) — Domain backup key risks
- [HackTricks: DPAPI Password Extraction](https://hacktricks.wiki/en/windows-hardening/windows-local-privilege-escalation/dpapi-extracting-passwords.html) — Attack techniques
- [SANS: Domain Compromise Recovery](https://www.sans.org/blog/critical-confusion-why-most-it-professionals-misunderstand-microsofts-domain-compromise-recovery-guidance) — Domain backup key implications
- [Tier Zero Security: DPAPI](https://tierzerosecurity.co.nz/2024/01/22/data-protection-windows-api.html) — DPAPI deep dive

### Community Discussions
- [Stack Overflow: Windows Vault Credential Storage](https://stackoverflow.com/questions/9221245/how-do-i-store-and-retrieve-credentials-from-the-windows-vault-credential-manage) — Credential Manager patterns
- [Stack Overflow: DPAPI for System Accounts](https://stackoverflow.com/questions/63512683/how-does-dpapi-protect-masterkey-for-system-accounts) — Service account DPAPI behavior
- [Stack Overflow: LocalSystem vs NetworkService](https://stackoverflow.com/questions/510170/the-difference-between-the-local-system-account-and-the-network-service-acco) — Service identity differences
- [Stack Overflow: Programmatic Elevation](https://stackoverflow.com/questions/133379/elevating-process-privilege-programmatically) — UAC elevation patterns
- [Stack Overflow: Python Read Credentials](https://stackoverflow.com/questions/77105434/python-read-stored-credentials-in-credential-manager) — win32cred usage
- [DEV: DPAPI with Python](https://dev.to/samklingdev/use-windows-data-protection-api-with-python-for-handling-credentials-5d4j) — Practical DPAPI tutorial
