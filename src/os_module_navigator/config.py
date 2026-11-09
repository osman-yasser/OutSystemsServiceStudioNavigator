"""Tunable timeouts and limits. Override with ``OS_NAV_*`` environment variables."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class NavigatorConfig(BaseModel):
    dialog_open_timeout_s: float = Field(default=30.0, gt=0)
    search_settle_timeout_s: float = Field(default=10.0, gt=0)
    open_verify_timeout_s: float = Field(default=60.0, gt=0)
    close_verify_timeout_s: float = Field(default=15.0, gt=0)
    poll_interval_s: float = Field(default=0.5, gt=0)
    key_delay_s: float = Field(default=0.15, ge=0)
    max_query_length: int = Field(default=200, gt=0)
    max_results: int = Field(default=50, gt=0)
    # Service Studio moves extra modules into a new window past a certain number
    # of tabs; open_module refuses to open a new module once this many are open.
    max_open_modules: int = Field(default=10, gt=0)

    @classmethod
    def from_env(cls) -> NavigatorConfig:
        overrides: dict[str, str] = {}
        for name in cls.model_fields:
            value = os.environ.get(f"OS_NAV_{name.upper()}")
            if value is not None:
                overrides[name] = value
        return cls.model_validate(overrides)
