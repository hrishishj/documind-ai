"""Curated end-to-end test set for the chat workflow, run against the REAL model by tools/eval_edits.py.

Each case is graded on outcomes a user would care about, read from the produced file (not from the model's say-so):
  contains  - text that must be present in the output document
  absent    - text that must be gone
  keeps     - original text that must survive untouched (collateral-damage check)
  count     - {text: exact number of occurrences}
  sheets    - sheet names that must exist (xlsx)
  order     - texts that must appear in this order (slide order)
Router-only cases (chat / clarify) have no file checks: the expected intent is the grade.
`expect`: edit = a new file must be produced | chat | clarify | no_edit = must NOT produce a file (impossible request).
Templates are the demo files in sample_templates/ (analysis views are in sample_outputs/).
"""
DOCX, PPTX, XLSX = "docx/acme_quarterly_report.docx", "pptx/acme_product_deck.pptx", "xlsx/acme_budget.xlsx"

CASES = [
    # ------------------------------------------------------------------ DOCX
    dict(id="docx-retitle", files=[DOCX], msg="Change the title to 'Quarterly Report - Q3 FY2024'", expect="edit",
         contains=["Quarterly Report - Q3 FY2024"], keeps=["Details", "Intro paragraph in Georgia"]),
    dict(id="docx-insert-para", files=[DOCX], msg="Add a sentence after the intro paragraph: Revenue grew in every region this quarter.",
         expect="edit", contains=["Revenue grew in every region this quarter."], keeps=["Intro paragraph in Georgia", "Details"]),
    dict(id="docx-set-cell", files=[DOCX], msg="In the table, set North's Q1 value to 12", expect="edit",
         contains=["12"], keeps=["Merged total", "North", "20"]),
    dict(id="docx-add-row", files=[DOCX], msg="Add a table row for South with 15 in Q1 and 25 in Q2", expect="edit",
         contains=["South", "15", "25"], keeps=["North", "Merged total"]),
    dict(id="docx-delete-heading", files=[DOCX], msg="Delete the 'Details' heading", expect="edit",
         absent=["Details"], keeps=["Quarterly Report", "Intro paragraph in Georgia"]),
    dict(id="docx-replace-all", files=[DOCX], msg="Replace the word 'Quarterly' with 'Annual' everywhere", expect="edit",
         contains=["Annual Report"], absent=["Quarterly Report"], keeps=["Details"]),
    dict(id="docx-append", files=[DOCX], msg="Add a closing paragraph at the end of the document saying: Thank you for reading.",
         expect="edit", contains=["Thank you for reading."], keeps=["Quarterly Report", "Merged total"]),
    dict(id="docx-total", files=[DOCX], msg="Change the merged total from 30 to 45", expect="edit",
         contains=["45"], absent=["30"], keeps=["Merged total", "North"]),
    dict(id="docx-rewrite-intro", files=[DOCX], msg="Rewrite the intro paragraph to say: Results are summarised below.", expect="edit",
         contains=["Results are summarised below."], absent=["Intro paragraph in Georgia"], keeps=["Details", "Quarterly Report"]),
    dict(id="docx-delete-row", files=[DOCX], msg="Remove the North row from the table", expect="edit",
         absent=["North"], keeps=["Merged total", "Region"]),
    # ------------------------------------------------------------------ PPTX
    dict(id="pptx-title", files=[PPTX], msg="Change the title slide title to 'Annual Review FY2025'", expect="edit",
         contains=["Annual Review FY2025"], keeps=["Highlights", "Revenue up 12%"]),
    dict(id="pptx-replace-all", files=[PPTX], msg="Replace FY2024 with FY2025 everywhere", expect="edit",
         contains=["FY2025"], absent=["FY2024"], keeps=["Highlights"]),
    dict(id="pptx-add-slide", files=[PPTX], msg="Add a new slide titled 'Next steps' with the text 'Launch in Q2'", expect="edit",
         contains=["Next steps", "Launch in Q2"], keeps=["Highlights", "Chart"]),
    dict(id="pptx-duplicate", files=[PPTX], msg="Duplicate the Highlights slide", expect="edit",
         count={"Highlights": 2}, keeps=["Annual Review", "Chart"]),
    dict(id="pptx-delete-slide", files=[PPTX], msg="Delete the third slide", expect="edit",
         absent=["Styled text"], keeps=["Highlights", "Annual Review"]),
    dict(id="pptx-subtitle", files=[PPTX], msg="Change the subtitle on the first slide to 'FY2025 Review'", expect="edit",
         contains=["FY2025 Review"], absent=["FY2024"], keeps=["Annual Review", "Highlights"]),
    dict(id="pptx-bullet", files=[PPTX], msg="On the Highlights slide change 'Revenue up 12%' to 'Revenue up 15%'", expect="edit",
         contains=["Revenue up 15%"], absent=["Revenue up 12%"], keeps=["Annual Review", "Metric"]),
    dict(id="pptx-move", files=[PPTX], msg="Move the Chart slide to be the first slide", expect="edit",
         order=["Styled text", "Annual Review", "Revenue up 12%"], keeps=["Highlights"]),
    # ------------------------------------------------------------------ XLSX
    dict(id="xlsx-append-row", files=[XLSX], msg="Add a row for West with revenue 410.75, margin 0.21 and date 2024-04-02", expect="edit",
         contains=["West", "410.75"], keeps=["North", "East", "Quarterly Sales"]),
    dict(id="xlsx-set-cell", files=[XLSX], msg="Set the North revenue (B4) to 1500", expect="edit",
         contains=["1500"], absent=["1234.5"], keeps=["South", "East", "Quarterly Sales"]),
    dict(id="xlsx-copy-sheet", files=[XLSX], msg="Copy the Report sheet and name the copy 'Report Q2'", expect="edit",
         sheets=["Report", "Report Q2"], keeps=["North", "Quarterly Sales"]),
    dict(id="xlsx-rename-region", files=[XLSX], msg="Change the region 'East' to 'Central'", expect="edit",
         contains=["Central"], absent=["East"], keeps=["North", "South", "Quarterly Sales"]),
    dict(id="xlsx-title", files=[XLSX], msg="Change the sheet title in A1 to 'FY2024 Sales'", expect="edit",
         contains=["FY2024 Sales"], absent=["Quarterly Sales"], keeps=["North", "Region"]),
    # ------------------------------------------------------------------ routing: chat / clarify
    dict(id="chat-formats", files=[DOCX], msg="Which file formats can you work with?", expect="chat"),
    dict(id="chat-explain", files=[DOCX], msg="Explain what is in the Details section of my document", expect="chat"),
    dict(id="chat-greeting", files=[], msg="hello there", expect="chat"),
    dict(id="clarify-no-file", files=[], msg="Change the title to Annual Report", expect="clarify"),
    dict(id="clarify-two-files", files=[DOCX, PPTX], msg="Change the title to Annual Review", expect="clarify"),
    # ------------------------------------------------------------------ requests that cannot be done correctly
    dict(id="impossible-paragraph", files=[DOCX], msg="Delete paragraph 99 of the document", expect="no_edit"),
    dict(id="impossible-sheet", files=[XLSX], msg="Set cell B4 on the sheet called 'Forecast' to 5", expect="no_edit"),
]
