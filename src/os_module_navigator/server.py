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
    AmbiguousDependencyElementError,
    AmbiguousDependencyError,
    AmbiguousModuleError,
    DependencyElementNotFoundError,
    DependencyNotFoundError,
    ModuleNotFoundInEnvironmentError,
    ModuleNotOpenError,
    NavigatorError,
    TooManyOpenModulesError,
)
from os_module_navigator.models import (
    CloseModuleResult,
    DependencyChangeResult,
    FindUsagesResult,
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

ADDING / REMOVING DEPENDENCIES (the official MCP cannot do this): the module
must be open (use open_module first). add_dependency(module_name,
dependency_name, elements=None) adds a reference to ALL public elements of the
named module or extension, or only to the given element names (e.g. a single
block or action), like ticking them in Manage Dependencies and pressing Apply.
remove_dependency does the opposite. refresh_dependencies(module_name,
dependency_name=None) refreshes outdated references (all of them, like the
dialog's Refresh all button, or only one dependency's) and applies; the
official omlRefreshReferences tool does the same in-process. After adding,
call the official listApps to confirm the new reference. Do not call these in the middle of an
applyModelApiCode chain: merge first, then change dependencies.
FINDING USAGES (the official MCP only sees open modules, so it cannot list who
consumes an element): find_usages(module_name, element_name) runs Service
Studio's "Find Usages in all Modules" on an element of an OPEN module (the
producer) and returns every module that uses it with its usage count. It opens
the element through the module search (Cmd+F), right-clicks it in the elements
tree and reads the result pane; nothing is changed. Then open the consumer
modules and read the callers with the official MCP.
These tools never save or publish modules, and close only modules with
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
    if isinstance(error, DependencyElementNotFoundError) and error.suggestions:
        payload["suggestions"] = error.suggestions
    if isinstance(error, AmbiguousDependencyElementError):
        payload["candidates"] = error.candidates
    if isinstance(error, DependencyNotFoundError) and error.suggestions:
        payload["suggestions"] = error.suggestions
    if isinstance(error, AmbiguousDependencyError):
        payload["candidates"] = error.candidates
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

    @mcp.tool(
        annotations={
            "title": "Add a dependency to an open OutSystems module",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def add_dependency(
        module_name: Annotated[
            str,
            Field(
                description="Exact name of an OPEN module to add the dependency to.",
                min_length=1,
                max_length=200,
            ),
        ],
        dependency_name: Annotated[
            str,
            Field(
                description="Exact name of the module or extension to depend on "
                "(e.g. 'OutSystemsCharts').",
                min_length=1,
                max_length=200,
            ),
        ],
        elements: Annotated[
            list[str] | None,
            Field(
                description="Optional exact names of individual public elements (e.g. "
                "'DonutChart'). Omit to add ALL of the module's public elements.",
                max_length=50,
            ),
        ] = None,
    ) -> DependencyChangeResult:
        """Add a dependency (reference) to ALL public elements of another module or
        extension, through Service Studio's Manage Dependencies dialog. The module
        must already be open (module_not_open otherwise). status 'applied' means
        Apply was pressed and the dialog closed; 'already_present' means nothing
        needed changing. Fails with dependency_not_found (with suggestions) or
        ambiguous_dependency if the name is not unique. Does not save or publish."""
        try:
            return await service.add_dependency(module_name, dependency_name, elements)
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Remove a dependency from an open OutSystems module",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def remove_dependency(
        module_name: Annotated[
            str,
            Field(
                description="Exact name of an OPEN module to remove the dependency from.",
                min_length=1,
                max_length=200,
            ),
        ],
        dependency_name: Annotated[
            str,
            Field(
                description="Exact name of the module or extension to stop depending on.",
                min_length=1,
                max_length=200,
            ),
        ],
        elements: Annotated[
            list[str] | None,
            Field(
                description="Optional exact names of individual public elements (e.g. "
                "'DonutChart'). Omit to remove ALL of the module's public elements.",
                max_length=50,
            ),
        ] = None,
    ) -> DependencyChangeResult:
        """Remove a dependency (all its referenced elements) from an open module,
        through Manage Dependencies. status 'applied' means Apply was pressed and
        the dialog closed; 'not_a_dependency' means nothing needed changing. If
        elements are still in use Service Studio may show a dialog: this tool then
        fails with unexpected_dialog and the user must handle it. Does not save
        or publish."""
        try:
            return await service.remove_dependency(module_name, dependency_name, elements)
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Refresh the dependencies of an open OutSystems module",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def refresh_dependencies(
        module_name: Annotated[
            str,
            Field(
                description="Exact name of an OPEN module whose references to refresh.",
                min_length=1,
                max_length=200,
            ),
        ],
        dependency_name: Annotated[
            str | None,
            Field(
                description="Optional exact name of one module or extension to refresh. "
                "Omit to refresh every outdated reference (Refresh all).",
                min_length=1,
                max_length=200,
            ),
        ] = None,
    ) -> DependencyChangeResult:
        """Refresh outdated or missing references of an open module through Manage
        Dependencies, then Apply. status 'applied' means references were refreshed;
        'up_to_date' means nothing was outdated. Does not save or publish."""
        try:
            return await service.refresh_dependencies(module_name, dependency_name)
        except NavigatorError as error:
            raise to_tool_error(error) from error

    @mcp.tool(
        annotations={
            "title": "Find usages of an element in all modules",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def find_usages(
        module_name: Annotated[
            str,
            Field(
                description="Exact name of the OPEN module that owns the element.",
                min_length=1,
                max_length=200,
            ),
        ],
        element_name: Annotated[
            str,
            Field(
                description="Exact name of the element (action, entity, structure, block...).",
                min_length=1,
                max_length=200,
            ),
        ],
    ) -> FindUsagesResult:
        """Run Service Studio's 'Find Usages in all Modules' on an element of an open
        module and return each module that uses it (is_producer marks the owner,
        shown by Service Studio as '(local)') with its usage count, plus the
        footer totals. Fails with module_not_open, element_not_found (no single
        element with that name; for a name shared by several elements, use a
        unique one) or usages_unavailable. Does not change the module."""
        try:
            return await service.find_usages(module_name, element_name)
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
