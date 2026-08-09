"""Dedicated tests for config propagation across repos."""

import os
from unittest.mock import mock_open, patch

import pytest

from engine.propagate import PolicyPropagator


# ── _should_override ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "target", "expected"),
    [
        ("guards", {"guards": True}, True),
        ("guards", {"pipeline": {}}, False),
        ("pipeline", {}, False),
    ],
)
def test_should_override(key, target, expected):
    assert PolicyPropagator._should_override(key, target) == expected


# ── _create_gitreins_dir ─────────────────────────────────────


def test_create_gitreins_dir_creates_path(tmp_path):
    target = str(tmp_path / "my-repo")
    PolicyPropagator._create_gitreins_dir(target)
    assert os.path.isdir(os.path.join(target, ".gitreins"))


def test_create_gitreins_dir_idempotent(tmp_path):
    path = str(tmp_path)
    PolicyPropagator._create_gitreins_dir(path)
    # Should not raise
    PolicyPropagator._create_gitreins_dir(path)


# ── _merge_dicts ─────────────────────────────────────────────


def test_merge_dicts_adds_source_only_keys():
    source = {"pipeline": {"stages": []}, "version": 1}
    target = {"guards": {"secrets": True}}

    merged, added, preserved = PolicyPropagator._merge_dicts(source, target)

    assert "pipeline" in merged
    assert "version" in merged
    assert "guards" in merged
    assert "pipeline" in added
    assert "version" in added
    assert not preserved


def test_merge_dicts_preserves_target_on_scalar_conflict():
    source = {"version": 2}
    target = {"version": 1}

    merged, added, preserved = PolicyPropagator._merge_dicts(source, target)

    assert merged["version"] == 1
    assert "version" in preserved
    assert not added


def test_merge_dicts_recursively_merges_nested_dicts():
    source = {
        "guards": {"secrets": True, "lint": True},
        "pipeline": {"stages": []},
    }
    target = {"guards": {"tests": True}, "version": 1}

    merged, added, preserved = PolicyPropagator._merge_dicts(source, target)

    assert merged["guards"]["secrets"] is True
    assert merged["guards"]["lint"] is True
    assert merged["guards"]["tests"] is True
    assert "guards.secrets" in added
    assert "guards.lint" in added
    assert "pipeline" in added
    # version is only in target (not in source), so it's silently kept
    assert merged["version"] == 1


def test_merge_dicts_handles_empty_source():
    merged, added, preserved = PolicyPropagator._merge_dicts({}, {"a": 1})
    assert merged == {"a": 1}
    assert not added
    assert not preserved


def test_merge_dicts_handles_empty_target():
    merged, added, preserved = PolicyPropagator._merge_dicts({"a": 1}, {})
    assert merged == {"a": 1}
    assert "a" in added


# ── _load_source_config ──────────────────────────────────────


def test_load_source_config_returns_none_when_missing():
    with patch("os.path.isfile", return_value=False):
        prop = PolicyPropagator("/nonexistent")
        assert prop._load_source_config() is None


def test_load_source_config_returns_none_on_yaml_error():
    with (
        patch("os.path.isfile", return_value=True),
        patch("builtins.open", mock_open(read_data=": invalid: yaml:")),
    ):
        prop = PolicyPropagator("/tmp")
        # Should log a warning but not raise
        result = prop._load_source_config()
        assert result is None


# ── propagate_policy — missing source config ─────────────────


def test_propagate_returns_error_when_source_config_missing():
    with patch("os.path.isfile", return_value=False):
        prop = PolicyPropagator("/nonexistent")
        result = prop.propagate_policy(["/target"])
    assert "error" in result
    assert "No config found" in result["error"]


# ── propagate_policy — copy to new target ────────────────────


def test_propagate_copies_to_target_without_existing_config(tmp_path):

    src_dir = tmp_path / "source"
    target = tmp_path / "target"
    os.makedirs(src_dir / ".gitreins")
    src_config_path = src_dir / ".gitreins" / "config.yaml"
    src_config_path.write_text("guards:\n  secrets: true\n")

    prop = PolicyPropagator(str(src_dir))
    result = prop.propagate_policy([str(target)])

    assert result["source"] == str(src_dir)
    assert len(result["results"]) == 1
    assert result["results"][0]["action"] == "created"
    assert os.path.isfile(target / ".gitreins" / "config.yaml")


# ── propagate_policy — merge to existing target ──────────────


def test_propagate_merges_to_target_with_existing_config(tmp_path):
    import yaml

    src_dir = tmp_path / "source"
    target = tmp_path / "target"
    os.makedirs(src_dir / ".gitreins")
    os.makedirs(target / ".gitreins")
    src_config_path = src_dir / ".gitreins" / "config.yaml"
    src_config_path.write_text("guards:\n  secrets: true\n  lint: true\n")
    target_config_path = target / ".gitreins" / "config.yaml"
    target_config_path.write_text("guards:\n  tests: true\n")

    prop = PolicyPropagator(str(src_dir))
    result = prop.propagate_policy([str(target)])

    assert result["results"][0]["action"] == "merged"
    # Verify target config was merged (source keys added, target preserved)
    merged = yaml.safe_load(target_config_path.read_text())
    assert merged["guards"]["secrets"] is True
    assert merged["guards"]["lint"] is True
    assert merged["guards"]["tests"] is True


# ── backward-compat alias (R2.14) ────────────────────────────


def test_propagator_alias_and_propagate_delegation(tmp_path):
    """R2.14 rename: ``Propagator`` alias and ``propagate()`` still work.

    ``Propagator`` is a module-level alias for :class:`PolicyPropagator`,
    and ``propagate()`` delegates to ``propagate_policy()`` — old callers
    keep functioning unchanged.
    """
    from engine.propagate import Propagator

    src_dir = tmp_path / "source"
    target = tmp_path / "target"
    os.makedirs(src_dir / ".gitreins")
    (src_dir / ".gitreins" / "config.yaml").write_text("guards:\n  secrets: true\n")

    # Alias: same class object, not a subclass or copy.
    assert Propagator is PolicyPropagator

    # Old method name still works through the alias…
    prop = Propagator(str(src_dir))
    result = prop.propagate([str(target)])
    assert result["results"][0]["action"] == "created"
    assert os.path.isfile(target / ".gitreins" / "config.yaml")

    # …and matches the new method exactly (delegation).
    target2 = tmp_path / "target2"
    result2 = prop.propagate_policy([str(target2)])
    assert result2["results"][0]["action"] == "created"
    assert os.path.isfile(target2 / ".gitreins" / "config.yaml")
