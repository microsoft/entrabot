# Provisioner credential migration

**Original severity:** High
**Filed:** 2026-04-19
**Status:** Resolved in code; existing tenants must run and verify migration

## Original issue

Early versions of Entrabot created a long-lived client secret for the `EntraBot Agent ID Provisioner` app and stored it in `.entrabot-state.json`. A gitignored plaintext state file is not an acceptable boundary for a credential that can create and grant permissions to Agent Identity resources.

## Current implementation

`scripts/entra_provisioning.py` now:

1. Authenticates the Provisioner with `azure.identity.CertificateCredential`.
2. Generates a self-signed certificate and stores the private key through the OS credential store rather than in repository state.
3. Purges `PROVISIONER_CLIENT_SECRET` from legacy state before certificate authentication proceeds.
4. Enumerates and removes legacy password credentials from the Provisioner app registration.
5. Fails with `ProvisionerBootstrapError` when credential migration cannot be completed safely.

The Blueprint certificate and Provisioner certificate remain separate credentials with separate purposes.

## Existing-machine action

Code migration does not prove that every previously provisioned tenant has been cleaned up. On each existing developer machine:

```bash
./scripts/setup.sh --migrate --use-blueprint=<blueprint-app-id>
```

Then verify:

```bash
# Local state must not contain a secret value.
grep -n 'PROVISIONER_CLIENT_SECRET' .entrabot-state.json

# Tenant app must have no password credentials.
az ad app show \
  --id "$(python -c 'import json; print(json.load(open(".entrabot-state.json"))["PROVISIONER_CLIENT_ID"])')" \
  --query 'passwordCredentials' \
  --output json
```

Expected results: the grep prints nothing and Azure CLI returns `[]`. Also run `./status.sh --health-only --strict` to confirm certificate-backed provisioning and Agent User authentication remain healthy.

## Regression rule

Do not reintroduce `ClientSecretCredential`, persist provisioner secrets, or leave password credentials beside the certificate credential. Tests and code comments may reference the legacy state key only to validate or perform migration.
