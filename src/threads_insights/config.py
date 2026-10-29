from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError


@dataclass(frozen=True)
class Config:
    app_id: str | None
    app_secret: str | None
    redirect_uri: str | None
    access_token: str | None
    user_id: str
    db_path: Path
    token_file: Path
    timeout: float
    enable_publish: bool = False
    enable_auto_post: bool = False
    auto_post_account: str = "junkerslabtech"
    auto_min_interval_minutes: int = 60
    auto_max_per_day: int = 4

    @classmethod
    def from_env(cls, load_env_file: bool = True) -> "Config":
        if load_env_file:
            load_dotenv()

        def get(name: str) -> str | None:
            v = os.environ.get(name, "").strip()
            return v or None

        try:
            timeout = float(get("THREADS_HTTP_TIMEOUT") or 15)
        except ValueError as e:
            raise ConfigError("THREADS_HTTP_TIMEOUT must be a number") from e
        if not 1 <= timeout <= 120:
            raise ConfigError("THREADS_HTTP_TIMEOUT must be between 1 and 120")
        def bounded(name: str, default: int, lo: int, hi: int) -> int:
            try:
                v = int(get(name) or default)
            except ValueError as e:
                raise ConfigError(f"{name} must be an integer") from e
            if not lo <= v <= hi:
                raise ConfigError(f"{name} must be between {lo} and {hi}")
            return v

        return cls(
            app_id=get("THREADS_APP_ID"),
            app_secret=get("THREADS_APP_SECRET"),
            redirect_uri=get("THREADS_REDIRECT_URI"),
            access_token=get("THREADS_ACCESS_TOKEN"),
            user_id=get("THREADS_USER_ID") or "me",
            db_path=Path(get("THREADS_DB_PATH") or "./data/threads_insights.db").expanduser(),
            token_file=Path(get("THREADS_TOKEN_FILE") or "./data/token.json").expanduser(),
            timeout=timeout,
            enable_publish=(get("THREADS_ENABLE_PUBLISH") or "").lower() in ("1", "true", "yes"),
            enable_auto_post=(get("THREADS_ENABLE_AUTO_POST") or "").lower() in ("1", "true", "yes"),
            auto_post_account=(get("THREADS_AUTO_POST_ACCOUNT") or "junkerslabtech").lstrip("@"),
            auto_min_interval_minutes=bounded("THREADS_AUTO_MIN_INTERVAL_MINUTES", 60, 1, 1440),
            auto_max_per_day=bounded("THREADS_AUTO_MAX_PER_DAY", 4, 1, 24),
        )
