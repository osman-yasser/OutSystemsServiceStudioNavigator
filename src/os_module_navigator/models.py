"""Public data models returned by providers and (later) MCP tools."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ModuleInfo(BaseModel):
    """A module found in the connected environment."""

    name: str
    application: str | None = None
    last_changed: str | None = Field(
        default=None, description="As displayed by Service Studio, e.g. 'October 22 at 1:33 AM'."
    )
    changed_by: str | None = None


class SearchResult(BaseModel):
    query: str
    modules: list[ModuleInfo]
    possibly_truncated: bool = Field(
        default=False,
        description="True when Service Studio may have more matches than it rendered; "
        "refine the query to see them.",
    )


class OpenStatus(StrEnum):
    OPENED_VERIFIED = "opened_verified"
    """Open was pressed and a module tab with the expected name became selected."""
    OPENED_UNVERIFIED = "opened_unverified"
    """Open was pressed but the new tab could not be confirmed within the timeout."""
    ALREADY_OPEN = "already_open"
    """The module already had a tab; that tab was brought to the front."""


class OpenModuleResult(BaseModel):
    module_name: str
    status: OpenStatus
    message: str
    tab_caption: str | None = None


class ServiceStudioStatus(BaseModel):
    running: bool
    window_detected: bool
    automation_backend: str
    environment_connected: bool | None = Field(
        default=None, description="True when an environment tab is present; None if unknown."
    )
    environment: str | None = None
    current_module: str | None = Field(
        default=None, description="Module in the selected editor tab, only if reliably known."
    )
    current_module_unsaved: bool | None = None
    open_modules: list[str] = Field(default_factory=list)
    dialog_open: str | None = Field(
        default=None, description="Title of a Service Studio dialog currently open, if any."
    )


class OpenModule(BaseModel):
    name: str
    application: str | None = None
    unsaved: bool = Field(default=False, description="True when the tab shows unsaved changes.")
    selected: bool = Field(default=False, description="True for the tab in front.")


class OpenModulesResult(BaseModel):
    count: int = Field(description="Number of module tabs open in Service Studio.")
    limit: int = Field(description="Maximum number of open modules allowed.")
    can_open_more: bool = Field(description="True when count < limit.")
    modules: list[OpenModule]


class CloseModuleResult(BaseModel):
    module_name: str
    status: str = Field(default="closed")
    message: str
    open_modules_count: int = Field(description="Number of module tabs still open.")
