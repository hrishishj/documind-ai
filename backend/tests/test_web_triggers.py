import pytest

from app.research.triggers import wants_web

# phrases taken from the real 10-K / investor-deck sample documents: these are about the DOCUMENT, never the web
DOCUMENT_QUESTIONS = [
    "What was search and news advertising revenue in fiscal 2026?",
    "What are the current liabilities?",
    "Tell me about the online services segment",
    "What recent accounting pronouncements does the filing mention?",
    "How did the company describe its latest strategy for cloud?",
    "What was research and development spending in 2026?",
    "What is the search engine market share mentioned in the report?",
    "Summarize today's agenda slide",
    "How much cash did the company have this year?",
]


@pytest.mark.parametrize("q", DOCUMENT_QUESTIONS)
def test_questions_about_the_documents_never_trigger_a_web_search(q):
    assert wants_web(q, docs_ready=True) is False


@pytest.mark.parametrize("q", [
    "Search the web for solid-state battery news",
    "search online for the current price of copper",
    "Look this up on the internet",
    "Find recent research on the web about lithium supply",
    "use google to check the latest EV sales",
    "Add a slide with figures from the internet",
    "Do a web search on quantum computing",
    "browse the web and tell me about Tavily",
    "Research the latest trends on the web and add a slide",
])
def test_explicit_requests_trigger_even_when_documents_exist(q):
    assert wants_web(q, docs_ready=True) is True


@pytest.mark.parametrize("q", ["What is the latest news about solid-state batteries?", "any recent developments in fusion energy?",
                               "what happened today in the markets", "up to date stats on EV adoption"])
def test_freshness_words_trigger_only_when_there_are_no_documents_to_answer_from(q):
    assert wants_web(q, docs_ready=False) is True
    assert wants_web(q, docs_ready=True) is False


# (a mixed request such as "search the web but use my documents for the numbers" is ambiguous and deliberately untested)
@pytest.mark.parametrize("q", [
    "Don't search the internet, just answer from the files",
    "without searching online, what does the deck say?",
    "use only the uploaded report",
    "answer from my documents please",
])
def test_the_user_can_forbid_the_web(q):
    assert wants_web(q, docs_ready=True) is False and wants_web(q, docs_ready=False) is False


def test_plain_chat_never_triggers():
    for q in ("hello", "thanks!", "explain photosynthesis", "write a poem"):
        assert wants_web(q, docs_ready=False) is False
