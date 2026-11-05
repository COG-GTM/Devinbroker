"""require_admin must fail closed: no ADMIN_KEY configured -> 403 on every admin route."""
import os
import time
from unittest.mock import patch

import pytest

ADMIN_ROUTES = [
    ("get", "/api/settings/api-keys", {}),
    ("put", "/api/settings/api-keys", {"json": {"env_key": "AIS_API_KEY", "value": "junk"}}),
    ("put", "/api/settings/news-feeds", {"json": []}),
    ("post", "/api/settings/news-feeds/reset", {}),
    ("post", "/api/system/update", {}),
]


@pytest.fixture()
def admin_env(monkeypatch):
    """Isolate side effects of admin routes and disable rate limiting."""
    import main

    monkeypatch.setattr(main.limiter, "enabled", False)
    with patch("main.update_api_key", return_value=True) as update_key, \
         patch("main.save_feeds", return_value=True), \
         patch("main.reset_feeds", return_value=True), \
         patch("main.perform_update", return_value={"status": "ok"}) as perform_update, \
         patch("main.threading.Timer") as timer:
        yield {"main": main, "update_key": update_key, "perform_update": perform_update, "timer": timer}


def _call(client, method, url, headers=None, **kw):
    return getattr(client, method)(url, headers=headers or {}, **kw)


@pytest.mark.parametrize("method,url,kw", ADMIN_ROUTES)
def test_admin_routes_rejected_when_admin_key_unset(client, admin_env, monkeypatch, method, url, kw):
    monkeypatch.setattr(admin_env["main"], "_ADMIN_KEY", "")
    r = _call(client, method, url, **kw)
    assert r.status_code == 403
    r = _call(client, method, url, headers={"X-Admin-Key": ""}, **kw)
    assert r.status_code == 403
    admin_env["update_key"].assert_not_called()
    admin_env["perform_update"].assert_not_called()
    admin_env["timer"].assert_not_called()


@pytest.mark.parametrize("method,url,kw", ADMIN_ROUTES)
@pytest.mark.parametrize("header", [None, "", "wrong-key", "correct-horse-battery-staplX"])
def test_admin_routes_reject_missing_or_wrong_key(client, admin_env, monkeypatch, method, url, kw, header):
    monkeypatch.setattr(admin_env["main"], "_ADMIN_KEY", "correct-horse-battery-staple")
    headers = {} if header is None else {"X-Admin-Key": header}
    r = _call(client, method, url, headers=headers, **kw)
    assert r.status_code == 403
    admin_env["update_key"].assert_not_called()
    admin_env["perform_update"].assert_not_called()


@pytest.mark.parametrize("method,url,kw", ADMIN_ROUTES)
def test_admin_routes_accept_correct_key(client, admin_env, monkeypatch, method, url, kw):
    monkeypatch.setattr(admin_env["main"], "_ADMIN_KEY", "correct-horse-battery-staple")
    r = _call(client, method, url, headers={"X-Admin-Key": "correct-horse-battery-staple"}, **kw)
    assert r.status_code == 200


def test_require_admin_uses_constant_time_compare(admin_env, monkeypatch):
    main = admin_env["main"]
    monkeypatch.setattr(main, "_ADMIN_KEY", "k3y")

    class _Req:
        headers = {"X-Admin-Key": "k3y"}

    with patch("main.secrets.compare_digest", wraps=main.secrets.compare_digest) as cd:
        main.require_admin(_Req())
    cd.assert_called_once()


def test_perform_update_removes_temp_dir(tmp_path, monkeypatch):
    from services import updater

    created = []
    real_mkdtemp = updater.tempfile.mkdtemp

    def _mkdtemp(*a, **kw):
        d = real_mkdtemp(dir=tmp_path, prefix="sb_update_")
        created.append(d)
        return d

    monkeypatch.setattr(updater.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(updater, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(updater, "_download_release", lambda d: (str(tmp_path / "x.zip"), "v1", "u"))
    monkeypatch.setattr(updater, "_backup_current", lambda root, d: str(tmp_path / "backups" / "b.zip"))
    monkeypatch.setattr(updater, "_extract_and_copy", lambda z, root, d: 3)

    assert updater.perform_update(str(tmp_path))["status"] == "ok"

    def _boom(*a):
        raise RuntimeError("download failed")

    monkeypatch.setattr(updater, "_download_release", _boom)
    assert updater.perform_update(str(tmp_path))["status"] == "error"

    assert len(created) == 2
    assert not any(os.path.exists(d) for d in created)


def test_prune_backups_keeps_newest(tmp_path):
    from services import updater

    for i in range(5):
        f = tmp_path / f"backup_{i}.zip"
        f.write_bytes(b"x")
        os.utime(f, (time.time() + i, time.time() + i))
    (tmp_path / "other.txt").write_text("keep")

    updater._prune_backups(str(tmp_path), keep=2)
    assert sorted(os.listdir(tmp_path)) == ["backup_3.zip", "backup_4.zip", "other.txt"]
