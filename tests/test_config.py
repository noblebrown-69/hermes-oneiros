import pytest

from dream_cycle.config import default_config, load_config, validate_config
from dream_cycle.errors import ConfigError


def test_default_valid():
    cfg = validate_config(default_config())
    assert cfg["apply_enabled"] is False
    assert cfg["profile_allowlist"] == []


def test_profile_allowlist_empty_means_unset_and_names_must_be_plain():
    cfg = default_config()
    cfg["profile_allowlist"] = None
    assert validate_config(cfg)["profile_allowlist"] == []
    cfg = default_config()
    cfg["profile_allowlist"] = ["alpha", "beta"]
    assert validate_config(cfg)["profile_allowlist"] == ["alpha", "beta"]
    cfg = default_config()
    cfg["profile_allowlist"] = ["bad name"]
    with pytest.raises(ConfigError, match="profile_allowlist"):
        validate_config(cfg)


def test_default_is_copy():
    a = default_config(); a["labels"].clear()
    assert default_config()["labels"]


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml")["max_proposals_per_run"] == 10


def test_yaml_override(tmp_path):
    p = tmp_path / "c.yaml"; p.write_text("max_proposals_per_run: 3\n")
    assert load_config(p)["max_proposals_per_run"] == 3


def test_session_block_merges(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("session_corroboration:\n  enabled: true\n  day_command: ['echo', '{date}']\n")
    c = load_config(p)
    assert c["session_corroboration"]["enabled"] and c["session_corroboration"]["timeout_s"] == 30


@pytest.mark.parametrize("bad,msg", [
    ({"labels": "x"}, "labels"),
    ({"labels": [{"name": "a"}]}, "pattern"),
    ({"labels": [{"name": "a", "pattern": "("}]}, "bad regex"),
    ({"labels": [{"name": "a", "pattern": "x", "nearby_label_re": "["}]}, "nearby_label_re"),
    ({"max_proposals_per_run": 0}, ">= 1"),
    ({"max_proposals_per_run": "lots"}, "integer"),
    ({"memory_globs": []}, "memory_globs"),
    ({"session_corroboration": {"enabled": True, "source": "command"}}, "day_command"),
    ({"session_corroboration": {"enabled": False, "day_command": "echo"}}, "argv list"),
])
def test_invalid_configs(bad, msg):
    c = default_config(); c.update(bad)
    with pytest.raises(ConfigError, match=msg):
        validate_config(c)


def test_bad_yaml(tmp_path):
    p = tmp_path / "c.yaml"; p.write_text("labels: [\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


def test_non_mapping_yaml(tmp_path):
    p = tmp_path / "c.yaml"; p.write_text("- a\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)



def test_state_db_enabled_without_day_command():
    """Fleet shape (enabled + source state.db, no day_command) must validate.

    Regression for the 2026-11-09 dashboard 502: pre-v0.6.0 validation required
    day_command whenever enabled, so overview/proposals/receipts raised ConfigError
    and the plugin host answered "could not run in this profile's plugin host".
    """
    c = default_config()
    c["session_corroboration"] = {
        "enabled": True,
        "source": "state.db",
        "lookback_days": 3,
        "max_rows": 8000,
        "timeout_s": 30,
        # intentionally omit days_command / day_command
    }
    out = validate_config(c)
    assert out["session_corroboration"]["enabled"] is True
    assert out["session_corroboration"]["source"] == "state.db"
    assert out["session_corroboration"].get("day_command") is None


def test_yaml_fleet_session_corroboration_no_day_command(tmp_path):
    """Fleet session-corroboration YAML after v0.6.0 (no day_command)."""
    p = tmp_path / "c.yaml"
    p.write_text(
        "session_corroboration:\n"
        "  enabled: true\n"
        "  source: state.db\n"
        "  lookback_days: 3\n"
        "  max_rows: 8000\n"
        "  timeout_s: 30\n"
    )
    c = load_config(p)
    assert c["session_corroboration"]["enabled"] is True
    assert c["session_corroboration"]["source"] == "state.db"
    assert c["session_corroboration"]["day_command"] is None


def test_load_config_accepts_str_path(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("max_proposals_per_run: 7\n")
    assert load_config(str(p))["max_proposals_per_run"] == 7
    # missing path as str → defaults (no AttributeError)
    assert load_config(str(tmp_path / "missing.yaml"))["max_proposals_per_run"] == 10
