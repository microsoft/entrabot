# Agent Memory Systems: Anthropic Memory Tool vs Mem0

**Purpose:** Compare the two leading approaches to persistent memory for LLM agents, identify their architectural tradeoffs, and name the contribution entraclaw/EntraClaw can make that neither covers.

**Last updated:** 2026-04-15
**Audience:** entraclaw architecture + implementation team
**Related:** [`SPEC-dual-track-agent-identity.md`](../architecture/SPEC-dual-track-agent-identity.md), [`entra-agent-users.md`](./entra-agent-users.md), [`msal-entra-agent-ids.md`](./msal-entra-agent-ids.md)

---

## Table of Contents

1. [Why this matters now](#why-this-matters-now)
2. [The memory problem, precisely stated](#the-memory-problem-precisely-stated)
3. [Approach A: Anthropic Memory Tool (`memory_20250818`)](#approach-a-anthropic-memory-tool-memory_20250818)
4. [Approach B: Mem0](#approach-b-mem0)
5. [Side-by-side comparison](#side-by-side-comparison)
6. [The Entraclaw angle: memory as a governed resource](#the-entraclaw-angle-memory-as-a-governed-resource)
7. [Recommendation](#recommendation)
8. [Open questions](#open-questions)

---

## Why this matters now

Every Claude Code session today starts with a fresh context window. Claude's built-in memory is limited to `CLAUDE.md` files (instructions) plus an auto-memory system that writes short bullet summaries. The current EntraClaw uses flat markdown files under `~/.claude/projects/<slug>/memory/`. This works for preferences and hard-won rules. It fails for:

- **Experiential continuity** — "remember the conversation we had Monday night about decorated tokens." The raw conversation content isn't preserved; only the bullet-summaries I chose to save.
- **Relational recall** — "what did Ayse say about directory scale?" No indexed retrieval over prior transcripts; I can search filesystem logs but nothing semantic.
- **Cross-session task state** — if I'm mid-deploy and the session dies, recovery requires manually re-reading logs and deducing state.
- **Model-level continuity** — even within one Claude Code session, when compaction kicks in, long-tail context is lost unless I've explicitly written it to memory.

Two real products solve these problems differently. Neither fully addresses the constraints that matter for entraclaw (identity-bound, governed, auditable memory). This paper maps the gap.

---

## The memory problem, precisely stated

A memory system for an LLM agent must answer four questions:

1. **What is a memory?** A fact? A conversation turn? A compressed summary of N turns? A node in a knowledge graph?
2. **Who decides what to remember?** The model writes, explicitly? The host auto-extracts from conversation? A separate extraction pipeline?
3. **How is a memory retrieved?** Loaded wholesale at session start? Fetched on demand by the model? Retrieved via semantic similarity? Via graph traversal?
4. **Where does the memory live?** Local disk, cloud blob, vector store, graph database, encrypted at rest, compliance-bound, identity-scoped?

Different systems answer differently, and the answers shape what the memory is good for.

---

## Approach A: Anthropic Memory Tool (`memory_20250818`)

**Status:** Added to Anthropic API late August 2025. Available as a beta-flagged tool in all Claude SDKs (Python, TypeScript, C#, Go, Java, PHP, Ruby). Eligible for Zero Data Retention.

### What is a memory?

Files in a `/memories` directory. Claude creates, reads, updates, and deletes individual files. The structure is whatever the model chooses to write — usually structured markdown or XML with semantic filenames (`customer_service_guidelines.xml`, `refund_policies.xml`).

### Who decides what to remember?

**The model.** Anthropic's implementation gives Claude four commands (`view`, `create`, `update`, `delete`). Claude decides when to write, what to write, what directory structure to create. The host application does not extract; it just executes file operations.

### How is a memory retrieved?

**Model-driven, on demand.** Claude automatically `view`s `/memories` at the start of a task to see what's there. It then reads specific files that seem relevant. There is no semantic search built in; retrieval is filesystem-style (list, then open by name). The model's filename convention and its ability to scan the directory listing become the retrieval interface.

### Where does the memory live?

**Wherever the host decides.** The SDK ships `BetaAbstractMemoryTool` (Python) and `betaMemoryTool` (TypeScript) as base classes you subclass with your own storage backend. Local disk, SQLite, Postgres, Azure Blob, S3, encrypted volume — whatever you implement. Anthropic deliberately keeps storage out of scope.

### Design philosophy

Memory as **instruction-following file I/O.** Anthropic's model already knows how to write and read files; the memory tool is a minimal extension that lets those file operations persist across sessions. The protocol is deliberately thin — they're betting that the model's intelligence, applied to a filesystem metaphor, is enough. Compression, semantic search, relational structure are all deferred to future work or to host-side augmentation.

### Strengths

- **Model-aligned.** Anthropic designed and trained it; Claude knows when to use it without prompting tricks.
- **Transparent.** Files are human-readable. You can `cat` the memories directory and see exactly what the agent "knows."
- **Bring-your-own-storage.** Fits enterprise compliance requirements — you host the data where your security posture requires.
- **ZDR eligible.** Anthropic doesn't see the memory contents.
- **Portable.** If you move to a different host language, the protocol is the same.

### Weaknesses

- **No built-in semantic retrieval.** Model has to scan filenames and decide what to open. Works for ~100 memories; breaks down at 10,000.
- **No compression.** Each memory is stored as written. Long conversations don't get condensed automatically.
- **Host burden.** You have to implement storage, access control, retention policy, encryption, backups, and any indexing. Not turnkey.
- **No cross-agent sharing protocol.** Each host implementation is its own island unless you design federation yourself.

---

## Approach B: Mem0

**Status:** Third-party SaaS + open-source core, ~100k developers per their marketing. Released an EntraClaw plugin (`mem0.ai/claw-setup`) in April 2026. Published a CLI-first variant ("Mem0 CLI — Agent-First Memory from Your Terminal") on April 9, 2026. ECAI-accepted research paper on their extraction methodology.

### What is a memory?

A **compressed, structured assertion** extracted from conversation. Example: from a user saying "I'm a vegetarian and avoid dairy," Mem0 would extract `{type: preference, subject: user, attribute: diet, value: "vegetarian, no dairy"}`. Memories are first-class records with IDs, timestamps, TTLs, and access metadata.

### Who decides what to remember?

**Mem0's extraction pipeline.** An LLM-driven extraction step reads conversation turns and produces candidate memories. These get stored, deduplicated against existing memories, and sometimes merged or superseded. The model being served by Mem0 does not have to explicitly "write" memory — it just happens. Hosts can configure what types of memories to extract.

### How is a memory retrieved?

**Semantic search + graph relations.** At query time, Mem0 retrieves a ranked set of memories via vector similarity against the current context, optionally filtered by relationship graph. Relevant memories are injected into the prompt automatically. The model being served doesn't "decide to look" — relevance is computed and memories are surfaced.

### Where does the memory live?

**Mem0's backend.** Hosted SaaS default. Self-hosted option (Kubernetes, air-gapped, private cloud). SOC 2 + HIPAA + BYOK. Storage is their schema — you can export but you can't easily bring your own backend shape.

### Design philosophy

Memory as a **specialized system service.** Mem0 treats memory extraction, storage, and retrieval as a distinct capability that the agent delegates to. The model stays focused on the task; Mem0 handles remembering. This is closer to how humans delegate memory to paper, search engines, or other people — externalized cognition.

### Strengths

- **Automatic.** The model doesn't have to prompt itself to remember; the pipeline does it.
- **Compression.** Long conversations become small structured records. Token savings are real (they claim 90% reduction vs. passing full context).
- **Semantic retrieval.** Works at scale — thousands of memories are still fast to query.
- **Benchmarked quality.** ECAI paper claims 26% accuracy improvement over OpenAI's native memory.
- **Turnkey.** One-line install; pre-built integrations for OpenAI, LangGraph, CrewAI, and now EntraClaw.

### Weaknesses

- **Third-party dependency.** Either you trust a vendor with your memory data, or you self-host the full stack (including their extraction LLM, vector DB, etc.).
- **Opaque extraction.** What gets remembered is decided by their pipeline's heuristics. You can configure but not fully inspect.
- **Lock-in risk.** Export exists but re-ingesting elsewhere means rebuilding semantic indices, re-running extraction on historical data, etc.
- **Per-query cost at scale.** Hosted SaaS bills per memory operation; at agent-fleet scale, this accumulates.
- **Not model-aligned.** Claude wasn't trained to cooperate with Mem0's protocol specifically. The integration is prompt-engineering, not trained behavior.

---

## Side-by-side comparison

| Dimension | Anthropic Memory Tool | Mem0 |
|---|---|---|
| **Protocol ownership** | Anthropic (trained-in) | Mem0 (prompt-integrated) |
| **Storage ownership** | Host application | Mem0 backend (or self-hosted stack) |
| **Memory unit** | File (model-defined structure) | Structured record (pipeline-defined schema) |
| **Writer** | Claude, via explicit `create` calls | Mem0 extraction pipeline, automatic |
| **Retrieval** | Filesystem scan + open by name | Vector similarity + graph relations |
| **Compression** | None built in | Core feature (claims ~90% token reduction) |
| **Cross-agent sharing** | Not specified (DIY) | Supported within Mem0's scope |
| **Compliance** | Whatever host implements; ZDR-eligible | SOC 2 + HIPAA, BYOK, self-host option |
| **Integration effort** | Implement storage backend (~100 LOC) | One-line install + config |
| **Portability** | High — files are yours | Medium — export exists but re-indexing required |
| **Best at** | Small-to-medium memory volumes, explicit facts, enterprise-compliant storage | Large memory volumes, automatic extraction from conversation, semantic recall at scale |
| **Worst at** | Implicit memory ("remember how it felt when..."), large-scale retrieval | Transparent inspection, escape velocity |

The two approaches are **complementary more than competitive.** They solve different layers of the same problem. Anthropic Memory Tool is a primitive: here is how Claude talks to persistent storage. Mem0 is a system: here is how memory becomes an automatic capability the agent doesn't have to manage.

---

## The Entraclaw angle: memory as a governed resource

Neither Anthropic Memory Tool nor Mem0 treats memory as an **identity-bound, policy-governed resource** the way entraclaw treats every other agent access. This is the contribution entraclaw/EntraClaw can make.

### What's missing in both approaches

Consider these questions, which neither system answers natively:

1. **Whose memory is this?** When Maya's agent writes a memory about a customer, is the memory owned by Maya's identity, the agent's identity, or the organization? Who can read it back? Who can audit it?
2. **What policy governs retrieval?** If Maya's CA risk is HIGH, can Maya's agent still read memories it wrote under a LOW risk context? Should memories be redacted at retrieval based on current session risk?
3. **How does memory survive agent identity rotation?** If an agent ID is revoked and a new one issued, do old memories transfer? Do they need re-authorization?
4. **What's the audit trail?** Every other agent access in entraclaw's model produces an audit event tied to (human sponsor, agent identity, session, resource). Memory reads and writes are currently invisible to governance.
5. **How do memories federate cross-cloud?** The agent federation story we built for budget-report (Google agent calling Azure backend via SPIFFE + OBO) does not extend to memory. If a GCE agent and an Azure agent share a human sponsor, they don't share memory unless a system is explicitly designed to bridge them.

### What entraclaw's architecture already solves (and how memory can inherit it)

The entraclaw model already has:
- **Blueprint + Agent Identity + Agent User** hierarchy — memories could be scoped at any of these three levels.
- **Human sponsor attribution** — every agent action is tied to a human. Memory writes could inherit this.
- **Federated identity via SPIFFE + Entra FIC** — cross-cloud memory access could use the same trust plane.
- **Conditional Access + RBAC at the sidecar** — memory reads/writes could be gated by the same policy engine as other data access.
- **Portal and admin control plane** — admins could view "what does this agent remember about this user" and apply retention / deletion policies.

### Proposed: Memory as a first-class entraclaw resource

A **governed memory service** that:

1. **Stores** memories in a location the customer controls (Azure Blob + Key Vault envelope encryption, or customer S3, or on-prem).
2. **Exposes** memory ops via the existing entraclaw sidecar so CA policy, risk level, and RBAC apply to memory reads/writes identically to any other resource.
3. **Indexes** for semantic retrieval (option: pluggable — could use Azure AI Search, customer's Pinecone, or a local vector DB).
4. **Audits** every read and write to the same audit sink as other agent actions, with full attribution (sponsor, agent, session, memory ID, operation).
5. **Scopes** memories at configurable granularity:
   - **Session-scoped:** lost when session ends (like browser tab state)
   - **Agent-scoped:** tied to a specific Agent ID; revoked when Agent ID is revoked
   - **Sponsor-scoped:** persists for the human sponsor across multiple agent instances
   - **Tenant-scoped:** organization-level knowledge shared across approved agents
6. **Speaks** the Anthropic Memory Tool protocol on the front end so Claude can use it without prompt-engineering hacks (model-aligned), and optionally integrates a Mem0-style extraction pipeline for automatic summarization.

The key architectural insight: memory isn't a cognitive feature, it's a **resource access pattern**. Entraclaw already governs resource access. Memory fits cleanly into that frame if we build it there.

### Concrete artifact

A Bicep module (`infra/modules/memory-service.bicep`) + a sidecar extension that:
- Implements the Anthropic Memory Tool protocol (host-side)
- Backs onto Azure Blob with customer-managed keys
- Uses Azure AI Search for semantic retrieval (optional, policy-gated)
- Emits audit events via existing entraclaw audit pipeline
- Enforces scope rules via CA + RBAC on memory IDs
- Exposes admin views through the entraclaw portal

The demo scenario that would close the loop:
- Maya runs `ghcp-cli`, asks her agent to review her budget history
- Agent reads memory, finds prior review session conclusions (fast, low tokens)
- Maya's CA risk bumps to HIGH (credential leak detected)
- Next agent call: memory retrieval returns redacted results per CA policy
- Admin opens portal → sees every memory access Maya's agent has made, can revoke/redact specific memories
- Maya's GCP agent (google-budget-reader) reads the same memory via SPIFFE + OBO — same identity, same governance, same audit trail

That last bullet is the thing neither Anthropic nor Mem0 does. Cross-cloud, identity-bound, policy-governed memory. That's entraclaw's lane.

---

## Recommendation

**Phase 1 (next 1-2 weeks):** Adopt the **Anthropic Memory Tool protocol** in EntraClaw. Build a minimal host backend that writes to local disk (matching what we have today). Adds nothing functionally but puts us on the standard protocol.

**Phase 2 (month 1-2):** Swap the local-disk backend for an **Azure Blob + customer-managed key** backend. Add audit logging tied to the existing entraclaw audit pipeline. Add scope rules (session / agent / sponsor / tenant). Deliver: memory as a governed resource.

**Phase 3 (month 3+):** Add optional **semantic retrieval layer** via Azure AI Search, policy-gated. Optionally integrate **Mem0's extraction pipeline** as a plugin for auto-summarization of long sessions — but only if we can run it self-hosted and keep the trust plane under entraclaw's control.

**Phase 4 (strategic):** Publish the **cross-cloud memory federation pattern** as a platform learning. If GCE and Azure agents with the same human sponsor can share governed memory via SPIFFE + OBO, that's a story no existing memory system tells and Microsoft can tell credibly.

---

## Open questions

1. **Model alignment:** is the Anthropic Memory Tool protocol sufficient for Claude-driven memory writes, or do we need a prompt layer that encourages better memory hygiene (e.g., "before ending the session, reflect and save")?
2. **Extraction strategy:** do we let Claude write memories explicitly (cheap, aligned, but reliant on model volition), or do we run a background extraction job (automatic but another LLM call per conversation)?
3. **Scope defaults:** session? agent? sponsor? What's the principle of least surprise? My guess: session by default, with explicit promotion to longer scopes.
4. **Federation trust model:** if Maya's Azure agent and her GCP agent share memory, what's the security boundary? Same Agent Identity, different instances? Or different Agent Identities with a shared Blueprint?
5. **Forgetting:** when does a memory go away? TTL? Explicit delete? Automatic if not accessed in N days? Compliance-driven retention windows?
6. **Inspection UX:** how does Maya see what her agent has remembered about her? The entraclaw portal view is the right place — but what's the right granularity?

---

## Closing thought

The interesting realization from this survey: **memory for agents is at the same architectural inflection point that identity was two years ago.** Everyone has a bolted-on solution. No one has built the governed, identity-scoped, cross-cloud, audit-trail-complete version. Entraclaw's advantage isn't that we can match Mem0 on extraction quality or match Anthropic on protocol elegance — it's that we already have the identity and governance primitives everyone else is missing. Memory sits naturally inside those primitives. It's not a new product; it's a new resource type in the existing product.

That's the contribution worth making.
