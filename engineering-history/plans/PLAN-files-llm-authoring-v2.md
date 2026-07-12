# PLAN: LLM-Driven Office-Format Authoring (V2)

> **Status:** STUB — explicitly deferred from V1 per CEO-review D2 (2026-04-30)
> **Branch:** unscheduled
> **Companion (V1):** [`docs/architecture/PLAN-files-mcp-tools.md`](./PLAN-files-mcp-tools.md)
> **Companion (research):** [`docs/platform-learnings/files-graph-api.md`](../platform-learnings/files-graph-api.md)

## Problem

Microsoft Graph cannot ingest Markdown and emit a Word / Excel / PowerPoint file. To produce real Office documents from agent-authored content, the agent must build the OOXML client-side and then upload.

V1 (the live `PLAN-files-mcp-tools.md`) ships **markdown-only** authoring (`write_text_file`). That decision is locked. Users who ask "make me a docx" in V1 get a `.md` file, which is honest but not what most knowledge-worker workflows want.

V2 closes that gap.

## Why a separate plan, not an extension of V1

Office authoring is **not** "wrap `python-docx` in an MCP tool." Tools that emit a single `.docx` from a Markdown blob (the original V1 sketch) produce visibly generic documents — wrong fonts, broken numbered lists, missing brand colors, no slide layouts. That output looks worse than the markdown it replaced.

To produce Office documents that pass the "would a human have made this?" test, you need three layers:

1. **Templates.** A library of `.docx` / `.xlsx` / `.pptx` templates with named styles, typography, color palette, and (for PowerPoint) slide layouts. Per-tenant or per-team — uploaded once, reused.
2. **Intermediate Representation (IR).** The LLM does not write OOXML. It emits structured JSON (sections, lists, tables, callouts, slide layouts, chart specs, bibliography refs). The IR is the authoring contract — versioned, documented, easy to diff, easy to test.
3. **Renderer.** A deterministic IR → OOXML pipeline using `python-docx` / `openpyxl` / `python-pptx`, applying the chosen template's styles to the IR's structural elements. Renderer is pure; given the same template + IR, output is byte-identical (for diffing and review).

This is the architecture used by Gamma, Tome, Beautiful.ai, Microsoft Copilot for Word, and Notion AI. It is roughly **600-1000 LOC** of renderer code per format, plus a template library, plus eval infrastructure. **It is not in V1's budget** — and trying to compress it into V1 would ship the wrong shape.

## V2 Tool Surface (sketch — not locked)

```python
list_office_templates() -> list[TemplateSummary]
author_word_doc(ir: WordIR, template_id: str, filename: str,
                target: UploadTarget = OneDriveTarget("/")) -> FileSummary
author_excel_workbook(ir: ExcelIR, template_id: str, filename: str,
                      target: UploadTarget = OneDriveTarget("/")) -> FileSummary
author_powerpoint_deck(ir: SlidesIR, template_id: str, filename: str,
                       target: UploadTarget = OneDriveTarget("/")) -> FileSummary
```

Same `UploadTarget` shape as V1. Same sponsor-allowlist + site-denylist enforcement (V1's `_check_site_allowed` is a precondition).

The IR types (`WordIR`, `ExcelIR`, `SlidesIR`) are the meaningful design work — they are the contract between the LLM and the renderer. They need their own design doc before any code lands.

## Open design questions (deferred)

1. **Template authority.** Per-tenant uploaded templates? A built-in starter set? A mix?
2. **IR schema discipline.** JSON Schema-validated? Pydantic-validated? Both?
3. **Renderer dependency footprint.** `python-docx` + `openpyxl` + `python-pptx` is ~10 MB. Acceptable, or split into optional extras (`pip install entrabot[office-authoring]`)?
4. **Eval strategy.** Output deterministic given (template, IR), so byte-diff golden files work for renderer. LLM-IR-quality eval is harder — need a rubric for "is this IR a good representation of what the user asked for?"
5. **Slide layout model for PowerPoint.** The hardest of the three by far. Slide authoring is closer to a layout-engine problem than a document problem. Possibly worth a third sub-plan.
6. **Chart specifications in Excel.** Built-in chart types only, or Plotly-rendered images embedded?

## Prerequisites for promoting this to a real plan

- V1 has shipped and is in production use
- At least one user has explicitly hit the "I asked for docx, got md" gap
- Template authority (Q1) has a tenant-admin owner
- Eval infrastructure (Q4) has a budget

## Estimated scope (rough)

- IR schema design + tests: 1-2 weeks
- Word renderer: 1-2 weeks
- Excel renderer: 1-2 weeks
- PowerPoint renderer: 3-4 weeks (the layout problem)
- Template library + tenant-admin tooling: 1 week
- LLM eval suite: 1-2 weeks
- Integration tests + docs: 1 week

Total: roughly 2-3 months of one engineer's time, or ~2-3 weeks with `/autoplan` + sub-agents per the gstack model. Either way, an order of magnitude bigger than V1.

## Until then

V1 produces Markdown. Markdown renders cleanly in Teams, GitHub, VS Code, and any modern doc viewer. For most "agent drafted me a spec" workflows it's fine. The gap is real but not urgent until a user complains.
