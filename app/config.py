"""Reads settings from environment variables (set in Railway > Variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ConfigError(f"Missing setting {name}. Add it in Railway > Variables.")
    return v


@dataclass(frozen=True)
class WPConfig:
    base_url: str
    user: str
    app_password: str

    @classmethod
    def from_env(cls) -> "WPConfig":
        base = _need("WP_BASE_URL").rstrip("/")
        if not base.startswith("https://"):
            raise ConfigError("WP_BASE_URL must start with https:// (Application Passwords need HTTPS).")
        return cls(base, _need("WP_USER"), _need("WP_APP_PASSWORD"))
