"""Module provider that drives the Service Studio 11 desktop GUI.

Workflow (see docs/phase1-findings.md):

1. Check Service Studio is running, connected (environment tab), and free of unexpected dialogs.
2. Open "Open from Environment" (Search Modules button, falling back to Ctrl+O).
3. Set the search box value; wait for the results to settle.
4. Require exactly one exact match in the Module column.
5. Select that row with Up/Down from the search box (UIA can't select rows).
6. Invoke Open, then verify a selected editor tab with that module name appears.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.driver import (
    LiveServiceStudioDriver,
    ServiceStudioDriver,
    UiState,
)
from os_module_navigator.automation.live import DependencyRoot
from os_module_navigator.config import NavigatorConfig
from os_module_navigator.errors import (
    AmbiguousDependencyElementError,
    AmbiguousDependencyError,
    AmbiguousModuleError,
    AutomationTimeoutError,
    DependencyChangeFailedError,
    DependencyElementNotFoundError,
    DependencyNotFoundError,
    ElementNotFoundError,
    FindUsagesUnavailableError,
    ModuleNotFoundInEnvironmentError,
    ModuleNotOpenError,
    NavigatorError,
    NotConnectedError,
    ServiceStudioNotRunningError,
    ServiceStudioWindowNotFoundError,
    TooManyOpenModulesError,
    UnexpectedDialogError,
    UnsavedChangesError,
    WrongDependencySelectedError,
    WrongElementSelectedError,
    WrongModuleOpenedError,
)
from os_module_navigator.models import (
    CloseModuleResult,
    DependencyChangeResult,
    FindUsagesResult,
    ModuleInfo,
    ModuleUsages,
    OpenModule,
    OpenModuleResult,
    OpenModulesResult,
    OpenStatus,
    SearchResult,
    ServiceStudioStatus,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

AUTOMATION_BACKEND = "pywinauto-uia"
REFRESH_NOTE = (
    " Service Studio reported outdated references, so all references were refreshed first."
)


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _to_module(row: ss.ModuleSearchRow) -> ModuleInfo:
    cells = row.cells
    return ModuleInfo(
        name=cells.get("Module", ""),
        application=cells.get("Application") or None,
        last_changed=cells.get("Changed") or None,
        changed_by=cells.get("By") or None,
    )


def _module_tabs(state: UiState) -> list[tuple[int, ss.ShellTab, ss.ModuleCaption]]:
    found = []
    for index, tab in enumerate(state.tabs):
        if tab.kind is not ss.ShellTabKind.MODULE_EDITOR:
            continue
        caption = ss.parse_module_caption(tab.caption)
        if caption is not None:
            found.append((index, tab, caption))
    return found


def _usages_result(
    module_name: str, panel: ss.UsagesPanel, element_type: str | None, *, other_matches: int
) -> FindUsagesResult:
    rows = [
        ModuleUsages(module=r.module, usages=r.usages, usages_text=r.usages_text)
        for r in panel.rows
    ]
    consumers = [r.module for r in rows if r.module != ss.LOCAL_MODULE and r.usages > 0]
    total = sum(r.usages for r in rows)
    note = (
        f" {other_matches} other element(s) with this name were not used; pass "
        "element_type to choose."
        if other_matches
        else ""
    )
    return FindUsagesResult(
        module_name=module_name,
        element_name=panel.element_name,
        element_type=element_type,
        usages=rows,
        modules_with_usages=consumers,
        total_usages=total,
        summary=panel.summary,
        other_matches=other_matches,
        message=f"{total} usage(s); {len(consumers)} consumer module(s) with usages.{note}",
    )


class DesktopGUINavigator:
    """Synchronous workflow logic. Must run on a single COM-initialized thread."""

    def __init__(
        self,
        driver: ServiceStudioDriver,
        config: NavigatorConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.driver = driver
        self.config = config or NavigatorConfig()
        self._clock = clock
        self._sleep = sleep

    # --- helpers -----------------------------------------------------------

    def _validate(self, text: str, what: str) -> str:
        value = " ".join(text.split())
        if not value:
            raise NavigatorError(f"{what} must not be empty.")
        if len(value) > self.config.max_query_length:
            raise NavigatorError(
                f"{what} is longer than {self.config.max_query_length} characters."
            )
        if any(ord(ch) < 32 for ch in value):
            raise NavigatorError(f"{what} contains control characters.")
        return value

    def _ready_state(self, *, require_connected: bool = True) -> UiState:
        state = self.driver.read_state()
        deadline = self._clock() + self.config.dialog_open_timeout_s
        while any(ss.is_transient_dialog(d) for d in state.dialogs) and self._clock() < deadline:
            self._sleep(self.config.poll_interval_s)
            state = self.driver.read_state()
        if not state.running:
            raise ServiceStudioNotRunningError("Service Studio is not running.")
        if not state.main_window:
            raise ServiceStudioWindowNotFoundError("The Service Studio main window was not found.")
        if require_connected and not any(t.kind is ss.ShellTabKind.ENVIRONMENT for t in state.tabs):
            raise NotConnectedError(
                "Service Studio does not show a connected environment. Connect and sign in first."
            )
        others = [d for d in state.dialogs if d != ss.OPEN_FROM_ENVIRONMENT_TITLE]
        if others:
            raise UnexpectedDialogError(
                f"Service Studio is showing a dialog ({others[0]!r}). Handle it, then retry."
            )
        return state

    def _search_rows(self, query: str) -> list[ss.ModuleSearchRow]:
        """Set the search text and wait until the result rows reflect it and stop changing."""
        before = self.driver.read_search_rows()
        self.driver.set_search_text(query)
        needle = _normalize(query)
        deadline = self._clock() + self.config.search_settle_timeout_s
        previous: list[ss.ModuleSearchRow] | None = None
        stable = 0
        while True:
            self._sleep(self.config.poll_interval_s)
            rows = self.driver.read_search_rows()
            stable = stable + 1 if rows == previous else 0
            previous = rows
            matches = all(needle in _normalize(" ".join(r.cells.values())) for r in rows)
            if (matches and stable >= 1) or (rows != before and stable >= 2):
                return rows
            if self._clock() > deadline:
                raise AutomationTimeoutError("Search results did not update in time.")

    def _with_search_dialog(self, state: UiState, work: Callable[[], T], *, keep_open: bool) -> T:
        opened_here = ss.OPEN_FROM_ENVIRONMENT_TITLE not in state.dialogs
        self.driver.open_search_dialog(
            self.config.dialog_open_timeout_s, self.config.poll_interval_s
        )
        try:
            result = work()
        except BaseException:
            if opened_here:
                self._close_dialog_quietly()
            raise
        if opened_here and not keep_open:
            self._close_dialog_quietly()
        return result

    def _close_dialog_quietly(self) -> None:
        try:
            self.driver.cancel_search_dialog()
        except NavigatorError:
            log.warning("Could not close the search dialog.")

    # --- public operations ---------------------------------------------------

    def status(self) -> ServiceStudioStatus:
        state = self.driver.read_state()
        env_tabs = [t for t in state.tabs if t.kind is ss.ShellTabKind.ENVIRONMENT]
        modules = _module_tabs(state)
        selected = next((c for _, t, c in modules if t.selected), None)
        return ServiceStudioStatus(
            running=state.running,
            window_detected=state.main_window,
            automation_backend=AUTOMATION_BACKEND,
            environment_connected=bool(env_tabs) if state.main_window else None,
            environment=env_tabs[0].caption or None if env_tabs else None,
            current_module=selected.name if selected else None,
            current_module_unsaved=selected.unsaved if selected else None,
            open_modules=[c.name for _, _, c in modules],
            dialog_open=state.dialogs[0] if state.dialogs else None,
        )

    def open_modules(self) -> OpenModulesResult:
        state = self.driver.read_state()
        if not state.running:
            raise ServiceStudioNotRunningError("Service Studio is not running.")
        if not state.main_window:
            raise ServiceStudioWindowNotFoundError("The Service Studio main window was not found.")
        modules = [
            OpenModule(
                name=c.name,
                application=c.application,
                unsaved=c.unsaved,
                selected=bool(t.selected),
            )
            for _, t, c in _module_tabs(state)
        ]
        limit = self.config.max_open_modules
        return OpenModulesResult(
            count=len(modules), limit=limit, can_open_more=len(modules) < limit, modules=modules
        )

    def close(self, module_name: str) -> CloseModuleResult:
        module_name = self._validate(module_name, "Module name")
        state = self._ready_state(require_connected=False)
        tabs = _module_tabs(state)
        match = next((m for m in tabs if ss.same_module_name(m[2].name, module_name)), None)
        if match is None:
            raise ModuleNotOpenError(
                f"No open module is named {module_name!r}.",
                open_modules=[c.name for _, _, c in tabs],
            )
        index, _, caption = match
        if caption.unsaved:
            raise UnsavedChangesError(
                f"{caption.name!r} has unsaved changes; it was not closed. "
                "Ask the user to save it in Service Studio, or close another module."
            )
        before = sum(ss.same_module_name(c.name, module_name) for _, _, c in tabs)
        self.driver.close_module_tab(index, caption.name)
        return self._verify_closed(caption.name, before)

    def _verify_closed(self, module_name: str, before: int) -> CloseModuleResult:
        # The same module can be open in two windows (e.g. two environments), so
        # success means one tab fewer with that name, not none left.
        deadline = self._clock() + self.config.close_verify_timeout_s
        while True:
            self._sleep(self.config.poll_interval_s)
            state = self.driver.read_state()
            remaining = [c.name for _, _, c in _module_tabs(state)]
            if sum(ss.same_module_name(n, module_name) for n in remaining) < before:
                return CloseModuleResult(
                    module_name=module_name,
                    message="The module tab was closed.",
                    open_modules_count=len(remaining),
                )
            unexpected = [d for d in state.dialogs if not ss.is_transient_dialog(d)]
            if unexpected:
                raise UnexpectedDialogError(
                    f"Service Studio showed a dialog ({unexpected[0]!r}) while closing "
                    f"{module_name!r}. Please handle it in Service Studio."
                )
            if self._clock() > deadline:
                raise AutomationTimeoutError(f"{module_name!r} was still open after closing it.")

    # --- dependencies ----------------------------------------------------------
    def _wait_dependencies_loaded(self) -> None:
        """Wait until Manage Dependencies has finished loading ("Processing... (44%)").

        Searching or cancelling while it still loads makes the module look missing,
        so nothing is read or changed until two checks in a row show no progress.
        """
        deadline = self._clock() + self.config.dependency_load_timeout_s
        idle = 0
        last = ""
        while True:
            transient = [d for d in self.driver.read_state().dialogs if ss.is_transient_dialog(d)]
            indicator = self.driver.read_dependencies_loading()
            if transient or indicator:
                idle = 0
                last = indicator or transient[0]
            else:
                idle += 1
                if idle >= 2:
                    return
            if self._clock() > deadline:
                raise AutomationTimeoutError(
                    f"Manage Dependencies was still loading ({last!r}) after "
                    f"{self.config.dependency_load_timeout_s:.0f}s."
                )
            self._sleep(self.config.poll_interval_s)

    def _dependency_rows(self, query: str) -> list[tuple[str, bool]]:
        """Filter Manage Dependencies' module list and wait until it settles."""
        before = self.driver.read_dependency_rows()
        self.driver.set_dependency_search(query)
        needle = _normalize(query)
        deadline = self._clock() + self.config.search_settle_timeout_s
        previous: list[tuple[str, bool]] | None = None
        stable = 0
        while True:
            self._sleep(self.config.poll_interval_s)
            rows = self.driver.read_dependency_rows()
            stable = stable + 1 if rows == previous else 0
            previous = rows
            matches = bool(rows) and all(needle in _normalize(name) for name, _ in rows)
            if (matches and stable >= 1) or (rows != before and stable >= 2):
                return rows
            if self.driver.read_dependencies_loading():
                deadline = self._clock() + self.config.search_settle_timeout_s
            if self._clock() > deadline:
                raise AutomationTimeoutError("Dependency list did not update in time.")

    def _wait_tab_selected(self, module_name: str) -> None:
        deadline = self._clock() + 5.0
        while self._clock() <= deadline:
            for _, tab, caption in _module_tabs(self.driver.read_state()):
                if tab.selected and ss.same_module_name(caption.name, module_name):
                    return
            self._sleep(self.config.poll_interval_s)
        raise AutomationTimeoutError(f"The tab of {module_name!r} could not be brought to front.")

    def _stable_dependency_root(self) -> DependencyRoot | None:
        """Read the elements tree root once it stops changing (Chromium renders lazily)."""
        previous: tuple[str, int] | None = None
        root: DependencyRoot | None = None
        for _ in range(6):
            self._sleep(self.config.poll_interval_s)
            root = self.driver.read_dependency_root()
            current = (root.label, root.state) if root else None
            if current is not None and current == previous:
                return root
            previous = current
        return root

    def _select_dependency(self, row_name: str, rendered_rows: int) -> DependencyRoot:
        """Move down the module list until the elements tree shows ``row_name``.

        The first Down only moves the focus into the list; each further Down selects
        the next row. Reading the tree after every key makes the selection verified
        instead of counted, so a wrong row is never changed.
        """
        self.driver.focus_dependency_search()
        seen = ""
        for _ in range(rendered_rows + 2):
            self.driver.press_dependency_key("{DOWN}", self.config.key_delay_s)
            root = self._stable_dependency_root()
            if root is None:
                continue
            seen = root.label
            if _normalize(root.label) == _normalize(row_name):
                return root
        raise WrongDependencySelectedError(
            f"Could not select {row_name!r} (last selected: {seen!r}); nothing was changed."
        )

    def _set_dependency_checked(self, want: int) -> None:
        for _ in range(3):
            root = self.driver.read_dependency_root()
            if root is not None and root.state == want:
                return
            self.driver.toggle_dependency_root(self.config.key_delay_s)
            self._sleep(self.config.poll_interval_s)
        root = self.driver.read_dependency_root()
        if root is None or root.state != want:
            raise DependencyChangeFailedError(
                "Could not change the dependency's checkbox; nothing was applied."
            )

    def _wait_dependencies_applied(self) -> bool:
        """Wait for Apply to finish. Returns True if outdated references had to be refreshed.

        When the module still has outdated or missing references Service Studio asks
        whether to proceed "keeping outdated definitions". We answer No, press
        *Refresh all* and Apply again (once), so no stale definition is left behind.
        """
        deadline = self._clock() + self.config.dependency_apply_timeout_s
        refreshed = False
        while True:
            self._sleep(self.config.poll_interval_s)
            dialogs = self.driver.read_state().dialogs
            if ss.MANAGE_DEPENDENCIES_TITLE not in dialogs and not any(
                ss.is_transient_dialog(d) for d in dialogs
            ):
                return refreshed
            if ss.OUTDATED_REFERENCES_TITLE in dialogs and not refreshed:
                self.driver.dismiss_outdated_references_prompt()
                self._sleep(self.config.poll_interval_s)
                self.driver.press_dependencies_refresh_all()
                self._sleep(self.config.poll_interval_s * 2)
                self.driver.press_dependencies_apply()
                refreshed = True
                continue
            unexpected = [
                d
                for d in dialogs
                if d != ss.MANAGE_DEPENDENCIES_TITLE and not ss.is_transient_dialog(d)
            ]
            if unexpected:
                raise UnexpectedDialogError(
                    f"Service Studio is showing a dialog ({unexpected[0]!r}) after Apply. "
                    "Please handle it in Service Studio."
                )
            if self._clock() > deadline:
                raise AutomationTimeoutError("Manage Dependencies was still open after Apply.")

    def _element_rows(self, query: str) -> list[tuple[str, int]]:
        """Filter the elements tree and return ``(label, state)`` of the rows below the module."""
        before = [r.label for r in self.driver.read_dependency_checkboxes()]
        self.driver.set_element_search(query)
        deadline = self._clock() + self.config.search_settle_timeout_s
        previous: list[tuple[str, int]] | None = None
        stable = 0
        while True:
            self._sleep(self.config.poll_interval_s)
            boxes = self.driver.read_dependency_checkboxes()
            rows = [(r.label, r.state) for r in boxes[1:]]
            stable = stable + 1 if rows == previous else 0
            previous = rows
            changed = [r.label for r in boxes] != before
            if (changed and stable >= 1) or stable >= 3:
                return rows
            if self._clock() > deadline:
                raise AutomationTimeoutError("The elements list did not update in time.")

    def _set_element(self, name: str, want: int) -> bool:
        """Make one element's checkbox ``want``. Returns True when it had to be toggled."""
        rows = self._element_rows(name)
        exact = [i for i, (label, _) in enumerate(rows) if _normalize(label) == _normalize(name)]
        if not exact:
            raise DependencyElementNotFoundError(
                f"No element named {name!r} was found in this dependency.",
                suggestions=[label for label, _ in rows if name.casefold() in label.casefold()][
                    :10
                ],
            )
        if len(exact) > 1:
            raise AmbiguousDependencyElementError(
                f"{len(exact)} rows are named {name!r}.",
                candidates=[rows[i][0] for i in exact],
            )
        label, state = rows[exact[0]]
        if state == want:
            return False
        for _ in range(3):
            self.driver.toggle_dependency_checkbox(label, self.config.key_delay_s)
            self._sleep(self.config.poll_interval_s)
            current = [(r.label, r.state) for r in self.driver.read_dependency_checkboxes()[1:]]
            now = [st for lab, st in current if lab == label]
            if len(now) == 1 and now[0] == want:
                return True
        raise DependencyChangeFailedError(
            f"Could not change the checkbox of {label!r}; nothing was applied."
        )

    def _change_elements(
        self, module_name: str, row_name: str, elements: list[str], *, add: bool, action: str
    ) -> DependencyChangeResult:
        want = 1 if add else 0
        changed = [e for e in elements if self._set_element(e, want)]
        if not changed:
            self._cancel_dependencies_quietly()
            if add:
                status, text = "already_present", "already included"
            else:
                status, text = "not_a_dependency", "not included"
            return DependencyChangeResult(
                module_name=module_name,
                dependency_name=row_name,
                action=action,
                elements=elements,
                status=status,
                message=f"{', '.join(elements)} {text} in {row_name!r} in {module_name!r}; "
                "nothing was changed.",
            )
        self.driver.set_element_search("")
        self.driver.press_dependencies_apply()
        refreshed = self._wait_dependencies_applied()
        note = REFRESH_NOTE if refreshed else ""
        verb = "added to" if add else "removed from"
        return DependencyChangeResult(
            module_name=module_name,
            dependency_name=row_name,
            action=action,
            elements=changed,
            status="applied",
            message=f"{', '.join(changed)} of {row_name!r} {verb} the dependencies of "
            f"{module_name!r}.{note}",
        )

    def _cancel_dependencies_quietly(self) -> None:
        try:
            self.driver.cancel_dependencies_dialog()
        except NavigatorError:
            log.warning("Could not close the Manage Dependencies dialog.")

    def change_dependency(
        self,
        module_name: str,
        dependency_name: str,
        *,
        add: bool,
        elements: list[str] | None = None,
    ) -> DependencyChangeResult:
        module_name = self._validate(module_name, "Module name")
        dependency_name = self._validate(dependency_name, "Dependency name")
        wanted = list(dict.fromkeys(self._validate(e, "Element name") for e in elements or []))
        action = "add" if add else "remove"
        state = self._ready_state(require_connected=False)
        tabs = _module_tabs(state)
        match = next((m for m in tabs if ss.same_module_name(m[2].name, module_name)), None)
        if match is None:
            raise ModuleNotOpenError(
                f"No open module is named {module_name!r}.",
                open_modules=[c.name for _, _, c in tabs],
            )
        index, tab, caption = match
        if not tab.selected:
            self.driver.select_tab(index)
            self._wait_tab_selected(caption.name)
        self.driver.open_dependencies_dialog(
            self.config.dialog_open_timeout_s, self.config.poll_interval_s
        )
        try:
            return self._change_in_dialog(
                caption.name, dependency_name, add=add, action=action, elements=wanted
            )
        except BaseException:
            self._cancel_dependencies_quietly()
            raise

    def _change_in_dialog(
        self,
        module_name: str,
        dependency_name: str,
        *,
        add: bool,
        action: str,
        elements: list[str],
    ) -> DependencyChangeResult:
        self._wait_dependencies_loaded()
        rows = self._dependency_rows(dependency_name)
        exact = [
            i for i, (name, _) in enumerate(rows) if ss.same_module_name(name, dependency_name)
        ]
        if not exact:
            raise DependencyNotFoundError(
                f"No module named {dependency_name!r} is available as a dependency.",
                suggestions=[name for name, _ in rows[:10]],
            )
        if len(exact) > 1:
            raise AmbiguousDependencyError(
                f"{len(exact)} entries are named {dependency_name!r}.",
                candidates=[
                    f"{rows[i][0]} (extension)" if rows[i][1] else rows[i][0] for i in exact
                ],
            )
        row_name = rows[exact[0]][0]
        self._select_dependency(row_name, len(rows))
        # The elements panel may start on 'Modified or missing', which hides them.
        self.driver.show_all_dependency_elements()
        root = self._stable_dependency_root()
        if root is None or _normalize(root.label) != _normalize(row_name):
            raise WrongDependencySelectedError(
                f"Could not read {row_name!r}'s elements; nothing was changed."
            )
        if elements:
            return self._change_elements(module_name, row_name, elements, add=add, action=action)
        want = 1 if add else 0
        # Partially selected (state 2) already counts as a dependency: adding all of its
        # elements would change which ones the module consumes. Refresh/Remove cover it.
        present = root.state in (1, 2)
        if (present and add) or (root.state == 0 and not add):
            self._cancel_dependencies_quietly()
            if add:
                return DependencyChangeResult(
                    module_name=module_name,
                    dependency_name=row_name,
                    action=action,
                    status="already_present",
                    message=f"{row_name!r} is already a dependency of {module_name!r}"
                    + (" (only some of its elements)" if root.state == 2 else "")
                    + "; nothing was changed.",
                )
            return DependencyChangeResult(
                module_name=module_name,
                dependency_name=row_name,
                action=action,
                status="not_a_dependency",
                message=f"{row_name!r} is not a dependency of {module_name!r}; "
                "nothing was changed.",
            )
        self._set_dependency_checked(want)
        self.driver.press_dependencies_apply()
        refreshed = self._wait_dependencies_applied()
        note = REFRESH_NOTE if refreshed else ""
        verb = "added to" if add else "removed from"
        return DependencyChangeResult(
            module_name=module_name,
            dependency_name=row_name,
            action=action,
            status="applied",
            message=f"{row_name!r} was {verb} the dependencies of {module_name!r}.{note}",
        )

    # --- Find Usages in all Modules ---------------------------------------------
    def find_usages(
        self, module_name: str, element_name: str, element_type: str | None = None
    ) -> FindUsagesResult:
        """Run Service Studio's *Find Usages in all Modules* (Ctrl+F12) on one element.

        1. Bring the (open) module's tab to the front.
        2. Close old "usages in all modules" panels so a stale one is never read.
        3. Quick search (Ctrl+F) for the name; open the exact-name result (Down/Enter).
        4. Open the selected tree element's context menu (Shift+F10), read its type
           from "Help with '<type>'", and run "Find Usages in all Modules".
        5. Wait for the panel titled "'<name>' usages in all modules" and read it.

        Read-only: nothing in the module is changed, saved or published.
        """
        module_name = self._validate(module_name, "Module name")
        element_name = self._validate(element_name, "Element name")
        wanted_type = self._validate(element_type, "Element type") if element_type else None
        state = self._ready_state()
        tabs = _module_tabs(state)
        match = next((m for m in tabs if ss.same_module_name(m[2].name, module_name)), None)
        if match is None:
            raise ModuleNotOpenError(
                f"No open module is named {module_name!r}. Open it with open_module first.",
                open_modules=[c.name for _, _, c in tabs],
            )
        index, tab, caption = match
        if not tab.selected:
            self.driver.select_tab(index)
            self._wait_tab_selected(caption.name)
        try:
            return self._find_usages(caption.name, element_name, wanted_type)
        except BaseException:
            self._close_popups_quietly()
            raise

    def _close_popups_quietly(self) -> None:
        try:
            self.driver.close_popups()
        except NavigatorError:
            log.warning("Could not close Service Studio's popup.")

    def _quick_search(self, query: str) -> list[ss.QuickSearchResult]:
        self.driver.open_quick_search()
        self._sleep(self.config.poll_interval_s)
        self.driver.set_quick_search_text(query)
        needle = _normalize(query)
        deadline = self._clock() + self.config.search_settle_timeout_s
        previous: list[ss.QuickSearchResult] | None = None
        stable = 0
        reopened = False
        while True:
            self._sleep(self.config.poll_interval_s)
            found = self.driver.read_quick_search_results()
            if found is None and not reopened:
                reopened = True
                self.driver.open_quick_search()
                continue
            results = found or []
            stable = stable + 1 if results == previous else 0
            previous = results
            if stable >= 1 and any(_normalize(r.name) == needle for r in results):
                return results
            if stable >= 3:
                return results
            if self._clock() > deadline:
                raise AutomationTimeoutError("The quick-search results did not update in time.")

    def _wait_context_menu(self) -> list[str]:
        deadline = self._clock() + 5.0
        while True:
            self._sleep(self.config.poll_interval_s)
            entries = self.driver.read_context_menu()
            if entries:
                return [e.name for e in entries if e.enabled]
            if self._clock() > deadline:
                raise FindUsagesUnavailableError(
                    "The element's context menu did not open (is an element selected in "
                    "the elements tree?)."
                )

    def _wait_usages_panel(self, element_name: str) -> ss.UsagesPanel:
        deadline = self._clock() + self.config.find_usages_timeout_s
        while True:
            self._sleep(self.config.poll_interval_s)
            panel = self.driver.read_usages_panel()
            if panel is not None and panel.summary is not None:
                if _normalize(panel.element_name) != _normalize(element_name):
                    raise WrongElementSelectedError(
                        f"Service Studio searched usages of {panel.element_name!r} instead of "
                        f"{element_name!r}."
                    )
                return panel
            unexpected = [
                d for d in self.driver.read_state().dialogs if not ss.is_transient_dialog(d)
            ]
            if unexpected:
                raise UnexpectedDialogError(
                    f"Service Studio is showing a dialog ({unexpected[0]!r}) while finding "
                    "usages. Please handle it in Service Studio."
                )
            if self._clock() > deadline:
                raise AutomationTimeoutError(
                    "The 'usages in all modules' panel did not finish in "
                    f"{self.config.find_usages_timeout_s:.0f}s."
                )

    def _find_usages(
        self, module_name: str, element_name: str, wanted_type: str | None
    ) -> FindUsagesResult:
        self.driver.close_usages_panels()
        results = self._quick_search(element_name)
        needle = _normalize(element_name)
        exact = [i for i, r in enumerate(results) if _normalize(r.name) == needle]
        if not exact:
            self._close_popups_quietly()
            raise ElementNotFoundError(
                f"Quick search found no element named {element_name!r} in {module_name!r}.",
                suggestions=list(dict.fromkeys(r.name for r in results))[:10],
            )
        seen_types: list[str] = []
        for attempt, position in enumerate(exact):
            if attempt:
                results = self._quick_search(element_name)
                if position >= len(results) or _normalize(results[position].name) != needle:
                    break
            self.driver.open_quick_search_result(position, self.config.key_delay_s)
            self._sleep(self.config.poll_interval_s * 2)
            self.driver.open_element_context_menu()
            items = self._wait_context_menu()
            found_type = ss.element_type_from_menu(items)
            if wanted_type and _normalize(found_type or "") != _normalize(wanted_type):
                seen_types.append(found_type or "unknown")
                self._close_popups_quietly()
                continue
            if ss.FIND_USAGES_ALL_MODULES_ITEM not in items:
                self._close_popups_quietly()
                raise FindUsagesUnavailableError(
                    "Service Studio offers no 'Find Usages in all Modules' for "
                    f"{element_name!r} ({found_type or 'unknown type'})."
                )
            self.driver.run_context_menu_item(
                ss.FIND_USAGES_ALL_MODULES_ITEM, self.config.key_delay_s
            )
            panel = self._wait_usages_panel(element_name)
            return _usages_result(module_name, panel, found_type, other_matches=len(exact) - 1)
        raise ElementNotFoundError(
            f"No {wanted_type!r} named {element_name!r} was found in {module_name!r} "
            f"(same-name elements found: {', '.join(seen_types) or 'none'}).",
            suggestions=seen_types,
        )

    def search(self, query: str) -> SearchResult:
        query = self._validate(query, "Query")
        state = self._ready_state()
        rows = self._with_search_dialog(state, lambda: self._search_rows(query), keep_open=False)
        modules = [_to_module(r) for r in rows if r.cells.get("Module")]
        truncated = any(not r.on_screen for r in rows) or len(modules) > self.config.max_results
        return SearchResult(
            query=query, modules=modules[: self.config.max_results], possibly_truncated=truncated
        )

    def open(self, module_name: str) -> OpenModuleResult:
        module_name = self._validate(module_name, "Module name")
        state = self._ready_state()

        for index, tab, caption in _module_tabs(state):
            if ss.same_module_name(caption.name, module_name):
                if not tab.selected:
                    self.driver.select_tab(index)
                return OpenModuleResult(
                    module_name=caption.name,
                    status=OpenStatus.ALREADY_OPEN,
                    message="The module was already open; its tab is now in front.",
                    tab_caption=tab.caption,
                )

        open_names = [c.name for _, _, c in _module_tabs(state)]
        if len(open_names) >= self.config.max_open_modules:
            raise TooManyOpenModulesError(
                f"{len(open_names)} modules are open (limit {self.config.max_open_modules}). "
                "Close one with close_module, then open this module.",
                open_modules=open_names,
            )
        known_tabs = {name.casefold() for name in open_names}

        def select_and_open() -> None:
            rows = self._search_rows(module_name)
            exact = [
                i
                for i, r in enumerate(rows)
                if ss.same_module_name(r.cells.get("Module", ""), module_name)
            ]
            if not exact:
                raise ModuleNotFoundInEnvironmentError(
                    f"No module named {module_name!r} was found.",
                    suggestions=[_to_module(r) for r in rows[:10]],
                )
            if len(exact) > 1:
                raise AmbiguousModuleError(
                    f"{len(exact)} modules are named {module_name!r}.",
                    candidates=[_to_module(rows[i]) for i in exact],
                )
            self.driver.select_search_row(exact[0], len(rows), self.config.key_delay_s)
            self.driver.press_open()

        self._with_search_dialog(state, select_and_open, keep_open=True)
        return self._verify_opened(module_name, known_tabs)

    def _verify_opened(self, module_name: str, known_tabs: set[str]) -> OpenModuleResult:
        deadline = self._clock() + self.config.open_verify_timeout_s
        last_state: UiState | None = None
        while self._clock() <= deadline:
            self._sleep(self.config.poll_interval_s)
            state = self.driver.read_state()
            last_state = state
            for _, tab, caption in _module_tabs(state):
                if not tab.selected:
                    continue
                if ss.same_module_name(caption.name, module_name):
                    return OpenModuleResult(
                        module_name=caption.name,
                        status=OpenStatus.OPENED_VERIFIED,
                        message="The module is open in Service Studio.",
                        tab_caption=tab.caption,
                    )
                if caption.name.casefold() not in known_tabs:
                    raise WrongModuleOpenedError(
                        f"Service Studio opened {caption.name!r} instead of {module_name!r}."
                    )
        if last_state and [d for d in last_state.dialogs if d != ss.OPEN_FROM_ENVIRONMENT_TITLE]:
            raise UnexpectedDialogError(
                f"Service Studio is waiting on a dialog ({last_state.dialogs[0]!r}). "
                "Please handle it in Service Studio."
            )
        return OpenModuleResult(
            module_name=module_name,
            status=OpenStatus.OPENED_UNVERIFIED,
            message="Open was pressed, but the module tab was not confirmed in time.",
        )


def _init_com_thread() -> None:
    import warnings

    # pywinauto announces that it picked up our COM flags; that's expected.
    warnings.filterwarnings("ignore", message="Apply externally defined coinit_flags")
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    if "comtypes" not in sys.modules:
        sys.coinit_flags = 0  # type: ignore[attr-defined]  # COINIT_MULTITHREADED
    import comtypes

    with contextlib.suppress(OSError):  # already initialized for this thread
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)


class DesktopGUIProvider:
    """Async ``ModuleProvider``. All UI work runs serially on one dedicated thread."""

    def __init__(
        self,
        navigator: DesktopGUINavigator | None = None,
        config: NavigatorConfig | None = None,
    ) -> None:
        self._navigator = navigator or DesktopGUINavigator(LiveServiceStudioDriver(), config)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="service-studio-ui", initializer=_init_com_thread
        )

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn)

    async def search_modules(self, query: str) -> SearchResult:
        return await self._run(lambda: self._navigator.search(query))

    async def open_module(self, module_name: str) -> OpenModuleResult:
        return await self._run(lambda: self._navigator.open(module_name))

    async def get_status(self) -> ServiceStudioStatus:
        return await self._run(self._navigator.status)

    async def get_open_modules(self) -> OpenModulesResult:
        return await self._run(self._navigator.open_modules)

    async def close_module(self, module_name: str) -> CloseModuleResult:
        return await self._run(lambda: self._navigator.close(module_name))

    async def add_dependency(
        self, module_name: str, dependency_name: str, elements: list[str] | None = None
    ) -> DependencyChangeResult:
        return await self._run(
            lambda: self._navigator.change_dependency(
                module_name, dependency_name, add=True, elements=elements
            )
        )

    async def remove_dependency(
        self, module_name: str, dependency_name: str, elements: list[str] | None = None
    ) -> DependencyChangeResult:
        return await self._run(
            lambda: self._navigator.change_dependency(
                module_name, dependency_name, add=False, elements=elements
            )
        )

    async def find_usages_all_modules(
        self, module_name: str, element_name: str, element_type: str | None = None
    ) -> FindUsagesResult:
        return await self._run(
            lambda: self._navigator.find_usages(module_name, element_name, element_type)
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
