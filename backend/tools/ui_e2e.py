"""Real-browser end-to-end test (Edge via Playwright) against RUNNING servers:

    backend:  python -m uvicorn app.main:app --port 8000
    frontend: npm run dev            (http://localhost:5173)
    run:      python tools/ui_e2e.py [screenshot_dir]

Uses the live LLM, so it consumes a few thousand Groq tokens.
"""
import io
import sys
import tempfile
from pathlib import Path

from docx import Document
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.document_engine.samples import build_docx  # noqa: E402

URL = "http://localhost:5173"
shots = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
shots.mkdir(parents=True, exist_ok=True)


def main():
    work = Path(tempfile.mkdtemp())
    sample = work / "Quarterly Report.docx"
    build_docx(sample)
    problems: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

        page.goto(URL)
        expect(page.get_by_role("button", name="+ New chat")).to_be_enabled(timeout=15000)
        page.get_by_role("button", name="+ New chat").click()  # isolate from whatever earlier runs left behind
        expect(page.get_by_text("Edit Office documents by chatting")).to_be_visible(timeout=15000)
        session_title = page.locator(".session-list li.active .session-btn")
        expect(session_title).to_have_text("New chat")
        page.screenshot(path=str(shots / "01-empty.png"))

        # --- attach a document and view its template analysis
        page.set_input_files("[data-testid=file-input]", str(sample))
        panel = page.get_by_label("Files")
        expect(panel.locator(".fname", has_text="Quarterly Report.docx")).to_have_count(1, timeout=20000)
        expect(panel.locator(".uploading")).to_have_count(0)  # the placeholder is gone once the file is listed
        panel.get_by_role("button", name="Template analysis").click()
        dlg = page.get_by_role("dialog")
        expect(dlg.get_by_text("8.5\" × 11\"")).to_be_visible()
        expect(dlg.locator("dd", has_text="Georgia")).to_be_visible()  # dominant font from the template census
        page.screenshot(path=str(shots / "02-template-analysis.png"))
        page.keyboard.press("Escape")
        expect(dlg).to_have_count(0)

        # --- first edit (live LLM)
        box = page.get_by_label("Message")
        box.fill("Change the report title to 'FY2025 Annual Report' and add a table row for West with 5 and 6.")
        box.press("Enter")
        expect(page.get_by_label("Working")).to_be_visible(timeout=5000)
        page.screenshot(path=str(shots / "03-working.png"))
        card = page.locator(".artifact-card").first
        expect(card).to_be_visible(timeout=90000)
        expect(card.get_by_text("layout and formatting preserved")).to_be_visible()
        expect(card.get_by_text("v1")).to_be_visible()
        page.screenshot(path=str(shots / "04-result-v1.png"))

        with page.expect_download() as dl:
            card.get_by_role("link", name="Download").click()
        v1 = work / "v1.docx"
        dl.value.save_as(v1)
        d1 = Document(str(v1))
        assert d1.paragraphs[0].text == "FY2025 Annual Report", d1.paragraphs[0].text
        assert [c.text for c in d1.tables[0].rows[-1].cells] == ["West", "5", "6"], [c.text for c in d1.tables[0].rows[-1].cells]

        # --- follow-up edit => version 2 of the same document
        box.fill("Now make the intro paragraph say 'Revised introduction'.")
        box.press("Enter")
        expect(page.locator(".artifact-card")).to_have_count(2, timeout=90000)
        expect(page.locator(".artifact-card").nth(1).get_by_text("v2")).to_be_visible()
        expect(panel.get_by_role("link", name="v2")).to_be_visible()
        with page.expect_download() as dl2:
            panel.get_by_role("link", name="v2").click()
        v2 = work / "v2.docx"
        dl2.value.save_as(v2)
        d2 = Document(str(v2))
        assert d2.paragraphs[0].text == "FY2025 Annual Report"           # the first edit is kept
        assert "Revised introduction" in d2.paragraphs[1].text            # the second edit applied on top
        assert "Revised introduction" not in Document(str(v1)).paragraphs[1].text  # v1 untouched
        page.screenshot(path=str(shots / "05-result-v2.png"), full_page=False)

        # --- reload: history and versions persist
        page.reload()
        expect(page.locator(".artifact-card")).to_have_count(2, timeout=15000)

        # --- clean up through the UI (also exercises delete + the confirm dialog)
        page.once("dialog", lambda d: d.accept())
        page.locator(".session-list li.active").hover()
        page.locator(".session-list li.active .icon-btn").click()
        expect(page.locator(".artifact-card")).to_have_count(0, timeout=15000)
        browser.close()

    print("screenshots:", sorted(p.name for p in shots.iterdir()))
    if problems:
        print("BROWSER PROBLEMS:")
        for x in problems:
            print("  ", x)
        sys.exit(1)
    print("UI E2E PASSED: upload, analysis dialog, live edit, download, follow-up v2, reload persistence; no console errors")


if __name__ == "__main__":
    main()
