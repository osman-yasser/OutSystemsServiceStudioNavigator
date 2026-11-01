"""Read-only snapshots of UI Automation trees.

The walker only reads element properties and children. It never calls any
control pattern method, never changes focus and never sends input, so taking a
snapshot cannot change Service Studio state. (tests/test_read_only_guard.py
enforces this for the whole package.)
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

# UIA "Is<Pattern>PatternAvailable" property ids. Reading these is side-effect free.
PATTERN_AVAILABILITY_PROPERTIES: dict[str, int] = {
    "ExpandCollapse": 30028,
    "Invoke": 30031,
    "Scroll": 30034,
    "ScrollItem": 30035,
    "SelectionItem": 30036,
    "Selection": 30037,
    "Text": 30040,
    "Toggle": 30041,
    "Value": 30043,
}
IS_KEYBOARD_FOCUSABLE_PROPERTY = 30009
SELECTION_ITEM_IS_SELECTED_PROPERTY = 30079


class Rect(BaseModel):
    left: int
    top: int
    width: int
    height: int


class ElementNode(BaseModel):
    """One UI Automation element and its descendants."""

    control_type: str = ""
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    framework_id: str = ""
    enabled: bool = True
    visible: bool = True
    keyboard_focusable: bool = False
    selected: bool | None = None
    rect: Rect | None = None
    patterns: list[str] = Field(default_factory=list)
    children: list[ElementNode] = Field(default_factory=list)
    children_truncated: bool = False

    def iter_nodes(self) -> Iterator[ElementNode]:
        """Depth-first iteration over this node and all descendants."""
        stack: list[ElementNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def count(self) -> int:
        return sum(1 for _ in self.iter_nodes())

    def control_type_counts(self) -> Counter[str]:
        return Counter(node.control_type for node in self.iter_nodes())


class ElementInfoLike(Protocol):
    """The subset of ``pywinauto.uia_element_info.UIAElementInfo`` we rely on."""

    @property
    def name(self) -> str: ...
    @property
    def control_type(self) -> str: ...
    @property
    def automation_id(self) -> str: ...
    @property
    def class_name(self) -> str: ...
    @property
    def framework_id(self) -> str: ...
    @property
    def enabled(self) -> bool: ...
    @property
    def visible(self) -> bool: ...
    @property
    def rectangle(self) -> Any: ...

    def children(self) -> Sequence[ElementInfoLike]: ...


def _safe(read: Callable[[], Any], default: Any) -> Any:
    try:
        value = read()
    except Exception:
        return default
    return default if value is None else value


def _read_property(info: ElementInfoLike, property_id: int) -> Any:
    element = getattr(info, "element", None)
    if element is None:
        return None
    return _safe(lambda: element.GetCurrentPropertyValue(property_id), None)


def _read_patterns(info: ElementInfoLike) -> list[str]:
    return [
        pattern
        for pattern, property_id in PATTERN_AVAILABILITY_PROPERTIES.items()
        if _read_property(info, property_id)
    ]


def _read_rect(info: ElementInfoLike) -> Rect | None:
    rect = _safe(lambda: info.rectangle, None)
    if rect is None:
        return None
    try:
        return Rect(
            left=int(rect.left),
            top=int(rect.top),
            width=int(rect.right - rect.left),
            height=int(rect.bottom - rect.top),
        )
    except (AttributeError, TypeError, ValueError):
        return None


class _Budget:
    def __init__(self, max_elements: int, deadline: float) -> None:
        self.remaining = max_elements
        self.deadline = deadline

    def exhausted(self) -> bool:
        return self.remaining <= 0 or time.monotonic() > self.deadline


def _snapshot(info: ElementInfoLike, depth: int, max_depth: int, budget: _Budget) -> ElementNode:
    budget.remaining -= 1
    patterns = _read_patterns(info)
    selected: bool | None = None
    if "SelectionItem" in patterns:
        selected = bool(_read_property(info, SELECTION_ITEM_IS_SELECTED_PROPERTY))
    node = ElementNode(
        control_type=str(_safe(lambda: info.control_type, "")),
        name=str(_safe(lambda: info.name, "")),
        automation_id=str(_safe(lambda: info.automation_id, "")),
        class_name=str(_safe(lambda: info.class_name, "")),
        framework_id=str(_safe(lambda: info.framework_id, "")),
        enabled=bool(_safe(lambda: info.enabled, True)),
        visible=bool(_safe(lambda: info.visible, True)),
        keyboard_focusable=bool(_read_property(info, IS_KEYBOARD_FOCUSABLE_PROPERTY)),
        selected=selected,
        rect=_read_rect(info),
        patterns=patterns,
    )
    children: Sequence[ElementInfoLike] = _safe(info.children, [])
    if not children:
        return node
    if depth >= max_depth:
        node.children_truncated = True
        return node
    for child in children:
        if budget.exhausted():
            node.children_truncated = True
            break
        node.children.append(_snapshot(child, depth + 1, max_depth, budget))
    return node


def snapshot_tree(
    root: ElementInfoLike,
    *,
    max_depth: int = 60,
    max_elements: int = 5000,
    timeout_s: float = 30.0,
) -> ElementNode:
    """Take a bounded, read-only snapshot of ``root`` and its descendants."""
    budget = _Budget(max_elements, time.monotonic() + timeout_s)
    return _snapshot(root, 0, max_depth, budget)


def element_info_from_handle(hwnd: int) -> ElementInfoLike:
    """Build a pywinauto UIA element for a native window handle."""
    from pywinauto.uia_element_info import UIAElementInfo

    return UIAElementInfo(handle_or_elem=hwnd)


def snapshot_handle(
    hwnd: int,
    *,
    max_depth: int = 60,
    max_elements: int = 5000,
    timeout_s: float = 30.0,
    warmup_timeout_s: float = 0.0,
    poll_interval_s: float = 0.5,
    factory: Callable[[int], ElementInfoLike] = element_info_from_handle,
) -> ElementNode:
    """Snapshot the UIA tree for a window handle.

    Chromium (CEF) only builds its accessibility tree after the first UIA query,
    so the first snapshot of a web view is often a single empty ``Document``.
    With ``warmup_timeout_s > 0`` we re-query until the tree has content or the
    warm-up time runs out.
    """
    deadline = time.monotonic() + max(warmup_timeout_s, 0.0)
    while True:
        node = snapshot_tree(
            factory(hwnd), max_depth=max_depth, max_elements=max_elements, timeout_s=timeout_s
        )
        if node.count() > 2 or time.monotonic() >= deadline:
            return node
        time.sleep(poll_interval_s)
