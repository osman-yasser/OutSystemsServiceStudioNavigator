"""Read-only discovery of the Service Studio 11 window structure.

Observed structure (Service Studio 11 on this machine, see docs/phase1-findings.md):

* The shell window is an Avalonia window (UIA framework ``Avalonia``, UIA class
  ``AggregatorWindow``; Win32 class ``Avalonia-<guid>``, which changes per run).
  It is not returned by ``pywinauto.Desktop(backend="uia").windows()`` and has to
  be attached by handle.
* The shell has a ``TabControl`` (automation id ``tabs``). Each ``TabItem`` name
  is a .NET type name that tells us what the tab hosts; its caption is a child
  ``Text`` (environment name, module name with a leading ``*`` when unsaved, ...).
* Tab content is rendered by embedded Chromium (CEF). Each tab has its own
  ``Chrome_RenderWidgetHostHWND``; only the selected one is visible. Hidden panes
  still expose their accessibility tree.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, Field

from os_module_navigator.automation.uia import ElementNode
from os_module_navigator.automation.windows import WindowHandle

PROCESS_NAME = "ServiceStudio.exe"
SHELL_WIN32_CLASS_PREFIX = "Avalonia-"
SHELL_UIA_CLASS = "AggregatorWindow"
DIALOG_UIA_CLASS = "DialogWindow"
OPEN_FROM_ENVIRONMENT_TITLE = "Open from Environment"
SHELL_TABS_AUTOMATION_ID = "tabs"
WEBVIEW_WIN32_CLASS = "Chrome_RenderWidgetHostHWND"
UNSAVED_MARKER = "*"


class ShellTabKind(StrEnum):
    ENVIRONMENT = "environment"
    MODULE_EDITOR = "module_editor"
    EMBEDDED_BROWSER = "embedded_browser"
    UNKNOWN = "unknown"


class WindowRole(StrEnum):
    MAIN = "main"
    DIALOG = "dialog"
    OTHER = "other"


class WebViewKind(StrEnum):
    ENVIRONMENT_HOME = "environment_home"
    OPEN_FROM_ENVIRONMENT = "open_from_environment"
    MODULE_EDITOR = "module_editor"
    EMPTY = "empty"
    UNKNOWN = "unknown"


# Substrings of the TabItem type names observed in the shell.
_TAB_KIND_MARKERS: tuple[tuple[str, ShellTabKind], ...] = (
    ("ServerPage", ShellTabKind.ENVIRONMENT),
    ("AggregatorView", ShellTabKind.MODULE_EDITOR),
    ("OutSystemsBrowser", ShellTabKind.EMBEDDED_BROWSER),
)


class Landmark(BaseModel):
    """A UI element that matters for the module search/open workflow."""

    key: str
    control_type: str
    label: str
    automation_id: str = ""
    patterns: list[str] = Field(default_factory=list)
    keyboard_focusable: bool = False
    enabled: bool = True


class LandmarkRule(BaseModel):
    key: str
    control_type: str
    name: str | None = None
    automation_id: str | None = None

    def matches(self, node: ElementNode) -> bool:
        if node.control_type != self.control_type:
            return False
        if self.name is not None and node.name != self.name:
            return False
        return self.automation_id is None or node.automation_id == self.automation_id


# Only labels that were actually observed in the accessibility tree.
LANDMARK_RULES: tuple[LandmarkRule, ...] = (
    LandmarkRule(key="search_modules_button", control_type="Button", name="Search Modules"),
    LandmarkRule(
        key="search_applications_edit", control_type="Edit", name="Search Applications..."
    ),
    LandmarkRule(
        key="applications_list", control_type="Group", automation_id="applications-list-root"
    ),
    LandmarkRule(key="recent_modules_heading", control_type="Text", name="Recent modules"),
    LandmarkRule(key="publish_label", control_type="Text", name="Publish"),
    LandmarkRule(key="debugger_menu", control_type="Text", name="Debugger"),
    # "Open from Environment" dialog (Ctrl+O), a separate Avalonia DialogWindow.
    LandmarkRule(
        key="module_search_edit",
        control_type="Edit",
        name="Search for Module Name, Application Name or Author",
    ),
    LandmarkRule(key="results_table", control_type="Table"),
    LandmarkRule(key="open_button", control_type="Button", name="Open"),
    LandmarkRule(key="view_versions_button", control_type="Button", name="View Versions"),
    LandmarkRule(key="cancel_button", control_type="Button", name="Cancel"),
)


class ModuleSearchRow(BaseModel):
    """One row of the "Open from Environment" results table, keyed by column header."""

    cells: dict[str, str]
    on_screen: bool


class ModuleSearchResults(BaseModel):
    columns: list[str]
    rows: list[ModuleSearchRow] = Field(default_factory=list)


class ShellTab(BaseModel):
    type_name: str
    kind: ShellTabKind
    selected: bool | None = None
    caption: str = ""
    unsaved_marker: bool = False


def classify_tab_type(type_name: str) -> ShellTabKind:
    for marker, kind in _TAB_KIND_MARKERS:
        if marker in type_name:
            return kind
    return ShellTabKind.UNKNOWN


def find_landmarks(root: ElementNode) -> list[Landmark]:
    found: list[Landmark] = []
    for node in root.iter_nodes():
        for rule in LANDMARK_RULES:
            if rule.matches(node):
                found.append(
                    Landmark(
                        key=rule.key,
                        control_type=node.control_type,
                        label=node.name,
                        automation_id=node.automation_id,
                        patterns=node.patterns,
                        keyboard_focusable=node.keyboard_focusable,
                        enabled=node.enabled,
                    )
                )
    return found


def classify_window(uia_class: str) -> WindowRole:
    if uia_class == SHELL_UIA_CLASS:
        return WindowRole.MAIN
    if uia_class == DIALOG_UIA_CLASS:
        return WindowRole.DIALOG
    return WindowRole.OTHER


def _first_text(node: ElementNode) -> str:
    for child in node.iter_nodes():
        if child.control_type == "Text" and child.name:
            return child.name
    return ""


def _is_visible_rect(node: ElementNode) -> bool:
    return node.rect is not None and node.rect.width > 0 and node.rect.height > 0


def read_module_search_results(root: ElementNode) -> ModuleSearchResults | None:
    """Rebuild result rows from the "Open from Environment" table.

    Chromium flattens this grid: the headers are ``Header`` elements, and every
    result cell is a ``Group`` (with a ``Text`` child) inside one ``DataItem``.
    Rows are rebuilt by chunking cells by the number of columns. The list is
    virtualized, so only rows near the viewport are present.
    """
    table = next((n for n in root.iter_nodes() if n.control_type == "Table"), None)
    if table is None:
        return None
    columns = [_first_text(n) or n.name for n in table.iter_nodes() if n.control_type == "Header"]
    if not columns:
        return None
    cells = [
        node
        for item in table.iter_nodes()
        if item.control_type == "DataItem"
        for node in item.children
        if node.control_type == "Group"
    ]
    rows: list[ModuleSearchRow] = []
    width = len(columns)
    for start in range(0, len(cells) - width + 1, width):
        chunk = cells[start : start + width]
        rows.append(
            ModuleSearchRow(
                cells={col: _first_text(cell) for col, cell in zip(columns, chunk, strict=True)},
                on_screen=all(_is_visible_rect(cell) for cell in chunk),
            )
        )
    return ModuleSearchResults(columns=columns, rows=rows)


def classify_webview(root: ElementNode) -> WebViewKind:
    keys = {landmark.key for landmark in find_landmarks(root)}
    if "module_search_edit" in keys:
        return WebViewKind.OPEN_FROM_ENVIRONMENT
    if "applications_list" in keys or "search_modules_button" in keys:
        return WebViewKind.ENVIRONMENT_HOME
    if {"publish_label", "debugger_menu"} <= keys:
        return WebViewKind.MODULE_EDITOR
    if root.count() <= 2:
        return WebViewKind.EMPTY
    return WebViewKind.UNKNOWN


def _caption(tab_item: ElementNode) -> str:
    for child in tab_item.children:
        if child.control_type == "Text" and child.name:
            return child.name
    return ""


def read_shell_tabs(
    shell: ElementNode, sanitize: Callable[[str, str], str] | None = None
) -> list[ShellTab]:
    """Read the shell tab strip. ``sanitize(control_type, text)`` redacts captions."""
    tabs: list[ShellTab] = []
    for node in shell.iter_nodes():
        if node.control_type != "Tab" or node.automation_id != SHELL_TABS_AUTOMATION_ID:
            continue
        for item in node.children:
            if item.control_type != "TabItem":
                continue
            caption = _caption(item)
            tabs.append(
                ShellTab(
                    type_name=item.name,
                    kind=classify_tab_type(item.name),
                    selected=item.selected,
                    caption=sanitize("Text", caption) if sanitize else caption,
                    unsaved_marker=caption.lstrip().startswith(UNSAVED_MARKER),
                )
            )
    return tabs


def is_shell_candidate(window: WindowHandle) -> bool:
    return (
        window.is_top_level
        and window.visible
        and window.class_name.startswith(SHELL_WIN32_CLASS_PREFIX)
    )


def is_webview(window: WindowHandle) -> bool:
    return window.class_name == WEBVIEW_WIN32_CLASS
