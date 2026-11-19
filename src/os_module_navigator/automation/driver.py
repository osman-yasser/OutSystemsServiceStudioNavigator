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
    DependenciesDialogUnavailableError,
    FindUsagesUnavailableError,
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
    def close_module_tab(self, index: int, module_name: str) -> None: ...
    def open_dependencies_dialog(self, timeout_s: float, poll_s: float) -> None: ...
    def set_dependency_search(self, text: str) -> None: ...
    def read_dependency_rows(self) -> list[tuple[str, bool]]: ...
    def focus_dependency_search(self) -> None: ...
    def press_dependency_key(self, key: str, key_delay_s: float) -> None: ...
    def show_all_dependency_elements(self) -> None: ...
    def set_element_search(self, text: str) -> None: ...
    def read_dependency_checkboxes(self) -> list[live.DependencyCheckbox]: ...
    def toggle_dependency_checkbox(self, label: str, key_delay_s: float) -> None: ...
    def read_dependency_root(self) -> live.DependencyRoot | None: ...
    def toggle_dependency_root(self, key_delay_s: float) -> None: ...
    def press_dependencies_apply(self) -> None: ...
    def read_dependencies_loading(self) -> str | None: ...
    def press_dependencies_refresh_all(self) -> None: ...
    def dismiss_outdated_references_prompt(self) -> None: ...
    def cancel_dependencies_dialog(self) -> None: ...
    def close_usages_panels(self) -> int: ...
    def open_quick_search(self) -> None: ...
    def set_quick_search_text(self, text: str) -> None: ...
    def read_quick_search_results(self) -> list[ss.QuickSearchResult] | None: ...
    def open_quick_search_result(self, index: int, key_delay_s: float) -> None: ...
    def open_element_context_menu(self) -> None: ...
    def read_context_menu(self) -> list[live.MenuEntry]: ...
    def run_context_menu_item(self, name: str, key_delay_s: float) -> None: ...
    def close_popups(self) -> None: ...
    def read_usages_panel(self) -> ss.UsagesPanel | None: ...


class LiveServiceStudioDriver:
    """Talks to the running Service Studio. Call from a single COM-initialized thread."""

    def _pid(self) -> int:
        pid = live.find_service_studio_pid()
        if pid is None:
            raise ServiceStudioNotRunningError("Service Studio is not running.")
        return pid

    def _windows(self) -> list[live.TopWindow]:
        return live.top_windows(self._pid())

    def _mains(self) -> list[live.TopWindow]:
        # Past a number of tabs Service Studio opens modules in another main window.
        # Sort by handle so tab indexes stay stable between reads.
        mains = [w for w in self._windows() if w.role is ss.WindowRole.MAIN]
        return sorted(mains, key=lambda w: w.handle.hwnd)

    def _main(self) -> live.TopWindow:
        mains = self._mains()
        if not mains:
            raise ServiceStudioWindowNotFoundError("The Service Studio main window was not found.")
        return mains[0]

    def _tab_items(self) -> list[Any]:
        """Live TabItems of every main window, in the same order as ``read_state().tabs``."""
        return [item for main in self._mains() for item in live.shell_tab_items(main.handle.hwnd)]

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
        mains = sorted(
            (w for w in windows if w.role is ss.WindowRole.MAIN), key=lambda w: w.handle.hwnd
        )
        tabs = [
            tab
            for main in mains
            for tab in ss.read_shell_tabs(live.snapshot_shell(main.handle.hwnd))
        ]
        dialogs = [w.title for w in windows if w.role is ss.WindowRole.DIALOG]
        return UiState(running=True, main_window=bool(mains), tabs=tabs, dialogs=dialogs)

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
                ss.is_transient_dialog(t) for t in titles
            ):
                return "open"
            unexpected = [
                t
                for t in titles
                if not ss.is_transient_dialog(t) and t != ss.OPEN_FROM_ENVIRONMENT_TITLE
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
        webviews = [h for m in self._mains() for h in live.all_webviews(m.handle.hwnd)]
        for webview in webviews:
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

    def _tab_item_at(self, index: int, *, attempts: int = 6, delay_s: float = 0.5) -> Any:
        """Live TabItem at ``index``, retrying while the UIA tree is transiently incomplete.

        ``live.shell_tab_items`` returns ``[]`` when a UIA call fails (Service Studio busy
        loading a module), which used to surface as "tab no longer exists" even though
        the tab was still there. Retry before giving up.
        """
        for attempt in range(attempts):
            items = self._tab_items()
            if 0 <= index < len(items):
                return items[index]
            if attempt < attempts - 1:
                time.sleep(delay_s)
        raise NavigatorError("The requested Service Studio tab no longer exists.")

    def select_tab(self, index: int) -> None:
        actions.select_item(self._tab_item_at(index))

    def close_module_tab(self, index: int, module_name: str) -> None:
        actions.close_module_tab(self._tab_item_at(index), module_name)

    # --- Manage Dependencies -------------------------------------------------
    def _deps_dialog(self) -> live.TopWindow | None:
        for window in self._windows():
            if window.role is ss.WindowRole.DIALOG and window.title == ss.MANAGE_DEPENDENCIES_TITLE:
                return window
        return None

    def _require_deps_dialog(self) -> tuple[live.TopWindow, int]:
        dialog = self._deps_dialog()
        if dialog is None:
            raise DependenciesDialogUnavailableError(
                "The 'Manage Dependencies' dialog is not open."
            )
        webview = live.child_webview(dialog.handle.hwnd)
        if webview is None:
            raise DependenciesDialogUnavailableError(
                "The 'Manage Dependencies' dialog has no readable content."
            )
        return dialog, webview

    def _deps_element(self, control_type: str, title: str) -> Any:
        _, webview = self._require_deps_dialog()
        element = live.find_first(webview, control_type=control_type, title=title)
        if element is None:
            raise DependenciesDialogUnavailableError(
                f"'{title}' was not found in the 'Manage Dependencies' dialog."
            )
        return element

    def open_dependencies_dialog(self, timeout_s: float, poll_s: float) -> None:
        if self._deps_dialog() is not None:
            return
        others = [w.title for w in self._windows() if w.role is ss.WindowRole.DIALOG]
        if others:
            raise UnexpectedDialogError(
                f"Service Studio is showing another dialog ({others[0]!r}); close it first."
            )
        main = self._main()
        self._press_open_shortcut(main.handle.hwnd)
        started = time.monotonic()
        deadline = started + timeout_s
        hard_deadline = started + max(timeout_s, 600.0)
        retried = False
        while time.monotonic() < deadline:
            dialogs = [w for w in self._windows() if w.role is ss.WindowRole.DIALOG]
            titles = [w.title for w in dialogs]
            if any(ss.is_transient_dialog(t) for t in titles):
                # Service Studio is still working ("Processing... (44%)"): keep waiting.
                deadline = min(hard_deadline, max(deadline, time.monotonic() + timeout_s))
            if ss.MANAGE_DEPENDENCIES_TITLE in titles and not any(
                ss.is_transient_dialog(t) for t in titles
            ):
                # Give the web content a moment to render its first rows.
                time.sleep(poll_s)
                return
            unexpected = [
                t
                for t in titles
                if not ss.is_transient_dialog(t) and t != ss.MANAGE_DEPENDENCIES_TITLE
            ]
            if unexpected:
                raise UnexpectedDialogError(
                    f"Service Studio opened an unexpected dialog: {unexpected[0]!r}."
                )
            # Service Studio sometimes swallows the first Ctrl+Q right after a publish or
            # merge. Try once more before giving up.
            if not retried and not titles and time.monotonic() - started > min(8.0, timeout_s / 3):
                retried = True
                self._press_open_shortcut(main.handle.hwnd)
            time.sleep(poll_s)
        # The dialog can still show up after we gave up (it then blocks merges and
        # publishes): wait briefly and close it so it is not left behind.
        late_deadline = time.monotonic() + 5.0
        while time.monotonic() < late_deadline:
            if self._deps_dialog() is not None:
                self.cancel_dependencies_dialog()
                break
            time.sleep(poll_s)
        raise DependenciesDialogUnavailableError("The 'Manage Dependencies' dialog did not open.")

    def _press_open_shortcut(self, main_hwnd: int) -> None:
        actions.ensure_foreground(main_hwnd, live.element_info(main_hwnd))
        actions.press_keys(main_hwnd, ["^q"])

    def set_dependency_search(self, text: str) -> None:
        actions.set_value(self._deps_element("Edit", ss.DEPENDENCY_MODULE_SEARCH_EDIT_NAME), text)

    def read_dependency_rows(self) -> list[tuple[str, bool]]:
        _, webview = self._require_deps_dialog()
        return live.dependency_rows(webview)

    def focus_dependency_search(self) -> None:
        dialog, _ = self._require_deps_dialog()
        edit = self._deps_element("Edit", ss.DEPENDENCY_MODULE_SEARCH_EDIT_NAME)
        actions.ensure_foreground(dialog.handle.hwnd, edit)
        actions.set_focus(edit)

    def press_dependency_key(self, key: str, key_delay_s: float) -> None:
        dialog, _ = self._require_deps_dialog()
        actions.press_keys(dialog.handle.hwnd, [key], delay_s=key_delay_s)

    def show_all_dependency_elements(self) -> None:
        _, webview = self._require_deps_dialog()
        button = live.elements_all_filter_button(webview)
        if button is None:
            raise DependenciesDialogUnavailableError("The elements 'All' filter was not found.")
        actions.invoke(button)

    def set_element_search(self, text: str) -> None:
        actions.set_value(self._deps_element("Edit", ss.DEPENDENCY_ELEMENT_SEARCH_EDIT_NAME), text)

    def read_dependency_checkboxes(self) -> list[live.DependencyCheckbox]:
        _, webview = self._require_deps_dialog()
        return live.dependency_checkboxes(webview)

    def toggle_dependency_checkbox(self, label: str, key_delay_s: float) -> None:
        dialog, webview = self._require_deps_dialog()
        rows = [r for r in live.dependency_checkboxes(webview)[1:] if r.label == label]
        if len(rows) != 1:
            raise DependenciesDialogUnavailableError(
                f"Expected exactly one row named {label!r} to toggle, found {len(rows)}."
            )
        actions.toggle_focused_checkbox(dialog.handle.hwnd, rows[0].element, delay_s=key_delay_s)

    def read_dependency_root(self) -> live.DependencyRoot | None:
        _, webview = self._require_deps_dialog()
        return live.dependency_root(webview)

    def toggle_dependency_root(self, key_delay_s: float) -> None:
        dialog, webview = self._require_deps_dialog()
        root = None
        for _ in range(5):  # Chromium's tree can be briefly incomplete
            root = live.dependency_root(webview)
            if root is not None:
                break
            time.sleep(0.4)
        if root is None:
            raise DependenciesDialogUnavailableError("The dependency's elements are not shown.")
        actions.toggle_focused_checkbox(dialog.handle.hwnd, root.element, delay_s=key_delay_s)

    def press_dependencies_apply(self) -> None:
        actions.invoke(self._deps_element("Button", ss.APPLY_BUTTON_NAME))

    def read_dependencies_loading(self) -> str | None:
        if self._deps_dialog() is None:
            return None
        _, webview = self._require_deps_dialog()
        return live.dependencies_loading(webview)

    def press_dependencies_refresh_all(self) -> None:
        actions.invoke(self._deps_element("Button", ss.REFRESH_ALL_BUTTON_NAME))

    def _outdated_prompt_window(self) -> live.TopWindow | None:
        for window in self._windows():
            if window.role is ss.WindowRole.DIALOG and window.title == ss.OUTDATED_REFERENCES_TITLE:
                return window
        return None

    def dismiss_outdated_references_prompt(self) -> None:
        """Answer 'No' to 'Modified or missing references found' (back to the dialog)."""
        window = self._outdated_prompt_window()
        if window is None:
            return
        webview = live.child_webview(window.handle.hwnd) or window.handle.hwnd
        button = live.find_first(webview, control_type="Button", title=ss.NO_BUTTON_NAME)
        if button is None:
            raise DependenciesDialogUnavailableError("The prompt's 'No' button was not found.")
        actions.invoke(button)

    def cancel_dependencies_dialog(self) -> None:
        # A confirmation prompt on top of the dialog blocks Cancel: answer it first.
        self.dismiss_outdated_references_prompt()
        if self._deps_dialog() is None:
            return
        actions.invoke(self._deps_element("Button", ss.CANCEL_BUTTON_NAME))

    # --- Find Usages in all Modules ---------------------------------------------
    def _editor_main(self) -> live.TopWindow:
        """The main window whose selected tab is a module editor.

        Retried briefly: right after a tab switch the shell's UIA tree can be incomplete.
        """
        for attempt in range(6):
            for main in self._mains():
                tabs = ss.read_shell_tabs(live.snapshot_shell(main.handle.hwnd))
                if any(t.selected and t.kind is ss.ShellTabKind.MODULE_EDITOR for t in tabs):
                    return main
            if attempt < 5:
                time.sleep(0.5)
        raise FindUsagesUnavailableError("No module editor tab is in front.")

    def _editor_webview(self) -> tuple[int, int]:
        main = self._editor_main()
        webviews = live.visible_webviews(main.handle.hwnd)
        if not webviews:
            raise FindUsagesUnavailableError("The module editor has no readable content.")
        return main.handle.hwnd, webviews[0]

    def _quick_search_edit(self, webview: int) -> Any:
        edit = live.quick_search_edit(webview)
        if edit is None:
            raise FindUsagesUnavailableError("The quick-search box (Ctrl+F) was not found.")
        return edit

    def close_usages_panels(self) -> int:
        _, webview = self._editor_webview()
        buttons = live.usages_tab_close_buttons(webview)
        for button in buttons:
            actions.invoke(button)
        return len(buttons)

    def open_quick_search(self) -> None:
        hwnd, _ = self._editor_webview()
        actions.ensure_foreground(hwnd, live.element_info(hwnd))
        actions.press_keys(hwnd, ["^f"])

    def set_quick_search_text(self, text: str) -> None:
        # Setting the value through UIA closes the popup but keeps the text;
        # Ctrl+F then reopens it with the results for that text.
        hwnd, webview = self._editor_webview()
        actions.set_value(self._quick_search_edit(webview), text)
        time.sleep(0.3)
        actions.ensure_foreground(hwnd, live.element_info(hwnd))
        actions.press_keys(hwnd, ["^f"])

    def read_quick_search_results(self) -> list[ss.QuickSearchResult] | None:
        _, webview = self._editor_webview()
        popup = live.quick_search_popup(webview)
        return None if popup is None else ss.parse_quick_search(popup)

    def open_quick_search_result(self, index: int, key_delay_s: float) -> None:
        hwnd, webview = self._editor_webview()
        if live.quick_search_popup(webview) is None:
            raise FindUsagesUnavailableError("The quick-search results closed unexpectedly.")
        edit = self._quick_search_edit(webview)
        if not live.has_keyboard_focus(edit):
            raise FindUsagesUnavailableError("The quick-search box does not have the focus.")
        # The first result is highlighted when the popup opens; Down moves to the next.
        actions.press_keys(hwnd, ["{DOWN}"] * index, delay_s=key_delay_s)
        actions.press_enter_on(hwnd, edit, delay_s=key_delay_s)

    def open_element_context_menu(self) -> None:
        hwnd, webview = self._editor_webview()
        tree_input = live.element_tree_input(webview)
        if tree_input is None:
            raise FindUsagesUnavailableError("The elements tree was not found.")
        actions.ensure_foreground(hwnd, live.element_info(hwnd))
        actions.set_focus(tree_input)
        actions.press_keys(hwnd, ["+{F10}"])

    def _menu(self) -> live.TopWindow | None:
        menus = live.context_menu_windows(self._pid())
        return menus[0] if menus else None

    def read_context_menu(self) -> list[live.MenuEntry]:
        menu = self._menu()
        return [] if menu is None else live.context_menu_entries(menu.handle.hwnd)

    def run_context_menu_item(self, name: str, key_delay_s: float) -> None:
        """Move the menu's keyboard focus to ``name`` with Down, verified each step, then Enter."""
        menu = self._menu()
        if menu is None:
            raise FindUsagesUnavailableError("The context menu closed unexpectedly.")
        entries = live.context_menu_entries(menu.handle.hwnd)
        main_hwnd, _ = self._editor_webview()
        for _ in range(len(entries) + 2):
            focused = [e.name for e in live.context_menu_entries(menu.handle.hwnd) if e.focused]
            if focused == [name]:
                items = live.descendants_robust(
                    live.element_info(menu.handle.hwnd), "MenuItem", name
                )
                if len(items) != 1:
                    break
                actions.press_enter_on(main_hwnd, items[0], delay_s=key_delay_s)
                return
            actions.press_keys(main_hwnd, ["{DOWN}"], delay_s=key_delay_s)
        raise FindUsagesUnavailableError(f"Could not move to the menu item {name!r}.")

    def close_popups(self) -> None:
        try:
            hwnd, webview = self._editor_webview()
        except NavigatorError:
            return
        for _ in range(2):
            if self._menu() is None and live.quick_search_popup(webview) is None:
                return
            if live.foreground_window() != hwnd:
                return  # the popups close by themselves when Service Studio loses focus
            actions.press_keys(hwnd, ["{ESC}"])

    def read_usages_panel(self) -> ss.UsagesPanel | None:
        _, webview = self._editor_webview()
        return live.usages_panel(webview)
