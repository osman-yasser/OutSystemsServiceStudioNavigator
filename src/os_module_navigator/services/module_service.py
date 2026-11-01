"""Provider-agnostic module service, the layer MCP tools will call."""

from __future__ import annotations

import logging

from os_module_navigator.models import OpenModuleResult, SearchResult, ServiceStudioStatus
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
