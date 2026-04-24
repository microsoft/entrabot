# GitHub CLI (gh)

> **Research date:** July 2025
> **Relevance:** Entraclaw agent identity, OBO token flows, Teams bidirectional communication

## Overview

GitHub CLI (`gh`) is GitHub's official open-source command-line tool (repo: [github/cli](https://github.com/cli/cli)), written in Go. It brings pull requests, issues, Actions, and the full GitHub API to the terminal. It ships on macOS, Linux, and Windows.

**Why it matters to Entraclaw:**

1. **Auth model precedent** — `gh auth` implements OAuth Device Flow, token storage in OS keychains, multi-account switching, and environment-variable overrides. This is the exact pattern we need for Agent IDs that authenticate on behalf of humans.
2. **Remote session steering** — Copilot CLI's `--remote` / session persistence / `/delegate` model is the closest existing analogy to our "agent running locally, human steering from Teams" scenario.
3. **Extension model** — `gh extension` shows how to build composable agent capabilities that inherit the host CLI's auth context. An Entraclaw agent could theoretically be a `gh` extension.
4. **`go-gh` SDK** — The official Go SDK for extensions auto-inherits `gh`'s stored credentials, demonstrating zero-friction OBO token delegation to child processes.

---

## Key APIs / Interfaces

### Core Command Groups

```
gh auth        # Authentication (login, logout, status, refresh, switch, token, setup-git)
gh repo        # Repository operations (clone, create, fork, view, list, archive, delete)
gh pr          # Pull request lifecycle (create, list, view, checkout, merge, review, diff)
gh issue       # Issue management (create, list, view, close, reopen, edit)
gh run / workflow  # GitHub Actions (list, view, watch, rerun, download)
gh api         # Raw REST/GraphQL API access (the Swiss Army knife)
gh extension   # Extension management (create, install, list, upgrade, remove)
gh copilot     # AI agent (now standalone Copilot CLI as of late 2025)
gh alias       # Command shortcuts
gh config      # Configuration management
```

### `gh api` — Direct API Access

The most powerful command for automation. Handles auth automatically.

```bash
# REST — list repo tags
gh api repos/cli/cli/tags --jq '.[].name'

# GraphQL — get last 5 issues
gh api graphql --field query='
  query {
    repository(owner:"cli", name:"cli") {
      issues(last:5) {
        nodes { title url }
      }
    }
  }
'

# Pagination — fetch ALL releases
gh api repos/cli/cli/releases --paginate --slurp --jq '.[].tag_name'

# POST with body
gh api repos/OWNER/REPO/issues --method POST \
  --field title="Bug report" --field body="Details here"

# Check rate limit
gh api rate_limit --jq '.rate'
```

Key flags: `--method`, `--field`, `--raw-field`, `--header`, `--jq`, `--template`, `--paginate`, `--slurp`, `--input`, `--hostname`.

Context variables `{owner}`, `{repo}`, `{branch}` auto-resolve from git context or `GH_REPO`.

### `gh auth token` — Programmatic Token Access

```bash
# Print the current OAuth token for github.com
gh auth token

# Print token for an enterprise host
gh auth token --hostname github.mycompany.com
```

This is critical for Entraclaw: an agent process can call `gh auth token` to obtain a valid token without managing credentials directly.

---

## Auth & Identity Model

### Authentication Flows

| Flow | Trigger | Best For | Token Storage |
|------|---------|----------|---------------|
| **Web-based OAuth (Browser)** | `gh auth login` or `--web` | Interactive developer use | OS credential store |
| **OAuth Device Flow** | Auto in headless env, or forced | CLI tools, SSH sessions, agents | OS credential store |
| **Personal Access Token** | `--with-token` flag or pipe | Scripting, one-off automation | Not persisted (stdin) |
| **Environment Variable** | `GH_TOKEN` / `GITHUB_TOKEN` | CI/CD, containers, agents | Not stored (env only) |

### OAuth Device Flow (Key for Entraclaw)

The Device Flow is GitHub's recommended pattern for CLI tools and is what `gh auth login` uses in headless environments:

1. CLI requests a device code from GitHub
2. User visits `https://github.com/login/device` on any browser
3. User enters the code and authorizes
4. CLI polls GitHub until authorization completes
5. CLI receives OAuth token and stores it

**This is directly analogous to how an Entraclaw agent could authenticate:**
- Agent starts and requests a device code
- Human approves in Teams (or browser)
- Agent receives an OBO token scoped to that human's permissions

### Token Storage

**Default locations (in precedence order):**
- `$XDG_CONFIG_HOME/gh/` (if `$XDG_CONFIG_HOME` set)
- `$AppData/GitHub CLI/` (Windows)
- `$HOME/.config/gh/` (default fallback)

**Files:**
- `hosts.yml` — per-host authentication credentials (OAuth tokens, usernames, git protocol)
- `config.yml` — user preferences (editor, aliases, pager, browser)

**`hosts.yml` example:**
```yaml
github.com:
  user: my-username
  oauth_token: gho_xxxxxxxxxxxxxxxxxxxxx
  git_protocol: https
github.mycompany.com:
  user: enterprise-user
  oauth_token: gho_yyyyyyyyyyyyyyyyyyyyy
  git_protocol: ssh
```

**Secure storage:** Token stored in OS keychain by default (macOS Keychain, Windows Credential Manager, Linux `pass`/keyring). Falls back to plaintext `hosts.yml` if no credential store available. Use `--secure-storage` flag to force encrypted storage. Use `--insecure-storage` to explicitly choose plaintext.

### Environment Variables (Precedence)

```
GH_TOKEN / GITHUB_TOKEN          → auth for github.com (overrides stored creds)
GH_ENTERPRISE_TOKEN              → auth for GHE hosts
GH_HOST                          → target hostname
GH_REPO                          → target repository [HOST/]OWNER/REPO
GH_CONFIG_DIR                    → custom config directory
GH_PROMPT_DISABLED               → disable interactive prompts
GH_DEBUG=api                     → verbose HTTP logging
GH_FORCE_TTY                     → force terminal output in pipes
GH_NO_UPDATE_NOTIFIER            → suppress update checks
```

**Critical insight:** `GH_TOKEN` takes **absolute precedence** over stored credentials. This means an Entraclaw agent can inject a scoped OBO token via environment variable, and all `gh` commands (and `go-gh`-based extensions) will automatically use it. No credential store manipulation needed.

### Multi-Account Support

```bash
# Login to multiple accounts
gh auth login                              # account 1
gh auth login                              # account 2 (different user)

# Switch active account
gh auth switch -u other-username

# Check which accounts are configured
gh auth status

# Refresh token scopes
gh auth refresh --scopes repo,read:org,admin:public_key
gh auth refresh --remove-scopes admin:public_key
```

### Default Scopes

The minimum required scopes for `gh auth login` are: `repo`, `read:org`, and `gist`. Additional scopes can be requested via `--scopes` flag or expanded later with `gh auth refresh`.

### Relevance to Agent IDs and OBO Flows

| gh Pattern | Entraclaw Analogy |
|------------|------------------|
| Device Flow approval | Agent requests OBO token, human approves in Teams |
| `GH_TOKEN` env var override | Agent injects OBO token into child processes |
| `hosts.yml` multi-host auth | Agent manages tokens for multiple tenants/identities |
| `gh auth token` programmatic access | Agent extracts token for API calls |
| `gh auth switch` | Agent switches between human identities |
| `--secure-storage` keychain | Agent stores tokens in OS-level secure storage |

---

## The --remote Flag & Session Steering

> **Note:** The "remote" capability is not a single `--remote` flag but a collection of features in Copilot CLI (the successor to `gh copilot`) that together enable remote session access.

### Architecture

Copilot CLI sessions are **persistent, portable, and steer-able**:

```
┌─────────────────┐     ┌─────────────────┐     ┌──────────────┐
│  Local Terminal  │     │  Browser (URL)  │     │  SSH Client  │
│  copilot ...     │────▶│  Session viewer │     │  copilot     │
│                  │     │  & steering     │     │  --continue  │
└────────┬────────┘     └────────┬────────┘     └──────┬───────┘
         │                       │                      │
         └───────────────────────┴──────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  Session Storage   │
                    │  ~/.copilot/       │
                    │  sessions/         │
                    │  (durable state)   │
                    └───────────────────┘
```

### Session Persistence

Every Copilot CLI session is automatically saved:
- Full conversation history
- Working directory context
- Tool approvals
- Code changes in progress

```bash
# Resume the most recent session
copilot --continue

# Resume a specific session by ID
copilot --resume=<session-id>

# Interactive session picker
# /resume (slash command within a session)
```

Sessions generate a **remote session URL** (since v1.0.10) — a clickable link in terminal output that can be:
- Bookmarked for later access
- Shared with collaborators
- Used to monitor/steer from a browser on any device

### Delegation (`/delegate`)

The `/delegate` command (or `&` shorthand) is the key remote pattern:

```bash
# Delegate work to GitHub's cloud coding agent
/delegate complete the API integration tests and fix failing edge cases

# Shorthand
& refactor the auth module to use JWT tokens
```

What happens:
1. Copilot packages current session context (including unstaged local changes)
2. Commits to a new feature branch
3. Hands off to Copilot coding agent running on **GitHub's infrastructure**
4. Agent works autonomously in the cloud
5. Opens a draft PR when complete
6. Requests human review

**Security model:** Delegated tasks use GitHub Actions-style permissions, not local credentials. Your laptop can sleep or shut down entirely.

### Agent Client Protocol (ACP)

The `--acp` flag starts Copilot CLI as an ACP server — a JSON-RPC interface for external tool integration:

```bash
# Start as ACP server (stdio mode, for IDE integration)
copilot --acp --stdio

# Start as ACP server (TCP mode, for networked access)
copilot --acp --port 3000
```

The ACP SDK (`@agentclientprotocol/sdk`) enables custom clients:

```typescript
// TypeScript ACP client example (conceptual)
import { AgentClient } from '@agentclientprotocol/sdk';

const client = new AgentClient({ transport: 'tcp', port: 3000 });
const session = await client.createSession();
await session.sendMessage("Refactor the auth module");
// Stream responses, handle tool approvals, etc.
```

### Programmatic / Automation Mode

```bash
# Single prompt, exit after response (no interactive UI)
copilot -p "Summarize changes in the last 10 commits"

# Fully autonomous with bounded iterations
copilot --autopilot --allow-all --max-autopilot-continues 10 \
  -p "Review open PRs and flag failing CI"

# JSON output for scripting
copilot --output-format=json -p "List TODO comments in src/"
```

Environment variables for automation:
- `GH_TOKEN` — authentication
- `COPILOT_ALLOW_ALL=true` — skip permission prompts
- `--deny-tool` / `--allow-tool` — fine-grained tool control

### Relevance to Entraclaw Teams Integration

| Copilot CLI Pattern | Entraclaw Analogy |
|---------------------|------------------|
| Session persistence + resume | Agent maintains state across Teams conversations |
| Remote session URL | Agent provides Teams deep-link to current work |
| `/delegate` to cloud agent | Agent hands off to background worker, reports back in Teams |
| ACP server mode | Agent exposes JSON-RPC for Teams bot to send commands |
| `--autopilot` + `--allow-all` | Agent runs autonomously within human-defined guardrails |
| `--deny-tool` / `--allow-tool` | Human sets permission boundaries via Teams |
| Browser steering | Human steers agent via Teams instead of browser |

**The key insight:** Copilot CLI has already solved the "human in one place, agent in another" problem. Entraclaw needs to replace "browser" with "Teams" as the steering channel.

---

## Extension Model

### How Extensions Work

GitHub CLI extensions are standalone executables that plug into the `gh` command namespace:

```
gh <extension-name> [args...]    ← runs the extension as a subcommand
```

**Naming convention:** Repository must be named `gh-<name>` (e.g., `gh-dash`, `gh-copilot`, `gh-models`).

**Two types:**

| Type | Language | Distribution | Portability |
|------|----------|-------------|-------------|
| **Script-based** | Bash/Python/any interpreted | Source (requires interpreter) | Any platform with interpreter |
| **Precompiled** | Go (first-class), Rust, C++ | Platform-specific binaries via GitHub Releases | Cross-platform via multi-arch builds |

### Creating an Extension

```bash
# Script-based (Bash)
gh extension create my-extension

# Precompiled Go extension
gh extension create --precompiled=go my-extension

# Precompiled other language
gh extension create --precompiled=other my-extension
```

**Generated Go extension structure:**
```
gh-my-extension/
├── .github/
│   └── workflows/       # CI/CD for multi-platform release builds
├── cmd/
│   └── root.go          # Cobra command definitions
├── main.go              # Entry point
├── go.mod
├── go.sum
└── README.md
```

### Managing Extensions

```bash
# Install from a GitHub repo
gh extension install owner/gh-my-extension

# List installed extensions
gh extension list

# Upgrade all extensions
gh extension upgrade --all

# Remove an extension
gh extension remove my-extension

# Browse available extensions
gh extension browse
```

### The `go-gh` SDK

The official Go module for building extensions: [`github.com/cli/go-gh/v2`](https://github.com/cli/go-gh)

**Key packages:**

| Package | Purpose |
|---------|---------|
| `github.com/cli/go-gh/v2` | Shell out to `gh` commands |
| `github.com/cli/go-gh/v2/pkg/api` | REST and GraphQL API clients |
| `github.com/cli/go-gh/v2/pkg/repository` | Resolve current repo context |
| `github.com/cli/go-gh/v2/pkg/tableprinter` | Formatted table output |
| `github.com/cli/go-gh/v2/pkg/browser` | Open URLs in user's browser |
| `github.com/cli/go-gh/v2/pkg/term` | Terminal capability detection |

**Critical behavior:** `go-gh` automatically inherits authentication from the host `gh` CLI:
- Reads `GH_TOKEN` / `GH_HOST` environment variables
- Falls back to the user's stored OAuth token in `hosts.yml` / keychain
- **Zero credential management code needed in extensions**

```go
package main

import (
    "fmt"
    "log"
    "github.com/cli/go-gh/v2"
    "github.com/cli/go-gh/v2/pkg/api"
)

func main() {
    // Shell out to gh (inherits full auth context)
    output, _, err := gh.Exec("issue", "list", "--repo", "cli/cli", "--limit", "5")
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println(output.String())

    // Direct API client (also inherits auth)
    client, err := api.DefaultRESTClient()
    if err != nil {
        log.Fatal(err)
    }
    var tags []struct{ Name string }
    err = client.Get("repos/cli/cli/tags", &tags)
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println(tags)
}
```

### Copilot CLI Extensions (Newer Model)

Copilot CLI has its own extension system, separate from `gh extension`:

```
~/.copilot/extensions/          # User-level extensions
.github/extensions/             # Project-level extensions
```

Extensions are `.mjs` files that can:
- Create new tools for the agent
- Intercept agent actions
- Inject context
- Hot-reload without restart
- Communicate via JSON-RPC

```javascript
// .github/extensions/my-tool.mjs (conceptual)
export default {
    name: "my-custom-tool",
    description: "Does something useful",
    async execute(context) {
        // Access session state, make API calls, etc.
        return { result: "done" };
    }
};
```

### Could Entraclaw Be a gh Extension?

**Pros:**
- Automatic auth inheritance (zero credential code)
- Runs as `gh entraclaw` — familiar to GitHub users
- Distribution via `gh extension install`
- Cross-platform binary distribution via Go + GitHub Releases
- Access to full GitHub API via `go-gh`

**Cons:**
- Extensions are single-user, local-only by default (no multi-tenant)
- No built-in daemon/service mode
- No built-in Teams integration channel
- Extension auth is tied to the human's `gh` session — the OBO model would need a layer on top
- The Copilot CLI extension model (.mjs) is more powerful for agent scenarios but is Copilot-specific

**Verdict:** A `gh` extension would be a good **bootstrap** mechanism (install agent, initial auth) but the agent runtime itself should be independent. The `go-gh` SDK is valuable regardless — use it for GitHub API access within the agent.

---

## CLI Source Code Architecture

The `gh` CLI source code ([github/cli](https://github.com/cli/cli)) follows standard Go project layout:

```
cli/cli/
├── cmd/
│   └── gh/
│       └── main.go              # Entry point
├── internal/                    # Private application logic
│   ├── authflow/                # OAuth + Device Flow implementation
│   ├── config/                  # Configuration management
│   ├── ghrepo/                  # Repository resolution
│   └── ...                      # Command implementations
├── pkg/                         # Public/reusable packages
│   ├── cmd/                     # Command definitions (Cobra-based)
│   │   ├── auth/                # gh auth subcommands
│   │   ├── pr/                  # gh pr subcommands
│   │   ├── issue/               # gh issue subcommands
│   │   ├── api/                 # gh api subcommand
│   │   └── extension/           # gh extension subcommands
│   ├── cmdutil/                 # Command utilities
│   └── ...
├── api/                         # API client wrappers
├── context/                     # Git/repo context resolution
├── docs/                        # Documentation
├── script/                      # Build/release scripts
├── go.mod
└── go.sum
```

**Key architectural decisions:**
- **Cobra** for CLI command parsing and help generation
- **`internal/authflow/`** contains the OAuth Device Flow implementation — worth studying for Entraclaw
- Clean separation between command parsing (`pkg/cmd/`) and business logic (`internal/`)
- API clients wrap `go-gh` for consistency

---

## Community Learnings & Gotchas

### Rate Limits

| Context | Limit | Notes |
|---------|-------|-------|
| Authenticated (PAT/OAuth) | 5,000 req/hr | Per user |
| GitHub Actions `GITHUB_TOKEN` | 1,000 req/hr | **Per repository** — easy to hit in busy repos |
| Unauthenticated | 60 req/hr | Per IP — essentially unusable |
| GraphQL | 5,000 points/hr | Different calculation than REST |

```bash
# Check current rate limit
gh api rate_limit --jq '.rate | {limit, remaining, reset: (.reset | strftime("%H:%M:%S"))}'
```

**Gotcha:** In GitHub Actions, the auto-provided `GITHUB_TOKEN` has a 1,000 req/hr limit. For automation-heavy workflows, use a PAT stored as a secret to get the full 5,000 limit.

### Token Confusion

**GH_TOKEN vs GITHUB_TOKEN vs stored credentials:**
- `GH_TOKEN` takes absolute precedence
- `GITHUB_TOKEN` is a fallback (lower precedence)
- Both override stored credentials in `hosts.yml`
- If `GITHUB_TOKEN` is set in your shell (e.g., from a previous CI export), `gh` may silently use it instead of your interactive login — leading to confusing permission errors

**Fix:** Always use `gh auth status` to verify which token is active. Unset stale env vars.

### gh-copilot Deprecation

The original `gh-copilot` CLI extension was deprecated on October 25, 2025, replaced by the standalone **Copilot CLI**. The new tool:
- Is installed separately (not as a `gh` extension)
- Has its own extension model (`.mjs` files, not `gh extension`)
- Supports session persistence, delegation, ACP
- Uses `copilot` command (not `gh copilot`)

### Fine-Grained PATs Gotcha

Fine-grained personal access tokens scope to specific repositories. When used with `gh`, commands targeting repos outside the token's scope fail silently or with confusing errors. GitHub recommends setting fine-grained PATs via `GH_TOKEN` env var rather than `--with-token` to make the scoping explicit.

### Credential Store Failures

On Linux without a graphical environment (common for agents/servers), the OS keyring may not be available. `gh` falls back to plaintext storage with a warning. For headless agents:
- Use `GH_TOKEN` environment variable (recommended)
- Or use `--insecure-storage` explicitly
- Or install `pass` (GPG-based credential store)

### Extension Auto-Update Notifications

Extensions check for updates every 24 hours when executed. In automated/agent scenarios, disable with `GH_NO_EXTENSION_UPDATE_NOTIFIER=1`.

### Multi-Account Session Confusion

When switching between accounts with `gh auth switch`, the change is global — affecting all terminal sessions. For agents managing multiple identities, use per-process `GH_TOKEN` environment variables instead.

---

## Open Questions

### For Entraclaw Agent Identity

1. **Can we use GitHub's OAuth Device Flow for agent registration?** The flow is designed for CLIs — could we adapt it so a human approves an agent's identity request via Teams instead of a browser?

2. **Token lifecycle for long-running agents:** `gh` tokens don't expire by default (OAuth tokens are long-lived). But fine-grained PATs can have expiry. What's the right token type for an Entraclaw agent that runs for weeks/months?

3. **OBO token delegation chain:** `gh` → `go-gh` extension auto-inherits tokens. Can we build a similar chain where: Human approves → Agent gets OBO token → Agent's sub-processes inherit via `GH_TOKEN`?

4. **Multi-tenant agent:** `gh auth switch` is global (affects all sessions). For an agent serving multiple humans, the `GH_TOKEN`-per-process pattern is the right model. But how do we manage token rotation and refresh across many concurrent identities?

### For Teams Integration

5. **ACP as the protocol for Teams ↔ Agent?** The Agent Client Protocol (JSON-RPC over stdio/TCP) is designed for exactly this — external clients steering an agent. Could a Teams bot be an ACP client?

6. **Session URLs in Teams:** Copilot CLI generates shareable session URLs. Could Entraclaw agents generate similar deep-links that open in Teams rather than a browser?

7. **Delegation model for Teams:** The `/delegate` pattern (hand off to cloud agent, report back) maps well to Teams. Human says "do X" in Teams → Agent delegates → Reports back with PR link.

### For Extension Architecture

8. **Hybrid model:** Use `gh extension` for installation/auth bootstrap, but run the agent as an independent daemon that uses `go-gh` for GitHub API access and ACP for human steering?

9. **Copilot CLI extension model:** The `.mjs` extension system for Copilot CLI is more powerful than `gh extension` for agent scenarios. Should Entraclaw agents integrate as Copilot CLI extensions rather than `gh` extensions?

10. **MCP server integration:** Both Copilot CLI and GitHub's coding agent support Model Context Protocol (MCP) servers. Should Entraclaw agents expose their capabilities as MCP tools, making them composable with Copilot?

---

## Sources

1. **GitHub CLI Manual** — Official command reference
   - https://cli.github.com/manual/index
   - https://cli.github.com/manual/gh_auth_login
   - https://cli.github.com/manual/gh_auth_refresh
   - https://cli.github.com/manual/gh_api
   - https://cli.github.com/manual/gh_help_environment
   - https://cli.github.com/manual/gh_extension_create

2. **GitHub Docs** — CLI documentation hub
   - https://docs.github.com/en/github-cli
   - https://docs.github.com/en/github-cli/github-cli/quickstart
   - https://docs.github.com/en/github-cli/github-cli/creating-github-cli-extensions
   - https://docs.github.com/en/github-cli/github-cli/using-github-cli-extensions

3. **GitHub CLI Source Code** — Open source Go repository
   - https://github.com/cli/cli

4. **go-gh SDK** — Official Go module for building extensions
   - https://github.com/cli/go-gh
   - https://pkg.go.dev/github.com/cli/go-gh/v2

5. **Copilot CLI Remote Access** — Detailed analysis of session persistence and remote steering
   - https://htek.dev/articles/copilot-cli-remote-access-your-agent-from-anywhere

6. **Copilot CLI Extensions Guide** — Custom agent extensions for Copilot CLI
   - https://htek.dev/articles/github-copilot-cli-extensions-complete-guide/

7. **GitHub Blog — Copilot CLI Announcements**
   - https://github.blog/changelog/2025-10-28-github-copilot-cli-use-custom-agents-and-delegate-to-copilot-coding-agent/
   - https://github.blog/changelog/2026-01-21-github-copilot-cli-plan-before-you-build-steer-as-you-go/
   - https://github.blog/changelog/2025-09-25-upcoming-deprecation-of-gh-copilot-cli-extension/

8. **GitHub Blog — Scripting with GitHub CLI**
   - https://github.blog/engineering/engineering-principles/scripting-with-github-cli/

9. **GitHub Blog — Extension Tools**
   - https://github.blog/developer-skills/github/new-github-cli-extension-tools/

10. **GitHub Blog — GraphQL with GitHub CLI**
    - https://github.blog/developer-skills/github/exploring-github-cli-how-to-interact-with-githubs-graphql-api-endpoint/

11. **Copilot CLI ACP Server Documentation**
    - https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server

12. **Copilot CLI Steering Documentation**
    - https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-agents/steer-agents

13. **OAuth Device Flow Integration Guide**
    - https://dev.to/ddebajyati/integrate-github-login-with-oauth-device-flow-in-your-js-cli-28fk

14. **GitHub OAuth Apps Scopes Reference**
    - https://docs.github.com/en/developers/apps/building-oauth-apps/scopes-for-oauth-apps

15. **Multi-Account Setup with GitHub CLI**
    - https://hubertkasperek.com/blog/post/how-to-use-multiple-github-accounts-on-one-computer-using-github-cli/

16. **Secure Token Storage**
    - https://kollitsch.dev/blog/2023/saving-github-access-token-in-local-encrypted-storage-via-gh-cli/

17. **GitHub CLI Auth Confusion (GITHUB_TOKEN gotcha)**
    - https://www.henriksommerfeld.se/github-cli-auth-confusion-using-github-token/

18. **Rate Limiting in CI Workflows**
    - https://www.cazzulino.com/github-actions-rate-limiting.html

19. **Stack Overflow — GitHub CLI Rate Limits**
    - https://stackoverflow.com/questions/71443093/do-github-cli-commnds-use-rest-apis-of-github-is-there-any-rate-limit-for-using

20. **Building Agents with Copilot SDK**
    - https://techcommunity.microsoft.com/blog/azuredevcommunityblog/building-agents-with-github-copilot-sdk-a-practical-guide-to-automated-tech-upda/4488948

21. **Extending gh CLI with Go (Tutorial)**
    - https://mikeball.info/blog/extending-the-gh-cli-with-go/

22. **VS Code Copilot CLI Sessions**
    - https://code.visualstudio.com/docs/copilot/agents/copilot-cli
