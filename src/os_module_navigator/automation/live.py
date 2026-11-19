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
from os_module_navigator.automation.uia import ElementNode, snapshot_handle, snapshot_tree
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


def _walk_descendants(
    info: Any, control_type: str, title: str | None = None, *, limit: int = 4000
) -> list[Any]:
    """Descendants found by walking ``children()``.

    Slower than ``descendants()`` (one UIA FindAll), but Chromium's accessibility
    tree can make FindAll return nothing after the page re-renders (for example
    after a checkbox was toggled) while a plain walk still sees every element.
    """
    found: list[Any] = []
    stack = [info]
    visited = 0
    while stack and visited < limit:
        node = stack.pop()
        try:
            kids = list(node.children())
        except Exception:
            continue
        for kid in reversed(kids):
            visited += 1
            try:
                if kid.control_type == control_type and (title is None or kid.name == title):
                    found.append(kid)
            except Exception:
                continue
            stack.append(kid)
    return found


def descendants_robust(info: Any, control_type: str, title: str | None = None) -> list[Any]:
    kwargs: dict[str, Any] = {"control_type": control_type}
    if title is not None:
        kwargs["title"] = title
    try:
        found = list(info.descendants(**kwargs))
    except Exception:
        found = []
    return found or _walk_descendants(info, control_type, title)


def find_first(root_hwnd: int, *, control_type: str, title: str | None = None) -> Any | None:
    """First descendant matching control type (and exact name) via UIA FindAll."""
    try:
        matches = descendants_robust(element_info(root_hwnd), control_type, title)
    except Exception:
        return None
    return matches[0] if matches else None


def shell_tab_items(main_hwnd: int) -> list[Any]:
    """Live TabItem elements of the shell tab strip, in display order.

    The shell strip is the window's direct ``Tab`` child with automation id
    ``tabs``. A module editor has its own inner tab strips (e.g. "Warnings",
    "Debugger"), so the first ``Tab`` descendant is not necessarily the shell's.
    Must match ``service_studio.read_shell_tabs`` so tab indexes line up.
    """
    try:
        strips = [
            child
            for child in element_info(main_hwnd).children(control_type="Tab")
            if child.automation_id == ss.SHELL_TABS_AUTOMATION_ID
        ]
        return list(strips[0].children(control_type="TabItem")) if strips else []
    except Exception:
        return []


def foreground_window() -> int:
    return int(win32gui.GetForegroundWindow())


@dataclass(frozen=True, slots=True)
class DependencyRoot:
    """The top checkbox of the 'public elements' tree in Manage Dependencies."""

    element: Any
    label: str
    state: int  # UIA ToggleState: 0 off, 1 on, 2 indeterminate


def _rect(info: Any) -> tuple[int, int, int, int]:
    r = info.rectangle
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def _non_empty(info: Any) -> bool:
    left, top, right, bottom = _rect(info)
    return right > left and bottom > top


def dependency_rows(webview_hwnd: int) -> list[tuple[str, bool]]:
    """Module rows listed in Manage Dependencies as ``(name, is_extension)``, top to bottom.

    The rows are plain ``Text`` elements. They are told apart from the right-hand
    panel by position: they sit between the module search box and the
    public-elements search box.
    """
    try:
        root = element_info(webview_hwnd)
        module_edit = descendants_robust(root, "Edit", ss.DEPENDENCY_MODULE_SEARCH_EDIT_NAME)
        element_edit = descendants_robust(root, "Edit", ss.DEPENDENCY_ELEMENT_SEARCH_EDIT_NAME)
        if not module_edit or not element_edit:
            return []
        m_left, _, _, m_bottom = _rect(module_edit[0])
        e_left = _rect(element_edit[0])[0]
        texts = [
            (info, _rect(info)) for info in descendants_robust(root, "Text") if _non_empty(info)
        ]
    except Exception:
        return []
    band = [
        (str(info.name), rect)
        for info, rect in texts
        if m_left - 60 <= rect[0] < e_left - 20 and rect[1] > m_bottom
    ]
    rows: dict[int, list[tuple[int, str]]] = {}
    for name, rect in band:
        rows.setdefault(rect[1], []).append((rect[0], name))
    result: list[tuple[str, bool]] = []
    for top in sorted(rows):
        cells = [name for _, name in sorted(rows[top])]
        names = [c for c in cells if c != ss.EXTENSION_TAG]
        if names:
            result.append((names[0], ss.EXTENSION_TAG in cells))
    return result


def dependency_root(webview_hwnd: int) -> DependencyRoot | None:
    """The first (top) checkbox of the public-elements tree and its label, if shown."""
    from pywinauto.uia_defines import get_elem_interface

    try:
        info = element_info(webview_hwnd)
        boxes = sorted(
            (c for c in descendants_robust(info, "CheckBox") if _non_empty(c)),
            key=lambda c: (_rect(c)[1], _rect(c)[0]),
        )
        if not boxes:
            return None
        box = boxes[0]
        _, b_top, b_right, _ = _rect(box)
        labels = [
            str(t.name)
            for t in descendants_robust(info, "Text")
            if _non_empty(t) and abs(_rect(t)[1] - b_top) <= 6 and _rect(t)[0] >= b_right
        ]
        state = int(get_elem_interface(box.element, "Toggle").CurrentToggleState)
    except Exception:
        return None
    return DependencyRoot(element=box, label=labels[0] if labels else "", state=state)


def elements_all_filter_button(webview_hwnd: int) -> Any | None:
    """The 'All' filter button of the public-elements panel (the right-hand one).

    Manage Dependencies has an 'All' button for the module list and another for the
    elements; the elements panel starts on 'Modified or missing' when the module
    already references the dependency, which hides the elements and their state.
    """
    try:
        buttons = [
            b
            for b in descendants_robust(
                element_info(webview_hwnd), "Button", ss.ALL_FILTER_BUTTON_NAME
            )
            if _non_empty(b)
        ]
    except Exception:
        return None
    return max(buttons, key=lambda b: _rect(b)[0]) if buttons else None


@dataclass(frozen=True, slots=True)
class DependencyCheckbox:
    """One row of the public-elements tree: its checkbox, label and ToggleState."""

    element: Any
    label: str
    state: int
    top: int


def dependency_checkboxes(webview_hwnd: int) -> list[DependencyCheckbox]:
    """All rendered rows of the public-elements tree, top to bottom (the first is the module)."""
    from pywinauto.uia_defines import get_elem_interface

    try:
        info = element_info(webview_hwnd)
        boxes = sorted(
            (c for c in descendants_robust(info, "CheckBox") if _non_empty(c)),
            key=lambda c: (_rect(c)[1], _rect(c)[0]),
        )
        if not boxes:
            return []
        edit = descendants_robust(info, "Edit", ss.DEPENDENCY_ELEMENT_SEARCH_EDIT_NAME)
        panel_left = _rect(edit[0])[0] - 40 if edit else 0
        texts = [
            (str(t.name), _rect(t))
            for t in descendants_robust(info, "Text")
            if _non_empty(t) and _rect(t)[0] >= panel_left
        ]
        rows: list[DependencyCheckbox] = []
        for box in boxes:
            _, b_top, b_right, _ = _rect(box)
            labels = sorted(
                (rect[0], name)
                for name, rect in texts
                if abs(rect[1] - b_top) <= 6 and rect[0] >= b_right and name.strip()
            )
            state = int(get_elem_interface(box.element, "Toggle").CurrentToggleState)
            rows.append(DependencyCheckbox(box, labels[0][1] if labels else "", state, b_top))
    except Exception:
        return []
    return rows


def dependencies_loading(webview_hwnd: int) -> str | None:
    """The loading text shown inside Manage Dependencies ("Processing... (44%)"), if any."""
    try:
        for text in descendants_robust(element_info(webview_hwnd), "Text"):
            name = str(text.name or "")
            if name and _non_empty(text) and ss.LOADING_TEXT.search(name):
                return name.strip()
    except Exception:
        return None
    return None


# --- Find Usages in all Modules ---------------------------------------------------


def visible_webviews(hwnd: int) -> list[int]:
    return [h for h in all_webviews(hwnd) if win32gui.IsWindowVisible(h)]


def quick_search_edit(webview_hwnd: int) -> Any | None:
    """The top-right quick-search box (Ctrl+F); the toolbox has another 'Search' box."""
    try:
        edits = [
            e
            for e in descendants_robust(
                element_info(webview_hwnd), "Edit", ss.QUICK_SEARCH_EDIT_NAME
            )
            if _non_empty(e)
        ]
    except Exception:
        return None
    return max(edits, key=lambda e: _rect(e)[0]) if edits else None


def quick_search_popup(webview_hwnd: int) -> ElementNode | None:
    """Snapshot of the quick-search results popup, if it is open."""
    try:
        groups = [
            g
            for g in descendants_robust(element_info(webview_hwnd), "Group")
            if str(g.name or "").startswith(ss.QUICK_SEARCH_GROUP_PREFIX) and _non_empty(g)
        ]
        if not groups:
            return None
        return ss.find_quick_search_popup(
            snapshot_tree(groups[0], max_depth=4, max_elements=600, timeout_s=5)
        )
    except Exception:
        return None


def has_keyboard_focus(info: Any) -> bool:
    try:
        return bool(info.element.CurrentHasKeyboardFocus)
    except Exception:
        return False


def element_tree_input(webview_hwnd: int) -> Any | None:
    """The keyboard input of the visible elements tree (Interface/Logic/Data panel).

    Chromium exposes the tree as a ``Tree`` whose only child is a hidden, focusable
    ``Edit`` that receives the tree's keyboard input; the rows are plain ``Text``.
    """
    try:
        trees = [t for t in descendants_robust(element_info(webview_hwnd), "Tree") if _non_empty(t)]
        for tree in trees:
            edits = [c for c in tree.children() if c.control_type == "Edit"]
            if edits:
                return edits[0]
    except Exception:
        return None
    return None


@dataclass(frozen=True, slots=True)
class MenuEntry:
    name: str
    enabled: bool
    focused: bool


def context_menu_windows(pid: int) -> list[TopWindow]:
    return [w for w in top_windows(pid) if w.uia_class == ss.CONTEXT_MENU_UIA_CLASS]


def context_menu_entries(menu_hwnd: int) -> list[MenuEntry]:
    try:
        items = descendants_robust(element_info(menu_hwnd), "MenuItem")
        return [MenuEntry(str(i.name or ""), bool(i.enabled), has_keyboard_focus(i)) for i in items]
    except Exception:
        return []


def usages_panel(webview_hwnd: int) -> ss.UsagesPanel | None:
    """Read the "'X' usages in all modules" panel from small subtree snapshots."""
    try:
        root = element_info(webview_hwnd)
        tabs = [
            t
            for t in descendants_robust(root, "TabItem")
            if ss.USAGES_TAB_AUTOMATION_ID_PART in str(t.automation_id or "")
        ]
        if not tabs:
            return None
        parts = [snapshot_tree(tabs[0], max_depth=3, max_elements=20, timeout_s=3)]
        for table in descendants_robust(root, "Table"):
            if any(h.name == "Module Name" for h in descendants_robust(table, "Header")):
                parts.append(snapshot_tree(table, max_depth=6, max_elements=2000, timeout_s=10))
                break
        for text in descendants_robust(root, "Text"):
            name = str(text.name or "")
            if ss.USAGES_SUMMARY.match(name):
                parts.append(ElementNode(control_type="Text", name=name))
                break
    except Exception:
        return None
    return ss.read_usages_panel(ElementNode(control_type="Group", children=parts))


def usages_tab_close_buttons(webview_hwnd: int) -> list[Any]:
    """'Close tab' buttons of existing "usages in all modules" panels."""
    try:
        tabs = [
            t
            for t in descendants_robust(element_info(webview_hwnd), "TabItem")
            if ss.USAGES_TAB_AUTOMATION_ID_PART in str(t.automation_id or "")
        ]
        return [b for t in tabs for b in t.children() if b.control_type == "Button"]
    except Exception:
        return []
