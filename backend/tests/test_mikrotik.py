"""mikrotik.py — единственный, кто пишет в роутер. Ошибка здесь либо снимает
контроль молча, либо блокирует весь дом, а уезжает в прод автодеплоем: до этих
тестов модуль исполнялся только на живом RouterOS.

Фейковый api повторяет поведение librouteros: path() отдаёт объект, по которому
можно итерироваться, добавлять/удалять/править строки, а select().where()
фильтрует по строкам-запросам вида «?=list=hs-blocked» (именно их порождает
Key("list") == "hs-blocked").
"""

import pytest

from app.services import mikrotik


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters: list[tuple[str, str]] = []

    def where(self, *args):
        for condition in args:
            for token in condition:
                if token.startswith("?="):
                    _, field, value = token.split("=", 2)
                    self.filters.append((field, value))
        return self

    def __iter__(self):
        for row in self.rows:
            if all(str(row.get(f, "")) == v for f, v in self.filters):
                yield row


class FakePath:
    def __init__(self, api, name):
        self.api = api
        self.name = name

    @property
    def rows(self) -> list[dict]:
        return self.api.data.setdefault(self.name, [])

    def __iter__(self):
        return iter(list(self.rows))

    def select(self, *keys):
        return FakeQuery(list(self.rows))

    def add(self, **kw):
        row = dict(kw)
        row.setdefault(".id", f"*{len(self.rows) + 1}{self.name[:2]}")
        self.rows.append(row)
        self.api.calls.append(("add", self.name, row))
        return row[".id"]

    def remove(self, *ids):
        for row_id in ids:
            self.api.data[self.name] = [r for r in self.rows if r[".id"] != row_id]
            self.api.calls.append(("remove", self.name, row_id))

    def update(self, **kw):
        for row in self.rows:
            if row[".id"] == kw[".id"]:
                row.update(kw)
        self.api.calls.append(("update", self.name, kw))


class FakeApi:
    def __init__(self, data=None):
        self.data: dict[str, list[dict]] = data or {}
        self.calls: list[tuple] = []

    def path(self, *parts):
        return FakePath(self, "/".join(parts))


@pytest.fixture
def api():
    return FakeApi({
        "ip/firewall/address-list": [
            {".id": "*1", "list": "hs-blocked", "address": "192.168.88.30"},
            {".id": "*2", "list": "hs-blocked", "address": "192.168.88.31"},
            # чужой список владельца — панель не должна его касаться
            {".id": "*3", "list": "my-vpn", "address": "192.168.88.31"},
        ],
    })


def test_address_list_sync_touches_only_its_own_list(api):
    added, removed = mikrotik.address_list_sync(
        api, "hs-blocked", {"192.168.88.30", "192.168.88.99"})
    assert added == {"192.168.88.99"} and removed == {"192.168.88.31"}

    rows = api.data["ip/firewall/address-list"]
    hs = {r["address"] for r in rows if r["list"] == "hs-blocked"}
    assert hs == {"192.168.88.30", "192.168.88.99"}
    assert any(r["list"] == "my-vpn" for r in rows)  # чужой список цел
    # уже совпадающий адрес не переписывается заново: ровно одна вставка
    adds = [c[2] for c in api.calls if c[0] == "add"]
    assert len(adds) == 1 and adds[0]["address"] == "192.168.88.99"


def test_address_list_sync_empty_desired_clears_list(api):
    added, removed = mikrotik.address_list_sync(api, "hs-blocked", set())
    assert added == set() and removed == {"192.168.88.30", "192.168.88.31"}
    assert [r["list"] for r in api.data["ip/firewall/address-list"]] == ["my-vpn"]


def test_normalize_limit():
    assert mikrotik._normalize_limit("10M/10M") == "10000000/10000000"
    assert mikrotik._normalize_limit("512k/1G") == "512000/1000000000"
    assert mikrotik._normalize_limit("5000/5000") == "5000/5000"


def test_queues_sync_ignores_foreign_queues_and_equal_limits():
    api = FakeApi({"queue/simple": [
        {".id": "*1", "name": "hs-dev-192-168-88-30", "max-limit": "10000000/10000000"},
        {".id": "*2", "name": "hs-dev-192-168-88-40", "max-limit": "5000000/5000000"},
        {".id": "*3", "name": "hs-guest-limit", "max-limit": "20000000/20000000"},
    ]})
    mikrotik.queues_sync(api, {"192.168.88.30": "10M/10M",   # не изменилась
                               "192.168.88.50": "2M/2M"})    # новая
    kinds = [(c[0], c[2] if c[0] != "update" else c[2][".id"]) for c in api.calls]
    # .30 не трогаем (лимит совпал после нормализации), .40 убираем, .50 добавляем
    assert ("remove", "*2") in kinds
    assert not any(k[0] == "update" for k in kinds)
    names = {r["name"] for r in api.data["queue/simple"]}
    assert names == {"hs-dev-192-168-88-30", "hs-guest-limit", "hs-dev-192-168-88-50"}


def test_queues_sync_updates_changed_limit():
    api = FakeApi({"queue/simple": [
        {".id": "*1", "name": "hs-dev-192-168-88-30", "max-limit": "10000000/10000000"},
    ]})
    mikrotik.queues_sync(api, {"192.168.88.30": "3M/3M"})
    assert api.data["queue/simple"][0]["max-limit"] == "3M/3M"


def test_kill_connections_matches_ip_with_port():
    api = FakeApi({"ip/firewall/connection": [
        {".id": "*1", "src-address": "192.168.88.30:44321"},
        {".id": "*2", "src-address": "192.168.88.30:443"},
        {".id": "*3", "src-address": "192.168.88.31:80"},
        {".id": "*4", "src-address": "192.168.88.300:80"},  # не наш IP, похожий префикс
    ]})
    mikrotik.kill_connections(api, "192.168.88.30")
    left = {r[".id"] for r in api.data["ip/firewall/connection"]}
    assert left == {"*3", "*4"}


def test_make_lease_static_replaces_dynamic_and_is_idempotent():
    api = FakeApi({"ip/dhcp-server/lease": [
        {".id": "*1", "mac-address": "AA:BB:CC:DD:EE:FF", "address": "192.168.88.30",
         "dynamic": True, "server": "defconf"},
    ]})
    mikrotik.make_lease_static(api, "aa:bb:cc:dd:ee:ff", "192.168.88.30", "hs: планшет")
    rows = api.data["ip/dhcp-server/lease"]
    assert len(rows) == 1 and not rows[0].get("dynamic")
    assert rows[0]["server"] == "defconf" and rows[0]["comment"] == "hs: планшет"

    before = len(api.calls)
    mikrotik.make_lease_static(api, "AA:BB:CC:DD:EE:FF", "192.168.88.30")
    assert len(api.calls) == before  # уже статический — роутер не трогаем


def test_get_online_ips_skips_invalid_arp():
    api = FakeApi({"ip/arp": [
        {"address": "192.168.88.30"},
        {"address": "192.168.88.31", "invalid": True},
        {"address": ""},
    ]})
    assert mikrotik.get_online_ips(api) == {"192.168.88.30"}
