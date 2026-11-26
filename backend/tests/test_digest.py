"""The evening summary: facts gathered by code, one layout, what needs an answer first."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.bot import digest as screen
from app.bot import texts
from app.db import Base, engine, session
from app.models import BonusRequest, Device, EventLog, Person, QuotaUsage, Rule
from app.services import digest

NOW = datetime(2026, 11, 26, 21, 0)
TODAY = "2026-11-26"


@pytest.fixture
def db(monkeypatch):
    Base.metadata.create_all(engine)
    s = session()
    for model in (BonusRequest, Rule, EventLog, QuotaUsage, Device, Person):
        s.query(model).delete()
    s.commit()
    monkeypatch.setattr(
        digest.adguard,
        "get_stats",
        lambda: {"num_dns_queries": 451812, "num_blocked_filtering": 45031},
    )
    yield s
    s.close()


def _event(db, kind, message, at):
    db.add(EventLog(kind=kind, message=message, ts=at))
    db.commit()


@pytest.fixture
def family(db):
    zaur = Person(name="Zaurbek", role="kid")
    dad = Person(name="Папа", role="owner")
    db.add_all([zaur, dad])
    db.flush()
    pc = Device(mac="AA:00:00:00:00:01", ip="192.168.88.239", name="Igra", person_id=zaur.id)
    phone = Device(mac="AA:00:00:00:00:02", ip="192.168.88.244", name="iPhone", person_id=zaur.id)
    mac = Device(mac="AA:00:00:00:00:03", ip="192.168.88.138", name="MacBook", person_id=dad.id)
    ap = Device(mac="AA:00:00:00:00:04", ip="192.168.88.250", name="ArcherC6", kind="infra")
    db.add_all([pc, phone, mac, ap])
    db.flush()
    for device, category, minutes in (
        (pc, "internet", 200),
        (pc, "games", 120),
        (phone, "internet", 60),
        (phone, "video", 40),
        (mac, "internet", 411),
        (ap, "internet", 1224),
    ):
        db.add(QuotaUsage(device_id=device.id, date=TODAY, category=category, minutes=minutes))
    db.add(QuotaUsage(device_id=pc.id, date="2026-11-25", category="games", minutes=999))
    db.add(Rule(name="Отбой", target="kid", start_time="22:00", end_time="07:00"))
    db.commit()
    return SimpleNamespace(zaur=zaur, pc=pc, phone=phone)


def test_children_are_counted_per_child_and_adults_and_equipment_are_left_out(db, family):
    facts = digest.collect(db, NOW, {family.zaur.id: 7})
    assert facts.kids == [
        digest.Kid("Zaurbek", 260, {"internet": 260, "games": 120, "video": 40}, 7)
    ]


def test_cutoffs_are_grouped_by_minute_with_the_reason(db, family):
    yesterday = NOW - timedelta(hours=22, minutes=30)
    _event(
        db,
        "block",
        "Заблокирован доступ: «Igra» (192.168.88.239)",
        yesterday.replace(hour=22, minute=0),
    )
    _event(
        db,
        "block",
        "Заблокирован доступ: «iPhone» (192.168.88.244)",
        yesterday.replace(hour=22, minute=0),
    )
    _event(
        db,
        "quota_block",
        "Квота на интернет исчерпана: Igra (AA:00:00:00:00:01, 192.168.88.239) — "
        "доступ выключен до полуночи.",
        NOW - timedelta(hours=2),
    )
    _event(db, "block", "Заблокирован доступ: 192.168.88.99", NOW - timedelta(hours=1))
    facts = digest.collect(db, NOW)
    assert facts.cutoffs == [
        digest.Cutoff("22:00", ["Igra", "iPhone"], "расписание «Отбой»"),
        digest.Cutoff("19:00", ["Igra"], "кончился лимит"),
        digest.Cutoff("20:00", ["устройство без имени"], ""),
    ]


def test_nothing_older_than_a_day_and_errors_keep_their_first_line(db, family):
    _event(db, "block", "Заблокирован доступ: «Igra» (192.168.88.239)", NOW - timedelta(days=2))
    _event(
        db,
        "error",
        "AdGuard API POST: 400\nFor more information check: …",
        NOW - timedelta(hours=3),
    )
    _event(db, "bot_action", "Объединено: MacBookPro → MacBook-Pro", NOW - timedelta(hours=4))
    facts = digest.collect(db, NOW)
    assert facts.cutoffs == []
    assert facts.errors == ["18:00 AdGuard API POST: 400"]
    assert facts.changes == ["17:00 Объединено: MacBookPro → MacBook-Pro"]


def _facts(**kw):
    return digest.Facts(day=NOW, dns_total=451812, dns_blocked=45031, **kw)


def test_a_calm_day_says_so_first():
    html = screen.render(_facts())
    assert "Сводка за 26 ноября" in html
    assert "<b>🟢 Всё спокойно</b>" in html
    assert "451 812 обращений" in html and "(10 %)" in html


def test_a_waiting_request_comes_first_with_its_own_button():
    facts = _facts(waiting=[digest.Waiting(5, "Zaurbek", NOW.replace(hour=20, minute=15))])
    html = screen.render(facts)
    assert "<b>🔴 Нужно ваше решение: 1</b>" in html
    assert html.index("Zaurbek просит ещё времени, с 20:15") < html.index("обращений к сайтам")
    rows = screen.keyboard(facts).inline_keyboard
    assert rows[0][0].callback_data == "dgr:5"
    assert [b.text for b in rows[-1]] == ["⏳ Квоты", "📈 Неделя"]


def test_technical_failures_are_only_in_the_owners_copy():
    facts = _facts(errors=["07:36 AdGuard API POST: 400"])
    assert "<b>🟢 Всё спокойно</b>" in screen.render(facts)
    assert "Технические сбои" not in screen.render(facts)
    owner = screen.render(facts, owner=True)
    assert "<b>🟡 Есть что посмотреть: 1</b>" in owner
    assert "<details><summary>🔧 Технические сбои: 1</summary>" in owner


def test_the_children_table_shows_only_the_columns_they_used():
    kids = [
        digest.Kid("Zaurbek", 349, {"internet": 349, "games": 60}, 0),
        digest.Kid("Alimbek", 0, {}, 0),
    ]
    html = screen.render(_facts(kids=kids))
    assert "<th>Игры</th>" in html and "<th>Видео</th>" not in html and "Luna" not in html
    assert "<td>Zaurbek</td><td>5 ч 49 мин</td><td>1 ч</td>" in html
    assert "<td>Alimbek</td><td>—</td><td>—</td>" in html


def test_folded_parts_count_their_lines_and_names_are_escaped():
    cutoffs = [digest.Cutoff("22:00", ["Igra", "<iPhone>"], "расписание «Отбой»")]
    html = screen.render(_facts(cutoffs=cutoffs))
    assert "<summary>⛔ Отключали интернет: 2 устройства</summary>" in html
    assert "22:00 — Igra, &lt;iPhone&gt; (расписание «Отбой»)" in html


def test_a_notice_leads_with_its_first_line_in_bold():
    assert texts.alert_html("⛔ Zaurbek: лимит исчерпан\nИгры <30 мин> & всё") == (
        "<b>⛔ Zaurbek: лимит исчерпан</b>\nИгры &lt;30 мин&gt; &amp; всё"
    )
    assert texts.alert_html("одна строка") == "<b>одна строка</b>"


def test_an_answered_notice_keeps_its_bold_line():
    message = SimpleNamespace(html_text="<b>🙏 Zaurbek просит</b>\nПричина: уроки")
    assert texts.with_result(message, "➡ ✅ Zaurbek: +30 мин <ok>") == (
        "<b>🙏 Zaurbek просит</b>\nПричина: уроки\n\n➡ ✅ Zaurbek: +30 мин &lt;ok&gt;"
    )
