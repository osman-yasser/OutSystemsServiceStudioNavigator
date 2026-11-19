"""The only macOS module allowed to interact with the GUI (the macOS ``actions.py``).

Allowed interactions, all scoped to Service Studio:

* bring Service Studio to the front and raise one of its windows,
* keys posted to the Service Studio process only (``CGEventPostToPid``), after
  confirming Service Studio is frontmost with the expected window focused:
  Up, Down, Cmd+O, Escape (cancels a dialog), and in the search dialogs ("Open
  from Environment", "Manage Dependencies") only: Cmd+A followed by the search
  text. Cmd+Q is never sent (it quits Service Studio),
* Return in the search dialog, only through ``confirm_open`` and only right
  after the caller re-verified (from a fresh screenshot) that the highlighted
  row is the requested module. On macOS the dialog's Open button is Chromium
  content that AX cannot press, so Return replaces UIA Invoke on Windows,
* one left click on a shell tab's caption, only when a system-wide hit test
  at that point returns exactly that caption element (Avalonia exposes no AX
  action to select a tab and ignores mouse events posted to its process); the
  pointer is moved back afterwards,
* one left click at a point inside a Service Studio window (``click_in_window``),
  only when a system-wide hit test at that point returns an element of exactly
  that window. Used for the module toolbar's Manage Dependencies button, the
  Manage Dependencies dialog's checkboxes, filters and buttons, and the module
  search drop-down's "Search in this Module" row (Chromium content AX cannot
  press); callers locate and re-verify targets from screenshots,
* Cmd+F in the main window, then Cmd+A and the element name, only through
  ``type_module_search`` (the module search box opened by Cmd+F),
* one right click on the elements tree (``right_click_in_window``), with the same
  hit test as ``click_in_window``; it only opens a context menu,
* one left click on a context menu item (``click_menu_item``), only when the hit
  test returns an ``AXMenuItem`` of that menu window. Callers read the item's text
  from a screenshot first; only "Find Usages in all Modules" is clicked,
* AXPress on a module tab's own close button, only when the tab's live caption
  names the expected module and shows no unsaved-changes marker.

Never: typing outside the search box, Delete, saving, publishing, closing
windows, or closing modules with unsaved changes.
``tests/test_read_only_guard.py`` keeps interaction calls out of every other module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from os_module_navigator.automation import mac_ax
from os_module_navigator.automation import service_studio as ss
from os_module_navigator.errors import FocusError, NavigatorError, UnsavedChangesError

# macOS virtual key codes.
KEY_A = 0x00
KEY_F = 0x03
KEY_O = 0x1F
KEY_RETURN = 0x24
KEY_ESCAPE = 0x35
KEY_DOWN = 0x7D
KEY_UP = 0x7E

# Named keys ``press_keys`` may send; anything else is refused.
ALLOWED_KEYS: dict[str, tuple[int, str]] = {
    "up": (KEY_UP, ""),
    "down": (KEY_DOWN, ""),
    "cmd+o": (KEY_O, "cmd"),
    "cmd+f": (KEY_F, "cmd"),
    "escape": (KEY_ESCAPE, ""),
}
MAX_TYPED_CHARS = 200
SEARCH_DIALOG_TITLES = frozenset({ss.OPEN_FROM_ENVIRONMENT_TITLE, ss.MANAGE_DEPENDENCIES_TITLE})


def _quartz() -> Any:
    import Quartz

    return Quartz


def _post_key(pid: int, keycode: int, modifiers: str = "", text: str | None = None) -> None:
    q = _quartz()
    flags = q.kCGEventFlagMaskCommand if modifiers == "cmd" else 0
    for down in (True, False):
        event = q.CGEventCreateKeyboardEvent(None, keycode, down)
        if text is not None:
            q.CGEventKeyboardSetUnicodeString(event, len(text), text)
        q.CGEventSetFlags(event, flags)
        q.CGEventPostToPid(pid, event)
        time.sleep(0.02)


# --- focus -------------------------------------------------------------------------


def activate(pid: int) -> None:
    """Ask macOS to bring Service Studio to the front."""
    app = mac_ax.running_app()
    if app is not None:
        from AppKit import NSApplicationActivateIgnoringOtherApps

        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    mac_ax.ax().AXUIElementSetAttributeValue(mac_ax.app_element(pid), "AXFrontmost", True)


def raise_window(window: Any) -> None:
    mac_ax.ax().AXUIElementPerformAction(window, "AXRaise")
    mac_ax.ax().AXUIElementSetAttributeValue(window, "AXMain", True)


def _has_focus(pid: int, window: Any) -> bool:
    if not mac_ax.is_frontmost(pid):
        return False
    focused = mac_ax.focused_window(pid)
    return focused is not None and focused == window


def _wait_focus(pid: int, window: Any, timeout_s: float, stable_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    since: float | None = None
    while time.monotonic() < deadline:
        now = time.monotonic()
        if _has_focus(pid, window):
            since = now if since is None else since
            if now - since >= stable_s:
                return True
        else:
            since = None
        time.sleep(0.03)
    return False


def ensure_focus(pid: int, window: Any, *, timeout_s: float = 3.0, stable_s: float = 0.3) -> None:
    """Make Service Studio frontmost with ``window`` focused, and wait until it sticks."""
    if not _has_focus(pid, window):
        activate(pid)
        raise_window(window)
    if not _wait_focus(pid, window, timeout_s, stable_s):
        raise FocusError(
            "Service Studio did not stay in the foreground with the expected window focused; "
            "keyboard input was not sent."
        )


def _check_focus(pid: int, window: Any, regain_timeout_s: float = 1.0) -> None:
    if not _has_focus(pid, window) and not _wait_focus(pid, window, regain_timeout_s, 0.1):
        raise FocusError("Service Studio lost the foreground; stopped sending keys.")


# --- keyboard ----------------------------------------------------------------------


def press_keys(pid: int, window: Any, keys: list[str], *, delay_s: float = 0.15) -> None:
    """Send allowed keys one by one, re-checking focus before each."""
    for key in keys:
        if key not in ALLOWED_KEYS:
            raise NavigatorError(f"Key {key!r} is not allowed.")
        _check_focus(pid, window)
        code, modifiers = ALLOWED_KEYS[key]
        _post_key(pid, code, modifiers)
        time.sleep(delay_s)


def type_search_text(pid: int, dialog: Any, text: str) -> None:
    """Replace the focused search box's text (Cmd+A, then the text)."""
    if str(mac_ax.attr(dialog, "AXTitle") or "") not in SEARCH_DIALOG_TITLES:
        raise NavigatorError("Text may only be typed into Service Studio's search dialogs.")
    if len(text) > MAX_TYPED_CHARS or any(ord(ch) < 32 for ch in text):
        raise NavigatorError("The search text is too long or contains control characters.")
    _check_focus(pid, dialog)
    _post_key(pid, KEY_A, "cmd")
    time.sleep(0.05)
    for ch in text:
        _check_focus(pid, dialog)
        _post_key(pid, 0, text=ch)


def type_module_search(pid: int, window: Any, text: str) -> None:
    """Replace the module search box's text (Cmd+A, then the text) in the main window.

    The caller opens the box with Cmd+F first and checks the screenshot afterwards.
    """
    if mac_ax.role(window) != "AXWindow" or str(mac_ax.attr(window, "AXTitle") or "") != (
        ss.MAIN_WINDOW_TITLE
    ):
        raise NavigatorError("The module search text may only be typed into the main window.")
    if len(text) > MAX_TYPED_CHARS or any(ord(ch) < 32 or ch.isspace() for ch in text):
        raise NavigatorError(
            "An element name is too long or contains spaces or control characters."
        )
    _check_focus(pid, window)
    _post_key(pid, KEY_A, "cmd")
    time.sleep(0.05)
    for ch in text:
        _check_focus(pid, window)
        _post_key(pid, 0, text=ch)


def confirm_open(pid: int, dialog: Any, verify: Callable[[], bool]) -> None:
    """Press Return in the search dialog, only if ``verify()`` confirms the selected row."""
    if str(mac_ax.attr(dialog, "AXTitle") or "") != ss.OPEN_FROM_ENVIRONMENT_TITLE:
        raise NavigatorError("Return may only be sent to the 'Open from Environment' dialog.")
    _check_focus(pid, dialog)
    if not verify():
        raise NavigatorError("The highlighted row is not the requested module; nothing was opened.")
    _check_focus(pid, dialog)
    _post_key(pid, KEY_RETURN)


# --- tabs --------------------------------------------------------------------------


def click_tab(pid: int, window: Any, tab_item: Any) -> None:
    """Click a shell tab's caption after confirming that exactly it is under the point."""
    caption, _ = mac_ax.tab_caption(tab_item)
    point = mac_ax.center(caption) if caption is not None else None
    if point is None:
        raise NavigatorError("The tab caption was not found; no tab was selected.")
    ensure_focus(pid, window)
    if mac_ax.element_at(*point) != caption:
        raise NavigatorError("Something covers the Service Studio tab; no tab was selected.")
    _hid_click(point)


def click_in_window(pid: int, window: Any, point: tuple[float, float]) -> None:
    """One left click at a screen point, only if that point shows ``window``'s own content."""
    ensure_focus(pid, window)
    hit = mac_ax.element_at(*point)
    if hit is None or mac_ax.attr(hit, "AXWindow") != window:
        raise NavigatorError("Something covers the Service Studio window; nothing was clicked.")
    _hid_click(point)


def right_click_in_window(pid: int, window: Any, point: tuple[float, float]) -> None:
    """One right click at a screen point inside ``window`` (opens a context menu)."""
    ensure_focus(pid, window)
    hit = mac_ax.element_at(*point)
    if hit is None or mac_ax.attr(hit, "AXWindow") != window:
        raise NavigatorError("Something covers the Service Studio window; nothing was clicked.")
    _hid_click(point, right=True)


def click_menu_item(pid: int, menu_window: Any, point: tuple[float, float]) -> None:
    """One left click on a context menu item, only if the point is that menu's item."""
    if not mac_ax.is_frontmost(pid):
        raise FocusError("Service Studio lost the foreground; the menu was not clicked.")
    hit = mac_ax.element_at(*point)
    item = hit
    for _ in range(6):  # the hit can be the item's text or icon inside it
        if item is None or mac_ax.role(item) == "AXMenuItem":
            break
        item = mac_ax.attr(item, "AXParent")
    if (
        item is None
        or mac_ax.role(item) != "AXMenuItem"
        or (mac_ax.attr(item, "AXWindow") != menu_window)
    ):
        raise NavigatorError("The context menu item is not under the pointer; nothing was clicked.")
    _hid_click(point)


def _hid_click(point: tuple[float, float], *, right: bool = False) -> None:
    """Click at ``point`` (Avalonia ignores clicks posted to its process), then put the
    pointer back where the user left it."""
    q = _quartz()
    from AppKit import NSEvent

    original = NSEvent.mouseLocation()  # Cocoa coords: origin bottom-left of main display
    screen_h = q.CGDisplayPixelsHigh(q.CGMainDisplayID())
    if right:
        kinds = (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp)
        button = q.kCGMouseButtonRight
    else:
        kinds = (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp)
        button = q.kCGMouseButtonLeft
    for kind in kinds:
        event = q.CGEventCreateMouseEvent(None, kind, point, button)
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
        q.CGEventPost(q.kCGHIDEventTap, event)
        time.sleep(0.05)
    q.CGWarpMouseCursorPosition((original.x, screen_h - original.y))
    q.CGAssociateMouseAndMouseCursorPosition(True)


def close_module_tab(tab_item: Any, module_name: str) -> None:
    """Press a module editor tab's close button after re-checking the tab live.

    Refuses unless the tab is a module editor tab whose caption names
    ``module_name`` and shows no unsaved-changes marker, so Service Studio never
    asks whether to save.
    """
    if (
        mac_ax.role(tab_item) != "AXRadioButton"
        or ss.classify_tab_type(str(mac_ax.attr(tab_item, "AXTitle") or ""))
        is not ss.ShellTabKind.MODULE_EDITOR
    ):
        raise NavigatorError("That Service Studio tab is not a module editor tab.")
    _, caption_text = mac_ax.tab_caption(tab_item)
    caption = ss.parse_module_caption(caption_text)
    if caption is None or not ss.same_module_name(caption.name, module_name):
        raise NavigatorError("The Service Studio tabs changed; nothing was closed. Retry.")
    if caption.unsaved:
        raise UnsavedChangesError(
            f"{caption.name!r} has unsaved changes; it was not closed. "
            "Save it in Service Studio first, or close another module."
        )
    buttons = mac_ax.tab_close_buttons(tab_item)
    if len(buttons) != 1:
        raise NavigatorError("The module tab's close button was not found; nothing was closed.")
    if mac_ax.ax().AXUIElementPerformAction(buttons[0], "AXPress") != 0:
        raise NavigatorError("Service Studio did not accept the close request.")
