"""Tests for the authenticated, validated AIS-catcher ingest endpoint."""
import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from services import ais_stream
from services import ais_feed

TOKEN = "test-ais-feed-token"

GOOD_MSG = {"mmsi": 123456789, "type": 1, "lat": 25.0, "lon": 55.0, "speed": 10.0,
            "course": 90.0, "heading": 90, "shipname": "TEST VESSEL"}


@pytest.fixture(autouse=True)
def _clean_vessels(monkeypatch):
    monkeypatch.setenv("AIS_FEED_TOKEN", TOKEN)
    with ais_stream._vessels_lock:
        saved = dict(ais_stream._vessels)
        ais_stream._vessels.clear()
        ais_stream._local_feed_mmsis.clear()
    yield
    with ais_stream._vessels_lock:
        ais_stream._vessels.clear()
        ais_stream._vessels.update(saved)
        ais_stream._local_feed_mmsis.clear()


def _post(path="/api/ais/feed", **kw):
    from main import app

    async def _run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.post(path, **kw)

    return asyncio.new_event_loop().run_until_complete(_run())


class TestFeedAuth:
    def test_missing_token_rejected(self):
        r = _post(json={"msgs": [GOOD_MSG]})
        assert r.status_code == 401
        assert ais_stream._vessels == {}

    def test_wrong_token_rejected(self):
        r = _post(json={"msgs": [GOOD_MSG]}, headers={"X-AIS-Feed-Token": "nope"})
        assert r.status_code == 403
        assert ais_stream._vessels == {}

    def test_disabled_when_token_unset(self, monkeypatch):
        monkeypatch.delenv("AIS_FEED_TOKEN")
        r = _post(json={"msgs": [GOOD_MSG]}, headers={"X-AIS-Feed-Token": ""})
        assert r.status_code == 403
        assert ais_stream._vessels == {}

    @pytest.mark.parametrize("kw", [
        {"headers": {"X-AIS-Feed-Token": TOKEN}},
        {"headers": {"Authorization": f"Bearer {TOKEN}"}},
        {"auth": ("aiscatcher", TOKEN)},
        {"params": {"token": TOKEN}},
    ])
    def test_valid_token_carriers_accepted(self, kw):
        r = _post(json={"msgs": [GOOD_MSG]}, **kw)
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "ingested": 1}
        assert ais_stream._vessels[123456789]["name"] == "TEST VESSEL"


class TestFeedValidation:
    auth = {"X-AIS-Feed-Token": TOKEN}

    @pytest.mark.parametrize("bad", [
        {**GOOD_MSG, "lat": "x"},
        {**GOOD_MSG, "lon": "55"},
        {**GOOD_MSG, "lat": 95.0},
        {**GOOD_MSG, "lon": -200.0},
        {**GOOD_MSG, "lat": True},
        {**GOOD_MSG, "lat": 10**400},
        {**GOOD_MSG, "speed": "fast"},
        {**GOOD_MSG, "shipname": "A" * 500},
        {**GOOD_MSG, "shipname": 42},
        {**GOOD_MSG, "mmsi": "123456789"},
        {**GOOD_MSG, "mmsi": 10**12},
        "not-an-object",
    ])
    def test_malformed_message_rejected(self, bad):
        r = _post(json={"msgs": [GOOD_MSG, bad]}, headers=self.auth)
        assert r.status_code == 400
        assert ais_stream._vessels == {}

    def test_nan_rejected(self):
        body = '{"msgs": [{"mmsi": 123456789, "type": 1, "lat": NaN, "lon": 55.0}]}'
        r = _post(content=body, headers={**self.auth, "Content-Type": "application/json"})
        assert r.status_code == 400

    def test_too_many_messages_rejected(self):
        msgs = [{"mmsi": 100000000 + i, "type": 1, "lat": 1.0, "lon": 1.0}
                for i in range(ais_feed.MAX_MSGS_PER_REQUEST + 1)]
        r = _post(json={"msgs": msgs}, headers=self.auth)
        assert r.status_code == 413
        assert ais_stream._vessels == {}

    def test_oversized_body_rejected(self):
        body = b'{"msgs": [], "pad": "' + b"a" * (ais_feed.MAX_BODY_BYTES + 1) + b'"}'
        r = _post(content=body, headers={**self.auth, "Content-Type": "application/json"})
        assert r.status_code == 413

    def test_invalid_json_rejected(self):
        r = _post(content=b"{nope", headers={**self.auth, "Content-Type": "application/json"})
        assert r.status_code == 400

    def test_unavailable_position_sentinel_accepted(self):
        msg = {**GOOD_MSG, "lat": 91.0, "lon": 181.0}
        r = _post(json={"msgs": [msg]}, headers=self.auth)
        assert r.status_code == 200
        assert r.json()["ingested"] == 0


class TestVesselStoreHardening:
    def test_local_feed_vessel_cap(self, monkeypatch):
        monkeypatch.setattr(ais_stream, "MAX_LOCAL_FEED_VESSELS", 3)
        msgs = [{"mmsi": 200000000 + i, "type": 1, "lat": 1.0, "lon": 1.0} for i in range(10)]
        assert ais_stream.ingest_ais_catcher(msgs) == 3
        assert len(ais_stream._vessels) == 3
        # Existing vessels can still be updated once the cap is reached.
        assert ais_stream.ingest_ais_catcher(msgs[:1]) == 1

    def test_cap_frees_up_after_prune(self, monkeypatch):
        monkeypatch.setattr(ais_stream, "MAX_LOCAL_FEED_VESSELS", 1)
        ais_stream.ingest_ais_catcher([{"mmsi": 300000001, "type": 1, "lat": 1.0, "lon": 1.0}])
        ais_stream._vessels[300000001]["_updated"] = time.time() - 3600
        ais_stream.get_ais_vessels()
        assert ais_stream.ingest_ais_catcher([{"mmsi": 300000002, "type": 1, "lat": 1.0, "lon": 1.0}]) == 1

    def test_malformed_record_does_not_blank_snapshot(self):
        now = time.time()
        ais_stream._vessels[111111111] = {"mmsi": 111111111, "lat": "x", "lng": 1.0, "_updated": now}
        ais_stream._vessels[222222222] = {"mmsi": 222222222, "lat": 2.0, "lng": 2.0, "_updated": now}
        result = ais_stream.get_ais_vessels()
        assert [v["mmsi"] for v in result] == [222222222]

    def test_cap_survives_cache_reload(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ais_stream, "MAX_LOCAL_FEED_VESSELS", 3)
        monkeypatch.setattr(ais_stream, "CACHE_FILE", str(tmp_path / "ais_cache.json"))
        ais_stream.ingest_ais_catcher(
            [{"mmsi": 400000000 + i, "type": 1, "lat": 1.0, "lon": 1.0} for i in range(3)])
        ais_stream._save_cache()
        ais_stream._vessels.clear()
        ais_stream._local_feed_mmsis.clear()
        ais_stream._load_cache()
        assert len(ais_stream._local_feed_mmsis) == 3
        assert ais_stream.ingest_ais_catcher(
            [{"mmsi": 400000100 + i, "type": 1, "lat": 1.0, "lon": 1.0} for i in range(3)]) == 0
