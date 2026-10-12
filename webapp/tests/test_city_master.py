"""Blinkit campaign config -> city id/name list. Made-up replies only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blinkit_config as bc  # noqa: E402


def test_finds_cities_wherever_they_sit():
    payload = {"data": {"objectives": [{"id": 1, "name": "Not a city"}],
                        "targeting": {"cities": [{"city_id": 7, "city_name": "Delhi"}, {"id": "9", "name": "Pune"}]}}}
    assert bc.parse_city_config(payload) == [("7", "Delhi"), ("9", "Pune")]


def test_nested_city_groups_and_duplicates():
    payload = {"city_list": [{"label": "North", "options": [{"value": 1, "label": "Delhi"}]},
                             {"value": 1, "label": "Delhi"}]}
    assert ("1", "Delhi") in bc.parse_city_config(payload)
    assert len([c for c in bc.parse_city_config(payload) if c[0] == "1"]) == 1


def test_nothing_found_is_empty():
    assert bc.parse_city_config({"objectives": [{"id": 1, "name": "x"}]}) == []
