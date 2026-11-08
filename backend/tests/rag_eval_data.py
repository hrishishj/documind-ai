"""Ground-truth retrieval questions over the bundled real documents. Each expected string was read out of the source
file itself (see tests/test_rag_eval.py); a question counts as retrieved when some returned passage contains it."""

import re


def contains(text: str, needle: str) -> bool:
    """Does the passage contain the expected fact? Numbers are compared ignoring thousands separators and spacing
    (the same figure is '217778' in the spreadsheet and '217,778' in the filing text)."""
    norm = lambda s: re.sub(r"[,\s\u00a0]+", "", s).lower()  # noqa: E731
    return norm(needle) in norm(text)


# (question, expected substring that must appear in a retrieved passage, which file)
QUESTIONS = [
    ("What was total revenue in fiscal 2026?", "331839", "xls"),
    ("How much did the company spend on research and development in 2026?", "35562", "xls"),
    ("What was operating income in 2026?", "155237", "xls"),
    ("What was net income in fiscal 2026?", "133749", "xls"),
    ("What were service and other revenues in 2025?", "217778", "xls"),
    ("What is the SEC commission file number?", "001-37845", "docx"),
    ("Where is the company headquartered?", "REDMOND", "docx"),
    ("What kinds of products does the company sell?", "operating systems, server applications", "docx"),
    ("Which fiscal year does the annual report cover?", "June 30, 2026", "docx"),
    ("What was India's real GDP growth?", "7.1%", "pdf"),
    ("What is the total shareholding in Cemindia Projects?", "67.46%", "pdf"),
    ("What is the net worth in FY26?", "89,178", "pdf"),
    ("When is this investor presentation dated?", "May 2026", "pdf"),
]

# HELD OUT: written AFTER the retrieval settings were frozen (lexical weight 0.15, cutoff 0.55) from facts not used
# above, and evaluated once without adjusting anything. This is the honest estimate; QUESTIONS above were used to tune.
HELD_OUT = [
    ("What was gross margin in 2026?", "225465", "xls"),
    ("What is the company's phone number?", "(425) 882-8080", "docx"),
    ("What is the investor relations website?", "microsoft.com/investor", "docx"),
    ("What was the net worth in FY25?", "56,470", "pdf"),
    ("What was operating income in 2025?", "128528", "xls"),
    ("What was Cash PAT in FY26?", "15,474", "pdf"),
]

# Messages that are NOT about the documents: retrieval should score them low.
OFF_TOPIC = [
    "hello there",
    "what is the capital of France?",
    "write me a short poem about autumn",
    "can you explain how photosynthesis works?",
    "thanks, that was helpful",
]
