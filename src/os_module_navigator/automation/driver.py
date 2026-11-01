"""Service Studio driver: low-level steps used by the desktop GUI provider.

``ServiceStudioDriver`` is the seam between workflow logic (unit-tested with a
fake) and the live UI. ``LiveServiceStudioDriver`` combines read-only lookups
(``live.py``) with the few allowed interactions (``actions.py``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from os_module_navigator.automation import actions, live
from os_module_navigator.automation import service_studio as ss
from os_module_navigator.errors import (
    NavigatorError,
    SearchDialogUnavailableError,
    ServiceStudioNotRunningError,
    ServiceStudioWindowNotFoundError,
    UnexpectedDialogError,
)


@dataclass
class UiState:
    running: bool
    main_window: bool
    tabs: list[ss.ShellTab] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)


class ServiceStudioDriver(Protocol):
    def read_state(self) -> UiState: ...
    def open_search_dialog(self, timeout_s: float, poll_s: float) -> None: ...
    def set_search_text(self, text: str) -> None: ...
    def read_search_rows(self) -> list[ss.ModuleSearchRow]: ...
    def select_search_row(self, index: int, rendered_rows: int, key_delay_s: float) -> None: ...
    def press_open(self) -> None: ...
    def cancel_search_dialog(self) -> None: ...
    def select_tab(self, index: int) -> None: ...


class LiveServiceStudioDriver:
    """Talks to the running Service Studio. Call from a single COM-initialized thread."""

    def _pid(self) -> int:
        pid = live.find_service_studio_pid()
        if pid is None:
            raise ServiceStudioNotRunningError("Service Studio is not running.")
        return pid

    def _windows(self) -> list[live.TopWindow]:
        return live.top_windows(self._pid())

    def _main(self) -> live.TopWindow:
        for window in self._windows():
            if window.role is ss.WindowRole.MAIN:
                return window
        raise ServiceStudioWindowNotFoundError("The Service Studio main window was not found.")

    def _search_dialog(self) -> live.TopWindow | None:
        for window in self._windows():
            if (
                window.role is ss.WindowRole.DIALOG
                and window.title == ss.OPEN_FROM_ENVIRONMENT_TITLE
            ):
                return window
        return None

    def _require_search_dialog(self) -> tuple[live.TopWindow, int]:
        dialog = self._search_dialog()
        if dialog is None:
            raise SearchDialogUnavailableError("The 'Open from Environment' dialog is not open.")
        webview = live.child_webview(dialog.handle.hwnd)
        if webview is None:
            raise SearchDialogUnavailableError("The search dialog has no readable content.")
        return dialog, webview

    def _dialog_element(self, control_type: str, title: str) -> Any:
        _, webview = self._require_search_dialog()
        element = live.find_first(webview, control_type=control_type, title=title)
        if element is None:
            raise SearchDialogUnavailableError(f"'{title}' was not found in the search dialog.")
        return element

    # --- reads -------------------------------------------------------------

    def read_state(self) -> UiState:
        pid = live.find_service_studio_pid()
        if pid is None:
            return UiState(running=False, main_window=False)
        windows = live.top_windows(pid)
        main = next((w for w in windows if w.role is ss.WindowRole.MAIN), None)
        tabs = ss.read_shell_tabs(live.snapshot_shell(main.handle.hwnd)) if main else []
        dialogs = [w.title for w in windows if w.role is ss.WindowRole.DIALOG]
        return UiState(running=True, main_window=main is not None, tabs=tabs, dialogs=dialogs)

    def read_search_rows(self) -> list[ss.ModuleSearchRow]:
        _, webview = self._require_search_dialog()
        results = ss.read_module_search_results(live.snapshot_webview(webview))
        return results.rows if results else []

    # --- interactions ------------------------------------------------------

    def _wait_for_dialog(self, timeout_s: float, poll_s: float, *, quiet_s: float) -> str:
        """Wait for the search dialog.

        Returns ``"open"``, ``"loading"`` (a Progress dialog was seen but the
        search dialog did not appear in time) or ``"nothing"`` (no dialog at all
        within ``quiet_s``, so the trigger probably did not work).
        """
        start = time.monotonic()
        saw_transient = False
        while True:
            elapsed = time.monotonic() - start
            dialogs = [w for w in self._windows() if w.role is ss.WindowRole.DIALOG]
            titles = [w.title for w in dialogs]
            if ss.OPEN_FROM_ENVIRONMENT_TITLE in titles and not any(
                t in ss.TRANSIENT_DIALOG_TITLES for t in titles
            ):
                return "open"
            unexpected = [
                t
                for t in titles
                if t not in ss.TRANSIENT_DIALOG_TITLES and t != ss.OPEN_FROM_ENVIRONMENT_TITLE
            ]
            if unexpected:
                raise UnexpectedDialogError(
                    f"Service Studio opened an unexpected dialog: {unexpected[0]!r}."
                )
            saw_transient = saw_transient or bool(titles)
            if not saw_transient and elapsed >= quiet_s:
                return "nothing"
            if elapsed >= timeout_s:
                return "loading" if saw_transient else "nothing"
            time.sleep(poll_s)

    def open_search_dialog(self, timeout_s: float, poll_s: float) -> None:
        if self._search_dialog() is not None:
            return
        main = self._main()
        others = [w.title for w in self._windows() if w.role is ss.WindowRole.DIALOG]
        if others:
            raise UnexpectedDialogError(
                f"Service Studio is showing another dialog ({others[0]!r}); close it first."
            )

        # 1) Invoke the environment page's "Search Modules" button (no keyboard needed).
        #    It shows a "Progress" dialog while the module list loads.
        for webview in live.all_webviews(main.handle.hwnd):
            button = live.find_first(
                webview, control_type="Button", title=ss.SEARCH_MODULES_BUTTON_NAME
            )
            if button is None:
                continue
            try:
                actions.invoke(button)
            except NavigatorError:
                break
            outcome = self._wait_for_dialog(timeout_s, poll_s, quiet_s=3.0)
            if outcome == "open":
                return
            if outcome == "loading":
                # Never trigger a second load while one is running.
                raise SearchDialogUnavailableError(
                    "Service Studio is still loading the module list; try again shortly."
                )
            break

        # 2) Fall back to Ctrl+O on the main window, only if nothing happened above.
        target = live.element_info(main.handle.hwnd)
        actions.ensure_foreground(main.handle.hwnd, target)
        actions.press_keys(main.handle.hwnd, ["^o"])
        if self._wait_for_dialog(timeout_s, poll_s, quiet_s=timeout_s) != "open":
            raise SearchDialogUnavailableError("The 'Open from Environment' dialog did not open.")

    def set_search_text(self, text: str) -> None:
        actions.set_value(self._dialog_element("Edit", ss.MODULE_SEARCH_EDIT_NAME), text)

    def select_search_row(self, index: int, rendered_rows: int, key_delay_s: float) -> None:
        dialog, _ = self._require_search_dialog()
        edit = self._dialog_element("Edit", ss.MODULE_SEARCH_EDIT_NAME)
        actions.ensure_foreground(dialog.handle.hwnd, edit)
        actions.set_focus(edit)
        # Up past the top makes the starting row deterministic (row 0), then Down to index.
        keys = ["{UP}"] * max(rendered_rows, 1) + ["{DOWN}"] * index
        actions.press_keys(dialog.handle.hwnd, keys, delay_s=key_delay_s)

    def press_open(self) -> None:
        actions.invoke(self._dialog_element("Button", ss.OPEN_BUTTON_NAME))

    def cancel_search_dialog(self) -> None:
        if self._search_dialog() is None:
            return
        actions.invoke(self._dialog_element("Button", ss.CANCEL_BUTTON_NAME))

    def select_tab(self, index: int) -> None:
        items = live.shell_tab_items(self._main().handle.hwnd)
        if not 0 <= index < len(items):
            raise NavigatorError("The requested Service Studio tab no longer exists.")
        actions.select_item(items[index])
