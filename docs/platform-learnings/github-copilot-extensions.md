# GitHub Copilot Extensions

> **Research date:** July 2025
> **Status:** ⚠️ GitHub App-based Copilot Extensions are being **deprecated November 10, 2025** in favor of MCP (Model Context Protocol) servers. This document covers the original extension model for architectural learning and the transition to MCP.

## Overview

GitHub Copilot Extensions were a mechanism that allowed third-party developers to extend GitHub Copilot Chat with custom agents invoked via `@agent-name` syntax. Introduced in public beta in September 2024, they enabled external tools, services, and AI capabilities to be surfaced directly inside Copilot Chat across VS Code, JetBrains, and GitHub.com.

**Why this matters for Entraclaw:** The Copilot Extension model demonstrates one of the most production-ready patterns for a platform hosting third-party AI agents with delegated identity. Even though the GitHub App-based model is being sunset, the architectural patterns — particularly around identity delegation, request signing, and the shift to MCP — are directly relevant to Entraclaw's Agent ID and OBO flow design.

### Key Takeaway

GitHub tried a proprietary extension model (GitHub App + SSE endpoint) and is now **abandoning it in favor of the open MCP standard**. This is a strong signal that:
1. Proprietary agent extension protocols don't survive — open standards win
2. MCP is becoming the de facto standard for AI agent tool integration
3. Entraclaw should design for MCP compatibility from day one

## Extension Architecture

### How Extensions Worked (GitHub App Model — Sunsetting Nov 2025)

The architecture had three core components:

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Copilot Chat │────▶│  Copilot Platform │────▶│  Extension       │
│  (IDE/Web)    │     │  (GitHub)         │     │  (Your Server)   │
│               │◀────│                   │◀────│                  │
└──────────────┘     └──────────────────┘     └──────────────────┘
       User types            Routes request          Processes &
       @agent query          + adds auth headers     streams response
```

1. **Copilot Platform** — GitHub's middleware that intercepts `@agent` mentions, routes requests to the registered extension endpoint, and manages auth headers.
2. **GitHub App** — The identity and permission broker. Each extension is backed by a registered GitHub App that defines permissions, webhook URLs, and the extension endpoint.
3. **Extension Server** — Your HTTP server that receives POST requests from the Copilot Platform and streams back Server-Sent Events (SSE).

### Request Flow

1. User types `@my-extension what is the status of deployment?` in Copilot Chat
2. Copilot Platform identifies the extension, packages the conversation as a request
3. Request is POST'd to the extension's registered endpoint with:
   - **Body:** JSON with a `messages` array (OpenAI Chat Completions format — `{role, content}` objects)
   - **Headers:**
     - `X-GitHub-Token` — A short-lived token representing the user, usable to call GitHub APIs on behalf of the user
     - `X-GitHub-Public-Key-Identifier` — Key ID for signature verification
     - `X-GitHub-Public-Key-Signature` — Signature of the request body
4. Extension verifies the signature, processes the request, and streams SSE responses

### Response Format (Server-Sent Events)

Extensions respond using SSE with specific event types:

```
data: {"choices":[{"delta":{"content":"Hello "}}]}

data: {"choices":[{"delta":{"content":"world!"}}]}

data: [DONE]
```

The `@copilot-extensions/preview-sdk` provides helpers:
- `createAckEvent()` — Acknowledge receipt
- `createTextEvent(text)` — Stream text chunks
- `createDoneEvent()` — Signal completion
- `createErrorsEvent(errors)` — Report errors

### Two Extension Types

| Type | Description | Control Level |
|------|-------------|---------------|
| **Agent** | Full control over LLM interaction, prompt engineering, tool calling | Maximum — you manage everything |
| **Skillset** | Define up to 5 API endpoints; Copilot handles LLM orchestration | Minimal — just expose REST endpoints |

## Skillsets

Skillsets were the simpler path for building Copilot Extensions. Instead of managing the full LLM interaction, you defined API endpoints and let Copilot's platform decide when to call them.

### How Skillsets Work

1. Register a GitHub App with "Copilot Skillset" type
2. Define up to 5 API endpoints with JSON schema descriptions
3. Copilot Platform autonomously decides which endpoint to call based on user query
4. Your endpoint returns data; Copilot formats it into a natural language response

### Skillset Definition Example

```json
{
  "skills": [
    {
      "name": "get_deployment_status",
      "description": "Get the current deployment status for a service",
      "parameters": {
        "type": "object",
        "properties": {
          "service_name": {
            "type": "string",
            "description": "The name of the service to check"
          }
        },
        "required": ["service_name"]
      },
      "endpoint": "https://api.example.com/deployments/status"
    }
  ]
}
```

### Skillsets vs Agents — Relevance to Entraclaw

For Entraclaw, the **agent model** (not skillsets) is more relevant because:
- Entraclaw agents need full autonomy over their behavior
- Agents need to manage their own identity and tool calling
- The skillset model delegates too much control to the platform

However, skillsets demonstrate an important pattern: **the platform can mediate tool discovery and invocation** — which is exactly what MCP now does in a standardized way.

## Auth & Identity Model

> **This is the most relevant section for Entraclaw.**

### GitHub App as Identity

Every Copilot Extension was backed by a **GitHub App**, which served as:
- The **identity** of the extension (app ID, name, permissions)
- The **trust anchor** for request verification (public key signatures)
- The **permission broker** defining what the extension can access

### Request Authentication — Signature Verification

GitHub signs every request to an extension using a public/private key pair:

```javascript
import { verifyAndParseRequest } from "@copilot-extensions/preview-sdk";

app.post("/", async (req, res) => {
  const signature = req.headers["x-github-public-key-signature"];
  const keyID = req.headers["x-github-public-key-identifier"];
  const token = req.headers["x-github-token"];

  const { isValidRequest, payload } = await verifyAndParseRequest(
    req.body,
    signature,
    keyID,
    { token }
  );

  if (!isValidRequest) {
    return res.status(401).send("Unauthorized");
  }

  // payload.messages contains the conversation
  // token can be used to call GitHub APIs as the user
});
```

The verification flow:
1. Extension receives the request with signature headers
2. SDK fetches GitHub's public key using the key ID
3. Verifies the signature against the request body
4. If valid, the request is authentic from GitHub's Copilot Platform

### The `X-GitHub-Token` — User Delegation

The `X-GitHub-Token` header is the closest thing to an OBO (On-Behalf-Of) token in this model:

- It's a **short-lived, scoped token** representing the user who invoked the extension
- The extension can use it to call GitHub APIs **as the user**
- It carries the user's permissions (limited by what the GitHub App requested)
- It's generated by the Copilot Platform at request time

```javascript
import { Octokit } from "@octokit/core";

// Use the user's token to access GitHub APIs
const octokit = new Octokit({ auth: token });
const { data: user } = await octokit.request("GET /user");
console.log(`Acting on behalf of: ${user.login}`);
```

### Identity Model Analysis for Entraclaw

| Aspect | Copilot Extension Model | Entraclaw Consideration |
|--------|------------------------|----------------------|
| **Agent Identity** | GitHub App (app ID + private key) | Agent ID (similar concept — registered identity) |
| **User Delegation** | `X-GitHub-Token` (platform-issued, short-lived) | OBO token (similar — platform issues token for agent to act as user) |
| **Trust Verification** | Public key signature on requests | Could use similar pattern — platform signs requests to agents |
| **Permission Scoping** | GitHub App permissions + token scopes | Agent permissions defined at registration |
| **Token Lifetime** | Per-request, short-lived | Should be similarly short-lived |
| **Who Issues Tokens** | Copilot Platform (GitHub) | Entraclaw Platform (identity service) |

### Key Insight: No True OBO Flow

The Copilot Extension model does **not** use a formal OAuth 2.0 OBO flow. Instead:
1. The user authenticates with GitHub (OAuth)
2. Copilot Platform creates a scoped token for the extension
3. The extension receives this token as a header — it didn't negotiate for it

This is a **platform-mediated delegation** pattern, not a standard OBO flow. The extension never directly authenticates with the user; the platform vouches for both parties.

**For Entraclaw:** This is actually simpler than full OBO and may be a better fit for device-based agents. The platform (Entraclaw identity service) could issue scoped tokens to agents without requiring the agent to participate in an OAuth dance.

### GitHub App-Based Auth is Being Deprecated

With the sunset of GitHub App-based extensions (November 2025), the auth model shifts to MCP:
- MCP servers authenticate differently (OAuth + PAT for remote servers, or local stdio)
- There's no equivalent of `X-GitHub-Token` in MCP — instead, the MCP host (e.g., VS Code) manages auth
- Remote MCP servers use standard OAuth 2.0 flows

## Building an Extension (Historical — For Reference)

### Prerequisites
- Node.js 18+
- A GitHub account with Copilot subscription
- A registered GitHub App

### Step 1: Create a GitHub App

1. Go to GitHub Settings → Developer Settings → GitHub Apps → New GitHub App
2. Configure:
   - **Name:** Your extension name
   - **Homepage URL:** Your extension's website
   - **Callback URL:** Your OAuth callback (if needed)
   - **Copilot:** Enable and set as Agent or Skillset
   - **URL:** Your extension's POST endpoint
3. Set permissions (e.g., read access to repos, issues, etc.)

### Step 2: Build the Server

```javascript
import express from "express";
import {
  verifyAndParseRequest,
  createTextEvent,
  createDoneEvent,
} from "@copilot-extensions/preview-sdk";

const app = express();
app.use(express.text());

app.post("/", async (req, res) => {
  const signature = req.headers["x-github-public-key-signature"];
  const keyID = req.headers["x-github-public-key-identifier"];
  const token = req.headers["x-github-token"];

  const { isValidRequest, payload } = await verifyAndParseRequest(
    req.body,
    signature,
    keyID,
    { token }
  );

  if (!isValidRequest) {
    return res.status(401).send("Unauthorized");
  }

  // Extract the latest user message
  const userMessage = payload.messages
    .filter((m) => m.role === "user")
    .pop()?.content;

  // Set SSE headers
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");

  // Stream response
  res.write(createTextEvent(`You said: ${userMessage}\n`));
  res.write(createDoneEvent());
  res.end();
});

app.listen(3000, () => {
  console.log("Extension server running on port 3000");
});
```

### Step 3: Test Locally

```bash
npm install express @copilot-extensions/preview-sdk @octokit/core
npx ngrok http 3000  # Expose local server
# Update GitHub App's Copilot URL to the ngrok URL
```

### SDK Package

- **NPM:** `@copilot-extensions/preview-sdk`
- **GitHub:** [copilot-extensions/preview-sdk.js](https://github.com/copilot-extensions/preview-sdk.js)
- **Key exports:** `verifyAndParseRequest`, `verifyRequestByKeyId`, `createTextEvent`, `createDoneEvent`, `createAckEvent`, `createErrorsEvent`

## Verification & Publishing

### Publisher Verification

To list an extension on the GitHub Marketplace:
1. Organization must have a verified domain
2. Two-factor authentication (2FA) must be enabled
3. Organization profile must be complete with support email
4. Submit for verification review via Developer Settings → Publisher Verification

### Marketplace Listing

1. Create a listing in your GitHub App settings
2. Provide description, screenshots, pricing plan
3. Submit for review
4. Once approved, visible in GitHub Marketplace under "Copilot Extensions" category

### Post-Sunset (November 2025)

- The "Copilot Extensions" Marketplace category will be **removed**
- MCP servers will be discoverable via the [GitHub MCP Registry](https://github.com/mcp)
- No formal marketplace listing process for MCP servers yet — most are open-source repos

## Integration Patterns

### Could an Entraclaw Agent Be a Copilot Extension?

**Short answer: Not anymore (or not via the GitHub App model).** But the question becomes: could an Entraclaw agent expose itself as an MCP server?

### Entraclaw Agent as MCP Server

This is the forward-looking integration pattern:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Copilot Chat │────▶│  MCP Host    │────▶│  Entraclaw Agent  │
│  (IDE)        │     │  (VS Code)   │     │  (MCP Server)    │
│               │◀────│              │◀────│                  │
└──────────────┘     └──────────────┘     └──────────────────┘
                                                   │
                                                   ▼
                                          ┌──────────────────┐
                                          │  Device / Local  │
                                          │  Agent Runtime   │
                                          └──────────────────┘
```

**How it would work:**

1. Entraclaw agent runs on a device (local runtime)
2. Agent exposes an MCP server interface (stdio for local, HTTP for remote)
3. User configures the MCP server in their IDE (VS Code, JetBrains, etc.)
4. Copilot (or any MCP-compatible AI) can invoke the agent's tools
5. Agent uses its Agent ID + OBO flow to act on behalf of the user

### Architectural Bridge: Agent on Device + MCP

```json
// .vscode/mcp.json — configure Entraclaw agent as MCP server
{
  "servers": {
    "entraclaw-agent": {
      "type": "stdio",
      "command": "entraclaw-agent",
      "args": ["serve", "--mcp"],
      "env": {
        "ENTRACLAW_AGENT_ID": "agent-xyz-123"
      }
    }
  }
}
```

For remote agents:
```json
{
  "servers": {
    "entraclaw-agent": {
      "type": "http",
      "url": "https://agent.entraclaw.dev/mcp",
      "auth": {
        "type": "oauth",
        "issuer": "https://identity.entraclaw.dev"
      }
    }
  }
}
```

### Key Design Considerations

1. **MCP doesn't have built-in identity delegation** — the host (e.g., VS Code) manages auth. Entraclaw would need to handle its own OBO flow within the MCP tool execution.
2. **MCP is tool-oriented, not agent-oriented** — MCP servers expose tools, not autonomous agents. An Entraclaw agent would need to expose its capabilities as discrete tools.
3. **MCP supports both local and remote** — local (stdio) is simpler but limited to the device; remote (HTTP + OAuth) enables cloud agents but adds auth complexity.

## Community Learnings & Gotchas

### From Early Adopters (Pre-Deprecation)

1. **SSE streaming is fragile** — Extensions had to carefully manage SSE formatting. Malformed events would silently fail.
2. **Token lifetime is very short** — The `X-GitHub-Token` is per-request. No refresh mechanism exists. Extensions needed to complete all API calls within the request lifecycle.
3. **No state between requests** — Extensions are stateless by design. Any conversation memory had to be managed externally.
4. **Limited to 5 endpoints (skillsets)** — Skillsets were constrained to 5 API endpoints, which frustrated developers with complex tools.
5. **Discovery was poor** — Users had to know the exact `@agent-name` to invoke an extension. No discoverability within the chat.
6. **Acceptance rates were modest** — ~27-30% of Copilot suggestions were accepted. Extensions had even lower engagement due to discoverability issues.
7. **VS Code vs GitHub.com parity** — Features shipped to VS Code first; GitHub.com and JetBrains often lagged behind.

### The Deprecation Signal

The most important community learning: **GitHub deprecated the entire model after ~14 months.** This tells us:
- Proprietary extension protocols are risky to build on
- The industry is consolidating around MCP
- Platform lock-in is being actively resisted by both GitHub and the developer community

### Example Repos (Historical Reference)

| Repository | Type | Description |
|-----------|------|-------------|
| [copilot-extensions/blackbeard-extension](https://github.com/copilot-extensions/blackbeard-extension) | Agent | "Hello world" — talks like a pirate |
| [copilot-extensions/skillset-example](https://github.com/copilot-extensions/skillset-example) | Skillset | Random test data generator |
| [copilot-extensions/function-calling-extension](https://github.com/copilot-extensions/function-calling-extension) | Agent | Function calling with confirmations |
| [copilot-extensions/preview-sdk.js](https://github.com/copilot-extensions/preview-sdk.js) | SDK | Official SDK for building extensions |

## Open Questions

### For Entraclaw's Scenario

1. **Should Entraclaw agents expose MCP interfaces?** Given that MCP is the emerging standard, it seems wise. But MCP is tool-oriented, not agent-oriented — how do we reconcile this with autonomous agent behavior?

2. **How does OBO work in MCP?** MCP doesn't have a built-in OBO concept. The host manages auth. For Entraclaw agents that need to act on behalf of users across services, we'd need to layer OBO on top of MCP's auth model.

3. **Local vs Remote agents in MCP:** Entraclaw agents on devices could be local MCP servers (stdio), but this limits them to the local machine. Remote MCP servers add auth complexity but enable cloud-based agents. Which model fits Entraclaw better?

4. **Agent discovery in MCP:** The GitHub MCP Registry exists but is nascent. How would users discover and trust Entraclaw agents? The GitHub App model had a marketplace; MCP doesn't (yet).

5. **Token scoping:** The Copilot Extension model had platform-mediated token scoping (GitHub controlled what the extension could access). In MCP, who controls what the agent can do? The host? The user? The agent itself?

6. **Multi-platform compatibility:** With MCP, an Entraclaw agent could work with Copilot, Claude Code, Cursor, and others. But each host may have different auth flows and policies. How do we ensure consistent identity across hosts?

7. **The deprecated model's auth pattern is still useful:** Even though GitHub App extensions are dying, the pattern of **platform-issued, short-lived, scoped tokens for agent delegation** is exactly what Entraclaw needs. Can we implement this pattern within MCP?

8. **Enterprise governance:** GitHub has MCP policies (enable/disable/allowlist per org). Entraclaw needs similar governance for Agent IDs in enterprise contexts.

## Sources

### Official Documentation
- [GitHub Copilot Documentation](https://docs.github.com/en/copilot) — Main Copilot docs (now pivoted to MCP)
- [Sunset Notice: GitHub App-based Copilot Extensions](https://github.blog/changelog/2025-09-24-deprecate-github-copilot-extensions-github-apps/) — Official deprecation announcement with FAQ
- [MCP and Copilot Cloud Agent](https://docs.github.com/en/copilot/concepts/agents/coding-agent/mcp-and-coding-agent) — MCP integration with Copilot
- [Applying for Publisher Verification](https://docs.github.com/en/apps/github-marketplace/github-marketplace-overview/applying-for-publisher-verification-for-your-organization) — Marketplace listing process
- [Updated Headers for Extension Requests (Jan 2025)](https://github.blog/changelog/2025-01-17-updated-headers-for-github-copilot-extension-requests/) — Header format changes

### SDK & Code
- [copilot-extensions/preview-sdk.js](https://github.com/copilot-extensions/preview-sdk.js) — Official TypeScript SDK for building extensions
- [@copilot-extensions/preview-sdk on NPM](https://www.npmjs.com/package/@copilot-extensions/preview-sdk) — NPM package
- [copilot-extensions GitHub Org](https://github.com/orgs/copilot-extensions/repositories) — All example repos
- [github/copilot-sdk](https://github.com/github/copilot-sdk) — Multi-platform Copilot SDK

### Blog Posts & Tutorials
- [Introducing GitHub Copilot Extensions (GitHub Blog)](https://github.blog/news-insights/product-news/introducing-github-copilot-extensions/) — Original announcement
- [Build Copilot Extensions Faster with Skillsets (Changelog)](https://github.blog/changelog/2024-11-19-build-copilot-extensions-faster-with-skillsets/) — Skillsets launch
- [Creating a Copilot Extension Step-by-Step (Nick Taylor)](https://www.nickyt.co/blog/creating-your-first-github-copilot-extension-a-step-by-step-guide-28g0/) — Detailed tutorial
- [Quick Guide to Building Copilot Extensions (Cassidy Williams)](https://cassidoo.co/post/gh-copilot-extensions/) — Practical walkthrough
- [GitHub Copilot Extensions (DevOps Journal)](https://devopsjournal.io/blog/2024/09/14/GitHub-Copilot-Extensions) — Architecture deep-dive
- [Getting Started with Copilot Extensions (OpenReplay)](https://blog.openreplay.com/getting-started-github-copilot-extensions/) — Beginner guide with deprecation context
- [Understanding Copilot Extensions (DEV Community)](https://dev.to/shrsv/understanding-github-copilot-extensions-how-they-work-and-whats-involved-ebd) — Community explainer

### MCP Migration
- [Migrating from Copilot Extensions to MCP (Agent Patterns)](https://agentpatterns.ai/tool-engineering/copilot-extensions-to-mcp-migration/) — Migration guide
- [GitHub Kills Copilot Extensions, Forces MCP Migration (ByteIota)](https://byteiota.com/github-kills-copilot-extensions-forces-mcp-migration/) — Analysis
- [MCP Official Documentation](https://modelcontextprotocol.io/introduction) — The MCP standard
- [GitHub MCP Registry](https://github.com/mcp) — Curated MCP server directory

### Community & Analysis
- [Announcing Public Beta of Copilot Extensions (Sept 2024)](https://github.blog/changelog/2024-09-17-announcing-the-public-beta-of-github-copilot-extensions-%F0%9F%8E%89/) — Public beta launch
- [GitHub Community Discussion: Copilot Advantages and Limitations](https://github.com/orgs/community/discussions/143827) — Community feedback
- [Copilot Instructions vs Prompts vs Custom Agents vs Skills (DEV)](https://dev.to/pwd9000/github-copilot-instructions-vs-prompts-vs-custom-agents-vs-skills-vs-x-vs-why-339l) — Taxonomy explainer
