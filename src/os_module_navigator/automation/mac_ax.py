"""Read-only access to Service Studio for macOS through the Accessibility (AX) API.

Observed structure (Service Studio 11.55 for Mac, Avalonia + CEF; docs/macos.md):

* Every Service Studio window is an ``AXWindow`` of the app. The main window
  contains an ``AXTabGroup`` with identifier ``tabs``; its tabs are
  ``AXRadioButton`` elements whose ``AXTitle`` is the .NET view type (as on
  Windows) and whose caption is a descendant ``AXStaticText`` *value*. Module
  tabs also contain one ``AXButton``: the tab's close button.
* Dialogs (``Progress``, ``Open from Environment``, ...) are separate windows
  titled with the dialog name.
* Tab content is embedded Chromium and is **not** exposed to AX on macOS.

Nothing here changes UI state. Input lives in ``mac_actions.py`` only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from os_module_navigator.automation import service_studio as ss
from os_module_navigator.errors import PermissionMissingError

BUNDLE_ID = "com.outsystems.servicestudio"
TABS_IDENTIFIER = "tabs"
MAX_WALK = 2000


def ax() -> Any:
    import ApplicationServices

    return ApplicationServices


def attr(element: Any, name: str) -> Any:
    err, value = ax().AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def frame(element: Any) -> tuple[float, float, float, float] | None:
    value = attr(element, "AXFrame")
    if value is None:
        return None
    ok, rect = ax().AXValueGetValue(value, ax().kAXValueCGRectType, None)
    if not ok:
        return None
    return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height)


def center(element: Any) -> tuple[float, float] | None:
    f = frame(element)
    return None if f is None else (f[0] + f[2] / 2, f[1] + f[3] / 2)


def children(element: Any) -> list[Any]:
    return list(attr(element, "AXChildren") or [])


def walk(element: Any, *, limit: int = MAX_WALK) -> Iterator[Any]:
    """Depth-first descendants (including ``element``), bounded."""
    stack = [element]
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        yield node
        stack.extend(reversed(children(node)))


def role(element: Any) -> str:
    return str(attr(element, "AXRole") or "")


def text_of(element: Any) -> str:
    value = attr(element, "AXValue")
    return value if isinstance(value, str) else ""


# --- permissions and process ---------------------------------------------------


def require_accessibility() -> None:
    if not ax().AXIsProcessTrusted():
        raise PermissionMissingError(
            "macOS has not granted Accessibility access to the app running this MCP server "
            "(e.g. Visual Studio Code, Terminal or iTerm). Enable it in System Settings > "
            "Privacy & Security > Accessibility, then restart that app."
        )


def require_screen_capture() -> None:
    import Quartz

    if not Quartz.CGPreflightScreenCaptureAccess():
        raise PermissionMissingError(
            "macOS has not granted Screen Recording access to the app running this MCP server; "
            "it is needed to read the 'Open from Environment' results. Enable it in System "
            "Settings > Privacy & Security > Screen & System Audio Recording, then restart "
            "that app."
        )


def running_app() -> Any | None:
    from AppKit import NSRunningApplication

    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    live = [a for a in apps or [] if not a.isTerminated()]
    return live[0] if live else None


def find_pid() -> int | None:
    app = running_app()
    return int(app.processIdentifier()) if app is not None else None


def app_element(pid: int) -> Any:
    return ax().AXUIElementCreateApplication(pid)


def is_frontmost(pid: int) -> bool:
    # Read live through AX. NSWorkspace.frontmostApplication is only refreshed by
    # run-loop notifications, which never arrive on the server's worker thread.
    return bool(attr(app_element(pid), "AXFrontmost"))


def focused_window(pid: int) -> Any | None:
    return attr(app_element(pid), "AXFocusedWindow")


# --- windows and tabs ------------------------------------------------------------


@dataclass
class MacWindow:
    element: Any
    title: str
    role: ss.WindowRole
    frame: tuple[float, float, float, float] | None
    tab_strip: Any | None = field(default=None, repr=False)


def _tab_strip(window: Any) -> Any | None:
    """The shallowest ``AXTabGroup`` with identifier ``tabs`` (the shell tab strip)."""
    level = [window]
    for _ in range(12):
        next_level = []
        for node in level:
            if role(node) == "AXTabGroup" and attr(node, "AXIdentifier") == TABS_IDENTIFIER:
                return node
            next_level.extend(children(node))
        if not next_level:
            return None
        level = next_level
    return None


def windows(pid: int) -> list[MacWindow]:
    """Service Studio windows: main windows (with a shell tab strip) and dialogs."""
    result: list[MacWindow] = []
    for window in attr(app_element(pid), "AXWindows") or []:
        title = str(attr(window, "AXTitle") or "")
        strip = _tab_strip(window)
        if strip is not None:
            window_role = ss.WindowRole.MAIN
        elif title and attr(window, "AXSubrole") in ("AXStandardWindow", "AXDialog"):
            window_role = ss.WindowRole.DIALOG
        else:
            window_role = ss.WindowRole.OTHER
        result.append(MacWindow(window, title, window_role, frame(window), strip))
    return result


def main_windows(pid: int) -> list[MacWindow]:
    """Main windows in a stable order (AX lists windows in z-order, which changes)."""
    mains = [w for w in windows(pid) if w.role is ss.WindowRole.MAIN]
    return sorted(mains, key=lambda w: w.frame or (0, 0, 0, 0))


def tab_items(strip: Any) -> list[Any]:
    """The strip's tabs in display order, not descending into tab content."""
    items: list[Any] = []
    stack = list(reversed(children(strip)))
    while stack:
        node = stack.pop()
        if role(node) == "AXRadioButton":
            items.append(node)
            continue
        if attr(node, "AXIdentifier") == "PART_SelectedContentHost":
            continue
        stack.extend(reversed(children(node)))
    return items


def tab_caption(item: Any) -> tuple[Any | None, str]:
    for node in walk(item, limit=50):
        if role(node) == "AXStaticText" and text_of(node):
            return node, text_of(node)
    return None, ""


def tab_close_buttons(item: Any) -> list[Any]:
    return [n for n in walk(item, limit=50) if role(n) == "AXButton"]


def read_tab(item: Any) -> ss.ShellTab:
    type_name = str(attr(item, "AXTitle") or "")
    _, caption = tab_caption(item)
    return ss.ShellTab(
        type_name=type_name,
        kind=ss.classify_tab_type(type_name),
        selected=bool(attr(item, "AXSelected")),
        caption=caption,
        unsaved_marker=caption.lstrip().startswith(ss.UNSAVED_MARKER),
    )


def element_at(x: float, y: float) -> Any | None:
    """The topmost element at a screen point, across all apps."""
    err, element = ax().AXUIElementCopyElementAtPosition(
        ax().AXUIElementCreateSystemWide(), x, y, None
    )
    return element if err == 0 else None


def window_number(pid: int, title: str) -> int | None:
    """CoreGraphics window id of an on-screen Service Studio window with this title."""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    for info in infos or []:
        if info.get("kCGWindowOwnerPID") == pid and info.get("kCGWindowName") == title:
            return int(info["kCGWindowNumber"])
    return None


def is_menu_window(window: MacWindow) -> bool:
    """True for an untitled popup holding menu items (Service Studio's context menus)."""
    if window.role is not ss.WindowRole.OTHER or window.title:
        return False
    return any(role(node) == "AXMenuItem" for node in walk(window.element, limit=80))


def menu_windows(pid: int) -> list[MacWindow]:
    return [w for w in windows(pid) if is_menu_window(w)]


def window_number_at(pid: int, frame_: tuple[float, float, float, float]) -> int | None:
    """CoreGraphics window id of the on-screen Service Studio window with these bounds."""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    for info in infos or []:
        if info.get("kCGWindowOwnerPID") != pid:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        found = (bounds.get("X"), bounds.get("Y"), bounds.get("Width"), bounds.get("Height"))
        pairs = zip(found, frame_, strict=True)
        if all(a is not None and abs(float(a) - b) < 1.5 for a, b in pairs):
            return int(info["kCGWindowNumber"])
    return None
