from app.main import health


def test_health_ok():
    assert health()["status"] == "ok"


def test_verify_backend_fails_fast_on_mismatch(monkeypatch):
    from core import llm

    monkeypatch.setattr(llm, "genai_mode", lambda: "vertex")
    monkeypatch.setattr(llm, "plain_backend", lambda: {"backend": "apikey", "project": None, "location": None})
    import pytest
    with pytest.raises(RuntimeError, match="GENAI_MODE=vertex but the google-genai client is apikey"):
        llm.verify_backend(check_adk=False)


def test_verify_backend_checks_adk(monkeypatch):
    from core import llm

    vertex = {"backend": "vertex", "project": "p", "location": "global"}
    monkeypatch.setattr(llm, "genai_mode", lambda: "vertex")
    monkeypatch.setattr(llm, "plain_backend", lambda: vertex)
    monkeypatch.setattr(llm, "adk_backend", lambda: {"backend": "apikey", "project": None, "location": None})
    import pytest
    with pytest.raises(RuntimeError, match="GOOGLE_GENAI_USE_VERTEXAI=TRUE"):
        llm.verify_backend()
