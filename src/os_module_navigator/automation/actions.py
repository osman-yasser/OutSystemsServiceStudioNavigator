"""The only module allowed to interact with the GUI.

Allowed interactions, all scoped to Service Studio windows:

* UIA ``SetFocus`` on an element (brings its window to the front),
* UIA ``Invoke`` on a button,
* UIA ``ValuePattern.SetValue`` on a search box,
* UIA ``SelectionItem.Select`` on a shell tab,
* UIA ``Invoke`` on a module tab's own close button, only when the tab's live
  caption names the expected module and shows no unsaved-changes marker,
* navigation keys (Up/Down) and Ctrl+O, only after confirming that the
  expected Service Studio window is the foreground window.

Never: Enter, Delete, typing free text, mouse input, closing windows, closing
modules with unsaved changes, saving or publishing.
``tests/test_read_only_guard.py`` keeps interaction calls out of
every other module.
"""

from __future__ import annotations

import time
from typing import Any

from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.live import foreground_window
from os_module_navigator.errors import FocusError, NavigatorError, UnsavedChangesError

# Keys this module may send. Anything else is refused.
ALLOWED_KEYS: frozenset[str] = frozenset({"{UP}", "{DOWN}", "^o"})


def _iface(element: Any, pattern: str) -> Any:
    from pywinauto.uia_defines import NoPatternInterfaceError, get_elem_interface

    try:
        return get_elem_interface(element.element, pattern)
    except NoPatternInterfaceError as exc:
        raise NavigatorError(f"Element does not support the {pattern} pattern.") from exc


def set_focus(element: Any) -> None:
    try:
        element.element.SetFocus()
    except Exception as exc:
        raise FocusError("Could not focus the Service Studio element.") from exc


def invoke(element: Any) -> None:
    if not element.enabled:
        raise NavigatorError(f"Button {element.name!r} is disabled.")
    _iface(element, "Invoke").Invoke()


def set_value(element: Any, text: str) -> None:
    _iface(element, "Value").SetValue(text)


def select_item(element: Any) -> None:
    _iface(element, "SelectionItem").Select()


def close_module_tab(tab_item: Any, module_name: str) -> None:
    """Press the close button of a module editor tab after re-checking it live.

    Refuses unless the tab is a module editor tab whose caption names
    ``module_name`` and shows no unsaved-changes marker, so Service Studio never
    asks whether to save (which would risk losing work).
    """
    if (
        tab_item.control_type != "TabItem"
        or ss.classify_tab_type(str(tab_item.name)) is not ss.ShellTabKind.MODULE_EDITOR
    ):
        raise NavigatorError("That Service Studio tab is not a module editor tab.")
    children = list(tab_item.children())
    caption_text = next((str(c.name) for c in children if c.control_type == "Text" and c.name), "")
    caption = ss.parse_module_caption(caption_text)
    if caption is None or not ss.same_module_name(caption.name, module_name):
        raise NavigatorError("The Service Studio tabs changed; nothing was closed. Retry.")
    if caption.unsaved:
        raise UnsavedChangesError(
            f"{caption.name!r} has unsaved changes; it was not closed. "
            "Save it in Service Studio first, or close another module."
        )
    buttons = [c for c in children if c.control_type == "Button"]
    if len(buttons) != 1:
        raise NavigatorError("The module tab's close button was not found; nothing was closed.")
    invoke(buttons[0])


def _foreground_class() -> str:
    import win32gui

    try:
        return str(win32gui.GetClassName(foreground_window()))
    except Exception:
        return "unknown"


def _wait_foreground(hwnd: int, timeout_s: float, stable_s: float) -> bool:
    """True once ``hwnd`` has been the foreground window continuously for ``stable_s``."""
    deadline = time.monotonic() + timeout_s
    since: float | None = None
    while time.monotonic() < deadline:
        now = time.monotonic()
        if foreground_window() == hwnd:
            since = now if since is None else since
            if now - since >= stable_s:
                return True
        else:
            since = None
        time.sleep(0.03)
    return False


def ensure_foreground(
    hwnd: int, focus_target: Any, *, timeout_s: float = 3.0, stable_s: float = 0.3
) -> None:
    """Make ``hwnd`` the foreground window (focusing ``focus_target``) and wait until it sticks.

    Windows can briefly flip the foreground while another app hands over focus,
    so we require it to stay on ``hwnd`` for ``stable_s`` before continuing.
    """
    if foreground_window() != hwnd:
        set_focus(focus_target)
    if not _wait_foreground(hwnd, timeout_s, stable_s):
        raise FocusError(
            "Service Studio did not stay in the foreground (foreground class: "
            f"{_foreground_class()}); keyboard input was not sent."
        )


def press_keys(
    hwnd: int, keys: list[str], *, delay_s: float = 0.15, regain_timeout_s: float = 1.0
) -> None:
    """Send allowed keys one by one, re-checking the foreground window before each.

    A brief foreground flicker is waited out (nothing is sent meanwhile); if
    ``hwnd`` does not return within ``regain_timeout_s`` we stop.
    """
    from pywinauto import keyboard

    for key in keys:
        if key not in ALLOWED_KEYS:
            raise NavigatorError(f"Key {key!r} is not allowed.")
        if foreground_window() != hwnd and not _wait_foreground(hwnd, regain_timeout_s, 0.1):
            raise FocusError(
                "Service Studio lost the foreground (foreground class: "
                f"{_foreground_class()}); stopped sending keys."
            )
        keyboard.send_keys(key)
        time.sleep(delay_s)
