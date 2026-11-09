"""Provider-agnostic module service, the layer MCP tools will call."""

from __future__ import annotations

import logging

from os_module_navigator.models import (
    CloseModuleResult,
    OpenModuleResult,
    OpenModulesResult,
    SearchResult,
    ServiceStudioStatus,
)
from os_module_navigator.providers.base import ModuleProvider

log = logging.getLogger(__name__)


class ModuleService:
    def __init__(self, provider: ModuleProvider) -> None:
        self._provider = provider

    async def search_modules(self, query: str) -> SearchResult:
        result = await self._provider.search_modules(query)
        log.info("search_modules returned %d result(s)", len(result.modules))
        return result

    async def open_module(self, module_name: str) -> OpenModuleResult:
        result = await self._provider.open_module(module_name)
        log.info("open_module finished with status %s", result.status.value)
        return result

    async def get_status(self) -> ServiceStudioStatus:
        return await self._provider.get_status()

    async def get_open_modules(self) -> OpenModulesResult:
        return await self._provider.get_open_modules()

    async def close_module(self, module_name: str) -> CloseModuleResult:
        result = await self._provider.close_module(module_name)
        log.info("close_module closed a module; %d still open", result.open_modules_count)
        return result
