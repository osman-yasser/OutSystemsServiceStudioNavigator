"""Provider interface. Future providers (LifeTime API, Service Center, CLI) plug in here."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from os_module_navigator.models import (
    CloseModuleResult,
    DependencyChangeResult,
    FindUsagesResult,
    OpenModuleResult,
    OpenModulesResult,
    SearchResult,
    ServiceStudioStatus,
)


@runtime_checkable
class ModuleProvider(Protocol):
    async def search_modules(self, query: str) -> SearchResult: ...

    async def open_module(self, module_name: str) -> OpenModuleResult: ...

    async def get_status(self) -> ServiceStudioStatus: ...

    async def get_open_modules(self) -> OpenModulesResult: ...

    async def close_module(self, module_name: str) -> CloseModuleResult: ...
    async def add_dependency(
        self, module_name: str, dependency_name: str, elements: list[str] | None = None
    ) -> DependencyChangeResult: ...
    async def remove_dependency(
        self, module_name: str, dependency_name: str, elements: list[str] | None = None
    ) -> DependencyChangeResult: ...
    async def find_usages_all_modules(
        self, module_name: str, element_name: str, element_type: str | None = None
    ) -> FindUsagesResult: ...
