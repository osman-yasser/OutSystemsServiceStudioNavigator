"""MCP server exposing Service Studio module navigation tools (stdio transport).

Run with ``uv run os-module-navigator``. Codex (or any MCP client) launches it
as a local process; it drives the Service Studio desktop app on this machine.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from os_module_navigator.config import NavigatorConfig
from os_module_navigator.errors import (
    AmbiguousModuleError,
    ModuleNotFoundInEnvironmentError,
    ModuleNotOpenError,
    NavigatorError,
    TooManyOpenModulesError,
)
from os_module_navigator.models import (
    CloseModuleResult,
    ModuleInfo,
    OpenModuleResult,
    OpenModulesResult,
    SearchResult,
    ServiceStudioStatus,
)
from os_module_navigator.services.module_service import ModuleService

log = logging.getLogger("os_module_navigator")

INSTRUCTIONS = """\
Navigates the OutSystems Service Studio 11 desktop app on this machine.

Use it together with the official OutSystems (Service Studio) MCP server, which
only works on modules that are open in Service Studio:

1. get_service_studio_status - check Service Studio is running and connected,
   and see which modules are open.
2. search_modules - find the exact module name (searches module name,
   application name and author).
3. open_module - open a module by its exact name. If it is already open, its
   tab is brought to the front. Then use the official OutSystems MCP on it.

KEEP AT MOST 10 MODULES OPEN. Past a certain number of tabs Service Studio
opens modules in a separate window, which breaks the workflow. Before every
open_module call for a module that is not already open:

  a. Call get_open_modules.
  b. If count < 10 (can_open_more is true), call open_module.
  c. If count >= 10, pick an open module you no longer need, call
     close_module with its name, then call open_module.

When picking a module to close, prefer one with unsaved=false that is not
selected and is not needed for the current task. Never close a module the
user is working on. close_module refuses modules with unsaved changes (error
module_has_unsaved_changes); pick another one or ask the user to save it.
open_module also refuses to open a new module when 10 are already open
(error too_many_open_modules, with the list of open modules).

These tools never edit, save or publish modules, and close only modules with
no unsaved changes. open_module briefly brings Service Studio to the
foreground and sends Up/Down keys to its search dialog, so avoid typing in
Service Studio while it runs.
"""


def _brief(modules: list[ModuleInfo]) -> list[dict[str, str | None]]:
    return [{"name": m.name, "application": m.application} for m in modules]


def to_tool_error(error: NavigatorError) -> ToolError:
    """Convert a provider error into a ToolError with a stable, parseable payload."""
    payload: dict[str, object] = {"code": error.code, "message": str(error)}
    if isinstance(error, AmbiguousModuleError):
        payload["candidates"] = _brief(error.candidates)
    if isinstance(error, ModuleNotFoundInEnvironmentError) and error.suggestions:
        payload["suggestions"] = _brief(error.suggestions)
    if isinstance(error, (TooManyOpenModulesError, ModuleNotOpenError)):
        payload["open_modules"] = error.open_modules
    return ToolError(json.dumps(payload, ensure_ascii=False))


def build_server(service: ModuleService) -> FastMCP:
    mcp = FastMCP(
        name="outsystems-module-navigator",
        instructions=INSTRUCTIONS,
        mask_error_details=True,
    )

    @mcp.tool(
        annotations={
            "title": "Search OutSystems modules",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def search_modules(
        query: Annotated[
            str,
            Field(
                description="Text to search for in module name, application name or author.",
                min_length=1,
                max_length=200,
            ),
        ],
    ) -> SearchResult:
        """Search modules in the connected environment through Service Studio's
        'Open from Environment' dialog. Returns module name, application, last
        change and author. Does not open anything."""
        try:
            return await service.search_modules(query)
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Open OutSystems module in Service Studio",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def open_module(
        module_name: Annotated[
            str,
            Field(
                description="Exact module name (case-insensitive), e.g. from search_modules.",
                min_length=1,
                max_length=200,
            ),
        ],
    ) -> OpenModuleResult:
        """Open a module in Service Studio so other tools (e.g. the official
        OutSystems MCP) can work on it. Call get_open_modules first: if 10 or
        more modules are open, close one with close_module before calling this.
        Requires exactly one module with this exact name; otherwise returns a
        not-found error with suggestions or an ambiguity error with candidates.
        If the module is already open, its tab is brought to the front (no limit
        check). Fails with too_many_open_modules when the limit is reached.
        status 'opened_verified' means the new tab was confirmed;
        'opened_unverified' means Open was pressed but not confirmed."""
        try:
            return await service.open_module(module_name)
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Service Studio status",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def get_service_studio_status() -> ServiceStudioStatus:
        """Report whether Service Studio is running, its window was detected,
        the automation backend, the connected environment, the module in the
        selected tab (and whether it has unsaved changes), and open modules."""
        try:
            return await service.get_status()
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Count open OutSystems modules",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def get_open_modules() -> OpenModulesResult:
        """Return how many modules are open in Service Studio, the limit (10),
        whether another module can be opened (can_open_more), and each open
        module's name, application, unsaved flag and whether it is in front.
        Call this before every open_module for a module that is not open yet."""
        try:
            return await service.get_open_modules()
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Close OutSystems module in Service Studio",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def close_module(
        module_name: Annotated[
            str,
            Field(
                description="Exact name of an open module (case-insensitive), "
                "as returned by get_open_modules.",
                min_length=1,
                max_length=200,
            ),
        ],
    ) -> CloseModuleResult:
        """Close an open module's tab in Service Studio to make room for another
        module. Only closes modules without unsaved changes; otherwise fails
        with module_has_unsaved_changes and nothing is closed. Fails with
        module_not_open (with the list of open modules) if no tab has this name.
        Does not save, publish or change the module."""
        try:
            return await service.close_module(module_name)
        except NavigatorError as error:
            raise to_tool_error(error) from error

    return mcp


def _configure_logging() -> None:
    # stdout carries the MCP protocol; logs must go to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("OS_NAV_LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    _configure_logging()
    from os_module_navigator.providers.desktop_gui import DesktopGUIProvider

    provider = DesktopGUIProvider(config=NavigatorConfig.from_env())
    try:
        build_server(ModuleService(provider)).run(show_banner=False)
    finally:
        provider.close()


if __name__ == "__main__":
    main()
