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
    ElementNotFoundError,
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
remove_dependency does the opposite. To refresh existing references use the
official omlRefreshReferences tool. After adding, call the official listApps
to confirm the new reference. Do not call these in the middle of an
applyModelApiCode chain: merge first, then change dependencies.
Manage Dependencies can take minutes to load ("Processing... 44%"): the tools wait
for it to finish and never close it while it loads. Call ONE dependency tool at a
time and wait for its result; never run two in parallel or retry while one is
still running.
FINDING USAGES IN ALL MODULES (the official MCP's Referrers only sees the open
module): find_usages_all_modules(module_name, element_name, element_type=None)
runs Service Studio's 'Find Usages in all Modules' (Ctrl+F12) on an element of
an OPEN module (open_module first) and returns [{module, usages}] - '(local)' is
the module itself, usages=0 ('No usages') means the consumer only keeps a dead
reference. It returns counts only, not the exact places. Typical flow: call it,
then for each name in modules_with_usages: get_open_modules / open_module, and
use the official MCP's Referrers on the element there to get the exact nodes.
Consumers are analysed as published on the server (they need not be open), so
unpublished changes in a consumer are not seen. Pass element_type ('Block',
'Server Action', ...) when names clash. It uses Ctrl+F, Shift+F10 and Enter
in Service Studio: do not type in Service Studio while it runs, and call it
one at a time.
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
    if isinstance(error, ElementNotFoundError) and error.suggestions:
        payload["suggestions"] = error.suggestions
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
            "title": "Find usages of an element in all modules",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def find_usages_all_modules(
        module_name: Annotated[
            str,
            Field(
                description="Exact name of the OPEN module that defines the element "
                "(e.g. 'MR_CW'). Use open_module first.",
                min_length=1,
                max_length=200,
            ),
        ],
        element_name: Annotated[
            str,
            Field(
                description="Exact name of the element (block, screen, action, entity, "
                "structure, ...), e.g. 'WB_AdditionalInfo_GeoLocation'.",
                min_length=1,
                max_length=200,
            ),
        ],
        element_type: Annotated[
            str | None,
            Field(
                description="Optional type as Service Studio names it ('Block', 'Screen', "
                "'Server Action', 'Client Action', 'Entity', 'Structure', ...). Use it "
                "when several elements share the name.",
                max_length=100,
            ),
        ] = None,
    ) -> FindUsagesResult:
        """Run Service Studio's 'Find Usages in all Modules' (Ctrl+F12) on one element
        of an open module and return the usages table: one row per module that
        references it, with the number of usages ('(local)' is the module itself;
        'No usages' = 0, i.e. a dead reference). modules_with_usages lists the
        consumers with usages > 0: open those with open_module and use the official
        MCP (Referrers) to find the exact places. Only counts are returned, not the
        places. Takes a few seconds to a few minutes. Does not change anything."""
        try:
            return await service.find_usages_all_modules(module_name, element_name, element_type)
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
