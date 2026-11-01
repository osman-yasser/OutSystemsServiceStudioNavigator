"""Read-only lookups against the live Service Studio UI.

These helpers return fresh UIA elements; they never change UI state. Interaction
lives in ``actions.py`` only.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

import win32gui

from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.uia import ElementNode, snapshot_handle
from os_module_navigator.automation.windows import (
    WindowHandle,
    find_process_ids,
    list_process_windows,
)

warnings.filterwarnings("ignore", category=SyntaxWarning)


@dataclass(frozen=True, slots=True)
class TopWindow:
    handle: WindowHandle
    uia_class: str
    title: str

    @property
    def role(self) -> ss.WindowRole:
        return ss.classify_window(self.uia_class)


def element_info(hwnd: int) -> Any:
    from pywinauto.uia_element_info import UIAElementInfo

    return UIAElementInfo(handle_or_elem=hwnd)


def find_service_studio_pid() -> int | None:
    pids = find_process_ids(ss.PROCESS_NAME)
    return pids[0] if pids else None


def top_windows(pid: int) -> list[TopWindow]:
    """Visible top-level Avalonia windows of Service Studio (main window and dialogs)."""
    result: list[TopWindow] = []
    for window in list_process_windows(pid, include_children=False):
        if not ss.is_shell_candidate(window):
            continue
        try:
            info = element_info(window.hwnd)
            result.append(TopWindow(window, str(info.class_name), str(info.name)))
        except Exception:
            continue  # window closed while we looked at it
    return result


def child_webview(hwnd: int) -> int | None:
    """The first Chromium render widget below ``hwnd`` that is visible, else any."""
    children: list[int] = []
    try:
        win32gui.EnumChildWindows(hwnd, lambda h, acc: acc.append(h) or True, children)
    except win32gui.error:
        return None
    webviews = [h for h in children if win32gui.GetClassName(h) == ss.WEBVIEW_WIN32_CLASS]
    visible = [h for h in webviews if win32gui.IsWindowVisible(h)]
    return (visible or webviews or [None])[0]


def all_webviews(hwnd: int) -> list[int]:
    children: list[int] = []
    try:
        win32gui.EnumChildWindows(hwnd, lambda h, acc: acc.append(h) or True, children)
    except win32gui.error:
        return []
    return [h for h in children if win32gui.GetClassName(h) == ss.WEBVIEW_WIN32_CLASS]


def snapshot_shell(hwnd: int) -> ElementNode:
    # Only the tab strip is needed; the shell tree is small (about 20 elements).
    return snapshot_handle(hwnd, max_depth=6, max_elements=200, timeout_s=5)


def snapshot_webview(hwnd: int, *, warmup_timeout_s: float = 3.0) -> ElementNode:
    return snapshot_handle(
        hwnd, max_depth=40, max_elements=3000, timeout_s=10, warmup_timeout_s=warmup_timeout_s
    )


def find_first(root_hwnd: int, *, control_type: str, title: str | None = None) -> Any | None:
    """First descendant matching control type (and exact name) via UIA FindAll."""
    kwargs: dict[str, Any] = {"control_type": control_type}
    if title is not None:
        kwargs["title"] = title
    try:
        matches = element_info(root_hwnd).descendants(**kwargs)
    except Exception:
        return None
    return matches[0] if matches else None


def shell_tab_items(main_hwnd: int) -> list[Any]:
    """Live TabItem elements of the shell tab strip, in display order."""
    tabs = find_first(main_hwnd, control_type="Tab")
    if tabs is None:
        return []
    try:
        return list(tabs.children(control_type="TabItem"))
    except Exception:
        return []


def foreground_window() -> int:
    return int(win32gui.GetForegroundWindow())
