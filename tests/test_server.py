import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from os_module_navigator.errors import (
    AmbiguousModuleError,
    ModuleNotFoundInEnvironmentError,
    ServiceStudioNotRunningError,
    TooManyOpenModulesError,
    UnsavedChangesError,
)
from os_module_navigator.models import (
    CloseModuleResult,
    ModuleInfo,
    OpenModule,
    OpenModuleResult,
    OpenModulesResult,
    OpenStatus,
    SearchResult,
    ServiceStudioStatus,
)
from os_module_navigator.server import build_server
from os_module_navigator.services.module_service import ModuleService


class FakeProvider:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def search_modules(self, query):
        self.calls.append(("search", query))
        if self.error:
            raise self.error
        return SearchResult(query=query, modules=[ModuleInfo(name="DGA_UI", application="DGA UI")])

    async def open_module(self, module_name):
        self.calls.append(("open", module_name))
        if self.error:
            raise self.error
        return OpenModuleResult(
            module_name=module_name, status=OpenStatus.OPENED_VERIFIED, message="ok"
        )

    async def get_status(self):
        if self.error:
            raise self.error
        return ServiceStudioStatus(running=True, window_detected=True, automation_backend="fake")

    async def get_open_modules(self):
        if self.error:
            raise self.error
        return OpenModulesResult(
            count=1, limit=10, can_open_more=True, modules=[OpenModule(name="DGA_UI")]
        )

    async def close_module(self, module_name):
        self.calls.append(("close", module_name))
        if self.error:
            raise self.error
        return CloseModuleResult(module_name=module_name, message="ok", open_modules_count=0)


def call(provider, tool, args=None):
    async def run():
        async with Client(build_server(ModuleService(provider))) as client:
            return await client.call_tool(tool, args or {})

    return asyncio.run(run())


def error_payload(provider, tool, args):
    with pytest.raises(ToolError) as err:
        call(provider, tool, args)
    return json.loads(str(err.value))


def test_tools_are_listed_with_annotations():
    async def run():
        async with Client(build_server(ModuleService(FakeProvider()))) as client:
            return {t.name: t for t in await client.list_tools()}

    tools = asyncio.run(run())
    assert set(tools) == {
        "search_modules",
        "open_module",
        "get_service_studio_status",
        "get_open_modules",
        "close_module",
    }
    assert tools["get_open_modules"].annotations.read_only_hint is True
    assert tools["close_module"].input_schema["required"] == ["module_name"]
    assert tools["search_modules"].annotations.read_only_hint is True
    assert tools["open_module"].annotations.destructive_hint is False
    assert tools["open_module"].input_schema["required"] == ["module_name"]


def test_search_returns_structured_result():
    provider = FakeProvider()
    result = call(provider, "search_modules", {"query": "DGA"})
    assert result.structured_content["modules"][0]["name"] == "DGA_UI"
    assert provider.calls == [("search", "DGA")]


def test_open_and_status():
    assert (
        call(FakeProvider(), "open_module", {"module_name": "DGA_UI"}).structured_content["status"]
        == "opened_verified"
    )
    status = call(FakeProvider(), "get_service_studio_status").structured_content
    assert status["running"] is True


def test_empty_arguments_are_rejected_by_schema():
    provider = FakeProvider()
    with pytest.raises(ToolError):
        call(provider, "open_module", {"module_name": ""})
    assert provider.calls == []


def test_navigator_errors_become_tool_errors_with_codes():
    payload = error_payload(
        FakeProvider(ServiceStudioNotRunningError("Service Studio is not running.")),
        "get_service_studio_status",
        {},
    )
    assert payload == {
        "code": "service_studio_not_running",
        "message": "Service Studio is not running.",
    }


def test_ambiguity_and_not_found_include_candidates():
    ambiguous = AmbiguousModuleError(
        "2 modules", [ModuleInfo(name="A", application="X"), ModuleInfo(name="a", application="Y")]
    )
    payload = error_payload(FakeProvider(ambiguous), "open_module", {"module_name": "A"})
    assert payload["code"] == "ambiguous_module"
    assert payload["candidates"] == [
        {"name": "A", "application": "X"},
        {"name": "a", "application": "Y"},
    ]

    missing = ModuleNotFoundInEnvironmentError("nope", [ModuleInfo(name="DGA_UI")])
    payload = error_payload(FakeProvider(missing), "open_module", {"module_name": "DGA_U"})
    assert payload["suggestions"] == [{"name": "DGA_UI", "application": None}]


def test_unexpected_errors_are_masked():
    with pytest.raises(ToolError) as err:
        call(FakeProvider(RuntimeError("secret internal detail")), "search_modules", {"query": "x"})
    assert "secret internal detail" not in str(err.value)


def test_open_modules_and_close():
    result = call(FakeProvider(), "get_open_modules").structured_content
    assert result["count"] == 1 and result["can_open_more"] is True
    assert result["modules"][0]["name"] == "DGA_UI"
    provider = FakeProvider()
    closed = call(provider, "close_module", {"module_name": "DGA_UI"}).structured_content
    assert closed["status"] == "closed"
    assert provider.calls == [("close", "DGA_UI")]


def test_limit_and_unsaved_errors():
    too_many = TooManyOpenModulesError("10 open", open_modules=["A", "B"])
    payload = error_payload(FakeProvider(too_many), "open_module", {"module_name": "C"})
    assert payload["code"] == "too_many_open_modules"
    assert payload["open_modules"] == ["A", "B"]
    payload = error_payload(
        FakeProvider(UnsavedChangesError("unsaved")), "close_module", {"module_name": "A"}
    )
    assert payload["code"] == "module_has_unsaved_changes"


def test_instructions_tell_agent_about_the_limit():
    server = build_server(ModuleService(FakeProvider()))
    assert "get_open_modules" in server.instructions
    assert "close_module" in server.instructions
    assert "10" in server.instructions
