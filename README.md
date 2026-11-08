# DocuMind AI

AI-powered multi-agent document intelligence and generation platform.

## Project Status

Initial development.

This repository is currently being initialized. The capabilities below
represent the intended scope, not completed functionality.

## Intended Capabilities

- Session-based conversational AI.
- Multi-turn conversations and document attachments.
- DOCX, PPTX and XLSX document analysis.
- Document structure and formatting inspection.
- Conversational document editing and generation.
- LangGraph-based AI workflows.
- Retrieval-Augmented Generation (RAG).
- Web research with source traceability.
- Document validation and export.

## Planned Technology Stack

| Component | Technology |
|---|---|
| Frontend | React, TypeScript |
| Backend | Python, FastAPI |
| AI orchestration | LangChain, LangGraph |
| Relational database | PostgreSQL |
| Vector database | Pinecone |
| Local LLM runtime | Ollama |
| DOCX processing | python-docx, OOXML |
| PPTX processing | python-pptx, OOXML |
| XLSX processing | openpyxl, OOXML |

## Repository Structure

- `backend/` - Backend application.
- `frontend/` - Frontend application.
- `docs/` - Project documentation.
- `sample_templates/` - Sample Office templates.
- `sample_outputs/` - Generated document outputs.
- `screenshots/` - Project screenshots.
- `tests/` - Project-level tests.

## Configuration

Copy `.env.example` to `.env` and configure the required values.

Never commit `.env` or real API credentials.

## Development Principles

- Modular and maintainable architecture.
- Secure handling of uploaded documents and credentials.
- Traceable sources for research and generated content.
- Testing before marking features complete.
- Honest documentation of implementation status and limitations.

## License

License selection is pending.