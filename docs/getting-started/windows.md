# Windows Installation

Assumes you have completed [Prerequisites](prerequisites.md). Use PowerShell 7 (`pwsh`), not Windows PowerShell 5.1.

## 1. Install platform prerequisites

```powershell
.\scripts\prereqs-windows.ps1
```

Windows setup prefers a TPM-backed CNG key and falls back to a software-protected key when TPM provisioning is unavailable.

## 2. Create a fresh identity chain

```powershell
pwsh -File scripts/setup-windows.ps1 -NewChain -UpnSuffix workstation
```

For an existing Blueprint:

```powershell
pwsh -File scripts/setup-windows.ps1 -UseBlueprint <blueprint-app-id>
```

See [scripts/setup-windows.ps1 reference](../reference/scripts/setup/setup-windows-ps1.md) for the full option list.

## Next step

Continue to [Verify Your Agent Identity](verify.md).
