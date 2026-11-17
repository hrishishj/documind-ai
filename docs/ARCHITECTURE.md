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

## Observability, reliability and cost

**Tracing** (`app/observability/trace.py`). Every chat turn is one *run* made of spans: each LangGraph node, each LLM
request attempt (model, attempt number, rate-limit wait, input/output tokens) and each external call (web search, vector
search). A span records start offset, duration, status and the error type/message. Spans nest through a `ContextVar`
(a tool call is a child of its node). Traces hold **metadata only** - never the user's message, document text or model
output - and are stored per turn in `run_traces` (owner-scoped like everything else):

* `GET /sessions/{id}/runs` - latency, tokens, cost and outcome (`ok | error | gave_up`) per turn;
* `GET /sessions/{id}/runs/{run_id}` - the full span tree (every retry and failure visible);
* `GET /metrics` - Prometheus text: runs by intent/status, tokens, cost, LLM calls/retries/failures, latency histogram;
* every server log line is stamped with the first 8 characters of its run id.

A streamed response resumes in a fresh context on each step, so the route re-activates the trace around every
`next()` of the graph stream (a test mutation-checks this).

**Cost** = tokens reported by the provider x list price (`PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK`).

**Reliability.** LLM: 60 s timeout per request, SDK retries disabled and replaced by our own loop (exponential backoff,
`retry-after` honoured, 5 attempts) behind a sliding-window token governor; a reply that is not valid structured output
is retried by the *workflow* with the validator's reasons (3 attempts), then the user gets an honest "could not" message.
Web search: Tavily then ddgs, 15 s client timeout, 30 s hard deadline. Vector store: 30 s hard deadline, and any failure
degrades to plain chat with a visible note. Deadlines use `app/core/timeouts.py` (daemon thread, caller stops waiting).
`tests/test_fault_injection.py` breaks each dependency on purpose and asserts the user-visible result and the trace.

**Evaluation.** `tools/eval_edits.py` (30 end-to-end cases, graded from the produced file) and `tools/eval_qa.py`
(answer correctness and citation validity) run against the real model and write `docs/EVAL_RESULTS.md` and
`docs/EVAL_QA_RESULTS.md`; retrieval recall is in `docs/RAG_EVAL.md`.

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
