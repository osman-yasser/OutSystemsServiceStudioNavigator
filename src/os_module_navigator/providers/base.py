"""Provider interface. Future providers (LifeTime API, Service Center, CLI) plug in here."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from os_module_navigator.models import OpenModuleResult, SearchResult, ServiceStudioStatus


@runtime_checkable
class ModuleProvider(Protocol):
    async def search_modules(self, query: str) -> SearchResult: ...

    async def open_module(self, module_name: str) -> OpenModuleResult: ...

    async def get_status(self) -> ServiceStudioStatus: ...
