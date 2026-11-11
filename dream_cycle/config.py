from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict

from .errors import ConfigError

DEFAULT: Dict[str, Any] = {
    "memory_globs": ["MEMORY.md", "MISTAKES.md"],
    "labels": [
        {"name": "build", "pattern": r"\bb\d{4,6}\b", "proximity_words": 3,
         "nearby_label_re": r"(?i)\b(build|llama|cpp|driver)\b"},
        {"name": "throughput", "pattern": r"\b\d{1,4}(?:\.\d+)?\s*tok/s\b", "proximity_words": 3},
        {"name": "port", "pattern": r"(?<![\d.]):(\d{2,5})(?!\d)(?!\.\d)", "proximity_words": 3,
         "nearby_label_re": r"(?i)\b(port|listen|bind|endpoint|url|http)\b",
         "drop_if_looks_like_pid": True},
    ],
    "max_proposals_per_run": 10,
    "apply_enabled": False,
    # Apply guards (2026-11-08). None = read memory.memory_char_limit from $HERMES_HOME/config.yaml.
    "apply_max_chars": None,
    "apply_evidence_max": 200,
    # Evidence grounding (2026-11-08): check `keep` against the day's state.db messages.
    "require_grounded_evidence": False,
    # Conversation evidence only by default (v0.6.1). Tool / compaction filtered in sessions.py.
    "grounding_roles": ["user", "assistant"],
    # Memory-side: skip "same label, multiple values" when each value names a distinct service/host.
    "skip_distinct_service_conflicts": True,
    "stage_subdir": "dream-proposals",
    "disabled_filename": "DISABLED",
    "lock_filename": "dream-cycle.lock",
    # Optional read-only session corroboration ("what did the day actually say?").
    # Commands are argv lists; "{date}" is replaced with YYYY-MM-DD.
    # days_command must print JSON with a list of known dates (see sessions.py);
    # a date missing from that list means "no sessions that day" — not an error.
    # Read-only token/cache/tool evidence from $HERMES_HOME/state.db (fail-soft).
    "session_costs": {"enabled": True, "db": None},
    # v0.8.0 — honor "don't save" / off-the-record directives in role=user messages
    "honor_nosave": True,
    # v0.8.4 — owner reminder when unreviewed dreams are older than overdue_days.
    # Proactive nags only. An explicit status question is never suppressed.
    # dashboard_base_url stays empty until deployment sets the real origin.
    # Empty profile_allowlist means every installed profile is in scope.
    "profile_allowlist": [],
    "review_reminder": {
        "enabled": True,
        "overdue_days": 7,
        "cooldown_hours": 24,
        "snooze_days": 7,
        "dashboard_base_url": "",
    },
    # v0.7.0 — learn about the person (user-role messages only; no model / no network)
    "user_facts": {
        "enabled": True,
        "lookback_days": 30,
        "max_rows": 8000,
        "categories": ["PERSON", "EVENT", "PREFERENCE", "STATUS"],
    },
    "session_corroboration": {
        "enabled": False,
        # source: "state.db" (default, read-only messages) | "command" (legacy argv)
        "source": "state.db",
        "lookback_days": 3,
        "max_rows": 8000,
        "days_command": None,
        "day_command": None,
        "timeout_s": 30,
        # v0.6.1 — safe defaults ON: tool dumps and compaction summaries are not corroboration.
        "ignore_tool_messages": True,
        "ignore_compaction_messages": True,
        # Assistant text may be scanned, but a fresh value needs at least one user citation.
        "require_user_evidence": True,
    },
}


def default_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT)


def validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    labels = cfg.get("labels")
    if not isinstance(labels, list):
        raise ConfigError("config 'labels' must be a list of {name, pattern} entries")
    for i, lab in enumerate(labels):
        if not isinstance(lab, dict) or not lab.get("name") or not lab.get("pattern"):
            raise ConfigError(f"labels[{i}] needs both 'name' and 'pattern'")
        for key in ("pattern", "nearby_label_re"):
            if lab.get(key):
                try:
                    re.compile(lab[key])
                except re.error as e:
                    raise ConfigError(f"labels[{i}] ({lab['name']}): bad regex in '{key}': {e}") from None
    try:
        raw = cfg.get("max_proposals_per_run")
        n = 10 if raw is None else int(raw)
    except (TypeError, ValueError):
        raise ConfigError("max_proposals_per_run must be an integer") from None
    if n < 1:
        raise ConfigError("max_proposals_per_run must be >= 1")
    cfg["max_proposals_per_run"] = n
    globs = cfg.get("memory_globs")
    if not isinstance(globs, list) or not globs:
        raise ConfigError("memory_globs must be a non-empty list, e.g. ['MEMORY.md']")
    sc = cfg.get("session_corroboration") or {}
    source = (sc.get("source") or "state.db").strip()
    if source not in ("state.db", "command"):
        raise ConfigError("session_corroboration.source must be 'state.db' or 'command'")
    sc["source"] = source
    try:
        sc["lookback_days"] = max(1, int(sc.get("lookback_days") or 1))
    except (TypeError, ValueError):
        raise ConfigError("session_corroboration.lookback_days must be an integer >= 1") from None
    if sc.get("enabled") and source == "command" and not sc.get("day_command"):
        raise ConfigError("session_corroboration.source=command needs day_command (argv list with {date})")
    for key in ("days_command", "day_command"):
        v = sc.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise ConfigError(f"session_corroboration.{key} must be an argv list of strings")
    for key in ("ignore_tool_messages", "ignore_compaction_messages", "require_user_evidence"):
        if key in sc and not isinstance(sc[key], bool):
            raise ConfigError(f"session_corroboration.{key} must be a boolean")
        sc.setdefault(key, True)
    cfg["session_corroboration"] = sc
    if "skip_distinct_service_conflicts" in cfg and not isinstance(cfg["skip_distinct_service_conflicts"], bool):
        raise ConfigError("skip_distinct_service_conflicts must be a boolean")
    cfg.setdefault("skip_distinct_service_conflicts", True)
    uf = cfg.get("user_facts") or {}
    if not isinstance(uf, dict):
        raise ConfigError("user_facts must be a mapping")
    if "enabled" in uf and not isinstance(uf["enabled"], bool):
        raise ConfigError("user_facts.enabled must be a boolean")
    uf.setdefault("enabled", True)
    try:
        uf["lookback_days"] = max(1, int(uf.get("lookback_days") or 30))
    except (TypeError, ValueError):
        raise ConfigError("user_facts.lookback_days must be an integer >= 1") from None
    try:
        uf["max_rows"] = max(1, int(uf.get("max_rows") or 8000))
    except (TypeError, ValueError):
        raise ConfigError("user_facts.max_rows must be an integer >= 1") from None
    cats = uf.get("categories") or ["PERSON", "EVENT", "PREFERENCE", "STATUS"]
    if not isinstance(cats, list) or not cats:
        raise ConfigError("user_facts.categories must be a non-empty list")
    bad = [c for c in cats if c not in ("PERSON", "EVENT", "PREFERENCE", "STATUS")]
    if bad:
        raise ConfigError(f"user_facts.categories unknown: {bad}")
    uf["categories"] = cats
    cfg["user_facts"] = uf
    if "honor_nosave" in cfg and not isinstance(cfg["honor_nosave"], bool):
        raise ConfigError("honor_nosave must be a boolean")
    cfg.setdefault("honor_nosave", True)
    cfg["profile_allowlist"] = _validate_profile_allowlist(cfg.get("profile_allowlist"))
    cfg["review_reminder"] = _validate_review_reminder(cfg.get("review_reminder"))
    return cfg


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_profile_allowlist(raw: Any) -> list:
    """Empty list: any profile with the plugin installed is allowed."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("profile_allowlist must be a list of profile names")
    names = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("profile_allowlist entries must be non-empty strings")
        name = item.strip()
        if not _PROFILE_NAME.match(name):
            raise ConfigError(f"profile_allowlist entry {name!r} is not a profile name")
        names.append(name)
    return names


def _validate_review_reminder(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("review_reminder must be a mapping")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ConfigError("review_reminder.enabled must be a boolean")
    raw.setdefault("enabled", True)
    try:
        days = int(7 if raw.get("overdue_days") is None else raw.get("overdue_days"))
    except (TypeError, ValueError):
        raise ConfigError("review_reminder.overdue_days must be an integer >= 1") from None
    if days < 1:
        raise ConfigError("review_reminder.overdue_days must be an integer >= 1")
    raw["overdue_days"] = days
    try:
        hours = float(24 if raw.get("cooldown_hours") is None else raw.get("cooldown_hours"))
    except (TypeError, ValueError):
        raise ConfigError("review_reminder.cooldown_hours must be a number > 0") from None
    if hours <= 0:
        raise ConfigError("review_reminder.cooldown_hours must be a number > 0")
    raw["cooldown_hours"] = hours
    try:
        snooze = int(7 if raw.get("snooze_days") is None else raw.get("snooze_days"))
    except (TypeError, ValueError):
        raise ConfigError("review_reminder.snooze_days must be an integer >= 1") from None
    if snooze < 1:
        raise ConfigError("review_reminder.snooze_days must be an integer >= 1")
    raw["snooze_days"] = snooze
    url = raw.get("dashboard_base_url") or ""
    if not isinstance(url, str):
        raise ConfigError("review_reminder.dashboard_base_url must be a string")
    url = url.strip()
    if url:
        _validate_dashboard_base(url)
    raw["dashboard_base_url"] = url
    return raw


def _validate_dashboard_base(url: str) -> None:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("review_reminder.dashboard_base_url must be an http(s) origin")
    if parts.username or parts.password:
        raise ConfigError("review_reminder.dashboard_base_url must not contain credentials")
    if parts.query or parts.fragment:
        raise ConfigError("review_reminder.dashboard_base_url must not include a query or fragment")


def load_config(path: Path | str | None = None) -> Dict[str, Any]:
    cfg = default_config()
    if path is not None:
        path = Path(path)
    if path and path.exists():
        from .yaml_load import YamlReadError, safe_load
        try:
            data = safe_load(path.read_text()) or {}
        except YamlReadError as e:
            raise ConfigError(f"{path}: not valid YAML ({e})") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        cs = data.pop("session_costs", None)
        if isinstance(cs, dict):
            cfg["session_costs"].update(cs)
        sc = data.pop("session_corroboration", None)
        uf = data.pop("user_facts", None)
        rr = data.pop("review_reminder", None)
        cfg.update(data)
        if isinstance(sc, dict):
            cfg["session_corroboration"].update(sc)
        if isinstance(uf, dict):
            cfg["user_facts"].update(uf)
        if isinstance(rr, dict):
            cfg["review_reminder"].update(rr)
    return validate_config(cfg)
