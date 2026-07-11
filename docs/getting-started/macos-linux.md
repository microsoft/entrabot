# macOS and Linux Installation

Assumes you have completed [Prerequisites](prerequisites.md).

## 1. Install platform prerequisites

=== "macOS"

    ```bash
    ./scripts/prereqs-macos.sh
    ```

    Installs the Azure CLI and confirms Keychain access for certificate storage.

=== "Linux"

    Install Python 3.12+, the Azure CLI, `git`, and a Secret Service–compatible keyring (e.g. `gnome-keyring` or KWallet).

## 2. Create a fresh identity chain

```bash
# Replace "workstation" with a short unique label for this Agent User.
./scripts/setup.sh --new --with-upn-suffix=workstation
```

To attach this device to an existing Blueprint instead:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

Use `--agent-user-upn=<existing-upn>` or `--with-upn-suffix=<label>` when the Blueprint has multiple Agent Users and auto-discovery would be ambiguous. Run `./scripts/setup.sh --help` for storage, Work IQ, migration, and status options — see [scripts/setup.sh reference](../reference/scripts/setup/setup-sh.md) for the full option list.

## Next step

Continue to [Verify Your Agent Identity](verify.md).
