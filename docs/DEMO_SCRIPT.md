# Demo video script (about 6-7 minutes)

Record at 1080p with the stack running (`docker compose up`, then <http://localhost:8080>).
Have ready: `sample_templates/docx/sec_filing_sample.docx`, `sample_templates/xlsx/sec_filing_sample.xls`,
`sample_templates/pdf/ael_investors_presentation.pdf`, and a `.pptx` of your own.
Do one dry run first so the Groq free-tier rate limit is not hit mid-recording (pause 60 s between heavy requests).

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00 | Empty app | What it is: a multi-agent assistant that analyses and edits Word/PowerPoint/Excel while keeping the template's design, and answers with citations. |
| 2 | 0:30 | Architecture diagram from docs/ARCHITECTURE.md | The LLM never writes a file; it proposes a validated Edit Plan; deterministic renderers apply it to a copy of the original; a preservation check verifies the result. |
| 3 | 1:15 | Upload `sec_filing_sample.docx` | Template analysis panel: styles, fonts, tables detected. Uploads are validated by content, macros rejected. |
| 4 | 2:00 | Ask: "Retitle the report ... and add a row to the first table ..." | Watch the streamed workflow steps (understand, plan, validate, render). Download v1, open it in Word: formatting intact. |
| 5 | 3:00 | Follow-up: "Make the title shorter" | Multi-turn: edits the latest version; v1 still downloadable. |
| 6 | 3:40 | Upload the PDF; wait for the "searchable" badge; ask a factual question | Answer with `[n]` chips; open one: exact page and supporting snippet. Then ask something not in the document: it says so instead of guessing. |
| 7 | 4:50 | Ask: "Search the web for ... and add a summary slide with sources" on a PPTX | Tavily results with URLs, WEB badge, Sources slide on the template's own layout. The server never fetches pages. |
| 8 | 5:40 | Repo tour: `sample_outputs/` (plan JSON next to edited file), `docs/AUDIT.md`, test counts | Plans are auditable JSON; every defect found during development is logged; ~375 backend + 70 frontend tests. |
| 9 | 6:20 | Wrap-up | Limits: free-tier token cap, anonymous sessions, scanned PDFs. |

Tips: keep prompts short and explicit; if a step is slow, say so rather than cutting (it shows the real rate limit handling);
never show `.env` on screen.
