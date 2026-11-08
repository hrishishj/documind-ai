from sqlalchemy import select

from app.database.models import Citation
from app.research.search import SearchError
from tests.fakes import FakeWeb
from tests.test_api import FakeLLM, make_client, new_session, sse

Q = "Search the web: what is the energy density of solid-state batteries?"
ANSWER = {"answer": "Prototypes reach about 400 Wh/kg [1] and commercial cells are expected by 2028 [2].", "answerable": True}


def test_web_answer_streams_a_search_step_and_persists_url_citations(test_database, tmp_path):
    with make_client(test_database, tmp_path, llm=FakeLLM(answers=[ANSWER]), web=FakeWeb()) as c:
        sid = new_session(c)
        ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": Q}))
        labels = [d["label"] for e, d in ev if e == "node"]
        assert labels[:2] == ["Understanding your request", "Searching the web"]
        final = ev[-1][1]
        assert [(x["ref"], x["source_type"]) for x in final["citations"]] == [(1, "web"), (2, "web")]
        assert final["citations"][0]["url"] == "https://www.mdpi.com/1996-1073/19/11/2659"
        assert final["citations"][0]["title"] == "Solid-State Battery Technology" and final["citations"][0]["file_id"] is None

        # survives a reload: stored with the message, URL intact
        stored = c.get(f"/sessions/{sid}").json()["messages"][-1]["citations"]
        assert [(x["ref"], x["source_type"], x["url"]) for x in stored] == [
            (1, "web", "https://www.mdpi.com/1996-1073/19/11/2659"),
            (2, "web", "https://www.idtechex.com/en/research-report/solid-state-batteries/1130")]
        with test_database.session() as db:
            row = db.scalar(select(Citation).where(Citation.url == stored[0]["url"]))
            assert row.source_type == "web" and row.file_id is None and "400 Wh/kg" in row.snippet


def test_a_turn_without_web_research_has_no_search_step(test_database, tmp_path):
    web = FakeWeb()
    with make_client(test_database, tmp_path, llm=FakeLLM(texts=["Hello!"]), web=web) as c:
        sid = new_session(c)
        ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": "hello there"}))
        assert "Searching the web" not in [d.get("label") for e, d in ev if e == "node"] and web.queries == []


def test_a_failed_search_still_returns_an_honest_answer_over_the_api(test_database, tmp_path):
    with make_client(test_database, tmp_path, llm=FakeLLM(texts=["Probably about 400 Wh/kg."]),
                     web=FakeWeb(error=SearchError("all engines failed"))) as c:
        sid = new_session(c)
        final = sse(c.post(f"/sessions/{sid}/chat", json={"message": Q}))[-1]
        assert final[0] == "final" and final[1]["citations"] == []
        assert "not based on a web search" in final[1]["content"]


def test_web_search_input_is_never_a_way_to_reach_internal_addresses(test_database, tmp_path):
    """The server only ever sends the user's text to the search API; it never fetches result URLs itself."""
    import inspect

    import app.research.search as s
    src = inspect.getsource(s)
    assert "requests.get" not in src and "httpx" not in src and "urlopen" not in src and "trafilatura" not in src
