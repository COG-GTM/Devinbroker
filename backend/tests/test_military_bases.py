"""Tests for military bases data and fetcher."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.fetchers._store import latest_data, _data_lock


BASES_PATH = Path(__file__).parent.parent / "data" / "military_bases.json"


class TestMilitaryBasesData:
    """Validate the static military_bases.json file."""

    def test_json_file_exists(self):
        assert BASES_PATH.exists()

    def test_all_entries_have_required_fields(self):
        raw = json.loads(BASES_PATH.read_text(encoding="utf-8"))
        assert len(raw) > 0
        for entry in raw:
            assert "name" in entry and entry["name"]
            assert "country" in entry and entry["country"]
            assert "operator" in entry and entry["operator"]
            assert "branch" in entry and entry["branch"]
            assert "lat" in entry and isinstance(entry["lat"], (int, float))
            assert "lng" in entry and isinstance(entry["lng"], (int, float))

    def test_coordinates_in_valid_range(self):
        raw = json.loads(BASES_PATH.read_text(encoding="utf-8"))
        for entry in raw:
            assert -90 <= entry["lat"] <= 90, f"{entry['name']} has invalid lat"
            assert -180 <= entry["lng"] <= 180, f"{entry['name']} has invalid lng"

    def test_branch_values_are_known(self):
        known_branches = {"air_force", "navy", "marines", "army"}
        raw = json.loads(BASES_PATH.read_text(encoding="utf-8"))
        for entry in raw:
            assert entry["branch"] in known_branches, f"{entry['name']} has unknown branch: {entry['branch']}"


class TestFetchMilitaryBases:
    """Test the fetcher populates latest_data correctly."""

    def test_fetch_populates_store(self):
        from services.fetchers.infrastructure import fetch_military_bases
        fetch_military_bases()
        with _data_lock:
            bases = latest_data["military_bases"]
        assert len(bases) > 0
        assert all("name" in b and "lat" in b and "lng" in b for b in bases)

    def test_includes_key_bases(self):
        from services.fetchers.infrastructure import fetch_military_bases
        fetch_military_bases()
        with _data_lock:
            names = {b["name"] for b in latest_data["military_bases"]}
        assert "Kadena Air Base" in names
        assert "Fleet Activities Yokosuka" in names
        assert "Andersen Air Force Base" in names
