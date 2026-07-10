# Microsoft Execution Containers (MXC) — Platform Research

**Date:** 2026-06-08
**Status:** **Early preview**, announced at **Build 2026** (Windows Developer Blog, 2026-06-02). OSS repo `github.com/microsoft/mxc` is public; the TypeScript SDK `@microsoft/mxc-sdk` is on npm at **v0.6.1** (MIT). Explicitly **not a security boundary yet** — the repo warns profiles are "overly permissive" and "no MXC profiles should be treated as security boundaries currently."
**Why this exists:** entrabot gives a device-local agent its own **identity** (Entra Agent User) and honest **attribution**. MXC is Microsoft's answer to the *other half* of the device-agent security story: **execution containment** — OS-enforced sandboxing of the code an agent runs. The two are complementary, and MXC is the most directly on-thesis platform primitive Microsoft has shipped for what entrabot is trying to do. This doc captures what MXC actually is (vs. the marketing), how third parties integrate, the macOS story (entrabot is macOS-primary), and where it slots into entrabot.

> **Read this before designing any MXC integration.** The single most load-bearing fact: the OSS MXC is a **code-execution sandbox with a TypeScript SDK**, not an identity system. The "strong agent identity" framing in the press comes from the *Windows + Agent 365 vision layer* (Entra/Intune), not from the `microsoft/mxc` repo. Don't conflate them.

---

## 1. What MXC actually is

**MXC (Microsoft eXecution Container) is a cross-platform, policy-driven sandbox for running untrusted code** — model output, plugins, tool calls — on **Windows, Linux, and macOS**. It is an abstraction layer over OS-native isolation primitives, driven by a single **versioned JSON policy schema** and exposed through a **TypeScript SDK** + a native binary.

Two layers, often conflated in coverage — keep them separate:

| Layer | What it is | Where it lives | Verifiable today? |
|---|---|---|---|
| **MXC SDK (OSS)** | Policy-driven code-execution sandbox. JSON policy → native runner → OS isolation primitive. | `github.com/microsoft/mxc`, `@microsoft/mxc-sdk` on npm | ✅ Yes — code is public |
| **Windows + Agent 365 vision** | MXC *session isolation* bound to an Entra/local identity, governed via Intune, attributing all container activity to an agent ID. | Windows Developer Blog; Agent 365; Windows 365 for Agents | 🟡 Announced, rolling out "in early preview shortly after Build" |

The OSS repo is `wxc`/`mxc` internally: **MXC** is the umbrella; **WXC** = the Windows eXecution Container runner (`wxc-exec.exe`). The core is **Rust**; the SDK is TypeScript.

---

## 2. The composable sandbox — containment backends

MXC's core idea is **one policy model, many isolation backends**, chosen by an **abstract intent** (`process` / `vm` / `microvm`) that the runner resolves per host, or a concrete backend name.

| Intent | Windows | Linux | macOS |
|---|---|---|---|
| `process` (lightweight, fast) | `processcontainer` (AppContainer → BaseContainer) | `bubblewrap` (default) / `lxc` | **`seatbelt`** |
| `vm` (hardware-isolated) | `windows_sandbox` | — | — |
| `microvm` (hypervisor, lightweight) | `microvm` (NanVix on Windows Hypervisor Platform) / `hyperlight` | `hyperlight` | — |

Other Windows backends: `wslc` (WSL containers), `isolation_session` (the OS-level "session isolation" from the blog — the only backend with a **state-aware lifecycle** today).

Default backends (`processcontainer`, `bubblewrap`, `lxc`) work out of the box. Everything else is **experimental** and requires `{ experimental: true }` (SDK) or `--experimental` (CLI).

The containment spectrum, weakest → strongest (from the Build blog):
**Process isolation** (inner-loop coding agents; GitHub Copilot CLI adopted this) → **Session isolation** (distinct user account / desktop; mitigates UI spoofing + input injection; attributed to a local or Entra identity) → **Micro-VM** (hypervisor-backed, raises the bar against LLM sandbox-escape; roadmap) → **Linux containers via WSL** (roadmap) → **Windows 365 for Agents** (disposable Intune-managed Cloud PC; roadmap).

---

## 3. The policy model

A single JSON document (default-deny everywhere). Current stable schema: **`0.6.0-alpha`**. Core sections:

```jsonc
{
  "version": "0.6.0-alpha",
  "containment": "process",                 // abstract intent or concrete backend
  "process": { "commandLine": "python app.py", "cwd": "...", "env": ["K=V"], "timeout": 30000 },
  "filesystem": {
    "readonlyPaths":  ["/Users/me/project"],
    "readwritePaths": ["/tmp/output"],
    "deniedPaths":    ["/Users/me/.ssh"]     // deny wins (last-match), see caveats
  },
  "network": {
    "defaultPolicy": "block",                // allow | block
    "allowedHosts": ["api.github.com"],      // not enforced on Windows
    "proxy": { "localhost": 8080 }           // not supported on macOS
  },
  "ui": { "allowWindows": false }            // clipboard/display/GUI; default-deny on 0.5.0+
}
```

**Default-deny is total:** no `network` → no network; no `readwritePaths` → can't even write `$TMPDIR`; no `ui` → no GUI. The SDK ships **discovery helpers** (`getAvailableToolsPolicy`, `getUserProfilePolicy`, `getTemporaryFilesPolicy`) that enumerate the host (PATH, PYTHONPATH, temp dir) so you compose a working baseline, then add task-specific paths on top.

**House guidance from MS for agentic use:** prefer **many narrow sandboxes** (one policy per task step) over one broad policy — scoped output dir in `readwritePaths`, project tree in `readonlyPaths`, secrets in `deniedPaths`.

---

## 4. How third parties integrate

Three surfaces, in order of how a non-Microsoft project would actually wire it up:

**A. TypeScript / Node SDK — `@microsoft/mxc-sdk`** (the blessed path; Node ≥18, depends on `node-pty`).
- One-shot: `createConfigFromPolicy(policy, intent)` → set `config.process.commandLine` → `spawnSandboxFromConfig(config, { usePty: false })` returns a `ChildProcess` (separated stdout/stderr) or an `IPty`.
- Convenience: `spawnSandbox(script, policy)` / `spawnSandboxAsync(...)` for process-isolation only.
- State-aware (long-lived agent loops): `provisionSandbox → startSandbox → execInSandboxAsync → stopSandbox → deprovisionSandbox` — **currently `isolation_session` (Windows) only**.
- `getPlatformSupport()` gates everything: returns `{ isSupported, ... }` and per-host backend availability.

**B. Native binary + JSON config** (the language-agnostic path — this is how a **Python** project integrates):
- `wxc-exec.exe config.json` (Windows), `./lxc-exec config.json` (Linux), `./mxc-exec-mac --experimental config.json` (macOS).
- Also accepts `--config-base64 <b64>`; `--dry-run` validates without executing; `--debug`/`--log-file` for diagnostics.
- Locate the binary via `MXC_BIN_DIR=<dir>` (expects `<dir>/<arch>/<binary>`).

**C. The Windows/Agent 365 governance plane** (enterprise, not OSS): Intune policies can *require* MXC isolation with guardrails (filesystem rules), and session isolation binds the container to an Entra-provisioned identity. This is where "third party" means "an IT admin governing someone else's agent," not "a developer embedding the SDK."

**Ecosystem partners already shipping on MXC:** GitHub Copilot CLI (process isolation), NVIDIA **OpenShell**, OpenClaw (node + gateway on Windows, with a Windows companion app), OpenAI **Codex**, Hermes/Nous Research, Manus.

---

## 5. The macOS story (entrabot is macOS-primary — read this)

macOS support is **experimental**, requires schema **`0.6.0-alpha`+** and the `--experimental` flag, and uses Apple's **Seatbelt** sandbox — the same kernel-enforced framework behind every Mac App Store app's App Sandbox.

- **Mechanism:** a TinyScheme profile is generated from the MXC policy and applied via `sandbox_init()` in `pre_exec` (after `fork`, before `exec`). No temp files; the child inherits the parent's Mach bootstrap namespace.
- **Process-scoped, not container-scoped:** no named container, no lifecycle, nothing to clean up — the sandbox lives only as long as the process tree. (So the state-aware lifecycle API does **not** apply on macOS.)
- **Filesystem:** `readonlyPaths`/`readwritePaths`/`deniedPaths` → `(allow file-read*)`/`(allow file-write*)`/`(deny …)` subpath rules; deny emitted last (last-match-wins). A baseline of `/usr/lib`, `/System`, `/Library`, `/dev/urandom`, etc. is always readable so the dynamic linker works. **SIP overrides the profile** — you cannot grant write to `/System` or `/usr` even explicitly.
- **Network:** `defaultPolicy: block|allow` works; `allowedHosts` is best-effort (Seatbelt does no DNS); **`blockedHosts` is rejected** (Seatbelt can't selectively block hosts); **`proxy` is rejected** (no TLS interposition on macOS).
- **`experimental.seatbelt` knobs that matter for entrabot:**
  - **`keychainAccess` (default false)** — opens the sandbox enough for `keytar` / `Security.framework` to reach the macOS Keychain (adds Mach lookups for `securityd`/`trustd`/`cfprefsd`/`lsd.*` and R/W on `~/Library/Keychains`). **Directly relevant:** entrabot stores the Blueprint/agent private keys in the macOS Keychain via the `keyring` package. Any sandboxed entrabot path that needs a token would need this opt-in.
  - **`nestedPty` (default true)** — required by anything that spawns a shell (`git`, `gh`, REPLs, test runners, agent tools that wrap commands in a pty).
  - **`guiAccess` / `launchMethod`** — GUI apps are limited; native AppKit works with `exec`, Terminal.app needs `open` (Apple Launch Constraints), Electron apps (VS Code) can escape via re-launch.
- **Prereqs:** macOS 11+. `sandbox_init()` is technically deprecated in headers since 10.8 but still ships and is used by Apple and Chromium. Building from source needs Xcode CLT + Rust 1.93; shipping needs codesign + notarization (binaries from `build-mac.sh` are unsigned).

**Startup cost (from MXC's own comparison):** Seatbelt ~10 ms (process), AppContainer ~10 ms, LXC ~1 s. Cheap enough to wrap per-command.

---

## 5.1 Entrabot macOS build/install notes (2026-06-18)

For Entrabot's macOS E2E work we build the native Seatbelt runner from source
and install it at `.mxc-build/target/release/mxc-exec-mac`.

- **Upstream source:** `https://github.com/microsoft/mxc`
- **Pinned version:** `v0.6.1`
- **Pinned commit:** `161598fd08a4fdd030f461de19af23ce4a310b41`
- **Local compatibility patch:** `scripts/mxc-mac-stdin-compat.patch`
  - Why: Entrabot's `SeatbeltRunner` streams policy JSON on stdin.
  - Upstream `mxc-exec-mac` v0.6.1 accepts file/base64 config but not stdin.
  - The patch adds: "if no config arg is present, read JSON from stdin and
    feed it through the existing base64 parse path."
- **Installed binary SHA256 (darwin-arm64):**
  `700e9e7120c78fe9ecdb8c99309ba6df0ea467ac5b581b803b73d655bbccff36`

Rebuild recipe:

```bash
git clone --depth 1 --branch v0.6.1 https://github.com/microsoft/mxc.git .mxc-build/mxc-src
git -C .mxc-build/mxc-src fetch --depth 1 origin 161598fd08a4fdd030f461de19af23ce4a310b41
git -C .mxc-build/mxc-src checkout --force 161598fd08a4fdd030f461de19af23ce4a310b41
git -C .mxc-build/mxc-src apply scripts/mxc-mac-stdin-compat.patch
cd .mxc-build/mxc-src && ./build-mac.sh --rust-only
cp src/target/aarch64-apple-darwin/release/mxc-exec-mac ../target/release/mxc-exec-mac
shasum -a 256 ../target/release/mxc-exec-mac
```

Smoke checks:

```bash
# File-based config (upstream interface)
.mxc-build/target/release/mxc-exec-mac --experimental .mxc-build/smoke-config.json

# Entrabot compatibility path (stdin)
cat .mxc-build/smoke-config.json | .mxc-build/target/release/mxc-exec-mac --experimental
```

Both should print the configured command output and exit 0.

---

## 6. Where MXC fits entrabot

entrabot and MXC are **two halves of the same security thesis**, and they don't overlap — they compose:

| Concern | entrabot today | MXC |
|---|---|---|
| **Identity** | Entra Agent User (Blueprint → Agent Identity → Agent User); cert in OS keystore | Local ID or Entra-bound identity for *session* isolation (Windows, vision layer) |
| **Attribution** | Every Graph action signed by the agent's object ID; audit log | All container activity attributed to the session identity (Windows) |
| **Authorization** | CA / DLP / sponsor-channel gates on the agent's *Microsoft 365* actions | OS policy on what the agent's *local code execution* can touch (files/net/UI) |
| **Containment** | ❌ **none** — agent tool/code execution runs with the user's full local authority | ✅ this is MXC's entire job |

**The gap MXC closes:** entrabot makes the agent a first-class *cloud* principal but its *local* execution (any shell/code the agent runs) inherits the human's full session. MXC is the missing OS-enforced boundary around that local execution. Pairing them yields the complete story the Build blog describes: **identity + attribution (entrabot/Entra) + containment (MXC)**.

**The headline integration constraint:** the MXC SDK is **TypeScript**; entrabot is **Python**. So entrabot would integrate via **surface B — the native binary + JSON policy**, not the SDK:
1. Detect MXC: probe for `mxc-exec-mac` (macOS) / `wxc-exec.exe` (Windows) / `lxc-exec` (Linux) via `MXC_BIN_DIR`.
2. Build a policy JSON (schema `0.6.0-alpha`) from a Python policy builder that mirrors entrabot's `platform/` abstraction (Keychain/TPM/Keyring already has the per-OS shim — MXC's per-OS backend split maps onto it cleanly).
3. Shell out to the native binary with `--config-base64` to run agent-issued shell/code steps under containment.
4. On macOS, set `experimental.seatbelt.keychainAccess` only on the narrow paths that must reach a token; default-deny the rest.

This slots naturally next to the existing **AppContainer sandbox spike** already on entrabot's Windows roadmap (README "Open" list) — MXC's `processcontainer` *is* the AppContainer/BaseContainer path, so MXC likely **supersedes** that spike with a cross-platform, MS-maintained abstraction.

---

## 7. Gap analysis / open questions before we build

- **Q1. Python integration shape.** Native-binary-+-JSON (surface B) is the only viable path today. Do we wrap it as a new `entrabot/sandbox/` module with a `MxcSandbox` that mirrors the `platform/` per-OS split, or a thinner `run_contained(cmd, policy)` helper? (Lean: a small module with a policy builder + a `subprocess` runner around the native binary.)
- **Q2. What do we actually contain?** entrabot's own MCP tools are Graph API calls (already identity-scoped server-side). The thing worth containing is **agent-issued local code/shell execution** (the CLI host running model-generated commands). Confirm the threat model: we're containing the *coding-agent inner loop*, not the Teams/Graph tools.
- **Q3. macOS Keychain tension.** entrabot's auth reads private keys from the Keychain. A contained execution path that needs a token must opt into `keychainAccess`, which widens the sandbox. Decide: keep token acquisition *outside* the sandbox (pass a short-lived token in) vs. grant `keychainAccess` inside. (Lean: acquire outside, inject the token — least privilege.)
- **Q4. Not a security boundary yet.** MXC explicitly says profiles are overly permissive and not a boundary. Treat MXC as **defense-in-depth**, not a control we rely on, until it hardens. Don't let it relax any existing entrabot gate.
- **Q5. Schema churn.** `0.x` alpha — breaking changes allowed at any release. Pin a schema version, validate with `--dry-run` in CI, and isolate the policy-builder so a schema bump is a one-file change.
- **Q6. Windows session-isolation + Entra.** The richest fit (container activity attributed to an Entra identity) is **Windows-only and vision-layer**, not in the OSS repo. For a macOS-primary project this is aspirational; revisit when entrabot's Windows port matures and the Intune/Entra binding ships.
- **Q7. Codesigning/notarization.** Shipping a bundled `mxc-exec-mac` to users needs Apple Developer-ID signing + notarization. If we depend on MXC, decide whether we vendor a signed binary or require the user to install MXC themselves.

---

## 8. TL;DR for the team

- **MXC = OS-level, policy-driven sandbox for untrusted agent code.** Cross-platform (Win/Linux/**macOS**). JSON policy, default-deny, multi-backend (`process`/`vm`/`microvm`). TypeScript SDK (`@microsoft/mxc-sdk` v0.6.1, MIT) + native binary. Rust core.
- **It's containment, not identity.** entrabot already owns identity + attribution; MXC is the missing **local execution boundary**. They compose into the full Build-2026 "secure agent" story.
- **macOS works via Seatbelt** (experimental, schema `0.6.0-alpha`+) — `sandbox_init()`, per-process, ~10 ms startup, with a `keychainAccess` knob that intersects entrabot's `keyring` usage.
- **We integrate from Python via the native binary + JSON policy**, not the TS SDK. Likely supersedes the Windows AppContainer spike with a cross-platform abstraction.
- **Caveats:** early preview, explicitly *not a security boundary yet*, `0.x` schema churn, macOS network host-filtering/proxy unsupported. Use as **defense-in-depth**; never relax an existing gate for it.

---

## References

- `github.com/microsoft/mxc` — OSS repo (README, `docs/schema.md`, `docs/macos-support/seatbelt-backend.md`, `docs/sandbox-policy/v1/policy.md`, `sdk/README.md`). Primary source.
- `@microsoft/mxc-sdk` on npm — TypeScript SDK, v0.6.1, MIT.
- Windows Developer Blog, *"Windows platform security for AI agents"* (2026-06-02) — the Build 2026 announcement + vision layer (process/session/micro-VM/WSL/Windows-365 spectrum; Entra/Intune binding; partner quotes).
- `docs/platform-learnings/microsoft-agent-365.md` — the identity/governance plane MXC plugs into.
- `docs/platform-learnings/platform-macos.md` — entrabot's existing macOS Keychain / Seatbelt-adjacent notes.
