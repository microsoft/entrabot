# Next: Windows Dev Environment

> **SUPERSEDED 2026-04-28** by [`PLAN-windows-port.md`](./PLAN-windows-port.md).
> Kept for the Azure VM provisioning recipe (still useful as a sub-step).
> The three-hop port itself is covered by the new plan.

> Scriptable Azure VM setup for developing and demonstrating Entrabot on Windows.

## Overview

We need an Entra-joined Windows 11 VM with Copilot CLI, Python, and our repo — fully automated so we can tear down and recreate at will. The VM must have WAM/PRT access for seamless identity bootstrap.

## Prerequisites (Manual, One-Time)

### 1. M365 Tenant Setup

Your Entra tenant needs:

| Requirement | Why | Cost |
|-------------|-----|------|
| **M365 E3 or Business Basic license** (your user) | Teams access + Graph API | ~$8-36/user/month |
| **Teams enabled** in the tenant | Chat API requires Teams | Included in M365 |
| **Optional: Agent User account** | Separate identity for agent messages in Teams | Additional M365 license (~$8/month) |

For MVP, skip the Agent User — use your own account. The OBO token's `azp` claim still distinguishes agent from human in Entra sign-in logs.

### 2. Entra App Registration

Create an app registration for the Entrabot agent:

```bash
# Create the app registration
# ⚠️ VERIFY GUIDS FIRST: run this to confirm permission IDs are correct:
#   az ad sp show --id 00000003-0000-0000-c000-000000000000 \
#     --query "oauth2PermissionScopes[?value=='Chat.Create' || value=='ChatMessage.Send' || value=='Chat.ReadWrite' || value=='User.Read' || value=='Presence.ReadWrite'].{name:value, id:id}" -o table
az ad app create \
  --display-name "Entrabot Agent" \
  --sign-in-audience "AzureADMyOrg" \
  --required-resource-accesses '[{
    "resourceAppId": "00000003-0000-0000-c000-000000000000",
    "resourceAccess": [
      {"id": "9ff7295e-131b-4d94-90e1-69fde507ac11", "type": "Scope"},
      {"id": "116b7235-7cc6-461e-b163-8e55691d839e", "type": "Scope"},
      {"id": "7427e0e9-2fba-42fe-b0c0-848c9e6a8182", "type": "Scope"},
      {"id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d", "type": "Scope"},
      {"id": "b7d083d5-8a28-4b4d-be3c-3d1c5c5f2c55", "type": "Scope"}
    ]
  }]'

# Scopes requested:
# - Chat.Create (9ff7295e)
# - ChatMessage.Send (116b7235)
# - Chat.ReadWrite (7427e0e9)
# - User.Read (e1fe6dd8)
# - Presence.ReadWrite (b7d083d5)
```

Then grant admin consent:
```bash
az ad app permission admin-consent --id <app-id>
```

Generate a client secret (for OBO exchange):
```bash
az ad app credential reset --id <app-id> --display-name "Entrabot MVP"
# SAVE the password — this is the client secret for ConfidentialClientApplication
# ⚠️ MVP ONLY — production must use split architecture or certificate auth.
#    The client secret on a device is a crown-jewel credential (see proposals.md Risk #1).
```

### 3. Agent ID Blueprint (Entra Beta API)

> ⚠️ Agent IDs require the **beta** Graph API and Frontier/Workload Identities Premium licensing.
> If not available in your tenant, skip this step — OBO still works without Agent IDs
> (the `azp` claim in sign-in logs still identifies the agent app).

Register an Agent ID blueprint for the Entrabot agent type:

```http
POST https://graph.microsoft.com/beta/agentIdentityBlueprints
Content-Type: application/json

{
  "displayName": "Entrabot Code Agent",
  "description": "Autonomous coding agent with Teams integration",
  "appId": "<app-registration-client-id>"
}
```

## VM Provisioning Script

```bash
#!/bin/bash
# provision-windows-vm.sh — Create an Entra-joined Windows 11 VM for Entrabot dev

RESOURCE_GROUP="entrabot-dev"
VM_NAME="entrabot-win11"
LOCATION="westus2"
ADMIN_USER="entrabotadmin"

# Create resource group
az group create --name $RESOURCE_GROUP --location $LOCATION

# Create the VM (Windows 11 Enterprise, Entra-joined)
az vm create \
  --resource-group $RESOURCE_GROUP \
  --name $VM_NAME \
  --image "MicrosoftWindowsDesktop:windows-11:win11-24h2-ent:latest" \
  --size "Standard_D4s_v5" \
  --admin-username $ADMIN_USER \
  --admin-password "$(openssl rand -base64 16)!" \
  # ⚠️ NOTE: This password is ephemeral — save it to a Key Vault or file if you need
  # local admin fallback. Primary auth is via Entra join (AAD login extension below).
  --public-ip-sku Standard \
  --nsg-rule RDP

# Enable Entra join (AAD login extension)
az vm extension set \
  --resource-group $RESOURCE_GROUP \
  --vm-name $VM_NAME \
  --name AADLoginForWindows \
  --publisher Microsoft.Azure.ActiveDirectory

# Assign your user the "Virtual Machine User Login" role
az role assignment create \
  --assignee "<your-user-upn>@example.com" \
  --role "Virtual Machine User Login" \
  --scope "/subscriptions/<sub-id>/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Compute/virtualMachines/$VM_NAME"

echo "VM created. Connect via: az ssh vm -n $VM_NAME -g $RESOURCE_GROUP"
```

## Post-Provisioning Setup Script

Run this inside the VM after RDP/SSH in:

```powershell
# setup-entrabot.ps1 — Install Copilot CLI, Python, and Entrabot on Windows

# Install Python 3.12
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements

# Install Copilot CLI
winget install GitHub.Copilot --accept-package-agreements --accept-source-agreements

# Install Git
winget install Git.Git --accept-package-agreements --accept-source-agreements

# Refresh PATH
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

# Clone the repo
git clone "<your-internal-or-github-remote-url>" C:\entrabot
cd C:\entrabot

# Create venv and install
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Verify
python --version
pytest --version
copilot --version

Write-Host "Entrabot dev environment ready. Launch 'copilot' to start."
```

## Connection

```bash
# RDP (traditional)
az vm show -g entrabot-dev -n entrabot-win11 --show-details --query publicIps -o tsv
# → RDP to that IP, sign in with your Entra credentials

# SSH (if enabled)
az ssh vm -n entrabot-win11 -g entrabot-dev
```

## Teardown

```bash
# Delete everything when done
az group delete --name entrabot-dev --yes --no-wait
```

## Cost Estimate

| Resource | SKU | Cost |
|----------|-----|------|
| Windows 11 VM | Standard_D4s_v5 (4 vCPU, 16 GB) | ~$0.19/hr (~$140/month if always on) |
| OS Disk | 128 GB Premium SSD | ~$19/month |
| Public IP | Standard | ~$4/month |
| **Total (dev hours only)** | ~8 hrs/week | **~$8/week** |

Deallocate the VM when not in use: `az vm deallocate -g entrabot-dev -n entrabot-win11`
