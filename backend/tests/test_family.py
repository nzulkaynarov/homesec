"""Тесты семейных функций: роли Telegram, радар присутствия «Кто дома»,
общие медиа-устройства (ТВ/колонки), семейная память и викторины для детей.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.db import Base, engine, session
from app.models import Device, FamilyNote, Person, QuotaBonus, kv_get
from app.services import family as family_svc


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (FamilyNote, QuotaBonus, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def test_get_user_role_owner_from_settings(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_ids", "111,222")
    role, person = family_svc.get_user_role(db, 111)
    assert role == "owner"
    assert person is None


def test_get_user_role_from_person_record(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_ids", "")

    p_admin = Person(name="Мама", role="adult", telegram_id=333, bot_role="admin")
    p_kid = Person(name="Миша", role="kid", telegram_id=444, bot_role="kid")
    p_guest = Person(name="Дедушка", role="guest", telegram_id=555, bot_role="guest")
    db.add_all([p_admin, p_kid, p_guest])
    db.commit()

    role1, person1 = family_svc.get_user_role(db, 333)
    assert role1 == "admin" and person1.name == "Мама"

    role2, person2 = family_svc.get_user_role(db, 444)
    assert role2 == "kid" and person2.name == "Миша"

    role3, person3 = family_svc.get_user_role(db, 555)
    assert role3 == "guest" and person3.name == "Дедушка"

    role4, person4 = family_svc.get_user_role(db, 999)
    assert role4 == "unauthorized" and person4 is None


def test_presence_status_and_formatting(db):
    p1 = Person(name="Папа", role="adult")
    p2 = Person(name="Аня", role="kid")
    db.add_all([p1, p2])
    db.commit()

    d_papa = Device(
        mac="AA:11:00:00:00:01",
        ip="192.168.88.50",
        name="iPhone-Папа",
        kind="personal",
        person_id=p1.id,
    )
    d_anya = Device(
        mac="AA:11:00:00:00:02",
        ip="192.168.88.51",
        name="iPad-Аня",
        kind="personal",
        person_id=p2.id,
    )
    d_tv = Device(
        mac="AA:11:00:00:00:03",
        ip="192.168.88.60",
        name="Гостиная ТВ",
        kind="shared",
        blocked_manual=False,
    )
    d_iot = Device(
        mac="AA:11:00:00:00:04",
        ip="192.168.88.70",
        name="Пылесос",
        kind="iot",
    )
    db.add_all([d_papa, d_anya, d_tv, d_iot])
    db.commit()

    # Мокаем получение ARP с роутера: онлайн папа и телевизор
    online = {"192.168.88.50", "192.168.88.60"}
    with patch("app.services.mikrotik.api_session") as mock_api:
        mock_api.return_value.__enter__.return_value = MagicMock()
        with patch("app.services.mikrotik.get_online_ips", return_value=online):
            status = family_svc.get_presence_status(db)

    assert len(status["people"]) == 2
    papa_stat = next(p for p in status["people"] if p["person"].name == "Папа")
    anya_stat = next(p for p in status["people"] if p["person"].name == "Аня")
    assert papa_stat["is_home"] is True
    assert anya_stat["is_home"] is False

    assert len(status["shared"]) == 1
    assert status["shared"][0]["is_online"] is True
    assert status["iot_total"] == 1
    assert status["iot_online"] == 0

    text = family_svc.format_presence(status)
    assert "🟢 👤 *Папа* — дома" in text
    assert "⚪ 🎒 *Аня* — не в сети" in text
    assert "🟢 *Гостиная ТВ* — онлайн" in text
    assert "0 из 1 устройств в сети" in text


def test_set_movie_mode(db):
    d_tv = Device(
        mac="AA:22:00:00:00:01",
        ip="192.168.88.61",
        name="LG OLED TV",
        kind="shared",
        blocked_manual=True,
    )
    db.add(d_tv)
    db.commit()

    msg = family_svc.set_movie_mode(db)
    assert "Режим «Смотрят взрослые» включён" in msg
    assert "LG OLED TV" in msg

    db.refresh(d_tv)
    assert d_tv.blocked_manual is False
    assert kv_get(db, "reconcile_asap") == "1"


def test_set_movie_mode_no_shared_devices(db):
    msg = family_svc.set_movie_mode(db)
    assert "нет устройств" in msg


def test_family_notes_crud(db):
    assert "пока пусто" in family_svc.format_family_notes([])

    note1 = family_svc.save_family_note(db, author="Папа", content="Размер ноги Миши: 36")
    note2 = family_svc.save_family_note(db, author="Мама", content="Код домофона: 4589")

    notes = family_svc.get_family_notes(db)
    assert len(notes) == 2
    assert notes[0].id == note2.id
    assert notes[1].id == note1.id

    formatted = family_svc.format_family_notes(notes)
    assert "Размер ноги Миши: 36" in formatted
    assert "Код домофона: 4589" in formatted
    assert "(Папа)" in formatted and "(Мама)" in formatted


def test_quiz_lifecycle(db):
    p = Person(name="Миша", role="kid")
    db.add(p)
    db.commit()

    q = family_svc.get_random_quiz_question()
    assert "question" in q and "options" in q and "correct" in q
    assert len(q["options"]) >= 2
    assert 0 <= q["correct"] < len(q["options"])

    assert family_svc.can_take_quiz_today(db, p.id) is True

    msg = family_svc.award_quiz_bonus(db, p, minutes=15)
    assert "+15 мин" in msg
    assert "Миша" in msg

    assert family_svc.can_take_quiz_today(db, p.id) is False

    bonuses = list(db.scalars(family_svc.select(QuotaBonus).where(QuotaBonus.target == str(p.id))))
    assert len(bonuses) == 1
    assert bonuses[0].minutes == 15
    assert bonuses[0].comment == "Победа в викторине"
