# requires -Version 7.0
<#
.SYNOPSIS
  EntraClaw — Windows cert rotation deploy.

.DESCRIPTION
  Wraps scripts/rotate_cert_windows.py. Captures the current cert's
  public DER (must happen BEFORE generating the new cert — for non-
  exportable TPM keys this is the only chance to grab it), generates
  a new cert, hands both DERs to rotate_cert_windows.rotate(), and
  deletes the old cert from Cert:\CurrentUser\My only after smoke
  passes.

  Failure modes:
    - Initial PATCH fails → rotate raises RotationFailed; nothing
      changed; old cert still in store; .env still points at old cert.
    - Smoke fails → rotate triggers a rollback PATCH + .env restore +
      MSAL cache invalidation, raises RotationRolledBack.
    - Rollback PATCH fails → rotate raises ManualInterventionRequired;
      operator must triage by hand.
#>

[CmdletBinding()]
param([switch]$Help)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

if (-not $IsWindows) { throw "deploy-windows.ps1 must run on Windows." }

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ScriptDir   = Join-Path $ProjectRoot 'scripts'

if (-not (Test-Path $VenvPython)) {
    throw "venv not found at $VenvPython. Run setup-windows.ps1 first."
}

# Read current SHA-1 from .env so we know which cert to dump and rotate.
$envText = Get-Content (Join-Path $ProjectRoot '.env') -Raw
$oldSha1 = ($envText | Select-String '(?m)^ENTRACLAW_BLUEPRINT_CERT_SHA1=(.+)$').Matches[0].Groups[1].Value
if (-not $oldSha1) { throw "ENTRACLAW_BLUEPRINT_CERT_SHA1 missing from .env" }

Write-Host "Capturing public DER of cert $oldSha1 BEFORE generating new cert..."
$oldDerPath = Join-Path $env:TEMP "entraclaw-old-$(Get-Random).cer"
$cert = Get-Item "Cert:\CurrentUser\My\$oldSha1" -ErrorAction Stop
[IO.File]::WriteAllBytes($oldDerPath, $cert.GetRawCertData())
Write-Host "  Saved old DER to $oldDerPath"

Write-Host "Generating new cert..."
$newDerPath = Join-Path $env:TEMP "entraclaw-new-$(Get-Random).cer"
$genOut = & $VenvPython (Join-Path $ScriptDir 'generate_windows_cert.py') `
    --subject "CN=entraclaw-blueprint" `
    --days 365 `
    --export-der $newDerPath
if ($LASTEXITCODE -ne 0) { throw "generate_windows_cert.py failed" }

$newThumb   = ($genOut | Select-String '^thumbprint=(.+)$').Matches[0].Groups[1].Value
$newX5tS256 = ($genOut | Select-String '^x5t_s256=(.+)$').Matches[0].Groups[1].Value

# Hand off to rotate_cert_windows.py via a Python driver that wires the
# graph_patch + smoke_test + delete_old callables to real Graph + a
# fresh acquire_agent_user_token call.
$driver = @"
import sys, base64, httpx
from pathlib import Path
sys.path.insert(0, r'$ScriptDir')
import rotate_cert_windows as rcw
from entraclaw.config import get_config
from entraclaw.tools.teams import acquire_agent_identity_token, acquire_agent_user_token

cfg   = get_config()
state = rcw.RotationState(
    env_path=Path(r'$ProjectRoot') / '.env',
    msal_cache_path=Path(r'$env:LOCALAPPDATA') / 'entraclaw' / '.msal-cache.bin',
    blueprint_object_id=cfg.blueprint_object_id,
)
old_der = Path(r'$oldDerPath').read_bytes()
new_der = Path(r'$newDerPath').read_bytes()

def graph_patch(*, token, der_bytes):
    body = {'keyCredentials': [{
        'type': 'AsymmetricX509Cert',
        'usage': 'Verify',
        'key': base64.b64encode(der_bytes).decode(),
    }]}
    r = httpx.patch(
        f'https://graph.microsoft.com/v1.0/applications/{cfg.blueprint_object_id}',
        headers={'Authorization': f'Bearer {token}'},
        json=body,
        timeout=15.0,
    )
    return r.status_code

def smoke_test():
    try:
        acquire_agent_user_token(cfg)
        return True
    except Exception:
        return False

def delete_old(sha1):
    import subprocess
    subprocess.run(['pwsh','-NoProfile','-Command',f'Remove-Item Cert:\\\\CurrentUser\\\\My\\\\{sha1}'], check=False)

rcw.rotate(
    state=state,
    old_der=old_der,
    new_thumbprint='$newThumb',
    new_x5t_s256='$newX5tS256',
    new_der=new_der,
    graph_patch=graph_patch,
    smoke_test=smoke_test,
    delete_old_cert=delete_old,
    graph_token_provider=lambda: acquire_agent_identity_token(cfg),
)
print('rotation succeeded')
"@

& $VenvPython -c $driver
if ($LASTEXITCODE -ne 0) {
    Write-Host "Rotation failed — see exception above. Triage required if ManualInterventionRequired raised." -ForegroundColor Red
    exit 1
}
Write-Host "Rotation complete." -ForegroundColor Green
