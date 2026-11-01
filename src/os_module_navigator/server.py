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
    NavigatorError,
)
from os_module_navigator.models import (
    ModuleInfo,
    OpenModuleResult,
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

These tools never edit, save, publish or close modules. open_module briefly
brings Service Studio to the foreground and sends Up/Down keys to its
search dialog, so avoid typing in Service Studio while it runs.
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
        OutSystems MCP) can work on it. Requires exactly one module with this
        exact name; otherwise returns a not-found error with suggestions or an
        ambiguity error with candidates. If the module is already open, its tab
        is brought to the front. status 'opened_verified' means the new tab was
        confirmed; 'opened_unverified' means Open was pressed but not confirmed."""
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
