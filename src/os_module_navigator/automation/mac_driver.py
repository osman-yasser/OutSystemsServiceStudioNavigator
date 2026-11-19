"""macOS implementation of ``ServiceStudioDriver``.

Native parts of Service Studio (windows, shell tabs, captions, tab close
buttons, dialog titles) are read through the Accessibility API (``mac_ax``).
The "Open from Environment" dialog's content is Chromium, invisible to AX on
macOS, so it is read from a screenshot with Vision text recognition
(``mac_ocr``) and driven with keys posted to Service Studio (``mac_actions``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from os_module_navigator.automation import mac_actions, mac_ax, mac_ocr
from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.driver import UiState
from os_module_navigator.automation.live import DependencyCheckbox, DependencyRoot
from os_module_navigator.errors import (
    DependenciesDialogUnavailableError,
    ElementNotFoundError,
    NavigatorError,
    SearchDialogUnavailableError,
    ServiceStudioNotRunningError,
    ServiceStudioWindowNotFoundError,
    UnexpectedDialogError,
    UsagesUnavailableError,
)

# The module editor toolbar's "Manage Dependencies" (plug) button, in points from the
# main window's top-left corner. The toolbar is Chromium content, invisible to AX,
# and Service Studio for Mac has no working keyboard shortcut for the dialog
# (Ctrl+Q does nothing; Cmd+Q would quit the app).
MANAGE_DEPENDENCIES_BUTTON_PT = (158.5, 51.5)
DEPENDENCY_KEYS = {"{DOWN}": "down", "{UP}": "up"}


@dataclass
class _DepsView:
    """One screenshot of Manage Dependencies, parsed."""

    window: mac_ax.MacWindow
    shot: mac_ocr.Screenshot
    scale: float
    layout: mac_ocr.DependencyLayout

    def point(self, px: float, py: float) -> tuple[float, float]:
        """Image pixels -> screen points."""
        x, y, _, _ = self.window.frame or (0, 0, 0, 0)
        return (x + px / self.scale, y + py / self.scale)


class MacServiceStudioDriver:
    """Talks to the running Service Studio for Mac. Call from a single thread."""

    def __init__(self) -> None:
        self._query: str | None = None
        self._last_rows: list[mac_ocr.TableRow] = []
        self._selected_module: str | None = None
        self._dependency_query: str | None = None
        self._element_query: str | None = None

    # --- lookups -----------------------------------------------------------

    def _pid(self) -> int:
        mac_ax.require_accessibility()
        pid = mac_ax.find_pid()
        if pid is None:
            raise ServiceStudioNotRunningError("Service Studio is not running.")
        return pid

    def _main(self, pid: int) -> mac_ax.MacWindow:
        mains = mac_ax.main_windows(pid)
        if not mains:
            raise ServiceStudioWindowNotFoundError("The Service Studio main window was not found.")
        return mains[0]

    def _dialogs(self, pid: int) -> list[mac_ax.MacWindow]:
        return [w for w in mac_ax.windows(pid) if w.role is ss.WindowRole.DIALOG]

    def _search_dialog(self, pid: int) -> mac_ax.MacWindow | None:
        return next(
            (w for w in self._dialogs(pid) if w.title == ss.OPEN_FROM_ENVIRONMENT_TITLE), None
        )

    def _require_search_dialog(self, pid: int) -> mac_ax.MacWindow:
        dialog = self._search_dialog(pid)
        if dialog is None:
            raise SearchDialogUnavailableError("The 'Open from Environment' dialog is not open.")
        return dialog

    def _tab_items(self, pid: int) -> list[tuple[mac_ax.MacWindow, Any]]:
        """Live tabs of every main window, in the same order as ``read_state().tabs``."""
        return [
            (main, item)
            for main in mac_ax.main_windows(pid)
            for item in mac_ax.tab_items(main.tab_strip)
        ]

    def _tab_item_at(
        self, pid: int, index: int, *, attempts: int = 6, delay_s: float = 0.5
    ) -> tuple[mac_ax.MacWindow, Any]:
        for attempt in range(attempts):
            items = self._tab_items(pid)
            if 0 <= index < len(items):
                return items[index]
            if attempt < attempts - 1:
                time.sleep(delay_s)
        raise NavigatorError("The requested Service Studio tab no longer exists.")

    def _read_table(self, pid: int) -> tuple[list[mac_ocr.TableRow], mac_ocr.Screenshot]:
        mac_ax.require_screen_capture()
        number = mac_ax.window_number(pid, ss.OPEN_FROM_ENVIRONMENT_TITLE)
        shot = mac_ocr.capture_window(number) if number is not None else None
        if shot is None:
            raise SearchDialogUnavailableError("The search dialog could not be captured.")
        rows = mac_ocr.parse_results(mac_ocr.recognize_text(shot))
        if rows is None:
            raise SearchDialogUnavailableError("The search results table was not found.")
        if self._query:
            # OCR can misread I/l/1 or O/0. When a Module cell equals the search
            # text up to such confusions, it is that module; opening still verifies
            # the exact name on the new tab's caption (read through AX).
            for r in rows:
                module = r.row.cells.get("Module", "")
                if module and mac_ocr.ocr_equivalent(module, self._query):
                    r.row.cells["Module"] = mac_ocr.repair_confusables(module, self._query)
        return rows, shot

    # --- reads -------------------------------------------------------------

    def read_state(self) -> UiState:
        mac_ax.require_accessibility()
        pid = mac_ax.find_pid()
        if pid is None:
            return UiState(running=False, main_window=False)
        windows = mac_ax.windows(pid)
        mains = sorted(
            (w for w in windows if w.role is ss.WindowRole.MAIN),
            key=lambda w: w.frame or (0, 0, 0, 0),
        )
        tabs = [
            mac_ax.read_tab(item) for main in mains for item in mac_ax.tab_items(main.tab_strip)
        ]
        dialogs = [w.title for w in windows if w.role is ss.WindowRole.DIALOG]
        return UiState(running=True, main_window=bool(mains), tabs=tabs, dialogs=dialogs)

    def read_search_rows(self) -> list[ss.ModuleSearchRow]:
        pid = self._pid()
        self._require_search_dialog(pid)
        rows, _ = self._read_table(pid)
        self._last_rows = rows
        return [r.row for r in rows]

    # --- interactions ------------------------------------------------------

    def _wait_for_dialog(self, pid: int, timeout_s: float, poll_s: float) -> str:
        start = time.monotonic()
        saw_transient = False
        while True:
            titles = [w.title for w in self._dialogs(pid)]
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
            if time.monotonic() - start >= timeout_s:
                return "loading" if saw_transient else "nothing"
            time.sleep(poll_s)

    def open_search_dialog(self, timeout_s: float, poll_s: float) -> None:
        pid = self._pid()
        self._query = None
        if self._search_dialog(pid) is not None:
            return
        main = self._main(pid)
        others = [w.title for w in self._dialogs(pid)]
        if others:
            raise UnexpectedDialogError(
                f"Service Studio is showing another dialog ({others[0]!r}); close it first."
            )
        # The environment page's "Search Modules" button is Chromium content AX
        # cannot reach on macOS, so use the keyboard shortcut.
        mac_actions.ensure_focus(pid, main.element)
        mac_actions.press_keys(pid, main.element, ["cmd+o"])
        outcome = self._wait_for_dialog(pid, timeout_s, poll_s)
        if outcome == "loading":
            raise SearchDialogUnavailableError(
                "Service Studio is still loading the module list; try again shortly."
            )
        if outcome != "open":
            raise SearchDialogUnavailableError("The 'Open from Environment' dialog did not open.")
        time.sleep(0.5)  # let the search box take focus

    def set_search_text(self, text: str) -> None:
        pid = self._pid()
        dialog = self._require_search_dialog(pid)
        mac_actions.ensure_focus(pid, dialog.element)
        mac_actions.type_search_text(pid, dialog.element, text)
        self._query = text
        self._selected_module = None

    def _highlighted_module(self, pid: int) -> str | None:
        rows, shot = self._read_table(pid)
        index = mac_ocr.selected_row_index(rows, shot.saturated_fraction)
        return rows[index].row.cells.get("Module") if index is not None else None

    def select_search_row(self, index: int, rendered_rows: int, key_delay_s: float) -> None:
        pid = self._pid()
        dialog = self._require_search_dialog(pid)
        rows = self._last_rows
        if not 0 <= index < len(rows) or not rows[index].row.on_screen:
            raise NavigatorError("The requested search row is not on screen.")
        expected = rows[index].row.cells.get("Module", "")
        mac_actions.ensure_focus(pid, dialog.element)
        # Up past the top makes the starting row deterministic (row 0), then Down to index.
        keys = ["up"] * max(rendered_rows, 1) + ["down"] * index
        mac_actions.press_keys(pid, dialog.element, keys, delay_s=key_delay_s)
        time.sleep(0.3)
        highlighted = self._highlighted_module(pid)
        if highlighted is None or not ss.same_module_name(highlighted, expected):
            raise NavigatorError(
                f"Could not confirm that {expected!r} is selected in the search dialog "
                f"(selected: {highlighted!r}); nothing was opened."
            )
        self._selected_module = expected

    def press_open(self) -> None:
        pid = self._pid()
        dialog = self._require_search_dialog(pid)
        expected = self._selected_module
        if not expected:
            raise NavigatorError("No search row was selected; nothing was opened.")

        def still_selected() -> bool:
            highlighted = self._highlighted_module(pid)
            return highlighted is not None and ss.same_module_name(highlighted, expected)

        mac_actions.confirm_open(pid, dialog.element, still_selected)
        self._selected_module = None

    def cancel_search_dialog(self) -> None:
        pid = self._pid()
        dialog = self._search_dialog(pid)
        if dialog is None:
            return
        mac_actions.ensure_focus(pid, dialog.element)
        mac_actions.press_keys(pid, dialog.element, ["escape"])
        self._query = None

    def select_tab(self, index: int) -> None:
        pid = self._pid()
        main, item = self._tab_item_at(pid, index)
        mac_actions.click_tab(pid, main.element, item)

    def close_module_tab(self, index: int, module_name: str) -> None:
        pid = self._pid()
        _, item = self._tab_item_at(pid, index)
        mac_actions.close_module_tab(item, module_name)

    # --- Manage Dependencies -------------------------------------------------------
    #
    # The dialog is Chromium content: read from screenshots (mac_ocr), driven with
    # hit-tested clicks and keys (mac_actions). Every change is re-read from a new
    # screenshot by the provider before it moves on, and nothing is applied unless
    # the provider confirmed the checkbox states.

    def _window_titled(self, pid: int, title: str) -> mac_ax.MacWindow | None:
        return next((w for w in self._dialogs(pid) if w.title == title), None)

    def _deps_window(self, pid: int) -> mac_ax.MacWindow:
        window = self._window_titled(pid, ss.MANAGE_DEPENDENCIES_TITLE)
        if window is None:
            raise DependenciesDialogUnavailableError(
                "The 'Manage Dependencies' dialog is not open."
            )
        return window

    def _deps_view(self) -> _DepsView:
        pid = self._pid()
        window = self._deps_window(pid)
        mac_ax.require_screen_capture()
        number = mac_ax.window_number(pid, ss.MANAGE_DEPENDENCIES_TITLE)
        shot = mac_ocr.capture_window(number) if number is not None else None
        if shot is None or not window.frame:
            raise DependenciesDialogUnavailableError(
                "The dependencies dialog could not be captured."
            )
        scale = shot.width / window.frame[2]
        layout = mac_ocr.parse_dependency_dialog(mac_ocr.recognize_text(shot), scale)
        if layout is None:
            raise DependenciesDialogUnavailableError("The dependencies dialog could not be read.")
        return _DepsView(window, shot, scale, layout)

    def _click(self, view: _DepsView, px: float, py: float) -> None:
        mac_actions.click_in_window(self._pid(), view.window.element, view.point(px, py))

    def _click_box(self, view: _DepsView, box: mac_ocr.TextBox) -> None:
        self._click(view, box.x + box.w / 2, box.cy)

    def _button(self, view: _DepsView, name: str) -> mac_ocr.TextBox:
        box = view.layout.buttons.get(name)
        if box is None:
            raise DependenciesDialogUnavailableError(
                f"'{name}' was not found in the 'Manage Dependencies' dialog."
            )
        return box

    @staticmethod
    def _repair(text: str, query: str | None) -> str:
        if query and mac_ocr.ocr_equivalent(text, query):
            return mac_ocr.repair_confusables(text, query)
        return text

    def open_dependencies_dialog(self, timeout_s: float, poll_s: float) -> None:
        pid = self._pid()
        if self._window_titled(pid, ss.MANAGE_DEPENDENCIES_TITLE) is not None:
            return
        others = [w.title for w in self._dialogs(pid)]
        if others:
            raise UnexpectedDialogError(
                f"Service Studio is showing another dialog ({others[0]!r}); close it first."
            )
        main = self._main(pid)
        if not main.frame:
            raise ServiceStudioWindowNotFoundError("The Service Studio main window has no frame.")
        button = (
            main.frame[0] + MANAGE_DEPENDENCIES_BUTTON_PT[0],
            main.frame[1] + MANAGE_DEPENDENCIES_BUTTON_PT[1],
        )
        self._dependency_query = self._element_query = None
        started = time.monotonic()
        retried = False
        mac_actions.click_in_window(pid, main.element, button)
        while time.monotonic() - started < timeout_s:
            titles = [w.title for w in self._dialogs(pid)]
            if ss.MANAGE_DEPENDENCIES_TITLE in titles and not any(
                t in ss.TRANSIENT_DIALOG_TITLES for t in titles
            ):
                time.sleep(max(poll_s, 1.0))  # let the lists render
                return
            unexpected = [
                t
                for t in titles
                if t not in ss.TRANSIENT_DIALOG_TITLES and t != ss.MANAGE_DEPENDENCIES_TITLE
            ]
            if unexpected:
                # The click hit another toolbar button: close what it opened.
                window = self._window_titled(pid, unexpected[0])
                if window is not None:
                    mac_actions.ensure_focus(pid, window.element)
                    mac_actions.press_keys(pid, window.element, ["escape"])
                raise UnexpectedDialogError(
                    f"Service Studio opened {unexpected[0]!r} instead of 'Manage Dependencies'; "
                    "it was closed."
                )
            if not retried and not titles and time.monotonic() - started > min(5.0, timeout_s / 3):
                retried = True
                mac_actions.click_in_window(pid, main.element, button)
            time.sleep(poll_s)
        raise DependenciesDialogUnavailableError("The 'Manage Dependencies' dialog did not open.")

    def set_dependency_search(self, text: str) -> None:
        self.focus_dependency_search()
        pid = self._pid()
        window = self._deps_window(pid)
        mac_actions.type_search_text(pid, window.element, text)
        self._dependency_query = text

    def read_dependency_rows(self) -> list[tuple[str, bool]]:
        view = self._deps_view()
        return [
            (self._repair(name, self._dependency_query), ext) for name, ext in view.layout.modules
        ]

    def focus_dependency_search(self) -> None:
        view = self._deps_view()
        box = view.layout.module_search
        if box is None:
            raise DependenciesDialogUnavailableError("The module search box was not found.")
        # Click the text, away from the clear (x) button at the box's right end.
        self._click(view, box.x + min(box.w, 60 * view.scale) / 2, box.cy)

    def press_dependency_key(self, key: str, key_delay_s: float) -> None:
        name = DEPENDENCY_KEYS.get(key)
        if name is None:
            raise NavigatorError(f"Key {key!r} is not supported on macOS.")
        pid = self._pid()
        window = self._deps_window(pid)
        mac_actions.press_keys(pid, window.element, [name], delay_s=key_delay_s)

    def show_all_dependency_elements(self) -> None:
        for _ in range(2):
            view = self._deps_view()
            box = self._button(view, "All")
            if view.shot.is_accent(box):
                return
            self._click_box(view, box)
            time.sleep(0.6)
        raise DependenciesDialogUnavailableError("The elements 'All' filter could not be selected.")

    def set_element_search(self, text: str) -> None:
        if not text and not self._element_query:
            return
        if not text:
            # Clearing would need Backspace, which is never sent; the filter does not
            # affect what Apply changes, so leave it.
            return
        view = self._deps_view()
        box = view.layout.element_search
        if box is None:
            raise DependenciesDialogUnavailableError("The elements search box was not found.")
        self._click(view, box.x + min(box.w, 60 * view.scale) / 2, box.cy)
        pid = self._pid()
        mac_actions.type_search_text(pid, view.window.element, text)
        self._element_query = text

    def _checkboxes(self, view: _DepsView) -> list[DependencyCheckbox]:
        rows = []
        origin_x = view.layout.elements[0].box[0] if view.layout.elements else 0.0
        step = mac_ocr.TREE_STEP_PT * view.scale
        for i, row in enumerate(view.layout.elements):
            box = row.box
            state = mac_ocr.checkbox_state(view.shot.pixel, *box, view.scale)
            if (
                state is None
                and i > 0
                and mac_ocr.is_selection_fill(view.shot.pixel, *box, view.scale)
            ):
                # A selected row is drawn on the accent colour and Vision may place its
                # text differently, so the indentation level can come out wrong. Try
                # the neighbouring levels; accept only a position with a real checkbox.
                for k in (1, 2, 3, -1, -2):
                    x = box[0] + k * step
                    if x < origin_x:
                        continue
                    found = mac_ocr.checkbox_state(view.shot.pixel, x, box[1], view.scale)
                    if found is not None:
                        box, state = (x, box[1]), found
                        break
            if state is None:
                continue  # an element this module cannot reference (no checkbox)
            query = self._dependency_query if i == 0 else self._element_query
            rows.append(
                DependencyCheckbox(box, self._repair(row.label, query), state, int(row.top))
            )
        return rows

    def read_dependency_checkboxes(self) -> list[DependencyCheckbox]:
        return self._checkboxes(self._deps_view())

    def toggle_dependency_checkbox(self, label: str, key_delay_s: float) -> None:
        view = self._deps_view()
        boxes = self._checkboxes(view)
        rows = [r for r in boxes[1:] if ss.same_tree_label(r.label, label)]
        if len(rows) != 1:
            seen = [(r.label, r.state) for r in boxes]
            parsed = [
                f"{row.label} @{tuple(int(v) for v in row.box)} "
                + mac_ocr.checkbox_stats(view.shot.pixel, *row.box, view.scale)
                for row in view.layout.elements
            ]
            raise DependenciesDialogUnavailableError(
                f"Expected exactly one row named {label!r} to toggle, found {len(rows)}. "
                f"Checkbox rows read: {seen}; tree rows read: {parsed}."
            )
        self._click(view, *rows[0].element)

    def read_dependency_root(self) -> DependencyRoot | None:
        rows = self._checkboxes(self._deps_view())
        if not rows:
            return None
        return DependencyRoot(element=rows[0].element, label=rows[0].label, state=rows[0].state)

    def toggle_dependency_root(self, key_delay_s: float) -> None:
        for _ in range(5):  # the tree can still be rendering
            view = self._deps_view()
            rows = self._checkboxes(view)
            if rows:
                self._click(view, *rows[0].element)
                return
            time.sleep(0.4)
        raise DependenciesDialogUnavailableError("The dependency's elements are not shown.")

    def _press_and_confirm(self, name: str) -> None:
        """Click a footer button; click once more if the first click was swallowed."""
        pid = self._pid()
        for _ in range(2):
            view = self._deps_view()
            self._click_box(view, self._button(view, name))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                titles = [w.title for w in self._dialogs(pid)]
                if titles != [ss.MANAGE_DEPENDENCIES_TITLE]:
                    return  # closed, or a prompt / progress window appeared
            if name != "Apply":
                return
        # Still open with nothing else showing: the provider's wait reports it.

    def press_dependencies_apply(self) -> None:
        self._press_and_confirm("Apply")

    def can_refresh_all(self) -> bool:
        view = self._deps_view()
        box = view.layout.buttons.get("Refresh all")
        return box is not None and view.shot.has_bright_text(box)

    def press_dependencies_refresh_all(self) -> None:
        view = self._deps_view()
        self._click_box(view, self._button(view, "Refresh all"))
        time.sleep(0.5)

    def press_dependency_refresh(self) -> bool:
        view = self._deps_view()
        box = view.layout.buttons.get("Refresh")
        if box is None or not view.shot.has_bright_text(box):
            return False
        self._click_box(view, box)
        time.sleep(0.5)
        return True

    def dismiss_outdated_references_prompt(self) -> None:
        """Answer 'No' to 'Modified or missing references found' (back to the dialog)."""
        pid = self._pid()
        window = self._window_titled(pid, ss.OUTDATED_REFERENCES_TITLE)
        if window is None or not window.frame:
            return
        number = mac_ax.window_number(pid, ss.OUTDATED_REFERENCES_TITLE)
        shot = mac_ocr.capture_window(number) if number is not None else None
        if shot is None:
            raise DependenciesDialogUnavailableError("The prompt could not be captured.")
        boxes = [b for b in mac_ocr.recognize_text(shot) if b.text.strip() == ss.NO_BUTTON_NAME]
        if len(boxes) != 1:
            raise DependenciesDialogUnavailableError("The prompt's 'No' button was not found.")
        scale = shot.width / window.frame[2]
        box = boxes[0]
        point = (
            window.frame[0] + (box.x + box.w / 2) / scale,
            window.frame[1] + box.cy / scale,
        )
        mac_actions.click_in_window(pid, window.element, point)
        time.sleep(0.5)

    def cancel_dependencies_dialog(self) -> None:
        # A confirmation prompt on top of the dialog blocks Cancel: answer it first.
        self.dismiss_outdated_references_prompt()
        pid = self._pid()
        window = self._window_titled(pid, ss.MANAGE_DEPENDENCIES_TITLE)
        if window is None:
            return
        mac_actions.ensure_focus(pid, window.element)
        mac_actions.press_keys(pid, window.element, ["escape"])
        self._dependency_query = self._element_query = None

    # --- Find Usages -------------------------------------------------------------
    #
    # The module editor is Chromium content: read from screenshots of the main window,
    # driven with Cmd+F and the element name, hit-tested clicks and one right click on
    # the elements tree. The context menu is a native popup whose items have no text
    # in AX, so it is read from its own screenshot before the item is clicked.

    def _main_view(
        self,
    ) -> tuple[mac_ax.MacWindow, mac_ocr.Screenshot, float, list[mac_ocr.TextBox]]:
        pid = self._pid()
        main = self._main(pid)
        mac_ax.require_screen_capture()
        number = mac_ax.window_number(pid, ss.MAIN_WINDOW_TITLE)
        shot = mac_ocr.capture_window(number) if number is not None else None
        if shot is None or not main.frame:
            raise UsagesUnavailableError("The Service Studio window could not be captured.")
        return main, shot, shot.width / main.frame[2], mac_ocr.recognize_text(shot)

    @staticmethod
    def _to_screen(
        window: mac_ax.MacWindow, scale: float, box: mac_ocr.TextBox
    ) -> tuple[float, float]:
        x, y, _, _ = window.frame or (0, 0, 0, 0)
        return (x + (box.x + min(box.w, 200 * scale) / 2) / scale, y + box.cy / scale)

    def select_element(self, name: str, timeout_s: float, poll_s: float) -> None:
        """Open ``name`` from the module search drop-down's element suggestions and check
        that the elements tree and the properties panel show it."""
        pid = self._pid()
        main = self._main(pid)
        mac_actions.ensure_focus(pid, main.element)
        mac_actions.press_keys(pid, main.element, ["cmd+f"])
        time.sleep(0.6)
        mac_actions.type_module_search(pid, main.element, name)
        suggestions: list[mac_ocr.TextBox] = []
        anchor_seen = False
        deadline = time.monotonic() + min(timeout_s, 10.0)
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            main, _, scale, boxes = self._main_view()
            anchor_seen, suggestions = mac_ocr.element_suggestions(boxes, name)
            if anchor_seen and suggestions:
                break
        if len(suggestions) != 1:
            self.dismiss_popups()
            if not anchor_seen:
                raise ElementNotFoundError("The module search drop-down did not open.")
            raise ElementNotFoundError(
                f"The module search suggests {len(suggestions)} elements named {name!r}; "
                "nothing was opened."
                if suggestions
                else f"No element named {name!r} was found in this module."
            )
        mac_actions.click_in_window(pid, main.element, self._to_screen(main, scale, suggestions[0]))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            _, shot, _, boxes = self._main_view()
            if len(mac_ocr.element_rows(boxes, name, shot.width)) >= 2:
                return
        raise ElementNotFoundError(
            f"Service Studio did not show {name!r} in the elements tree after opening it."
        )

    def open_usages_in_all_modules(self, name: str, timeout_s: float, poll_s: float) -> None:
        """Right-click the element's tree row and choose 'Find Usages in all Modules'."""
        pid = self._pid()
        main, shot, scale, boxes = self._main_view()
        rows = mac_ocr.element_rows(boxes, name, shot.width)
        if len(rows) < 2:
            raise ElementNotFoundError(f"{name!r} is not selected in the elements tree.")
        if mac_ax.menu_windows(pid):
            self.dismiss_popups()
        mac_actions.right_click_in_window(pid, main.element, self._to_screen(main, scale, rows[0]))
        menu = None
        deadline = time.monotonic() + min(timeout_s, 5.0)
        while menu is None and time.monotonic() < deadline:
            time.sleep(poll_s / 2)
            menus = mac_ax.menu_windows(pid)
            menu = menus[0] if len(menus) == 1 else None
        if menu is None or not menu.frame:
            raise UsagesUnavailableError("The element's context menu did not open.")
        number = mac_ax.window_number_at(pid, menu.frame)
        menu_shot = mac_ocr.capture_window(number) if number is not None else None
        if menu_shot is None:
            self.dismiss_popups()
            raise UsagesUnavailableError("The context menu could not be captured.")
        found = mac_ocr.find_text(mac_ocr.recognize_text(menu_shot), mac_ocr.FIND_USAGES_ALL_ITEM)
        if len(found) != 1:
            self.dismiss_popups()
            raise UsagesUnavailableError(
                f"'{mac_ocr.FIND_USAGES_ALL_ITEM}' was not found in the element's context menu."
            )
        menu_scale = menu_shot.width / menu.frame[2]
        box = found[0]
        point = (
            menu.frame[0] + (box.x + box.w / 2) / menu_scale,
            menu.frame[1] + box.cy / menu_scale,
        )
        mac_actions.click_menu_item(pid, menu.element, point)

    def read_usages(self) -> ss.UsagesPane | None:
        _, _, _, boxes = self._main_view()
        return mac_ocr.parse_usages_pane(boxes)

    def dismiss_popups(self) -> None:
        """Close an open context menu or search drop-down with Escape."""
        pid = self._pid()
        main = self._main(pid)
        mac_actions.ensure_focus(pid, main.element)
        mac_actions.press_keys(pid, main.element, ["escape"])
