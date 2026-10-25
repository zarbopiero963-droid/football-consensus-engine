"""Offline checks for explicit evidence file formats (no provider calls)."""
import ast
from pathlib import Path
import csv
import json


def test_inventory_produces_both_file_formats():
    src = Path("scripts/api_football_39_route_inventory.py").read_text()
    ast.parse(src)
    assert 'args.output.with_suffix(".csv")' in src
    assert '"records"' in src


def test_capability_exports_unabridged_catalog_names_and_fields():
    src = Path("scripts/api_football_capability_probe.py").read_text()
    ast.parse(src)
    for expected in ('"capabilities.json"', '"catalogs.csv"', '"field_registry.csv"'):
        assert expected in src


def test_uploads_explicit_non_secret_files():
    route = Path(".github/workflows/api-football-39-route-inventory.yml").read_text()
    cap = Path(".github/workflows/api-football-capability-inventory.yml").read_text()
    assert "actions/upload-artifact@v4" in route
    assert "actions/upload-artifact@v4" in cap
    assert "/tmp/route_inventory.json" in route
    assert "/tmp/apifootball_capability_evidence/" in cap
    assert "group: api-football-live-provider" in route
    assert "group: api-football-live-provider" in cap
    assert "retention-days: 7" in route and "retention-days: 7" in cap
