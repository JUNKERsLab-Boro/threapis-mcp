"""Token persistence. The token file is written 0600 and is never logged."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import AuthError


@dataclass
class TokenInfo:
    access_token: str
    expires_at: float | None = None  # unix seconds; None = unknown (env token)
    refreshed_at: float | None = None
    user_id: str | None = None
    from_file: bool = False
    scopes: list[str] | None = None  # None = unknown (env token / legacy file)

    def __repr__(self) -> str:  # never leak the token
        return f"TokenInfo(expires_at={self.expires_at}, from_file={self.from_file})"

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at


class TokenStore:
    def __init__(self, config: Config):
        self.config = config

    def load(self) -> TokenInfo:
        p: Path = self.config.token_file
        if p.is_file():
            try:
                d = json.loads(p.read_text())
                if d.get("access_token"):
                    return TokenInfo(
                        access_token=d["access_token"],
                        expires_at=d.get("expires_at"),
                        refreshed_at=d.get("refreshed_at"),
                        user_id=d.get("user_id"),
                        from_file=True,
                        scopes=d.get("scopes"),
                    )
            except (OSError, ValueError):
                pass  # fall through to env
        if self.config.access_token:
            return TokenInfo(self.config.access_token, user_id=self.config.user_id)
        raise AuthError(
            "No Threads access token. Run `threads-insights-auth login` or set THREADS_ACCESS_TOKEN."
        )

    def save(self, token: str, expires_in: int | float, user_id: str | None,
             scopes: list[str] | None = None) -> TokenInfo:
        now = time.time()
        data = {
            "access_token": token,
            "expires_at": now + float(expires_in),
            "refreshed_at": now,
            "user_id": user_id,
            "scopes": scopes,
        }
        p = self.config.token_file
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".token-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return TokenInfo(token, data["expires_at"], now, user_id, True, scopes)
