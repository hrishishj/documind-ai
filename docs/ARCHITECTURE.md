# DocuMind AI - Architecture

## Principle

**The LLM never writes a document.** It only proposes an *Edit Plan*: a small, schema-validated list of
operations (`docx.replace_text`, `pptx.add_slide`, `xlsx.set_cell`, ...). Deterministic renderers apply that
plan to a copy of the **original** file, so styles, masters, themes, numbering, charts and layouts are
preserved by construction, not by hoping the model reproduces them.

Every edit is then re-analysed and checked by `verify.check_preservation`. If a plan is invalid or breaks
the document, the error is fed back to the planner (max 3 attempts) before the user is told it failed.

## Components

```
React UI (Vite)  --/api-->  FastAPI  --->  LangGraph workflow  --->  Groq (gpt-oss-120b)
 SSE progress, versions      sessions,       route -> research ->        structured Edit Plan
 citations, downloads        uploads, SSE    understand -> plan ->
                                |            validate -> render -> respond
                                |                  |
                          PostgreSQL          Document engine (deterministic)
                      sessions, messages,     analyzers: DOCX / PPTX / XLSX(.xls)
                      versions, citations,    renderers: edit a copy of the original
                      LangGraph checkpoints   verify: preservation checks
                                |
                       Pinecone (namespace per conversation) + local bge-small embeddings
                       Tavily -> ddgs meta-search (web research, URLs only)
```

## Workflow (LangGraph)

| Node | Job |
|---|---|
| route | Cheap rule-based routing first (`quick_route`), LLM only when ambiguous: chat / grounded Q&A / edit / research. Resets per-turn state. |
| research | Only when the user asked for the web (or freshness words and no documents). Tavily, then ddgs. Results are *untrusted data*. |
| understand | Builds a compact planner view of the document (outline + formatting census) to fit the 8k tokens/min budget. |
| plan | LLM emits a validated Edit Plan (pydantic discriminated union, per format). Sources section is appended deterministically. |
| validate | Dry-validates the plan against the real document; errors loop back to *plan* (max 3). |
| render | Applies the plan to a copy of the original, re-analyses, checks preservation, stores a new version. |
| respond | Plain-language summary of what changed, plus citations. |

State is checkpointed in PostgreSQL by `thread_id = session_id`, so a conversation resumes across turns and restarts.

## RAG and citations

* Documents are split into ~900-character passages that keep an exact locator (paragraph, table, slide, sheet rows, PDF page).
* Embeddings: `BAAI/bge-small-en-v1.5` locally (384-d, no API key). Vector store: Pinecone, one namespace per conversation (in-memory twin for tests).
* Ranking: cosine plus a small lexical term; below a relevance floor the answer is "not found in the documents" instead of a guess.
* Every `[n]` marker in an answer is verified against the retrieved passages; invented markers are stripped, missing ones recovered, and each citation shows the supporting snippet.
* Web citations carry the URL. The server **never fetches** result pages (no SSRF surface); it only uses the snippet the search API returned.

## Security model

* Uploads: extension allow-list, magic-byte match, macro-enabled formats rejected, zip-bomb / zip-slip / XXE limits, size and complexity caps.
* Sessions are scoped to an anonymous httpOnly cookie; one visitor cannot list or open another's conversations.
* Document and web text is treated as data; the planner prompt states it must not follow instructions found inside it (probed in `tools/injection_probe.py`).
* Frontend renders model output through a safe renderer (no `dangerouslySetInnerHTML`); links are re-validated to http(s).

## Why these choices

* **Groq `openai/gpt-oss-120b`**: fast, free tier; its 8,000 tokens/minute cap drove the compact planner view and the `TokenGovernor`.
* **Edit Plan instead of code generation**: auditable, testable, and cannot execute arbitrary code.
* **Pure-Python `.xls` converter**: no LibreOffice dependency; cross-checked against Excel COM (252 cells, 0 mismatches).
* **Local embeddings**: no extra key, no per-call cost, reproducible.

See [AUDIT.md](AUDIT.md) for every defect found and fixed, and [RAG_EVAL.md](RAG_EVAL.md) for retrieval measurements.
