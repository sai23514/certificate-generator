def test_root_serves_the_web_ui(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Bulk Certificate Generator" in response.text
