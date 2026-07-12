# `scripts/health_check.py`

## Purpose

Compatibility wrapper for the consolidated Agent Identity status command.
All health-check logic — the Graph queries, the pass/fail/warn/skip
checks, the exit-code rules — lives in
[`show_agent_status.py`](show-agent-status-py.md); this entry point exists
only so that users and scripts still calling `health_check.py` by name
keep working.

## Requirements

Identical to [`show_agent_status.py`](show-agent-status-py.md#requirements):
Python 3.12+ with `entrabot` and `azure-identity` installed, the Provisioner
app bootstrapped with its certificate in the OS keystore. This script
imports `show_agent_status` directly and calls into it — there is no
separate dependency surface.

## Usage

```bash
python scripts/health_check.py
python scripts/health_check.py --json
python scripts/health_check.py --help
```

## Options

`health_check.py` defines no options of its own. Its `main(argv)`:

- If `--help` or `-h` appears anywhere in `argv`, forwards `argv`
  **unchanged** to `show_agent_status.main(argv)` — this shows
  `show_agent_status.py`'s own help text, not a forced health-only run.
- Otherwise, forwards `[*argv, "--health-only"]` to
  `show_agent_status.main(...)` — i.e. it always appends `--health-only`,
  so any other flag you pass (e.g. `--json`) is combined with health-only
  mode. See [`show_agent_status.py` → Options](show-agent-status-py.md#options)
  for what each flag does once forwarded.

## Effects

No independent logic: `main()` calls straight into
`show_agent_status.main(...)` as described above. No Graph calls, file
reads, or output formatting happen in this module itself.

## Exit behavior

Returns exactly whatever `show_agent_status.main(...)` returns — `0` on
success with no failing checks, `1` when a health check fails (always
true here, since `--health-only` is force-appended) or the Provisioner
token can't be acquired. `--help` exits `0` via argparse, same as calling
`show_agent_status.py --help` directly.

## Common failures

Same failure modes as `show_agent_status.py --health-only`; see
[`show_agent_status.py` → Common failures](show-agent-status-py.md#common-failures).

## Related commands

- [`show_agent_status.py`](show-agent-status-py.md) — the real implementation this wraps.
- [`status.sh`](status-sh.md) / [`status-windows.ps1`](status-windows-ps1.md) — full status wrappers (not health-only).
- [Operations scripts index](../index.md#operations)
