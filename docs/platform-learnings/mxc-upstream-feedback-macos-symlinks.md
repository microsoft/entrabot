# Upstream feedback for `microsoft/mxc` — macOS symlink canonicalization at the policy boundary

**Audience:** the MXC maintainers (`github.com/microsoft/mxc`).
**From:** the entrabot project (a third-party integrator embedding MXC for OS-enforced
local code execution behind an Entra Agent identity).
**Binary under test:** `mxc-exec-mac`, built from MXC **v0.6.1** (commit
`161598fd08a4fdd030f461de19af23ce4a310b41`), macOS **arm64**, Seatbelt backend,
invoked with `--experimental`, policy schema `0.6.0-alpha`, config piped on stdin.
**Date:** 2026-06-20.

This note is intentionally self-contained so it can be forwarded as-is. It reports one
concrete, reproducible behavior (Issue 1) and one design/security observation that
follows from it (Issue 2).

---

## TL;DR

1. **`mxc-exec-mac` enforces filesystem rules on the kernel-resolved (realpath) path, but
   builds the Seatbelt profile from the *literal* policy path.** On macOS, `/tmp`, `/var`,
   and `/etc` are symlinks into `/private`. A policy that grants `readwritePaths: ["/tmp"]`
   therefore **silently denies** all writes under `/tmp`, because the kernel resolves
   `/tmp/foo` → `/private/tmp/foo` at syscall time and the `(subpath "/tmp")` rule never
   matches. The failure is a generic `Operation not permitted` with no hint that symlink
   resolution is the cause.

2. **Consider canonicalizing policy paths (realpath) during profile generation — and note
   that the *order* of canonicalization vs. containment is security-relevant**, especially
   for `deniedPaths`. We hit the mirror-image of this in our own clamp layer and it would
   apply to MXC's allow/deny matching too.

---

## Issue 1 — Filesystem rules don't match symlinked allowlist paths (macOS)

### Reproduction

```bash
BIN=./mxc-exec-mac

# (A) Grant /tmp, write under /tmp  →  DENIED (unexpected)
echo '{
  "version":"0.6.0-alpha","containment":"process",
  "process":{"commandLine":"echo ok > /tmp/mxc-probe.txt","timeout":5000},
  "filesystem":{"readonlyPaths":["/tmp"],"readwritePaths":["/tmp"]},
  "network":{"defaultPolicy":"block"},"keychainAccess":false
}' | "$BIN" --experimental
# -> /bin/sh: /tmp/mxc-probe.txt: Operation not permitted     (exit 1)

# (B) Grant /private/tmp (the realpath), write under /tmp  →  ALLOWED
echo '{
  "version":"0.6.0-alpha","containment":"process",
  "process":{"commandLine":"echo ok > /tmp/mxc-probe.txt","timeout":5000},
  "filesystem":{"readonlyPaths":["/private/tmp"],"readwritePaths":["/private/tmp"]},
  "network":{"defaultPolicy":"block"},"keychainAccess":false
}' | "$BIN" --experimental
# -> (exit 0), file written
```

The only difference between (A) and (B) is `/tmp` vs `/private/tmp` in the policy. The
command and the file it touches are identical.

### Root cause

macOS keeps several top-level directories as symlinks into `/private`:

```
/tmp  -> /private/tmp
/var  -> /private/var      # note: the real $TMPDIR lives under /var/folders/...
/etc  -> /private/etc
```

Seatbelt rules such as `(allow file-write* (subpath "/tmp"))` are matched by the kernel
against the **canonical** path of the file being accessed. Because the profile carries the
literal `/tmp` rather than the resolved `/private/tmp`, the rule does not fire for
`/private/tmp/...`, and the access is denied.

### Why this is a sharp edge for integrators

- **It's silent and non-obvious.** The error is a generic `Operation not permitted`. Nothing
  in the output points at symlink resolution. We only diagnosed it via differential testing
  of the binary (granting `/private/tmp` vs `/tmp`).
- **`/tmp` is the most obvious thing to grant.** It's the canonical "scratch space" an agent
  needs for outputs. The first policy a developer writes is the one that fails.
- **`$TMPDIR` is also affected.** The real per-user temp dir on macOS is
  `/var/folders/<...>/T/`, i.e. under the `/var → /private/var` symlink, so the same trap
  applies to anything using `tempfile`/`mkstemp` defaults.
- **The discovery helpers may paper over or expose this depending on what they return.**
  If `getTemporaryFilesPolicy()` returns `/tmp` (literal) it would inherit the bug; if it
  returns the realpath it would mask it. Either way the literal-path contract is implicit.

### Suggested fixes (any one would help; not mutually exclusive)

1. **Canonicalize `readonlyPaths` / `readwritePaths` (and `deniedPaths`) during profile
   generation** — resolve symlinks to realpaths before emitting Seatbelt rules. This makes
   the obvious policy "just work".
2. **Or emit rules for both the link and its target** when a granted path is (or traverses)
   a symlink.
3. **Or, at minimum, document the contract explicitly** ("policy filesystem paths must be
   realpaths on macOS; `/tmp`, `/var`, `/etc` are symlinks") and **fail loudly** — e.g.
   `--dry-run`/validation could warn when a policy path differs from its realpath.

A one-line `realpath()` normalization in the macOS profile builder would have saved us a
half-day of binary-level debugging, and will bite every macOS integrator who grants `/tmp`.

---

## Issue 2 — Canonicalization order is security-relevant (allow *and* deny matching)

This is a design note rather than a bug report; we raise it because we hit the exact mirror
of it in our own ceiling-clamp layer and the same reasoning applies to MXC's policy matching.

When you move to canonicalizing policy paths (Issue 1, fix #1), the **order** of operations
matters:

- **Canonicalize first, then match.** Resolve the realpath of both the policy path and the
  accessed path, *then* test containment/equality. This is safe.
- **Match on un-resolved strings (e.g. prefix check), then canonicalize.** This is unsafe:
  a symlink located *inside* a granted directory can point *outside* it, and a naive
  string-prefix test admits it.

Concretely, with a grant of `readwritePaths: ["/work/granted"]` and a symlink
`/work/granted/evil -> /work/secret`:

- A string-prefix check sees `/work/granted/evil` starts with `/work/granted/` → **admit**
  (escape: writes land in `/work/secret`).
- A realpath-first check resolves to `/work/secret`, which is **not** under `/work/granted`
  → **deny** (correct).

For `deniedPaths` the failure is inverted but equally bad: if a denied path is given as a
symlink and only the literal is matched, the *real* target remains reachable (a deny that
doesn't deny). Since MXC's own README currently cautions that profiles "should not be
treated as security boundaries yet," symlink handling at the profile-generation boundary is
concrete, actionable hardening in exactly that area.

**Recommendation:** when canonicalizing (Issue 1), do it as **realpath-first, then
allow/deny matching**, for both allow and deny lists, and treat `deniedPaths` resolution as
load-bearing.

---

## How we worked around it downstream (for reference)

In entrabot we don't rely on MXC to canonicalize. Our policy layer:

1. Resolves the operator-set "ceiling" and the agent-requested paths to realpaths
   (`expanduser` + `realpath`) and admits a request only if it equals or is a descendant of
   a ceiling entry — **canonicalize-first, then containment** (so the symlink-escape in
   Issue 2 is closed on our side).
2. Passes the resolved realpaths to MXC, which is what makes `/tmp` writes actually work
   (Issue 1 workaround — we hand MXC `/private/tmp`).

This works, but every integrator will independently rediscover both points. Pushing the
realpath normalization (and the realpath-first ordering) into MXC would make the obvious
policy correct by default and remove a silent, security-relevant footgun.

---

## Environment

| Field | Value |
|---|---|
| MXC version | v0.6.1 (commit `161598fd08a4fdd030f461de19af23ce4a310b41`) |
| Binary | `mxc-exec-mac`, Seatbelt backend, `--experimental` |
| Policy schema | `0.6.0-alpha` |
| OS | macOS, arm64 (Apple Silicon) |
| Delivery | config JSON piped on stdin |

Happy to provide the full differential-test harness or pair on a repro if useful.
