# macOS Platform APIs

## Overview

macOS provides a layered security model that is highly relevant to Entrabot's autonomous agent architecture. The key subsystems are:

- **Keychain Services** — hardware-backed credential storage with per-process access control
- **launchd** — the system and user process manager for background execution
- **Code Signing & Entitlements** — process identity and trust chain
- **TCC (Transparency, Consent, and Control)** — user-facing privacy permissions
- **App Sandbox** — optional process-level capability restrictions
- **XPC** — secure inter-process communication

For an Entrabot agent running on macOS, the critical path is: the agent is a **code-signed, launchd-managed background process** that stores credentials in the **Keychain**, has a distinct **process identity** via its code signature, and requests necessary **TCC permissions** from the user.

---

## Keychain Services

### Architecture

The macOS Keychain is an encrypted database managed by the Security framework (`Security.framework`). Items are encrypted at rest using the user's login password (or the system key for system keychains). The Secure Enclave can protect individual items via `SecAccessControl`.

**Keychain locations:**
- **Login keychain:** `~/Library/Keychains/login.keychain-db` — unlocked on user login
- **System keychain:** `/Library/Keychains/System.keychain` — shared, requires admin to modify
- **Local Items (iCloud Keychain):** Data Protection keychain managed by `securityd`

### Core API (Security Framework — C/Swift)

#### SecItemAdd — Store a credential

```c
OSStatus SecItemAdd(CFDictionaryRef attributes, CFTypeRef *result);
```

**Swift example:**

```swift
import Security

func saveCredential(service: String, account: String, token: Data) -> OSStatus {
    let query: [String: Any] = [
        kSecClass as String:       kSecClassGenericPassword,
        kSecAttrService as String: service,      // e.g. "com.entrabot.agent"
        kSecAttrAccount as String: account,      // e.g. "agent-id-abc123"
        kSecValueData as String:   token,        // the OBO token bytes
        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    return SecItemAdd(query as CFDictionary, nil)
}
```

#### SecItemCopyMatching — Retrieve a credential

```c
OSStatus SecItemCopyMatching(CFDictionaryRef query, CFTypeRef *result);
```

**Swift example:**

```swift
func loadCredential(service: String, account: String) -> Data? {
    let query: [String: Any] = [
        kSecClass as String:       kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: account,
        kSecReturnData as String:  true,
        kSecMatchLimit as String:  kSecMatchLimitOne,
    ]
    var item: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &item)
    guard status == errSecSuccess else { return nil }
    return item as? Data
}
```

#### SecItemUpdate — Update an existing credential

```c
OSStatus SecItemUpdate(CFDictionaryRef query, CFDictionaryRef attributesToUpdate);
```

#### SecItemDelete — Remove a credential

```c
OSStatus SecItemDelete(CFDictionaryRef query);
```

#### Common error codes

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `errSecSuccess` | Operation succeeded |
| -25299 | `errSecDuplicateItem` | Item already exists (use Update instead) |
| -25300 | `errSecItemNotFound` | No matching item found |
| -34018 | `errSecMissingEntitlement` | Process lacks keychain entitlement |
| -25293 | `errSecAuthFailed` | Keychain locked or user denied access |

#### Key dictionary keys

| Key | Purpose |
|-----|---------|
| `kSecClass` | Item class: `kSecClassGenericPassword`, `kSecClassInternetPassword`, `kSecClassCertificate`, `kSecClassKey` |
| `kSecAttrService` | Service identifier (reverse-DNS, e.g. `com.entrabot.agent`) |
| `kSecAttrAccount` | Account name (e.g. agent ID or username) |
| `kSecValueData` | The secret data (password, token bytes) |
| `kSecAttrAccessible` | When the item is accessible (e.g. `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`) |
| `kSecAttrAccessControl` | Fine-grained access control (biometric, passcode) |
| `kSecReturnData` | Return the item data on query |
| `kSecMatchLimit` | `kSecMatchLimitOne` or `kSecMatchLimitAll` |

### Access Control with SecAccessControl

Items can require biometric (Touch ID) or passcode authentication before access:

```swift
var error: Unmanaged<CFError>?
let access = SecAccessControlCreateWithFlags(
    nil,
    kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly,
    [.userPresence],  // Touch ID or passcode
    &error
)

let query: [String: Any] = [
    kSecClass as String:          kSecClassGenericPassword,
    kSecAttrService as String:    "com.entrabot.agent",
    kSecAttrAccount as String:    "obo-token",
    kSecValueData as String:      tokenData,
    kSecAttrAccessControl as String: access!,
]
SecItemAdd(query as CFDictionary, nil)
```

**Access control flags:**
- `.userPresence` — Touch ID, Face ID, or passcode (most flexible)
- `.biometryAny` — any enrolled biometric
- `.biometryCurrentSet` — only current biometric enrollment (invalidates if biometrics change)

### Python Access: `keyring` Library (Recommended)

The `keyring` library provides a cross-platform abstraction over OS credential stores. On macOS, it uses the system Keychain as its backend.

```python
import keyring

# Store an OBO token
keyring.set_password("com.entrabot.agent", "agent-id-abc123", "eyJhbGciOi...")

# Retrieve it
token = keyring.get_password("com.entrabot.agent", "agent-id-abc123")

# Delete it
keyring.delete_password("com.entrabot.agent", "agent-id-abc123")
```

**Installation:**
```bash
pip install keyring
```

**Force macOS backend explicitly:**
```python
import keyring
from keyring.backends import macOS
keyring.set_keyring(macOS.Keyring())
```

**CLI usage:**
```bash
# Store (prompts for password interactively)
keyring set com.entrabot.agent agent-id-abc123

# Retrieve
keyring get com.entrabot.agent agent-id-abc123

# Delete
keyring del com.entrabot.agent agent-id-abc123
```

### Python Access: `security` CLI Tool

The `security` command-line tool provides direct Keychain manipulation from shell scripts:

```bash
# Add a generic password
security add-generic-password \
    -a "agent-id-abc123" \
    -s "com.entrabot.agent" \
    -w "eyJhbGciOi..." \
    -U  # Update if exists

# Retrieve (prints only the password)
security find-generic-password \
    -a "agent-id-abc123" \
    -s "com.entrabot.agent" \
    -w

# Delete
security delete-generic-password \
    -a "agent-id-abc123" \
    -s "com.entrabot.agent"

# Use in scripts as environment variable
export OBO_TOKEN=$(security find-generic-password -a "$USER" -s "com.entrabot.agent" -w 2>/dev/null)
```

**Key flags:**
- `-a` — account name
- `-s` — service name
- `-w` — password value (or return password on query)
- `-U` — update existing item (avoids duplicate error)
- `-A` — allow any app access (⚠️ insecure, avoid in production)

### Python Access: PyObjC (Low-Level, Direct API)

For cases where `keyring` is insufficient (e.g., needing `SecAccessControl`):

```python
from Security import (
    SecItemAdd, SecItemCopyMatching, SecItemDelete,
    kSecClass, kSecClassGenericPassword,
    kSecAttrService, kSecAttrAccount,
    kSecValueData, kSecReturnData, kSecMatchLimit, kSecMatchLimitOne,
)

# Store
query = {
    kSecClass: kSecClassGenericPassword,
    kSecAttrService: "com.entrabot.agent",
    kSecAttrAccount: "agent-id-abc123",
    kSecValueData: b"eyJhbGciOi...",
}
status, _ = SecItemAdd(query, None)

# Retrieve
query = {
    kSecClass: kSecClassGenericPassword,
    kSecAttrService: "com.entrabot.agent",
    kSecAttrAccount: "agent-id-abc123",
    kSecReturnData: True,
    kSecMatchLimit: kSecMatchLimitOne,
}
status, result = SecItemCopyMatching(query, None)
```

> **Note:** Requires `pip install pyobjc-framework-Security`.

---

## launchd Agents & Daemons

### Agent vs Daemon

| Property | Launch Agent | Launch Daemon |
|----------|-------------|---------------|
| **Runs as** | Logged-in user | root (or specified user) |
| **Lifecycle** | Starts at user login | Starts at boot |
| **GUI access** | ✅ Yes — can show dialogs | ❌ No |
| **Keychain** | ✅ Login keychain available | ⚠️ System keychain only |
| **Plist location** | `~/Library/LaunchAgents/` (per-user) or `/Library/LaunchAgents/` (all users) | `/Library/LaunchDaemons/` |
| **Use for Entrabot** | ✅ **Primary choice** — needs Keychain + consent UI | ❌ Cannot show consent prompts |

**For Entrabot: Use a Launch Agent**, not a Daemon. The agent needs access to the login keychain and may need to present consent dialogs.

### Plist Configuration

#### Entrabot Agent Plist Example

**File:** `~/Library/LaunchAgents/com.entrabot.agent.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Unique identifier for the job -->
    <key>Label</key>
    <string>com.entrabot.agent</string>

    <!-- The program and arguments to run -->
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/entrabot-agent</string>
        <string>--config</string>
        <string>~/.config/entrabot/config.toml</string>
    </array>

    <!-- Start when the plist is loaded (i.e., on user login) -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if the process exits abnormally -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- Throttle restarts: wait 10 seconds between crashes -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!-- Environment variables -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>ENTRABOT_HOME</key>
        <string>/Users/username/.config/entrabot</string>
    </dict>

    <!-- Working directory -->
    <key>WorkingDirectory</key>
    <string>/Users/username/.config/entrabot</string>

    <!-- Log output -->
    <key>StandardOutPath</key>
    <string>/Users/username/.config/entrabot/logs/agent.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/username/.config/entrabot/logs/agent.err.log</string>

    <!-- Nice level (lower priority to not interfere with user) -->
    <key>Nice</key>
    <integer>5</integer>
</dict>
</plist>
```

### Lifecycle Management

#### Modern commands (macOS 10.10+, recommended)

```bash
# Load (register + start) — modern approach
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.entrabot.agent.plist

# Unload (stop + deregister) — modern approach
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.entrabot.agent.plist

# Start/Stop a loaded job
launchctl kickstart gui/$(id -u)/com.entrabot.agent
launchctl kill SIGTERM gui/$(id -u)/com.entrabot.agent

# Check status
launchctl print gui/$(id -u)/com.entrabot.agent

# List all loaded user agents
launchctl list | grep entrabot
```

#### Legacy commands (still functional but deprecated)

```bash
launchctl load   ~/Library/LaunchAgents/com.entrabot.agent.plist
launchctl unload ~/Library/LaunchAgents/com.entrabot.agent.plist
launchctl start  com.entrabot.agent
launchctl stop   com.entrabot.agent
```

### Important Plist Keys Reference

| Key | Type | Description |
|-----|------|-------------|
| `Label` | String | **Required.** Unique reverse-DNS identifier |
| `ProgramArguments` | Array | **Required.** Command and arguments |
| `RunAtLoad` | Boolean | Start immediately when loaded |
| `KeepAlive` | Bool/Dict | Restart policy. Dict allows conditional restart |
| `StartInterval` | Integer | Run every N seconds |
| `StartCalendarInterval` | Dict | Cron-like scheduling |
| `ThrottleInterval` | Integer | Minimum seconds between launches (default: 10) |
| `WorkingDirectory` | String | Working directory for the process |
| `EnvironmentVariables` | Dict | Environment variables to set |
| `StandardOutPath` | String | stdout log file path |
| `StandardErrorPath` | String | stderr log file path |
| `Nice` | Integer | Process priority adjustment |
| `ProcessType` | String | `Background`, `Standard`, `Adaptive`, `Interactive` |
| `LimitLoadToSessionType` | String | `Aqua` (GUI sessions only) |
| `AssociatedBundleIdentifiers` | Array | Link to app bundle IDs (macOS 13+) |

### Programmatic Installation (Python)

```python
import os
import plistlib
import subprocess

def install_launch_agent(agent_binary: str, label: str = "com.entrabot.agent"):
    """Install an Entrabot agent as a launchd LaunchAgent."""
    home = os.path.expanduser("~")
    plist_path = os.path.join(home, "Library", "LaunchAgents", f"{label}.plist")
    log_dir = os.path.join(home, ".config", "entrabot", "logs")
    os.makedirs(log_dir, exist_ok=True)

    plist = {
        "Label": label,
        "ProgramArguments": [agent_binary],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": os.path.join(log_dir, "agent.out.log"),
        "StandardErrorPath": os.path.join(log_dir, "agent.err.log"),
    }

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", plist_path],
        check=True,
    )

def uninstall_launch_agent(label: str = "com.entrabot.agent"):
    """Uninstall the Entrabot LaunchAgent."""
    home = os.path.expanduser("~")
    plist_path = os.path.join(home, "Library", "LaunchAgents", f"{label}.plist")
    uid = os.getuid()

    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", plist_path],
        check=False,  # May fail if not loaded
    )
    if os.path.exists(plist_path):
        os.remove(plist_path)
```

---

## Process Identity

### Code Signing

macOS uses **code signatures** to establish process identity. Every process has a code signing identity that the OS checks when:

- Loading the process
- The process accesses Keychain items
- TCC checks privacy permissions
- Gatekeeper validates software origin

#### Signing hierarchy

1. **Apple-signed** — Apple's own binaries (highest trust)
2. **Developer ID-signed** — third-party apps signed with Apple-issued Developer ID certificate
3. **Ad-hoc signed** — locally signed, no certificate authority (local testing only)
4. **Unsigned** — blocked by Gatekeeper by default

#### Signing an Entrabot agent binary

```bash
# Ad-hoc signing (development/testing only)
codesign --force --options=runtime --sign - /usr/local/bin/entrabot-agent

# Developer ID signing (distribution)
codesign --force --options=runtime --timestamp \
    --entitlements entrabot.entitlements \
    --sign "Developer ID Application: Entrabot Inc (TEAMID)" \
    /usr/local/bin/entrabot-agent

# Verify signature
codesign --verify --verbose /usr/local/bin/entrabot-agent

# Display signing details
codesign --display --verbose=4 /usr/local/bin/entrabot-agent
```

### Bundle Identifiers

The **Bundle Identifier** (`CFBundleIdentifier`) is a reverse-DNS string that uniquely identifies an application within Apple's ecosystem. For command-line tools and agents without a `.app` bundle, the code signing identifier serves a similar purpose.

```
com.entrabot.agent        — main agent process
com.entrabot.agent.helper — privileged helper (if needed)
com.entrabot.consent-ui   — consent prompt UI app
```

### Entitlements

Entitlements are key-value pairs embedded in the code signature that declare which system capabilities a process may use.

**Example `entrabot.entitlements` plist:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Access keychain items shared within the team -->
    <key>keychain-access-groups</key>
    <array>
        <string>$(AppIdentifierPrefix)com.entrabot.shared</string>
    </array>

    <!-- Network access (client) -->
    <key>com.apple.security.network.client</key>
    <true/>

    <!-- Read/write access to user-selected files -->
    <key>com.apple.security.files.user-selected.read-write</key>
    <true/>
</dict>
</plist>
```

### Hardened Runtime & Notarization

For distribution, macOS requires the **hardened runtime** and **notarization**:

```bash
# 1. Sign with hardened runtime
codesign --force --options=runtime --timestamp \
    --sign "Developer ID Application: Entrabot Inc (TEAMID)" \
    /usr/local/bin/entrabot-agent

# 2. Package for notarization
ditto -c -k --keepParent /usr/local/bin/entrabot-agent entrabot-agent.zip

# 3. Submit for notarization
xcrun notarytool submit entrabot-agent.zip \
    --apple-id "dev@entrabot.com" \
    --team-id "TEAMID" \
    --password "@keychain:AC_PASSWORD" \
    --wait

# 4. Staple the ticket
xcrun stapler staple /usr/local/bin/entrabot-agent
```

### How macOS Identifies Processes

When a process attempts a privileged operation, macOS checks:

1. **Code signature** — Is the binary signed? By whom?
2. **Team ID** — Which developer team signed it?
3. **Code Directory Hash (cdhash)** — Unique hash of the exact binary
4. **Entitlements** — What capabilities does it declare?
5. **Bundle ID / Signing Identifier** — Logical identity

These are tracked in TCC databases, Keychain ACLs, and firewall rules. Changing the binary (e.g., updating the agent) changes the cdhash, which may invalidate existing permissions.

---

## TCC & Privacy Permissions

### What is TCC?

**Transparency, Consent, and Control (TCC)** is macOS's privacy permissions framework. It mediates access to sensitive user data and system resources, requiring explicit user consent.

### TCC Architecture

- **tccd daemon** — The TCC daemon runs at both system and user levels:
  - System: `/System/Library/PrivateFrameworks/TCC.framework/Support/tccd`
  - Per-user: one instance per logged-in user
- **TCC databases** (SQLite):
  - User: `~/Library/Application Support/com.apple.TCC/TCC.db`
  - System: `/Library/Application Support/com.apple.TCC/TCC.db` (SIP-protected)

### Permissions Relevant to Entrabot

| Permission | TCC Service Key | Needed? | Notes |
|-----------|----------------|---------|-------|
| Full Disk Access | `kTCCServiceSystemPolicyAllFiles` | Maybe | Only if agent reads arbitrary user files |
| Automation (AppleScript) | `kTCCServiceAppleEvents` | Maybe | If automating other apps |
| Accessibility | `kTCCServiceAccessibility` | Maybe | If simulating input |
| Camera | `kTCCServiceCamera` | No | |
| Microphone | `kTCCServiceMicrophone` | No | |
| Screen Recording | `kTCCServiceScreenCapture` | Maybe | If taking screenshots for context |
| Files and Folders | `kTCCServiceSystemPolicyDocumentsFolder` etc. | Maybe | Access to Desktop, Documents, Downloads |
| Notifications | Not TCC-managed | Yes | Use `UserNotifications` framework |

### How Consent Works

1. **First access** — When a process first attempts to access a TCC-protected resource, the system shows a consent dialog to the user.
2. **Recording** — The user's decision (allow/deny) is recorded in the TCC database, keyed by the process's **code signing identity**.
3. **Subsequent access** — Future requests are allowed/denied based on the recorded decision.
4. **Revocation** — Users can change decisions in System Settings → Privacy & Security.

### Requesting Consent Programmatically

There is no API to "request" TCC permissions proactively — the consent dialog appears automatically on first use. However, you can check status:

```swift
import AppKit

// For screen recording (example)
let hasScreenCapture = CGPreflightScreenCaptureAccess()
if !hasScreenCapture {
    CGRequestScreenCaptureAccess()  // Opens System Settings
}
```

### Managing TCC via MDM (Enterprise)

For managed deployments, PPPC (Privacy Preferences Policy Control) profiles can pre-approve permissions:

```xml
<!-- Example PPPC profile payload (simplified) -->
<dict>
    <key>Authorization</key>
    <string>Allow</string>
    <key>CodeRequirement</key>
    <string>identifier "com.entrabot.agent" and anchor apple generic and
        certificate leaf[subject.OU] = "TEAMID"</string>
    <key>IdentifierType</key>
    <string>bundleID</string>
    <key>Identifier</key>
    <string>com.entrabot.agent</string>
    <key>Services</key>
    <dict>
        <key>SystemPolicyAllFiles</key>
        <dict>
            <key>Authorization</key>
            <string>Allow</string>
        </dict>
    </dict>
</dict>
```

### TCC Reset Utility

```bash
# Reset all TCC permissions for an app
tccutil reset All com.entrabot.agent

# Reset specific service
tccutil reset ScreenCapture com.entrabot.agent

# Reset all apps for a service
tccutil reset ScreenCapture
```

---

## App Sandbox

### Overview

The App Sandbox is an opt-in macOS security mechanism that restricts what an application can do. It limits file system access, network access, hardware access, and IPC to only what the app declares via entitlements.

### Implications for Entrabot Agents

**Recommendation: Do NOT sandbox the Entrabot agent process.** Here's why:

| Factor | Sandboxed | Non-Sandboxed |
|--------|-----------|---------------|
| File access | Restricted to container + user-granted | Full user permissions |
| Network | Requires entitlement | Available by default |
| Keychain | Limited to app's own items | Full login keychain |
| IPC/XPC | Restricted | Unrestricted |
| App Store | Required | Not required |
| Distribution | App Store or Developer ID | Developer ID only |

**Key considerations:**

1. **Child process inheritance** — If a sandboxed app launches the Entrabot agent as a subprocess, the agent inherits the sandbox restrictions. The agent must be an independent launchd-managed process.
2. **Keychain scope** — Sandboxed apps can only access their own keychain items (within their app group). Non-sandboxed agents can access any item the user has authorized.
3. **File system** — Agents often need to read project files, config files, etc. A sandbox would require security-scoped bookmarks for each file path.

### Using `sandbox-exec` for Custom Restrictions

Even without App Sandbox, you can apply a custom sandbox profile for defense in depth:

```bash
# Create a custom sandbox profile
cat > /tmp/entrabot-sandbox.sb << 'EOF'
(version 1)
(allow default)
(deny file-write*
    (subpath "/System")
    (subpath "/usr"))
(deny process-exec
    (subpath "/System"))
EOF

# Run agent with custom profile
sandbox-exec -f /tmp/entrabot-sandbox.sb /usr/local/bin/entrabot-agent
```

> **Note:** `sandbox-exec` is technically deprecated but still functional. Apple has not provided a public replacement for CLI tools.

---

## XPC Services

### Overview

XPC (Cross-Process Communication) is macOS's preferred mechanism for inter-process communication. It enables privilege separation by allowing a main application to offload tasks to separate, isolated helper processes.

### Architecture for Entrabot

```
┌─────────────────────────────┐
│  Entrabot Consent UI (.app) │  ← GUI app for consent prompts
│     NSXPCConnection         │
└──────────┬──────────────────┘
           │ XPC Protocol
           ▼
┌─────────────────────────────┐
│  Entrabot Agent (launchd)   │  ← Background agent process
│     NSXPCConnection         │
└──────────┬──────────────────┘
           │ XPC Protocol
           ▼
┌─────────────────────────────┐
│  Privileged Helper          │  ← Optional: for operations
│  (SMAppService-managed)     │     requiring elevated privileges
└─────────────────────────────┘
```

### XPC Connection Types

1. **XPC Service (bundled)** — Lives inside an app bundle, launched on demand, inherits app's sandbox. Terminates when parent exits.
2. **Mach Service (launchd)** — Registered with launchd, independently managed, persists across app launches. This is what Entrabot should use.
3. **Anonymous connection** — Direct pipe between parent and child process.

### NSXPCConnection Example (Swift)

**Define a protocol:**

```swift
@objc protocol EntrabotAgentProtocol {
    func storeToken(_ token: String, forAgentID agentID: String,
                    reply: @escaping (Bool, String?) -> Void)
    func getToken(forAgentID agentID: String,
                  reply: @escaping (String?) -> Void)
    func requestConsent(forScope scope: String,
                        reply: @escaping (Bool) -> Void)
}
```

**Agent-side listener (in the launchd agent):**

```swift
import Foundation

class AgentDelegate: NSObject, NSXPCListenerDelegate {
    func listener(_ listener: NSXPCListener,
                  shouldAcceptNewConnection conn: NSXPCConnection) -> Bool {
        conn.exportedInterface = NSXPCInterface(
            with: EntrabotAgentProtocol.self
        )
        conn.exportedObject = AgentService()
        conn.resume()
        return true
    }
}

// Register as a Mach service (label must match launchd plist)
let listener = NSXPCListener(machServiceName: "com.entrabot.agent.xpc")
listener.delegate = AgentDelegate()
listener.resume()
RunLoop.main.run()
```

**Client-side connection (from the consent UI app):**

```swift
let connection = NSXPCConnection(
    machServiceName: "com.entrabot.agent.xpc"
)
connection.remoteObjectInterface = NSXPCInterface(
    with: EntrabotAgentProtocol.self
)
connection.resume()

let proxy = connection.remoteObjectProxyWithErrorHandler { error in
    print("XPC error: \(error)")
} as! EntrabotAgentProtocol

proxy.getToken(forAgentID: "abc123") { token in
    print("Got token: \(token ?? "nil")")
}
```

### XPC from Python

Python can't use `NSXPCConnection` directly, but it can communicate with XPC services via:

1. **PyObjC bridge** — import Foundation and use NSXPCConnection (complex but possible)
2. **Unix domain sockets** — simpler alternative for Python agents
3. **Named pipes / stdin-stdout** — if spawned as a subprocess

For a Python-based Entrabot agent, **Unix domain sockets** are the pragmatic choice for IPC with a Swift-based consent UI.

---

## Integration Patterns

### Recommended Architecture for Entrabot on macOS

```
Installation:
  1. Install agent binary to /usr/local/bin/entrabot-agent (or ~/.local/bin/)
  2. Install consent UI to /Applications/Entrabot.app
  3. Create launchd plist in ~/Library/LaunchAgents/
  4. Sign & notarize both binaries with Developer ID
  5. Load agent: launchctl bootstrap gui/$(id -u) <plist>

Runtime:
  ┌──────────────────────┐
  │  User's Terminal/IDE │
  │  (CLI commands)      │
  └──────────┬───────────┘
             │ IPC (socket/pipe)
             ▼
  ┌──────────────────────┐
  │  entrabot-agent      │──── Keychain (login)
  │  (LaunchAgent)       │     ├── OBO tokens
  │  PID: distinct       │     ├── Agent private key
  │  Code-signed         │     └── Refresh tokens
  └──────────┬───────────┘
             │ XPC / Socket
             ▼
  ┌──────────────────────┐
  │  Entrabot.app        │──── TCC permissions
  │  (Consent UI)        │     └── User approvals
  │  Shows dialogs       │
  └──────────────────────┘
```

### Credential Storage Pattern

```python
import keyring
import json
import time

class MacOSCredentialStore:
    """Credential store using macOS Keychain via keyring."""

    SERVICE = "com.entrabot.agent"

    def store_token(self, agent_id: str, token_data: dict) -> None:
        """Store an OBO token with metadata."""
        payload = json.dumps({
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": token_data.get("expires_at"),
            "scope": token_data.get("scope"),
            "stored_at": time.time(),
        })
        keyring.set_password(self.SERVICE, f"token:{agent_id}", payload)

    def get_token(self, agent_id: str) -> dict | None:
        """Retrieve and validate a stored token."""
        raw = keyring.get_password(self.SERVICE, f"token:{agent_id}")
        if raw is None:
            return None
        data = json.loads(raw)
        if data.get("expires_at") and data["expires_at"] < time.time():
            return None  # Token expired
        return data

    def delete_token(self, agent_id: str) -> None:
        """Remove a stored token."""
        try:
            keyring.delete_password(self.SERVICE, f"token:{agent_id}")
        except keyring.errors.PasswordDeleteError:
            pass  # Already deleted

    def store_agent_key(self, agent_id: str, private_key_pem: str) -> None:
        """Store the agent's private key."""
        keyring.set_password(self.SERVICE, f"key:{agent_id}", private_key_pem)

    def get_agent_key(self, agent_id: str) -> str | None:
        """Retrieve the agent's private key."""
        return keyring.get_password(self.SERVICE, f"key:{agent_id}")
```

### Background Execution Pattern

```python
import subprocess
import os
import plistlib

class MacOSAgentInstaller:
    """Install/manage the Entrabot agent as a macOS LaunchAgent."""

    LABEL = "com.entrabot.agent"

    @property
    def plist_path(self) -> str:
        return os.path.expanduser(
            f"~/Library/LaunchAgents/{self.LABEL}.plist"
        )

    def install(self, agent_binary: str) -> None:
        """Install and start the agent."""
        log_dir = os.path.expanduser("~/.config/entrabot/logs")
        os.makedirs(log_dir, exist_ok=True)

        plist = {
            "Label": self.LABEL,
            "ProgramArguments": [agent_binary, "--daemon"],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardOutPath": f"{log_dir}/agent.out.log",
            "StandardErrorPath": f"{log_dir}/agent.err.log",
        }

        with open(self.plist_path, "wb") as f:
            plistlib.dump(plist, f)

        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", self.plist_path],
            check=True,
        )

    def uninstall(self) -> None:
        """Stop and remove the agent."""
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", self.plist_path],
            check=False,
        )
        if os.path.exists(self.plist_path):
            os.remove(self.plist_path)

    def is_running(self) -> bool:
        """Check if the agent is currently running."""
        result = subprocess.run(
            ["launchctl", "list", self.LABEL],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    def restart(self) -> None:
        """Restart the agent."""
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{self.LABEL}"],
            check=True,
        )
```

### Consent UX Pattern

Since a LaunchAgent can technically display UI (it runs in the user's GUI session), the consent flow would be:

1. **Agent receives an OBO token request** from a client (IDE plugin, CLI)
2. **Agent checks if consent has been granted** for the requested scope
3. **If not, agent launches a consent dialog** — either:
   - A native macOS alert via `NSAlert` (requires a Swift helper)
   - A system notification via `terminal-notifier` or `osascript`
   - A dedicated Entrabot.app consent window
4. **User approves/denies** → decision is cached locally
5. **Agent proceeds** with the OBO token exchange

**Quick consent via osascript (for prototyping):**

```python
import subprocess

def show_consent_dialog(scope: str, requesting_app: str) -> bool:
    """Show a macOS dialog asking user to approve agent access."""
    script = f'''
    display dialog "The application '{requesting_app}' is requesting access to:\\n\\n{scope}\\n\\nAllow this agent to act on your behalf?" ¬
        buttons {{"Deny", "Allow"}} default button "Allow" ¬
        with title "Entrabot Agent Consent" ¬
        with icon caution
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True,
    )
    return "Allow" in result.stdout
```

---

## Community Learnings & Gotchas

### Keychain Gotchas

1. **Binary-specific ACLs** — Keychain items are ACL'd to the specific binary that created them. If you update the Entrabot agent binary (new cdhash), the user may be prompted again to allow access. Mitigation: use the `keyring` library which handles ACL management, or store items with permissive ACLs during development.

2. **Python binary fragmentation** — Credentials stored by `/usr/bin/python3` are NOT accessible from `/opt/homebrew/bin/python3` (different binaries = different ACL entries). Always use a consistent Python path or use a bundled interpreter.

3. **Keychain locked in automation** — The login keychain may be locked if the screen saver is active or the session is via SSH. Use `security unlock-keychain` before access in automation contexts.

4. **macOS Tahoe (26.x) regression** — The `security find-generic-password -w` CLI command was reported to hang on macOS Tahoe in some automation contexts. The `keyring` Python library was unaffected since it calls the C API directly.

5. **Duplicate item errors** — `SecItemAdd` returns `errSecDuplicateItem` (-25299) if the item already exists. Always handle this by calling `SecItemUpdate` or `SecItemDelete` + `SecItemAdd`.

### launchd Pitfalls

1. **`bootout` EIO errors** — On macOS Sonoma+, `launchctl bootout` may return `EIO` if the job isn't loaded or you target the wrong domain. Always verify with `launchctl print` first.

2. **Tilde expansion** — `~` is NOT expanded in plist `ProgramArguments`. Use absolute paths.

3. **PATH not inherited** — launchd agents don't inherit the user's shell PATH. Set `EnvironmentVariables` in the plist or use absolute paths for all binaries.

4. **Login Items UI** — Since macOS Ventura (13), all LaunchAgents appear in System Settings → General → Login Items. Users can disable your agent here. Use `AssociatedBundleIdentifiers` to link your agent to a parent app for a cleaner appearance.

5. **File permissions** — Plist files must be owned by the user (for per-user agents) or root:wheel (for system agents), with permissions `644`. Incorrect permissions cause silent load failures.

6. **Log rotation** — launchd does NOT rotate logs. Implement rotation in your agent or use `newsyslog.conf`.

### TCC Surprises

1. **Permissions keyed to cdhash** — Updating your binary invalidates TCC permissions. Users must re-grant access. Use stable code signing identities and minimize binary changes.

2. **No programmatic approval** — There is no API to programmatically grant TCC permissions. The user MUST interact with a system dialog or System Settings.

3. **Automation permission per-pair** — `kTCCServiceAppleEvents` (Automation) is granted per source-target pair. If your agent automates different apps, each pair needs separate approval.

4. **Full Disk Access required for some paths** — Reading `~/Library/Mail/`, `~/Library/Messages/`, Time Machine backups, and some other locations requires Full Disk Access, not just Files and Folders permission.

5. **SIP protects system TCC.db** — You cannot modify `/Library/Application Support/com.apple.TCC/TCC.db` even as root (unless SIP is disabled).

### Code Signing Surprises

1. **Gatekeeper on first launch** — Even a properly signed and notarized binary may trigger a "downloaded from the internet" warning on first launch. Use `xattr -d com.apple.quarantine` during installation or distribute via a signed installer package (`.pkg`).

2. **Hardened runtime + Python** — If you ship a Python interpreter with hardened runtime, you may need entitlements for JIT (`com.apple.security.cs.allow-jit`), unsigned memory (`com.apple.security.cs.allow-unsigned-executable-memory`), or dylib loading (`com.apple.security.cs.disable-library-validation`).

---

## Open Questions

1. **Agent ID persistence** — Should the Entrabot Agent ID be stored in the Keychain as a credential, or as a configuration file? Keychain provides integrity guarantees but adds complexity.

2. **Consent caching** — Where should user consent decisions be cached? Options: Keychain (encrypted), a local SQLite database (simpler), or a signed plist (tamper-evident).

3. **Token refresh in background** — When the LaunchAgent refreshes OBO tokens, will Keychain access work reliably without user interaction? Need to test with `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` vs `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`.

4. **Multi-user scenarios** — Each macOS user gets their own LaunchAgent and Keychain. How does Entrabot handle shared machines with multiple users who each have their own Agent ID?

5. **MDM deployment** — For enterprise customers, should we provide a PPPC profile template? How do we handle the case where IT pre-approves TCC permissions?

6. **XPC vs socket IPC** — Should the agent communicate with IDEs/CLIs via XPC (native, typed, Apple-blessed) or Unix domain sockets (simpler, Python-friendly)? XPC requires Swift/ObjC bridges; sockets work natively in Python.

7. **Code signing identity for Python scripts** — If the agent is a Python script, is it the Python interpreter's identity that matters, or can we sign the script itself? (Answer: the interpreter's identity is what macOS checks.)

8. **Sandbox profile** — Should we ship a custom `sandbox-exec` profile to restrict the agent's capabilities, even without full App Sandbox? This provides defense-in-depth.

9. **Notarization for updates** — Every binary update requires re-notarization. How does this affect our CI/CD pipeline and update cadence? Consider using Sparkle framework for auto-updates.

10. **Touch ID for high-value operations** — Should we use `SecAccessControl` with `.userPresence` to require Touch ID before the agent can access tokens? This adds security but introduces UX friction for autonomous operation.

---

## Sources

### Apple Developer Documentation
- [Keychain Items](https://developer.apple.com/documentation/security/keychain-items) — Overview of keychain item types and usage
- [SecItemAdd](https://developer.apple.com/documentation/security/secitemadd(_:_:)) — API reference for adding keychain items
- [SecItemCopyMatching](https://developer.apple.com/documentation/security/secitemcopymatching(_:_:)) — API reference for querying keychain items
- [Searching for Keychain Items](https://developer.apple.com/documentation/security/searching-for-keychain-items) — Guide with query construction patterns
- [Item Return Result Keys](https://developer.apple.com/documentation/security/item-return-result-keys) — Controlling what keychain queries return
- [SecAccessControl](https://developer.apple.com/documentation/security/secaccesscontrol) — Fine-grained access control for keychain items
- [SecAccessControlCreateFlags](https://developer.apple.com/documentation/security/secaccesscontrolcreateflags) — Biometric and passcode flags
- [Accessing Keychain Items with Face ID or Touch ID](https://developer.apple.com/documentation/localauthentication/accessing-keychain-items-with-face-id-or-touch-id) — Biometric-protected keychain access
- [Entitlements](https://developer.apple.com/documentation/bundleresources/entitlements) — Declaring app capabilities
- [Diagnosing Issues with Entitlements](https://developer.apple.com/documentation/bundleresources/diagnosing-issues-with-entitlements) — Troubleshooting entitlement errors
- [Configuring the macOS App Sandbox](https://developer.apple.com/documentation/xcode/configuring-the-macos-app-sandbox) — Sandbox configuration guide
- [XPC](https://developer.apple.com/documentation/xpc) — XPC framework overview
- [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) — Apple's launchd guide
- [Notarizing macOS Software Before Distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) — Notarization requirements
- [TN2206: macOS Code Signing In Depth](https://developer.apple.com/library/archive/technotes/tn2206/_index.html) — Deep dive on code signing
- [Controlling App Access to Files in macOS](https://support.apple.com/guide/security/controlling-app-access-to-files-secddd1d86a6/web) — Apple's TCC overview
- [Background Task Management (Declarative)](https://support.apple.com/guide/deployment/background-task-management-declarative-dep931381403/web) — macOS 15+ MDM management

### Community & Technical References
- [launchd.info](https://launchd.info/) — Comprehensive launchd tutorial with examples
- [launchd.plist(5) man page](https://keith.github.io/xcode-man-pages/launchd.plist.5.html) — Complete plist key reference
- [launchctl "new" subcommand basics](https://www.alansiu.net/2023/11/15/launchctl-new-subcommand-basics-for-macos/) — Modern launchctl commands
- [keyring · PyPI](https://pypi.org/project/keyring/) — Python keyring library documentation
- [keyring 25.7.0 documentation](https://keyring.readthedocs.io/en/latest/) — Full API docs
- [jaraco/keyring on GitHub](https://github.com/jaraco/keyring) — Source code and issues
- [macOS Tahoe Broke Keychain CLI Reads](https://dev.to/euda1mon1a/macos-tahoe-broke-keychain-cli-reads-novel-findings-from-an-ai-agent-deployment-2p3o) — Agent-specific keychain gotchas
- [Scripting OS X: Get Password from Keychain](https://scriptingosx.com/2021/04/get-password-from-keychain-in-shell-scripts/) — `security` CLI tool guide
- [ss64.com: security command](https://ss64.com/mac/security-password.html) — `security` CLI reference
- [Building a macOS Agent: Signing, Notarization, Distribution](https://www.inventory-os.com/blog/macos-agent-signing) — End-to-end agent deployment guide

### Security Research
- [HackTricks: macOS TCC](https://hacktricks.wiki/en/macos-hardening/macos-security-and-privilege-escalation/macos-security-protections/macos-tcc/index.html) — TCC internals and attack surface
- [HackTricks: macOS Sandbox Debug & Bypass](https://hacktricks.wiki/en/macos-hardening/macos-security-and-privilege-escalation/macos-security-protections/macos-sandbox/macos-sandbox-debug-and-bypass/index.html) — Sandbox internals
- [Huntress: Full Transparency — Controlling Apple's TCC](https://www.huntress.com/blog/full-transparency-controlling-apples-tcc) — TCC deep dive
- [CISA: TCC Manipulation (T1548.006)](https://www.cisa.gov/eviction-strategies-tool/info-attack/T1548.006) — MITRE ATT&CK TCC technique
- [OWASP: Keychain Services (MASTG-KNOW-0057)](https://mas.owasp.org/MASTG/knowledge/ios/MASVS-AUTH/MASTG-KNOW-0057/) — Security assessment of keychain
- [TCC and the macOS Platform Sandbox Policy](https://bdash.net.nz/posts/tcc-and-the-platform-sandbox-policy/) — How TCC and sandbox interact
- [agent-seatbelt-sandbox](https://github.com/michaelneale/agent-seatbelt-sandbox) — Native macOS sandboxing for AI agents

### Stack Overflow & Community
- [SecItemAdd errSecMissingEntitlement (-34018)](https://stackoverflow.com/questions/20344255/secitemadd-and-secitemcopymatching-returns-error-code-34018-errsecmissingentit) — Common entitlement error
- [launchctl bootout EIO on Sonoma](https://stackoverflow.com/questions/78246166/launchctl-bootout-on-macos-sonoma-is-failing-with-eio-input-output-error) — Modern launchctl issues
- [Sandboxing a Command Line Tool With Paths](https://mjtsai.com/blog/2022/08/26/sandboxing-a-command-line-tool-with-paths-as-arguments/) — Sandbox limitations for CLI tools
- [macOS Sonoma sandbox security changes](https://lapcatsoftware.com/articles/2023/6/1.html) — Sonoma-specific sandbox behavior
- [Manage Custom Login Items](https://macblog.org/manage-custom-login-items/) — macOS Ventura Login Items UI
