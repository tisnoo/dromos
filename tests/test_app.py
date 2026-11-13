from fastapi.testclient import TestClient

from dromos.app import app

# Not used as a context manager, so the lifespan (and its scheduler) doesn't start.
client = TestClient(app)


def test_health_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("DROMOS_DB", str(tmp_path / "dromos.db"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_db_failure(monkeypatch, tmp_path):
    (tmp_path / "dromos.db").mkdir()  # a directory where the DB file should be
    monkeypatch.setenv("DROMOS_DB", str(tmp_path / "dromos.db"))
    assert client.get("/health").status_code == 503


def test_api_docs_are_not_exposed():
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
