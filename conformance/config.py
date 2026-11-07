"""Validated contract and environment-only endpoints; no implementation detection."""
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FEATURES = frozenset({
    "transactions", "session_affinity", "backend_reuse", "release_after_transaction",
    "release_after_statement", "session_state", "extended", "named_prepared", "sql_prepare",
    "pipeline", "cancel", "auth_rejection", "tls", "tls_required", "certificate_validation",
    "multi_user", "multi_database", "client_limits", "limits", "wait_timeout", "reserve_pool", "independent_pools",
    "idle_client_timeout", "idle_server_timeout", "server_lifetime", "backend_terminate",
    "postgres_restart", "network_interruption", "pooler_restart", "reload", "admin_stats",
    "pause_resume", "drain", "unsupported_config", "prepared_eviction",
})


@dataclass(frozen=True)
class Contract:
    name: str
    mode: str
    features: dict[str, bool]
    settings: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Contract":
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("manifest must be an object")
        if set(data) - {"name", "mode", "features", "settings", "actions", "notes"}:
            raise ValueError("unknown manifest field")
        if not isinstance(data.get("name"), str) or not data["name"]:
            raise ValueError("manifest name must be a nonempty string")
        if not isinstance(data["mode"], str) or data["mode"] not in {"session", "transaction", "statement", "routing"}:
            raise ValueError("mode must be session, transaction, statement, or routing")
        features = data["features"]
        if not isinstance(features, dict):
            raise ValueError("features must be an object")
        if set(features) - FEATURES or any(type(value) is not bool for value in features.values()):
            raise ValueError("unknown capability or non-boolean declaration")
        obligations = {
            "session": {"transactions", "session_affinity", "backend_reuse"},
            "transaction": {"transactions", "release_after_transaction", "backend_reuse"},
            "statement": {"release_after_statement", "backend_reuse"},
        }
        missing = obligations.get(data["mode"], set()) - {key for key, value in features.items() if value}
        if missing:
            raise ValueError(f"{data['mode']} mode cannot disable required guarantees: {sorted(missing)}")
        settings = data.get("settings", {})
        validate_settings(features, settings)
        actions = data.get("actions", {})
        if not isinstance(actions, dict):
            raise ValueError("actions must be an object")
        for argv in actions.values():
            if not isinstance(argv, list) or not argv or not all(isinstance(s, str) for s in argv):
                raise ValueError("adapter actions must be nonempty argv arrays; shell strings forbidden")
        return cls(data["name"], data["mode"], features, settings, actions)

    def number(self, key: str, default: float) -> float:
        return float(self.settings.get(key, default))


def endpoints() -> tuple[str, str]:
    values = (os.environ.get("POOLER_DSN", ""), os.environ.get("POSTGRES_DSN", ""))
    if not all(values):
        raise ValueError("POOLER_DSN and POSTGRES_DSN must both be set")
    return values


def validate_settings(features: dict, settings: dict) -> None:
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    for key in ("operation_timeout", "recovery_timeout", "connect_timeout", "observation_window", "poll_interval"):
        value = settings.get(key, 10 if key != "poll_interval" else 0.05)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
    levels = settings.get("concurrency_levels", [10, 100, 1000])
    if not levels or any(type(n) is not int or not 1 <= n <= 10000 for n in levels):
        raise ValueError("concurrency_levels must be integers in 1..10000")
    for key in ("pool_size", "max_clients", "capacity", "cycles", "queries_per_client"):
        value = settings.get(key, 1)
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    needs_pool = {"backend_reuse", "release_after_transaction", "release_after_statement", "limits", "wait_timeout", "reserve_pool", "independent_pools"}
    if any(features.get(feature) for feature in needs_pool) and "pool_size" not in settings:
        raise ValueError("declared pooling/limit tests require explicit pool_size")
    if (features.get("limits") or features.get("client_limits")) and "max_clients" not in settings:
        raise ValueError("limits require explicit max_clients")
    if features.get("wait_timeout"):
        if not 0 < settings.get("wait_timeout", 0) < settings.get("operation_timeout", 10):
            raise ValueError("pool wait_timeout must be positive and shorter than operation_timeout")
