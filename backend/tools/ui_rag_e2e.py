"""Real-browser check of document Q&A with citations (Edge via Playwright) against RUNNING servers + live services.

    backend:  python -m uvicorn app.main:app --port 8000
    frontend: npm run dev
    run:      python tools/ui_rag_e2e.py [screenshot_dir]
"""
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = "http://localhost:5173"
R = Path(__file__).resolve().parents[2] / "sample_templates"
shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
shots.mkdir(parents=True, exist_ok=True)


def main():
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("response", lambda r: problems.append(f"HTTP {r.status} {r.request.method} {r.url}") if r.status >= 400 else None)
        page.goto(URL)
        expect(page.get_by_role("button", name="+ New chat")).to_be_enabled(timeout=15000)
        page.get_by_role("button", name="+ New chat").click()
        expect(page.locator(".session-list li.active .session-btn")).to_have_text("New chat")

        page.set_input_files("[data-testid=file-input]", [str(R / "xlsx/sec_filing_sample.xls"), str(R / "pdf/ael_investors_presentation.pdf")])
        panel = page.get_by_label("Files")
        expect(panel.locator(".fname")).to_have_count(2, timeout=60000)
        expect(panel.get_by_text("Preparing for search…").first).to_be_visible(timeout=15000)
        page.screenshot(path=str(shots / "10-indexing.png"))
        expect(panel.get_by_text("Searchable", exact=False)).to_have_count(2, timeout=240000)   # badges flip by themselves
        expect(panel.get_by_text("Preparing for search…")).to_have_count(0)
        page.screenshot(path=str(shots / "11-searchable.png"))

        box = page.get_by_label("Message")
        box.fill("What was total revenue in fiscal 2026 and how did it compare with 2025?")
        box.press("Enter")
        bubble = page.locator(".msg.assistant .bubble").last
        chip = bubble.locator("button.cite").first
        expect(chip).to_be_visible(timeout=120000)
        text = bubble.inner_text()
        assert "331,839" in text.replace("\u00a0", " ") or "331839" in text, text
        sources = bubble.get_by_label("Sources")
        expect(sources).to_be_visible()
        expect(sources.get_by_text("sec_filing_sample.xls")).to_be_visible()
        chip.click()
        quote = sources.locator("blockquote")
        expect(quote).to_be_visible()
        assert "Fiscal Year 2026" in quote.inner_text() or "revenue" in quote.inner_text().lower(), quote.inner_text()
        page.screenshot(path=str(shots / "12-answer-with-citations.png"))

        # citations are stored with the message: they survive a reload
        page.reload()
        chip2 = page.locator(".msg.assistant .bubble button.cite").first
        expect(chip2).to_be_visible(timeout=15000)

        # clean up through the UI (also removes the conversation's vectors from Pinecone)
        before = page.locator(".session-list li").count()
        page.once("dialog", lambda d: d.accept())
        page.locator(".session-list li.active").hover()
        page.locator(".session-list li.active .icon-btn").click()
        expect(page.locator(".session-list li")).to_have_count(before - 1, timeout=30000)  # wait: closing early would cancel the DELETE
        browser.close()

    if problems:
        print("BROWSER PROBLEMS:", *problems, sep="\n  ")
        sys.exit(1)
    print("UI RAG E2E PASSED: indexing badge -> searchable, grounded answer, citation chip, Sources panel, persistence; no console errors")


if __name__ == "__main__":
    main()
