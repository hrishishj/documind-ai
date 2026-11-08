# DocuMind AI – Architecture & Execution Plan

## Requirements (from assignment)
Session-wise multi-turn chatbot; file attachments; DOCX/PPTX/XLSX generation; template analysis
(logos/images + positions, fonts, colors, headings, margins, tables, borders, headers/footers,
layouts, slide masters, spreadsheet formatting); format-preserving edit/generate; LangGraph
workflows (understand → generate → validate → render); web research + RAG; citations;
conversational artifact editing; secure files; docs, README, requirements.txt, samples, GitHub.

## Decisions
| Area | Choice |
|---|---|
| LLM | Groq (via `langchain-groq`), behind a provider interface |
| Embeddings | Local `sentence-transformers` (BAAI/bge-small-en-v1.5, 384-d) |
| Web search | Tavily; DuckDuckGo fallback when no key |
| Relational DB | PostgreSQL 16 (+ LangGraph Postgres checkpointer) |
| Vector DB | Pinecone (namespace per session) |
| Frontend | React + TypeScript (Vite) |

## Core design: Template Profile + Edit Plan
1. Analyzer (deterministic) → Template Profile JSON.
2. LLM → structured Edit Plan (Pydantic), never raw files/XML.
3. Renderer (deterministic) applies the plan to the ORIGINAL file in place, cloning existing
   styles/layouts so formatting is preserved.

## LangGraph
`route_intent → document_understanding → retrieve_context → plan_content → generate_content →
validate (retry ≤2) → render_file → verify_output → respond(with citations)`

## Phases
0 Foundation · 1 Sessions/chat/uploads · 2 Template analysis · 3 Render engine ·
4 LangGraph workflows · 5 RAG + web research + citations · 6 Conversational editing/versioning ·
7 Hardening/tests/Docker · 8 Samples, docs, README, GitHub.

## Audit policy
Every step is verified by executing it (tests, real files, real API calls). Claims in docs are
only made for behaviour that has been run. Known gaps are listed honestly in the README.
