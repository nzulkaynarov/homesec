"""Смоук-тесты веб-панели: интеграции недоступны в CI, но роуты обязаны
отдавать страницы (ошибки MikroTik/AdGuard ловятся и логируются)."""

from fastapi.testclient import TestClient

from app.main import app


def test_requires_auth():
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_login_flow_and_pages():
    with TestClient(app) as c:
        assert c.post("/login", data={"username": "admin", "password": "nope"}).status_code == 200
        r = c.post("/login", data={"username": "admin", "password": "testpass"},
                   follow_redirects=False)
        assert r.status_code == 302
        for page in ["/", "/devices", "/people", "/rules", "/events"]:
            assert c.get(page).status_code == 200


def test_create_person_rule_policy():
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "testpass"})
        assert c.post("/people/add", data={"name": "Timur", "role": "kid"},
                      follow_redirects=True).status_code == 200
        r = c.post("/rules/add", data={
            "name": "Ночь", "target_type": "group", "target": "kid",
            "days": ["0", "1", "2", "3", "4"], "start_time": "22:00", "end_time": "07:00",
        }, follow_redirects=True)
        assert r.status_code == 200 and "Ночь" in r.text
        assert c.post("/policies/kid", data={"categories": ["games", "video"], "safe_search": "1"},
                      follow_redirects=True).status_code == 200
