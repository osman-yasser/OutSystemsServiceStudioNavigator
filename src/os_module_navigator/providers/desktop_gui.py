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
from os_module_navigator.config import NavigatorConfig
from os_module_navigator.errors import (
    AmbiguousModuleError,
    AutomationTimeoutError,
    ModuleNotFoundInEnvironmentError,
    ModuleNotOpenError,
    NavigatorError,
    NotConnectedError,
    ServiceStudioNotRunningError,
    ServiceStudioWindowNotFoundError,
    TooManyOpenModulesError,
    UnexpectedDialogError,
    UnsavedChangesError,
    WrongModuleOpenedError,
)
from os_module_navigator.models import (
    CloseModuleResult,
    ModuleInfo,
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
        while (
            any(d in ss.TRANSIENT_DIALOG_TITLES for d in state.dialogs) and self._clock() < deadline
        ):
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
            unexpected = [d for d in state.dialogs if d not in ss.TRANSIENT_DIALOG_TITLES]
            if unexpected:
                raise UnexpectedDialogError(
                    f"Service Studio showed a dialog ({unexpected[0]!r}) while closing "
                    f"{module_name!r}. Please handle it in Service Studio."
                )
            if self._clock() > deadline:
                raise AutomationTimeoutError(f"{module_name!r} was still open after closing it.")

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

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
