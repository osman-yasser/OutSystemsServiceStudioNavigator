"""Fake UIA element infos for tests (no Windows UI needed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeRect:
    left: int = 0
    top: int = 0
    right: int = 10
    bottom: int = 10


@dataclass
class FakeComElement:
    properties: dict[int, Any] = field(default_factory=dict)

    def GetCurrentPropertyValue(self, property_id: int) -> Any:
        return self.properties.get(property_id, False)


@dataclass
class FakeInfo:
    control_type: str = "Pane"
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    framework_id: str = ""
    enabled: bool = True
    visible: bool = True
    rectangle: Any = field(default_factory=FakeRect)
    kids: list[FakeInfo] = field(default_factory=list)
    element: FakeComElement | None = None

    def children(self) -> list[FakeInfo]:
        return self.kids


class BrokenInfo(FakeInfo):
    """An element whose properties throw, like a COM element that just vanished."""

    @property
    def name(self) -> str:  # type: ignore[override]
        raise RuntimeError("element not available")

    @name.setter
    def name(self, value: str) -> None:
        pass

    def children(self) -> list[FakeInfo]:
        raise RuntimeError("element not available")
