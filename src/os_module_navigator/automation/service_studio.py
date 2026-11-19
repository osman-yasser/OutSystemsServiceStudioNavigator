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

import re
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
# Dialogs Service Studio shows briefly while loading; wait them out instead of failing.
TRANSIENT_DIALOG_TITLES: frozenset[str] = frozenset({"Progress"})
TRANSIENT_DIALOG_PREFIXES: tuple[str, ...] = ("Progress", "Processing", "Loading")
# In-dialog loading indicators, e.g. "Processing... (44%)" or "Loading".
LOADING_TEXT = re.compile(r"^\s*(Processing|Loading)\b|\(\s*\d+\s*%\s*\)|^\s*\d+\s*%\s*$")


def is_transient_dialog(title: str) -> bool:
    """True for progress dialogs Service Studio shows while it works ("Processing... (44%)")."""
    return title in TRANSIENT_DIALOG_TITLES or title.startswith(TRANSIENT_DIALOG_PREFIXES)


SHELL_TABS_AUTOMATION_ID = "tabs"
WEBVIEW_WIN32_CLASS = "Chrome_RenderWidgetHostHWND"
UNSAVED_MARKER = "*"
SEARCH_MODULES_BUTTON_NAME = "Search Modules"
MODULE_SEARCH_EDIT_NAME = "Search for Module Name, Application Name or Author"
OPEN_BUTTON_NAME = "Open"
MANAGE_DEPENDENCIES_TITLE = "Manage Dependencies"
DEPENDENCY_MODULE_SEARCH_EDIT_NAME = "Search modules"
DEPENDENCY_ELEMENT_SEARCH_EDIT_NAME = "Search public elements"
APPLY_BUTTON_NAME = "Apply"
ALL_FILTER_BUTTON_NAME = "All"
REFRESH_ALL_BUTTON_NAME = "Refresh all"
NO_BUTTON_NAME = "No"
# Shown after Apply when the module still has outdated/missing references.
OUTDATED_REFERENCES_TITLE = "Modified or missing references found"
EXTENSION_TAG = "(extension)"
CANCEL_BUTTON_NAME = "Cancel"


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
    LandmarkRule(
        key="search_modules_button", control_type="Button", name=SEARCH_MODULES_BUTTON_NAME
    ),
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
        name=MODULE_SEARCH_EDIT_NAME,
    ),
    LandmarkRule(key="results_table", control_type="Table"),
    LandmarkRule(key="open_button", control_type="Button", name=OPEN_BUTTON_NAME),
    LandmarkRule(key="view_versions_button", control_type="Button", name="View Versions"),
    LandmarkRule(key="cancel_button", control_type="Button", name=CANCEL_BUTTON_NAME),
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


def _shallowest_shell_strip(root: ElementNode) -> ElementNode | None:
    level = [root]
    while level:
        for node in level:
            if node.control_type == "Tab" and node.automation_id == SHELL_TABS_AUTOMATION_ID:
                return node
        level = [child for node in level for child in node.children]
    return None


def read_shell_tabs(
    shell: ElementNode, sanitize: Callable[[str, str], str] | None = None
) -> list[ShellTab]:
    """Read the shell tab strip. ``sanitize(control_type, text)`` redacts captions.

    Only the shallowest ``Tab`` with automation id ``tabs`` is read: module
    editors contain inner tab strips that must not be mixed in.
    """
    tabs: list[ShellTab] = []
    strip = _shallowest_shell_strip(shell)
    if strip is not None:
        for item in strip.children:
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


class ModuleCaption(BaseModel):
    name: str
    application: str | None = None
    unsaved: bool = False


_CAPTION_WITH_APP = re.compile(r"^(?P<name>.+?)\s*\((?P<app>[^()]*)\)$")


def parse_module_caption(caption: str) -> ModuleCaption | None:
    """Parse a module editor tab caption such as ``* MyModule (My App)``."""
    text = " ".join(caption.split())
    unsaved = text.startswith(UNSAVED_MARKER)
    text = text.removeprefix(UNSAVED_MARKER).strip()
    if not text:
        return None
    match = _CAPTION_WITH_APP.match(text)
    if match:
        return ModuleCaption(
            name=match.group("name").strip(),
            application=match.group("app").strip(),
            unsaved=unsaved,
        )
    return ModuleCaption(name=text, unsaved=unsaved)


def same_module_name(a: str, b: str) -> bool:
    """OutSystems module names are unique case-insensitively."""
    return a.strip().casefold() == b.strip().casefold()


# --- Find Usages in all Modules ----------------------------------------------------
# Quick search (Ctrl+F): a search box at the top right whose popup lists actions
# ("Search in this Module", ...) and then results grouped under category headers
# (Interface, Logic, Data, ...). Chromium flattens the popup into one Group of
# Image/Text pairs, so results are told apart from headers by their icon.
QUICK_SEARCH_EDIT_NAME = "Search"
QUICK_SEARCH_GROUP_PREFIX = "Search in "
FIND_USAGES_ALL_MODULES_ITEM = "Find Usages in all Modules"
HELP_WITH_ITEM = re.compile(r"^Help with '(?P<type>.+)'$")
CONTEXT_MENU_UIA_CLASS = "PopupRoot"
USAGES_TAB_SUFFIX = " usages in all modules"
USAGES_TAB_AUTOMATION_ID_PART = "findConsumersPane"
USAGES_SUMMARY = re.compile(
    r"^\s*(?P<usages>\d+)\s+usages?\s+found\s+in\s+(?P<modules>\d+)\s+consumer"
)
USAGES_CELL = re.compile(r"^\s*(?P<count>\d+)\s+usages?\s*$")
NO_USAGES = "No usages"
LOCAL_MODULE = "(local)"


class QuickSearchResult(BaseModel):
    category: str
    name: str


def parse_quick_search(popup: ElementNode) -> list[QuickSearchResult]:
    """Results of the quick-search popup, in display order (the keyboard order).

    The first rows ("Search in ...", "Search for Help", ...) also have icons but
    come before the first category header, so they are skipped.
    """
    results: list[QuickSearchResult] = []
    category: str | None = None
    previous_was_icon = False
    for node in popup.children:
        if node.control_type == "Image":
            previous_was_icon = True
            continue
        if node.control_type == "Text" and node.name:
            if not previous_was_icon:
                category = node.name
            elif category is not None:
                results.append(QuickSearchResult(category=category, name=node.name))
        previous_was_icon = False
    return results


def find_quick_search_popup(root: ElementNode) -> ElementNode | None:
    for node in root.iter_nodes():
        if (
            node.control_type == "Group"
            and node.keyboard_focusable
            and node.name.startswith(QUICK_SEARCH_GROUP_PREFIX)
            and _is_visible_rect(node)
        ):
            return node
    return None


def element_type_from_menu(items: list[str]) -> str | None:
    """The element type named by the context menu's "Help with 'Server Action'" item."""
    for item in items:
        match = HELP_WITH_ITEM.match(item.strip())
        if match:
            return match.group("type")
    return None


class UsageRow(BaseModel):
    module: str
    usages: int
    usages_text: str


class UsagesPanel(BaseModel):
    element_name: str
    rows: list[UsageRow] = Field(default_factory=list)
    summary: str | None = None


def parse_usage_count(text: str) -> int | None:
    if text.strip().casefold() == NO_USAGES.casefold():
        return 0
    match = USAGES_CELL.match(text)
    return int(match.group("count")) if match else None


def usages_tab_element(caption: str) -> str | None:
    """``'X' usages in all modules`` -> ``X``."""
    text = caption.strip()
    if not text.endswith(USAGES_TAB_SUFFIX):
        return None
    quoted = text[: -len(USAGES_TAB_SUFFIX)].strip()
    if len(quoted) >= 2 and quoted[0] == quoted[-1] == "'":
        return quoted[1:-1]
    return None


def read_usages_panel(root: ElementNode) -> UsagesPanel | None:
    """Read the "'X' usages in all modules" bottom panel, if it is shown.

    The results grid is flattened like the module search table: two ``Header``
    elements (Module Name, Usages) and one ``DataItem`` whose ``Text`` children
    are the cells. Cells are paired by row (same top) and column (header x).
    """
    tab = next(
        (
            n
            for n in root.iter_nodes()
            if n.control_type == "TabItem" and USAGES_TAB_AUTOMATION_ID_PART in n.automation_id
        ),
        None,
    )
    if tab is None:
        return None
    caption = _first_text(tab) or tab.name
    element = usages_tab_element(caption)
    if element is None:
        return None
    summary = next(
        (
            n.name.strip()
            for n in root.iter_nodes()
            if n.control_type == "Text" and USAGES_SUMMARY.match(n.name)
        ),
        None,
    )
    headers = [n for n in root.iter_nodes() if n.control_type == "Header" and n.rect is not None]
    usages_header = next((h for h in headers if h.name == "Usages"), None)
    module_header = next((h for h in headers if h.name == "Module Name"), None)
    rows: list[UsageRow] = []
    if usages_header is not None and module_header is not None and usages_header.rect is not None:
        split = usages_header.rect.left
        cells: dict[int, dict[str, str]] = {}
        for item in root.iter_nodes():
            if item.control_type != "DataItem":
                continue
            for cell in item.iter_nodes():
                if cell.control_type != "Text" or not cell.name or cell.rect is None:
                    continue
                column = "usages" if cell.rect.left >= split else "module"
                cells.setdefault(cell.rect.top, {})[column] = cell.name.strip()
        for top in sorted(cells):
            row = cells[top]
            count = parse_usage_count(row.get("usages", ""))
            if "module" in row and count is not None:
                rows.append(UsageRow(module=row["module"], usages=count, usages_text=row["usages"]))
    return UsagesPanel(element_name=element, rows=rows, summary=summary)
