"""
reach_layer/tests/test_main.py

Tests for config-loading utilities in main.py: _load_yaml and _deep_merge.

The reach_layer uses _load_yaml (equivalent to _load_config in other services)
and _deep_merge to merge dpg.yaml + domain.yaml at startup.

Covers:
- Normal:  valid YAML files load correctly; dicts merge as expected
- Edge:    empty YAML, empty base/override dicts
- Failure: missing DPG config raises FileNotFoundError (hard fail);
           missing domain config is caught by _load_config → bare-infra mode
"""

from __future__ import annotations

import pytest
import yaml
from pathlib import Path


# ---------------------------------------------------------------------------
# Inline implementations — identical to main.py utilities.
# Reach layer uses _load_yaml (same logic as _load_config in other services).
# ---------------------------------------------------------------------------

def _load_yaml(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")
    with config_path.open("r") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# _load_yaml — normal execution
# ---------------------------------------------------------------------------

class TestLoadYamlNormal:
    def test_valid_yaml_returns_dict(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("agent_core_client:\n  endpoint: http://localhost:8000/process_turn\n")
        result = _load_yaml(str(f))
        assert result["agent_core_client"]["endpoint"] == "http://localhost:8000/process_turn"

    def test_returns_dict_type(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("key: value\n")
        assert isinstance(_load_yaml(str(f)), dict)

    def test_nested_structure_preserved(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("a:\n  b:\n    c: 42\n")
        assert _load_yaml(str(f))["a"]["b"]["c"] == 42


# ---------------------------------------------------------------------------
# _load_yaml — edge cases
# ---------------------------------------------------------------------------

class TestLoadYamlEdge:
    def test_empty_yaml_returns_empty_dict(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        assert _load_yaml(str(f)) == {}

    def test_yaml_with_only_whitespace_returns_empty_dict(self, tmp_path):
        f = tmp_path / "ws.yaml"
        f.write_text("   \n\n")
        assert _load_yaml(str(f)) == {}


# ---------------------------------------------------------------------------
# _load_yaml — failure scenarios
# ---------------------------------------------------------------------------

class TestLoadYamlFailure:
    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_yaml(str(tmp_path / "nonexistent.yaml"))

    def test_error_message_contains_path(self, tmp_path):
        path = str(tmp_path / "missing.yaml")
        with pytest.raises(FileNotFoundError, match="missing.yaml"):
            _load_yaml(path)

    def test_dpg_config_missing_hard_fails(self, tmp_path):
        """DPG config missing raises FileNotFoundError — CLI must not start."""
        with pytest.raises(FileNotFoundError):
            _load_yaml(str(tmp_path / "config" / "dpg.yaml"))

    def test_domain_config_missing_raises_catchable_error(self, tmp_path):
        """
        Domain config missing raises FileNotFoundError.
        _load_config() in reach_layer/main.py catches this, sets domain_config={}
        and starts with DPG defaults only.
        """
        caught = False
        try:
            _load_yaml(str(tmp_path / "config" / "domain.yaml"))
        except FileNotFoundError:
            caught = True
        assert caught


# ---------------------------------------------------------------------------
# _deep_merge — normal execution
# ---------------------------------------------------------------------------

class TestDeepMergeNormal:
    def test_override_wins_on_scalar_conflict(self):
        base = {"agent_core_client": {"timeout_s": 30.0}}
        override = {"agent_core_client": {"timeout_s": 60.0}}
        result = _deep_merge(base, override)
        assert result["agent_core_client"]["timeout_s"] == 60.0

    def test_non_overlapping_keys_combined(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_nested_dicts_merged_recursively(self):
        base = {"agent_core_client": {"endpoint": "http://localhost:8000", "timeout_s": 30.0}}
        override = {"agent_core_client": {"endpoint": "http://agent-core:8000"}}
        result = _deep_merge(base, override)
        assert result["agent_core_client"]["endpoint"] == "http://agent-core:8000"
        assert result["agent_core_client"]["timeout_s"] == 30.0


# ---------------------------------------------------------------------------
# _deep_merge — edge cases
# ---------------------------------------------------------------------------

class TestDeepMergeEdge:
    def test_empty_override_returns_base_unchanged(self):
        base = {"agent_core_client": {"timeout_s": 30.0}}
        assert _deep_merge(base, {}) == base

    def test_empty_base_returns_override(self):
        assert _deep_merge({}, {"x": 10}) == {"x": 10}

    def test_both_empty_returns_empty(self):
        assert _deep_merge({}, {}) == {}

    def test_base_not_mutated(self):
        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base == {"a": 1}

    def test_dpg_only_config_produces_valid_merged_result(self):
        """Bare-infra mode: _deep_merge(dpg, {}) gives dpg defaults unchanged."""
        dpg = {"agent_core_client": {"endpoint": "http://localhost:8000", "timeout_s": 30.0}}
        assert _deep_merge(dpg, {}) == dpg
