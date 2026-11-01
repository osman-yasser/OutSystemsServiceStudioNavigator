"""Win32 process and window discovery.

Everything here only *reads* window state (enumeration, class names, visibility).
Nothing focuses, moves, closes or sends input to any window.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil
import win32gui
import win32process


@dataclass(frozen=True, slots=True)
class WindowHandle:
    """A Win32 window that belongs to a process we are interested in."""

    hwnd: int
    pid: int
    class_name: str
    visible: bool
    parent_hwnd: int
    is_top_level: bool


def find_process_ids(image_name: str) -> list[int]:
    """Return the PIDs of running processes whose executable name matches (case-insensitive)."""
    target = image_name.lower()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if name.lower() == target:
            pids.append(int(proc.info["pid"]))
    return sorted(pids)


def _describe(hwnd: int, *, top_level: bool) -> WindowHandle | None:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return WindowHandle(
            hwnd=hwnd,
            pid=pid,
            class_name=win32gui.GetClassName(hwnd),
            visible=bool(win32gui.IsWindowVisible(hwnd)),
            parent_hwnd=win32gui.GetParent(hwnd),
            is_top_level=top_level,
        )
    except win32gui.error:
        # The window disappeared while we were enumerating it.
        return None


def list_process_windows(pid: int, *, include_children: bool = True) -> list[WindowHandle]:
    """List the top-level windows of ``pid`` and, optionally, all their descendants.

    Window titles are deliberately not collected: they can contain module or
    environment names. Use UI Automation (with sanitization) for labels instead.
    """
    top_level: list[int] = []

    def _collect(hwnd: int, acc: list[int]) -> bool:
        _, owner = win32process.GetWindowThreadProcessId(hwnd)
        if owner == pid:
            acc.append(hwnd)
        return True

    win32gui.EnumWindows(_collect, top_level)

    result: list[WindowHandle] = []
    for hwnd in top_level:
        info = _describe(hwnd, top_level=True)
        if info is not None:
            result.append(info)
        if not include_children:
            continue
        children: list[int] = []
        try:
            win32gui.EnumChildWindows(hwnd, lambda h, acc: acc.append(h) or True, children)
        except win32gui.error:
            continue
        for child in children:
            child_info = _describe(child, top_level=False)
            if child_info is not None and child_info.pid == pid:
                result.append(child_info)
    return result
