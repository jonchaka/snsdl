from tests.api_helpers import CSRF, _history


# -- round 10: history ----------------------------------------------------------------

def test_history_list_filter_and_limit(client):
    _history(client, ("https://youtu.be/a", "Cats"), ("https://youtu.be/b", "Dogs"),
             ("https://vimeo.com/1", "More cats"))
    body = client.get("/api/history").json()
    assert body["total"] == 3 and len(body["entries"]) == 3
    assert {"id", "url", "title", "format", "quality", "finished_at"} <= set(body["entries"][0])
    cats = client.get("/api/history", params={"q": "cats"}).json()
    assert sorted(e["title"] for e in cats["entries"]) == ["Cats", "More cats"] and cats["total"] == 3
    assert len(client.get("/api/history", params={"limit": 1}).json()["entries"]) == 1
    for bad in ({"limit": 0}, {"limit": 1001}, {"q": "x" * 201}):
        assert client.get("/api/history", params=bad).status_code == 422


def test_history_delete_one_and_clear(client):
    entries = _history(client, ("https://youtu.be/a", "A"), ("https://youtu.be/b", "B"))
    eid = entries[0].id
    assert client.delete(f"/api/history/{eid}").status_code == 403
    assert client.delete(f"/api/history/{eid}", headers=CSRF).status_code == 204
    assert client.delete(f"/api/history/{eid}", headers=CSRF).status_code == 404
    assert client.get("/api/history").json()["total"] == 1
    assert client.delete("/api/history").status_code == 403
    assert client.delete("/api/history", headers=CSRF).status_code == 204
    assert client.get("/api/history").json() == {"entries": [], "total": 0}
