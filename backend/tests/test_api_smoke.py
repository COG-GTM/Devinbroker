"""Smoke tests for all API endpoints — verifies routes exist and return valid responses."""
import pytest


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "sources" in data
        assert "freshness" in data

    def test_health_has_uptime(self, client):
        r = client.get("/api/health")
        data = r.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))


class TestLiveDataEndpoints:
    def test_live_data_returns_200(self, client):
        r = client.get("/api/live-data")
        assert r.status_code == 200

    def test_live_data_fast_returns_200_or_304(self, client):
        r = client.get("/api/live-data/fast")
        assert r.status_code in (200, 304)
        if r.status_code == 200:
            data = r.json()
            assert "freshness" in data

    def test_live_data_slow_returns_200_or_304(self, client):
        r = client.get("/api/live-data/slow")
        assert r.status_code in (200, 304)
        if r.status_code == 200:
            data = r.json()
            assert "freshness" in data

    def test_fast_has_expected_keys(self, client):
        r = client.get("/api/live-data/fast")
        if r.status_code == 200:
            data = r.json()
            for key in ("commercial_flights", "military_flights", "ships", "satellites"):
                assert key in data, f"Missing key: {key}"

    def test_slow_has_expected_keys(self, client):
        r = client.get("/api/live-data/slow")
        if r.status_code == 200:
            data = r.json()
            for key in ("news", "stocks", "weather", "earthquakes"):
                assert key in data, f"Missing key: {key}"


class TestDebugEndpoint:
    def test_debug_latest_returns_list(self, client):
        r = client.get("/api/debug-latest")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)


class TestSettingsEndpoints:
    def test_get_api_keys(self, client, monkeypatch):
        import main
        monkeypatch.setattr(main, "_ADMIN_KEY", "test-admin-key")
        r = client.get("/api/settings/api-keys", headers={"X-Admin-Key": "test-admin-key"})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_get_news_feeds(self, client):
        r = client.get("/api/settings/news-feeds")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)


class TestRadioEndpoints:
    def test_radio_top_returns_200(self, client):
        r = client.get("/api/radio/top")
        assert r.status_code == 200

    def test_radio_openmhz_systems(self, client):
        r = client.get("/api/radio/openmhz/systems")
        assert r.status_code == 200


class TestQueryValidation:
    def test_region_dossier_rejects_invalid_lat(self, client):
        r = client.get("/api/region-dossier?lat=999&lng=0")
        assert r.status_code == 422

    def test_region_dossier_rejects_invalid_lng(self, client):
        r = client.get("/api/region-dossier?lat=0&lng=999")
        assert r.status_code == 422

    def test_sentinel_rejects_invalid_coords(self, client):
        r = client.get("/api/sentinel2/search?lat=-100&lng=0")
        assert r.status_code == 422

    def test_radio_nearest_rejects_invalid_lat(self, client):
        r = client.get("/api/radio/nearest?lat=91&lng=0")
        assert r.status_code == 422


class TestETagBehavior:
    def test_fast_returns_etag_header(self, client):
        r = client.get("/api/live-data/fast")
        if r.status_code == 200:
            assert "etag" in r.headers

    def test_slow_returns_etag_header(self, client):
        r = client.get("/api/live-data/slow")
        if r.status_code == 200:
            assert "etag" in r.headers


class TestAdminGate:
    FEED_PAYLOADS = (
        [{"name": "t", "url": "file:///etc/hostname", "weight": 3}],
        [{"name": "t", "url": "http://127.0.0.1:8000/api/health", "weight": 3}],
    )

    def test_admin_endpoints_fail_closed_without_admin_key(self, client, monkeypatch):
        import main
        monkeypatch.setattr(main, "_ADMIN_KEY", "")
        assert client.get("/api/settings/api-keys").status_code == 403
        for payload in self.FEED_PAYLOADS:
            assert client.put("/api/settings/news-feeds", json=payload).status_code == 403

    def test_wrong_admin_key_rejected(self, client, monkeypatch):
        import main
        monkeypatch.setattr(main, "_ADMIN_KEY", "test-admin-key")
        r = client.get("/api/settings/api-keys", headers={"X-Admin-Key": "wrong"})
        assert r.status_code == 403

    def test_unsafe_feed_urls_rejected_with_admin_key(self, client, monkeypatch, tmp_path):
        import main
        from services import news_feed_config
        monkeypatch.setattr(main, "_ADMIN_KEY", "test-admin-key")
        monkeypatch.setattr(news_feed_config, "CONFIG_PATH", tmp_path / "news_feeds.json")
        for payload in self.FEED_PAYLOADS:
            r = client.put("/api/settings/news-feeds", json=payload,
                           headers={"X-Admin-Key": "test-admin-key"})
            assert r.status_code == 400
            assert "Validation failed" in r.json()["message"]
        assert not (tmp_path / "news_feeds.json").exists()
