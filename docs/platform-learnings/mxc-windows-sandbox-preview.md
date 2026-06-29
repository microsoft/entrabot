# MXC Windows Preview — What the `processcontainer` Build Actually Exposes

**Date:** 2026-06-25
**Author:** Windows port (entrabot PR #86, `feat/mxc-sandbox-integration`)
**Status:** Verified against a real Windows preview build — not announcement-only.
**Companion to:** [`mxc-windows-sandbox.md`](mxc-windows-sandbox.md) (the pre-build
research) and [ADR-007](../decisions/007-mxc-sandbox-integration.md).

This note records what the **shipped** MXC Windows binary actually does, measured
on a real machine, versus what the earlier platform research inferred from the
Build-2026 announcement and the SDK README. The macOS instance literally could
not test any of this; everything below was run against the binary.

## Environment under test

- **Host:** Windows 11, build **28120** (26H1), **ARM64**.
- **Binary:** `wxc-exec.exe` from `@microsoft/mxc-sdk` **v0.7.0** (npm,
  Microsoft-published, 41.7 MB). Ships **both** `bin/arm64/` and `bin/x64/`
  `wxc-exec.exe` (plus `lxc-exec`, `mxc-exec-mac`, and the experimental
  `wxc-windows-sandbox-*`, `winhttp-proxy-shim`, `mxc-diagnostic-console`).
- **Python:** 3.13, `sys.platform == "win32"`, `platform.machine() == "ARM64"`.

Pinned SHA256 (taken from the published package, not a local build):

| Arch | `wxc-exec.exe` SHA256 |
|---|---|
| arm64 | `e430d0e4f44f616e91db684f8d825a6dc93e06a1262b8d00bcaac7522a317aab` |
| x64   | `db0a3422be9e1b396cc1b2547c70ff16b27412438a31c10a45abf370cac86ae2` |

## What matched the research

- **`processcontainer` is a default, non-experimental backend.** `run_code`
  works **without** `--experimental` once the binary is wired (confirmed by a
  real run, exit 0). The minimum build is 26100 (24H2); this host (28120) is well
  above it.
- **The abstract `process` intent resolves to `processcontainer`.** A config with
  `"containment": "process"` is rewritten by the binary to
  `"containment": "processcontainer"` (visible in `--dry-run` output).
- **Filesystem allow-listing is real and kernel-enforced.** With only a scratch
  dir in `readwritePaths`, a write **into** it succeeds (exit 0, file created); a
  write **outside** it fails with `Access is denied.` (exit 1, no file). This is
  the §2 demo matrix, reproduced on Windows through the exact entrabot
  ceiling→clamp→canonicalize→MXC chain.
- **Network host filtering is NOT enforced on Windows.** The README states it
  outright (`network.allowedHosts` / `blockedHosts` have no effect; only
  `network.defaultPolicy` and `network.proxy` constrain egress). `get_capabilities`
  therefore reports `network_host_filtering=False`, and `clamp_to_ceiling` fails
  closed if a policy ever asks for an allow-list.

## What the research got wrong / didn't know (load-bearing)

1. **No stdin config path. Use a file or `--config-base64`.** The macOS runner
   streams policy JSON on stdin (via a local patch). `wxc-exec.exe` does **not**
   read stdin: the CLI is `wxc-exec.exe [CONFIG_PATH] [--config <path>]
   [--config-base64 <b64>] [-- <COMMAND>...]`. The Windows runner uses
   `--config-base64` (no temp file to create/secure/clean up).

2. **The parser strictly rejects unknown top-level fields.** entrabot's
   `build_policy()` emitted a top-level `"keychainAccess": false`. The real
   v0.7.0 binary rejects it:
   `Unknown top-level field(s) in config: keychainAccess`. This was a
   **cross-platform latent bug** — the macOS v0.6.1 build tolerated it, the
   Windows v0.7.0 build does not. Fix: stop emitting the field entirely. No MXC
   schema version defines a top-level `keychainAccess`; on macOS it lives under
   `experimental.seatbelt.keychainAccess`. `keychain_access` stays denied by
   default-deny regardless, so omitting it is safe, not a relaxation.

3. **`process.commandLine` runs via `CreateProcessW` — there is no implicit
   shell.** `"echo hi"` fails (`CreateProcessW failed: cannot find the file`,
   because `echo` is a `cmd` builtin); `"whoami"` failed with
   `STATUS_DLL_INIT_FAILED`. Commands needing shell builtins, redirection, pipes,
   or PATH resolution must be invoked as `cmd /c ...`. The agent (caller) owns
   this; the runner passes `commandLine` through verbatim. Redirection like
   `cmd /c echo X > file` works and is enforced against the filesystem ceiling.

4. **`processcontainer` auto-grants the system DLL baseline.** A `cmd /c echo`
   succeeded even with `readonlyPaths: []` — the backend supplies the baseline
   needed to load `cmd.exe` + system DLLs (analogous to Seatbelt's `/usr/lib`
   baseline). Operators do not need to add `C:\Windows` to read every command.

5. **`platform.machine()` is upper-case on Windows (`AMD64` / `ARM64`).** This
   broke two assumptions: the `MXC_BIN_DIR/<arch>/<binary>` lookup and the
   `PINNED_HASHES` key. The npm package uses lower-case `bin/arm64` and `bin/x64`.
   entrabot now normalizes arch (`AMD64`→`x64`, `ARM64`→`arm64`) for both the
   lookup and the hash key (`normalize_arch` in `binary.py`).

6. **The `os.pathsep` ceiling bug was fatal on Windows.** The operator ceiling
   was parsed with `.split(":")`. On Windows a single `C:\Users\me` ceiling entry
   split into `["C", "\\Users\\me"]`, shredding every path at the drive-letter
   colon and making the ceiling unusable. Now parsed with `os.pathsep`
   (`;` on Windows). Operator ceiling lists are **`;`-separated on Windows**.

## Schema version

- Current **stable** schema is **`0.7.0-alpha`** (the README says "pick 0.7.0-alpha
  for new code"). entrabot still emits **`0.6.0-alpha`**, which the v0.7.0 binary
  accepts without complaint (validated by `--dry-run`, exit 0). Both are "Stable".
  Policy-building stays isolated in `policy.py`, so a bump to `0.7.0-alpha` is a
  one-line change when we choose to make it.
- Experimental backends, the `experimental.*` block, and the state-aware
  lifecycle live in the `0.8.0-dev` schema. The parser accepts them only with
  `--experimental`. **Schema choice affects editor validation, not runtime.**

## Phase 2 reconnaissance — session isolation + Entra binding

The Phase 2 thesis (container activity attributed to the entrabot Agent User) hinges
on the **`isolation_session`** backend. Findings from this preview:

- `isolation_session` is present in the SDK's backend table but marked
  **experimental**, "concrete-only" (no abstract intent maps to it), and requires
  a **higher minimum build — 26300.8553 (Insider Preview)** than this host
  (28120). It is the only backend with a state-aware
  provision→start→exec→stop→deprovision lifecycle.
- **No Entra-binding surface is exposed in the OSS binary or SDK.** The CLI has no
  `--session`, no identity, no tenant flag; the only session-shaped surfaces are
  `--delete`/`--containername` (profile cleanup) and the WSLC/Hyperlight setup
  flags. The "attribute the container to an Entra identity" story remains in the
  **Windows + Agent 365 vision/governance layer** (Intune), not in the shipping
  `wxc-exec.exe`.
- **Conclusion:** Phase 2 stays a stub (`session.py`, `identity_binding()` →
  `NotImplementedError`). The preview does **not** yet expose the APIs needed to
  bind a container to the entrabot Agent User. Re-check when (a) the host can run
  `isolation_session` (build ≥ 26300.8553) and (b) an identity-binding surface
  appears in the SDK/CLI or a documented Intune/Entra API.

## Defense-in-depth caveat (unchanged)

MXC still self-describes as **not a security boundary yet** ("profiles are overly
permissive"). The filesystem enforcement demonstrated here is real, but MXC remains
**defense-in-depth** layered under entrabot's existing identity/attribution/audit
gates — it must never relax one. (See `mxc-windows-sandbox.md` §7 Q4.)
