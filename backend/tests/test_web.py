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
        assert c.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
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


def test_login_rate_limited_after_many_fails():
    from app.routers import auth_routes
    auth_routes._fails.clear()
    with TestClient(app) as c:
        for _ in range(8):
            assert c.post("/login", data={"username": "admin", "password": "x"}).status_code == 401
        # девятая — уже 429
        assert c.post("/login", data={"username": "admin", "password": "x"}).status_code == 429
    auth_routes._fails.clear()


def test_security_headers_present():
    with TestClient(app) as c:
        r = c.get("/login")
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "Content-Security-Policy" in r.headers
        assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_dashboard_quick_actions_pause_and_bonus():
    from sqlalchemy import select

    from app.db import session
    from app.models import Device, Pause, Person, QuotaBonus

    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "testpass"})
        s = session()
        try:
            kid = Person(name="Vasya", role="kid")
            s.add(kid)
            s.commit()
            dev = Device(mac="CC:00:00:00:00:11", ip="192.168.88.55",
                         name="Vasya-tablet", person_id=kid.id)
            s.add(dev)
            s.commit()
            dev_id = dev.id
        finally:
            s.close()

        # дашборд отдаёт консоль с ребёнком
        r = c.get("/")
        assert r.status_code == 200 and "Vasya" in r.text

        # пауза на час → создаётся Pause
        assert c.post(f"/devices/{dev_id}/pause",
                      data={"preset": "60", "redirect_to": "/"},
                      follow_redirects=False).status_code == 302
        # бонус +30 игр → создаётся QuotaBonus
        assert c.post(f"/devices/{dev_id}/bonus",
                      data={"category": "games", "minutes": "30", "redirect_to": "/"},
                      follow_redirects=False).status_code == 302
        # снятие паузы удаляет Pause
        assert c.post(f"/devices/{dev_id}/unpause",
                      data={"redirect_to": "/"}, follow_redirects=False).status_code == 302

        s = session()
        try:
            assert s.scalars(select(Pause).where(Pause.target == str(dev_id))).first() is None
            bonus = s.scalars(select(QuotaBonus).where(QuotaBonus.target == str(dev_id))).first()
            assert bonus is not None and bonus.category == "games" and bonus.minutes == 30
        finally:
            s.close()


def test_quick_action_redirect_rejects_external():
    from app.routers.devices import _safe_redirect
    assert _safe_redirect("/") == "/"
    assert _safe_redirect("/devices") == "/devices"
    assert _safe_redirect("//evil.com") == "/"
    assert _safe_redirect("https://evil.com") == "/"
