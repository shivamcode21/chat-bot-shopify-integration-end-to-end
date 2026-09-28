"""
Centralized environment loader for Fashion Bot runtime modules.

Rules:
- development: load local .env fallback (outer first, then inner)
- staging/production: never load .env; rely on system env (Render/K8s/etc.)
- never override existing process environment variables
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Literal

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

logger = logging.getLogger(__name__)

Environment = Literal["development", "staging", "production"]

_settings: Optional["RuntimeEnv"] = None


@dataclass(frozen=True)
class RuntimeEnv:
    environment: Environment
    env_file_loaded: bool
    env_file_path: Optional[str]
    values: Dict[str, str]

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(key, default)


def _normalize_environment(value: str) -> Environment:
    normalized = (value or "").strip().lower()
    if normalized in {"production", "prod"}:
        return "production"
    if normalized in {"staging", "stage"}:
        return "staging"
    return "development"


def _detect_environment() -> Environment:
    for key in ("ENVIRONMENT", "NODE_ENV", "DEPLOY_ENV"):
        value = os.environ.get(key)
        if value:
            return _normalize_environment(value)
    return "development"


def _candidate_env_paths() -> list[Path]:
    package_dir = Path(__file__).resolve().parent
    outer_env = package_dir.parent / ".env"
    inner_env = package_dir / ".env"
    return [outer_env, inner_env]


def bootstrap_environment(required_vars: Optional[Iterable[str]] = None) -> RuntimeEnv:
    global _settings
    if _settings is not None:
        if required_vars:
            require_settings(*required_vars)
        return _settings

    environment = _detect_environment()
    env_file_loaded = False
    env_file_path: Optional[str] = None

    if environment == "development":
        if load_dotenv is None:
            logger.warning("python-dotenv not installed; skipping local .env loading")
        else:
            for candidate in _candidate_env_paths():
                if candidate.exists():
                    load_dotenv(dotenv_path=candidate, override=False)
                    env_file_loaded = True
                    env_file_path = str(candidate)
                    break
    else:
        logger.info("Environment=%s; using system environment variables only", environment)

    _settings = RuntimeEnv(
        environment=environment,
        env_file_loaded=env_file_loaded,
        env_file_path=env_file_path,
        values=dict(os.environ),
    )

    if required_vars:
        require_settings(*required_vars)
    return _settings


def get_settings() -> RuntimeEnv:
    return bootstrap_environment()


def require_settings(*keys: str) -> None:
    settings = get_settings()
    missing = [key for key in keys if not settings.get(key)]
    if missing:
        searched_paths = ", ".join(str(p) for p in _candidate_env_paths())
        raise ValueError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + f". environment={settings.environment}. "
            + (
                f"loaded_env_file={settings.env_file_path}."
                if settings.env_file_loaded
                else f"loaded_env_file=None. searched_paths=[{searched_paths}]"
            )
        )


def get_runtime_environment() -> Environment:
    return get_settings().environment


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    return get_settings().get(key, default)


def get_bool(key: str, default: bool = False) -> bool:
    value = get_env(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_int(key: str, default: int) -> int:
    value = get_env(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    value = get_env(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def set_env(key: str, value: str) -> None:
    os.environ[key] = value

