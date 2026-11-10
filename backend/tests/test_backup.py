"""The nightly backup must keep only verified snapshots and report its outcome."""

import os
import pathlib
import sqlite3
import stat
import subprocess

import httpx

from app import scheduler

SCRIPT = pathlib.Path(__file__).parents[2] / "deploy" / "pull-db-backup.sh"
HC_URL = "https://hc.invalid/check"


def _executable(path: pathlib.Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(
    tmp_path: pathlib.Path, ssh_body: str, keep: int = 30, router_host: str = ""
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _executable(bin_dir / "ssh", ssh_body)
    _executable(bin_dir / "curl", f'echo "${{@: -1}}" >> "{tmp_path}/pings"')
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOMESEC_BACKUP_DIR": str(tmp_path / "backups"),
        "HOMESEC_BACKUP_KEEP": str(keep),
        "HOMESEC_BACKUP_HC_URL": HC_URL,
        "HOMESEC_ROUTER_HOST": router_host,
    }
    return subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)


def _live_backend(tmp_path: pathlib.Path) -> pathlib.Path:
    """A backend directory whose database is open in WAL mode with unmerged writes."""
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".env").write_text("HS_DATABASE_PATH=./data.db\n")
    connection = sqlite3.connect(backend / "data.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE person (name TEXT)")
    connection.execute("INSERT INTO person VALUES ('uncheckpointed')")
    connection.commit()
    _CONNECTIONS.append(connection)
    return backend


_CONNECTIONS: list[sqlite3.Connection] = []


def _snapshots(tmp_path: pathlib.Path) -> list[pathlib.Path]:
    return sorted((tmp_path / "backups").glob("homesec-*.db"))


def _pings(tmp_path: pathlib.Path) -> list[str]:
    return (tmp_path / "pings").read_text().split()


def _real_snapshot_ssh(backend: pathlib.Path) -> str:
    return f'exec python3 - "{backend}"'


def test_snapshot_of_a_live_wal_database_contains_recent_writes(tmp_path):
    result = _run(tmp_path, _real_snapshot_ssh(_live_backend(tmp_path)))

    assert result.returncode == 0, result.stderr
    [snapshot] = _snapshots(tmp_path)
    rows = sqlite3.connect(snapshot).execute("SELECT name FROM person").fetchall()
    assert rows == [("uncheckpointed",)]
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert _pings(tmp_path) == [HC_URL]


def test_rotation_keeps_only_the_newest_snapshots(tmp_path):
    backend = _live_backend(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    for day in range(1, 4):
        (backups / f"homesec-2026010{day}-030000.db").write_text("old")

    assert _run(tmp_path, _real_snapshot_ssh(backend), keep=2).returncode == 0

    names = [path.name for path in _snapshots(tmp_path)]
    assert len(names) == 2
    assert names[0] == "homesec-20260103-030000.db"


def test_corrupt_snapshot_is_rejected_and_reported(tmp_path):
    result = _run(tmp_path, "cat >/dev/null; echo not-a-database")

    assert result.returncode != 0
    assert _snapshots(tmp_path) == []
    assert list((tmp_path / "backups").glob(".partial*")) == []
    assert _pings(tmp_path) == [f"{HC_URL}/fail"]


def test_unreachable_pi_is_reported(tmp_path):
    result = _run(tmp_path, "cat >/dev/null; exit 255")

    assert result.returncode != 0
    assert _snapshots(tmp_path) == []
    assert _pings(tmp_path) == [f"{HC_URL}/fail"]


def test_heartbeat_failure_does_not_log_the_secret_url(monkeypatch):
    secret = "https://hc-ping.com/secret-uuid"
    monkeypatch.setattr(scheduler.settings, "healthcheck_url", secret)
    logged: list[str] = []
    monkeypatch.setattr(scheduler.log, "warning", lambda text, *args: logged.append(text % args))

    def refuse(url, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(scheduler.httpx, "get", refuse)
    scheduler._heartbeat()

    assert logged == ["heartbeat ping failed: ConnectError"]


def test_heartbeat_is_scheduled_only_when_configured(monkeypatch):
    added: list[str] = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: added.append(k["id"]))
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "_tick", lambda: None)

    monkeypatch.setattr(scheduler.settings, "healthcheck_url", "")
    scheduler.start()
    assert "heartbeat" not in added

    monkeypatch.setattr(scheduler.settings, "healthcheck_url", "https://hc-ping.com/x")
    scheduler.start()
    assert "heartbeat" in added


TOKEN = "1234567890:AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQ"


def _router_ssh(backend: pathlib.Path, export: str) -> str:
    """The database comes from the Pi, the configuration from the router."""
    return (
        'case "$*" in\n'
        f'  *router*) cat <<"RSC"\n{export}\nRSC\n  ;;\n'
        f'  *) exec python3 - "{backend}" ;;\n'
        "esac"
    )


def test_router_export_is_saved_without_the_bot_token(tmp_path):
    export = f'/ip address\nadd address=192.168.88.1/24\n/system script\nadd source="t={TOKEN}"'
    ssh = _router_ssh(_live_backend(tmp_path), export)
    result = _run(tmp_path, ssh, router_host="hs-backup@router")

    assert result.returncode == 0, result.stderr
    [copy] = sorted((tmp_path / "backups").glob("router-*.rsc"))
    text = copy.read_text()
    assert TOKEN not in text and "<telegram-bot-token>" in text and "/ip address" in text
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert _pings(tmp_path) == [HC_URL]


def test_empty_router_export_is_reported(tmp_path):
    ssh = _router_ssh(_live_backend(tmp_path), "# nothing")
    result = _run(tmp_path, ssh, router_host="hs-backup@router")

    assert result.returncode != 0
    assert list((tmp_path / "backups").glob("router-*.rsc")) == []
    assert list((tmp_path / "backups").glob(".partial*")) == []
    assert len(_snapshots(tmp_path)) == 1  # the database copy is kept
    assert _pings(tmp_path) == [f"{HC_URL}/fail"]
