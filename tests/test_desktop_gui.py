import asyncio
from typing import ClassVar

import pytest

from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.driver import UiState
from os_module_navigator.automation.live import DependencyCheckbox, DependencyRoot
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
    ModuleNotFoundInEnvironmentError,
    ModuleNotOpenError,
    NavigatorError,
    NotConnectedError,
    ServiceStudioNotRunningError,
    TooManyOpenModulesError,
    UnexpectedDialogError,
    UnsavedChangesError,
    WrongDependencySelectedError,
    WrongModuleOpenedError,
)
from os_module_navigator.models import OpenStatus
from os_module_navigator.providers.base import ModuleProvider
from os_module_navigator.providers.desktop_gui import DesktopGUINavigator, DesktopGUIProvider

ENV = ss.ShellTab(
    type_name="x.ServerPageTopLevelView", kind=ss.ShellTabKind.ENVIRONMENT, caption="Dev - host"
)
DIALOG = ss.OPEN_FROM_ENVIRONMENT_TITLE


def editor(caption: str, selected: bool = False) -> ss.ShellTab:
    return ss.ShellTab(
        type_name="x.AggregatorView",
        kind=ss.ShellTabKind.MODULE_EDITOR,
        caption=caption,
        selected=selected,
    )


def row(module: str, app: str = "App", on_screen: bool = True) -> ss.ModuleSearchRow:
    return ss.ModuleSearchRow(
        cells={"Module": module, "Application": app, "Changed": "today", "By": "someone"},
        on_screen=on_screen,
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDriver:
    """Simulates Service Studio: a catalogue of modules, a search dialog and tabs."""

    def __init__(self, catalogue, tabs=None, *, running=True, dialogs=None):
        self.catalogue = catalogue
        self.state = UiState(
            running=running,
            main_window=running,
            tabs=list(tabs if tabs is not None else [ENV]),
            dialogs=list(dialogs or []),
        )
        self.rows = catalogue[:12]
        self.selected_row = 0
        self.calls: list[tuple] = []
        self.open_effect = "open_selected"  # or "nothing", "dialog"
        self.close_effect = "close"  # or "nothing", "dialog"

    def read_state(self):
        self.calls.append(("read_state",))
        return UiState(
            running=self.state.running,
            main_window=self.state.main_window,
            tabs=[t.model_copy() for t in self.state.tabs],
            dialogs=list(self.state.dialogs),
        )

    def open_search_dialog(self, timeout_s, poll_s):
        self.calls.append(("open_search_dialog",))
        if DIALOG not in self.state.dialogs:
            self.state.dialogs.append(DIALOG)
            self.rows = self.catalogue[:12]

    def set_search_text(self, text):
        self.calls.append(("set_search_text", text))
        needle = text.casefold()
        self.rows = [r for r in self.catalogue if needle in " ".join(r.cells.values()).casefold()]
        self.selected_row = 0

    def read_search_rows(self):
        return list(self.rows)

    def select_search_row(self, index, rendered_rows, key_delay_s):
        self.calls.append(("select_search_row", index, rendered_rows))
        self.selected_row = index

    def press_open(self):
        self.calls.append(("press_open",))
        self.state.dialogs.remove(DIALOG)
        if self.open_effect == "open_selected":
            chosen = self.rows[self.selected_row].cells
            for tab in self.state.tabs:
                tab.selected = False
            self.state.tabs.append(editor(f"{chosen['Module']} ({chosen['Application']})", True))
        elif self.open_effect == "dialog":
            self.state.dialogs.append("Upgrade Module")

    def cancel_search_dialog(self):
        self.calls.append(("cancel_search_dialog",))
        if DIALOG in self.state.dialogs:
            self.state.dialogs.remove(DIALOG)

    def select_tab(self, index):
        self.calls.append(("select_tab", index))
        for i, tab in enumerate(self.state.tabs):
            tab.selected = i == index

    def close_module_tab(self, index, module_name):
        self.calls.append(("close_module_tab", index, module_name))
        if self.close_effect == "close":
            del self.state.tabs[index]
        elif self.close_effect == "dialog":
            self.state.dialogs.append("Unsaved Changes")

    # --- Manage Dependencies simulation ---
    DEPS: ClassVar[list[tuple[str, bool]]] = [
        ("OfficeUtils", False),
        ("OfficeUtils", True),
        ("OutSystemsCharts", False),
        ("OutSystemsUI", False),
    ]

    def _deps_setup(self):
        if not hasattr(self, "deps_rows"):
            self.deps_rows = list(self.DEPS)
            self.deps_selected = None
            self.deps_cursor = -1
            self.element_filter = ""
            self.root_states = {}
            self.applied = {}
            self.wrong_selection = False
            self.apply_effect = "close"  # or "dialog", "hang"
            self.toggle_works = True

    def open_dependencies_dialog(self, timeout_s, poll_s):
        self._deps_setup()
        self.calls.append(("open_dependencies_dialog",))
        if ss.MANAGE_DEPENDENCIES_TITLE not in self.state.dialogs:
            self.state.dialogs.append(ss.MANAGE_DEPENDENCIES_TITLE)
            self.deps_rows = list(self.DEPS)

    def set_dependency_search(self, text):
        self._deps_setup()
        self.calls.append(("set_dependency_search", text))
        needle = text.casefold()
        self.deps_rows = [r for r in self.DEPS if needle in r[0].casefold()]
        self.deps_selected = None

    def read_dependency_rows(self):
        self._deps_setup()
        return list(self.deps_rows)

    def focus_dependency_search(self):
        self._deps_setup()
        self.calls.append(("focus_dependency_search",))
        self.deps_cursor = -1  # focus is in the search box, nothing selected

    def press_dependency_key(self, key, key_delay_s):
        self.calls.append(("press_dependency_key", key))
        # The first Down only enters the list; the next ones select rows 0, 1, ...
        self.deps_cursor = min(self.deps_cursor + 1, len(self.deps_rows))
        row = self.deps_cursor - 1
        if self.wrong_selection:
            self.deps_selected = "WRONG"
        elif 0 <= row < len(self.deps_rows):
            self.deps_selected = self.deps_rows[row][0]

    def show_all_dependency_elements(self):
        self.calls.append(("show_all_dependency_elements",))

    def read_dependency_root(self):
        if self.deps_selected is None:
            return None
        return DependencyRoot(
            element=None,
            label=self.deps_selected,
            state=self.root_states.get(self.deps_selected, 0),
        )

    def toggle_dependency_root(self, key_delay_s):
        self.calls.append(("toggle_dependency_root",))
        if self.toggle_works:
            current = self.root_states.get(self.deps_selected, 0)
            self.root_states[self.deps_selected] = 0 if current else 1

    def press_dependencies_refresh_all(self):
        self.calls.append(("press_dependencies_refresh_all",))

    outdated: ClassVar[frozenset[str]] = frozenset()

    def can_refresh_all(self):
        return bool(self.outdated)

    def press_dependency_refresh(self):
        self.calls.append(("press_dependency_refresh",))
        return self.deps_selected in self.outdated

    def dismiss_outdated_references_prompt(self):
        self.calls.append(("dismiss_outdated_references_prompt",))
        if ss.OUTDATED_REFERENCES_TITLE in self.state.dialogs:
            self.state.dialogs.remove(ss.OUTDATED_REFERENCES_TITLE)

    def press_dependencies_apply(self):
        self.calls.append(("press_dependencies_apply",))
        self.applied[self.deps_selected] = self.root_states.get(self.deps_selected, 0)
        if self.apply_effect == "outdated_once":
            self.apply_effect = "close"  # the second Apply goes through
            self.state.dialogs.append(ss.OUTDATED_REFERENCES_TITLE)
        elif self.apply_effect == "outdated_always":
            if ss.OUTDATED_REFERENCES_TITLE not in self.state.dialogs:
                self.state.dialogs.append(ss.OUTDATED_REFERENCES_TITLE)
        elif self.apply_effect == "close":
            self.state.dialogs.remove(ss.MANAGE_DEPENDENCIES_TITLE)
        elif self.apply_effect == "dialog":
            self.state.dialogs.append("Elements In Use")

    def cancel_dependencies_dialog(self):
        self.calls.append(("cancel_dependencies_dialog",))
        if ss.MANAGE_DEPENDENCIES_TITLE in self.state.dialogs:
            self.state.dialogs.remove(ss.MANAGE_DEPENDENCIES_TITLE)

    # element-level simulation: label -> state, per dependency
    ELEMENTS: ClassVar[dict[str, list[tuple[str, int]]]] = {
        "OutSystemsCharts": [
            ("UI Flows", 0),
            ("Charts", 0),
            ("BarChart", 1),
            ("DonutChart", 0),
            ("DonutChart_v1", 0),
        ]
    }

    def set_element_search(self, text):
        self.calls.append(("set_element_search", text))
        self.element_filter = text.casefold()

    def _visible_elements(self):
        self._deps_setup()
        if not hasattr(self, "element_states"):
            self.element_states = {dep: dict(rows) for dep, rows in self.ELEMENTS.items()}
            self.element_filter = ""
        states = self.element_states.get(self.deps_selected, {})
        return [(k, v) for k, v in states.items() if self.element_filter in k.casefold()]

    def read_dependency_checkboxes(self):
        self._deps_setup()
        root = self.read_dependency_root()
        if root is None:
            return []
        rows = [DependencyCheckbox(None, root.label, root.state, 0)]
        for i, (label, state) in enumerate(self._visible_elements(), 1):
            rows.append(DependencyCheckbox(None, label, state, i))
        return rows

    def toggle_dependency_checkbox(self, label, key_delay_s):
        self.calls.append(("toggle_dependency_checkbox", label))
        states = self.element_states[self.deps_selected]
        states[label] = 0 if states[label] else 1

    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    # --- Find Usages simulation ---
    ELEMENTS_IN_MODULE: ClassVar[set[str]] = {"Order_CreateOrUpdateWrapper"}
    USAGES: ClassVar[list[tuple[str, int]]] = [("(local)", 14), ("Shipping_BL", 6)]
    usages_footer = (20, 2)
    usages_delay_reads = 1
    selected_element: str | None = None
    usages_pane: ss.UsagesPane | None = None

    def select_element(self, name, timeout_s, poll_s):
        self.calls.append(("select_element", name))
        if name not in self.ELEMENTS_IN_MODULE:
            raise ElementNotFoundError(f"no {name}")
        self.selected_element = name

    def open_usages_in_all_modules(self, name, timeout_s, poll_s):
        self.calls.append(("open_usages_in_all_modules", name))
        assert self.selected_element == name
        self._reads_left = self.usages_delay_reads
        self.usages_pane = ss.UsagesPane(
            title=name,
            rows=list(self.USAGES),
            total_usages=self.usages_footer[0],
            consumer_modules=self.usages_footer[1],
        )

    def read_usages(self):
        if getattr(self, "_reads_left", 0) > 0:
            self._reads_left -= 1
            return None
        return self.usages_pane

    def dismiss_popups(self):
        self.calls.append(("dismiss_popups",))


CATALOGUE = [row("DGA_UI", "DGA UI"), row("MOD_DGA_UI", "MOD DGA UI"), row("Other", "Other App")]


def navigator(driver, **config):
    clock = FakeClock()
    cfg = NavigatorConfig(poll_interval_s=0.5, **config)
    return DesktopGUINavigator(driver, cfg, clock=clock, sleep=clock.sleep)


def test_provider_satisfies_protocol():
    provider = DesktopGUIProvider(navigator=navigator(FakeDriver(CATALOGUE)))
    try:
        assert isinstance(provider, ModuleProvider)
    finally:
        provider.close()


def test_search_returns_modules_and_closes_dialog_it_opened():
    driver = FakeDriver(CATALOGUE)
    result = navigator(driver).search("dga_ui")
    assert [m.name for m in result.modules] == ["DGA_UI", "MOD_DGA_UI"]
    assert result.modules[0].application == "DGA UI"
    assert not result.possibly_truncated
    assert driver.called("cancel_search_dialog")
    assert DIALOG not in driver.state.dialogs


def test_search_keeps_dialog_open_if_user_had_it_open():
    driver = FakeDriver(CATALOGUE, dialogs=[DIALOG])
    navigator(driver).search("Other")
    assert not driver.called("cancel_search_dialog")


def test_search_flags_possible_truncation():
    catalogue = [row(f"M{i}", on_screen=i < 10) for i in range(12)]
    assert navigator(FakeDriver(catalogue)).search("M").possibly_truncated


def test_search_times_out_when_results_never_settle():
    class Flapping(FakeDriver):
        def read_search_rows(self):
            self.n = getattr(self, "n", 0) + 1
            return [row(f"X{self.n}")]

    with pytest.raises(AutomationTimeoutError):
        navigator(Flapping(CATALOGUE), search_settle_timeout_s=3).search("DGA")


@pytest.mark.parametrize("query", ["", "   ", "a" * 201, "bad\x00name"])
def test_invalid_queries_are_rejected_before_touching_ui(query):
    driver = FakeDriver(CATALOGUE)
    with pytest.raises(NavigatorError):
        navigator(driver).search(query)
    assert not driver.called("open_search_dialog")


def test_not_running_and_not_connected():
    with pytest.raises(ServiceStudioNotRunningError):
        navigator(FakeDriver(CATALOGUE, running=False)).search("x")
    with pytest.raises(NotConnectedError):
        navigator(FakeDriver(CATALOGUE, tabs=[])).search("x")


def test_unexpected_dialog_blocks_work():
    driver = FakeDriver(CATALOGUE, dialogs=["Some Other Dialog"])
    with pytest.raises(UnexpectedDialogError):
        navigator(driver).open("DGA_UI")
    assert not driver.called("open_search_dialog")


def test_open_selects_exact_match_not_first_row():
    driver = FakeDriver([row("MOD_DGA_UI"), row("DGA_UI")])
    result = navigator(driver).open("dga_ui")
    assert result.status is OpenStatus.OPENED_VERIFIED
    assert result.module_name == "DGA_UI"
    assert driver.called("select_search_row") == [("select_search_row", 1, 2)]
    assert not driver.called("cancel_search_dialog")


def test_open_already_open_module_brings_tab_to_front():
    tabs = [ENV, editor("* DGA_UI (DGA UI)"), editor("Other (App)", selected=True)]
    driver = FakeDriver(CATALOGUE, tabs=tabs)
    result = navigator(driver).open("DGA_UI")
    assert result.status is OpenStatus.ALREADY_OPEN
    assert driver.called("select_tab") == [("select_tab", 1)]
    assert not driver.called("open_search_dialog")


def test_open_missing_module_suggests_and_closes_dialog():
    driver = FakeDriver(CATALOGUE)
    with pytest.raises(ModuleNotFoundInEnvironmentError) as err:
        navigator(driver).open("DGA")
    assert [m.name for m in err.value.suggestions] == ["DGA_UI", "MOD_DGA_UI"]
    assert driver.called("cancel_search_dialog")
    assert not driver.called("press_open")


def test_open_ambiguous_module_never_presses_open():
    driver = FakeDriver([row("Dup", "A"), row("dup", "B")])
    with pytest.raises(AmbiguousModuleError) as err:
        navigator(driver).open("Dup")
    assert [m.application for m in err.value.candidates] == ["A", "B"]
    assert not driver.called("press_open")


def test_open_detects_wrong_module():
    class WrongRow(FakeDriver):
        def select_search_row(self, index, rendered_rows, key_delay_s):
            self.selected_row = 0  # keyboard selection silently failed

    driver = WrongRow([row("MOD_DGA_UI"), row("DGA_UI")])
    with pytest.raises(WrongModuleOpenedError):
        navigator(driver).open("DGA_UI")


def test_open_unverified_when_nothing_happens():
    driver = FakeDriver(CATALOGUE)
    driver.open_effect = "nothing"
    result = navigator(driver, open_verify_timeout_s=2).open("DGA_UI")
    assert result.status is OpenStatus.OPENED_UNVERIFIED


def test_open_reports_dialog_left_waiting():
    driver = FakeDriver(CATALOGUE)
    driver.open_effect = "dialog"
    with pytest.raises(UnexpectedDialogError):
        navigator(driver, open_verify_timeout_s=2).open("DGA_UI")


def test_status_reports_tabs_and_current_module():
    tabs = [ENV, editor("* DGA_UI (DGA UI)", selected=True), editor("Other (App)")]
    status = navigator(FakeDriver(CATALOGUE, tabs=tabs)).status()
    assert status.running and status.window_detected
    assert status.environment_connected and status.environment == "Dev - host"
    assert status.current_module == "DGA_UI"
    assert status.current_module_unsaved is True
    assert status.open_modules == ["DGA_UI", "Other"]


def test_status_when_not_running():
    status = navigator(FakeDriver(CATALOGUE, running=False)).status()
    assert not status.running
    assert status.environment_connected is None


def test_async_provider_runs_on_worker_thread():
    provider = DesktopGUIProvider(navigator=navigator(FakeDriver(CATALOGUE)))
    try:
        result = asyncio.run(provider.search_modules("Other"))
        assert [m.name for m in result.modules] == ["Other"]
    finally:
        provider.close()


def test_transient_progress_dialog_is_waited_out():
    class Loading(FakeDriver):
        def read_state(self):
            state = super().read_state()
            if len(self.called("read_state")) == 1:
                state.dialogs.append("Progress")
            return state

    result = navigator(Loading(CATALOGUE)).search("Other")
    assert [m.name for m in result.modules] == ["Other"]


def test_persistent_progress_dialog_eventually_blocks():
    driver = FakeDriver(CATALOGUE, dialogs=["Progress"])
    with pytest.raises(UnexpectedDialogError):
        navigator(driver, dialog_open_timeout_s=2).search("Other")


def many_modules(n: int, *, unsaved: frozenset[int] = frozenset()) -> list[ss.ShellTab]:
    tabs = [ENV]
    for i in range(n):
        marker = "* " if i in unsaved else ""
        tabs.append(editor(f"{marker}M{i} (App)", selected=i == n - 1))
    return tabs


def test_open_modules_reports_count_limit_and_tabs():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(3, unsaved=frozenset({0})))
    result = navigator(driver).open_modules()
    assert result.count == 3 and result.limit == 10 and result.can_open_more
    assert [m.name for m in result.modules] == ["M0", "M1", "M2"]
    assert result.modules[0].unsaved and not result.modules[1].unsaved
    assert result.modules[2].selected and result.modules[0].application == "App"


def test_open_modules_at_limit_cannot_open_more():
    result = navigator(FakeDriver(CATALOGUE, tabs=many_modules(10))).open_modules()
    assert result.count == 10 and not result.can_open_more


def test_open_modules_when_not_running():
    with pytest.raises(ServiceStudioNotRunningError):
        navigator(FakeDriver(CATALOGUE, running=False)).open_modules()


def test_open_refuses_new_module_at_limit():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(10))
    with pytest.raises(TooManyOpenModulesError) as err:
        navigator(driver).open("DGA_UI")
    assert err.value.open_modules == [f"M{i}" for i in range(10)]
    assert not driver.called("open_search_dialog")


def test_open_at_limit_still_brings_open_module_to_front():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(10))
    assert navigator(driver).open("m3").status is OpenStatus.ALREADY_OPEN


def test_open_limit_is_configurable():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(2))
    with pytest.raises(TooManyOpenModulesError):
        navigator(driver, max_open_modules=2).open("DGA_UI")


def test_close_saved_module():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(10))
    result = navigator(driver).close("m4")
    assert result.module_name == "M4" and result.status == "closed"
    assert result.open_modules_count == 9
    assert driver.called("close_module_tab") == [("close_module_tab", 5, "M4")]


def test_close_refuses_unsaved_module():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(3, unsaved=frozenset({1})))
    with pytest.raises(UnsavedChangesError):
        navigator(driver).close("M1")
    assert not driver.called("close_module_tab")


def test_close_unknown_module_lists_open_ones():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(2))
    with pytest.raises(ModuleNotOpenError) as err:
        navigator(driver).close("Nope")
    assert err.value.open_modules == ["M0", "M1"]
    assert not driver.called("close_module_tab")


def test_close_never_touches_environment_tab():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(1))
    with pytest.raises(ModuleNotOpenError):
        navigator(driver).close("Dev - host")


def test_close_works_without_environment_connection():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(2)[1:])
    assert navigator(driver).close("M0").open_modules_count == 1


def test_close_reports_dialog():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(2))
    driver.close_effect = "dialog"
    with pytest.raises(UnexpectedDialogError):
        navigator(driver).close("M0")


def test_close_times_out_when_tab_stays():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(2))
    driver.close_effect = "nothing"
    with pytest.raises(AutomationTimeoutError):
        navigator(driver, close_verify_timeout_s=2).close("M0")


def test_close_blocked_by_unexpected_dialog():
    driver = FakeDriver(CATALOGUE, tabs=many_modules(2), dialogs=["Some Dialog"])
    with pytest.raises(UnexpectedDialogError):
        navigator(driver).close("M0")
    assert not driver.called("close_module_tab")


def test_close_module_open_in_two_windows_closes_one():
    # Two windows (e.g. two environments) can each have the same module open.
    tabs = [*many_modules(2), ENV, editor("M0 (App)")]
    driver = FakeDriver(CATALOGUE, tabs=tabs)
    result = navigator(driver).close("M0")
    assert result.open_modules_count == 2
    assert driver.called("close_module_tab") == [("close_module_tab", 1, "M0")]


# --- dependencies ---------------------------------------------------------------


def deps_nav(selected=True, **config):
    tabs = [ENV, editor("M1 (App)", selected=selected), editor("M2 (App)")]
    driver = FakeDriver(CATALOGUE, tabs)
    return driver, navigator(driver, **config)


def test_add_dependency_applies_and_closes_dialog():
    driver, nav = deps_nav()
    result = nav.change_dependency("m1", "outsystemscharts", add=True)
    assert result.status == "applied"
    assert result.dependency_name == "OutSystemsCharts"
    assert driver.applied == {"OutSystemsCharts": 1}
    assert driver.called("toggle_dependency_root") == [("toggle_dependency_root",)]
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_all_filter_is_chosen_before_reading_the_state():
    driver, nav = deps_nav()
    nav.change_dependency("M1", "OutSystemsUI", add=True)
    names = [c[0] for c in driver.calls]
    assert names.index("show_all_dependency_elements") < names.index("toggle_dependency_root")


def test_partially_selected_dependency_counts_as_present_for_add():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.root_states["OutSystemsUI"] = 2
    result = nav.change_dependency("M1", "OutSystemsUI", add=True)
    assert result.status == "already_present"
    assert "some of its elements" in result.message
    assert not driver.called("press_dependencies_apply")


def test_add_selects_tab_of_the_module_first():
    driver, nav = deps_nav(selected=False)
    nav.change_dependency("M1", "OutSystemsUI", add=True)
    assert driver.called("select_tab") == [("select_tab", 1)]


def test_add_is_a_noop_when_already_present():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.root_states["OutSystemsUI"] = 1
    result = nav.change_dependency("M1", "OutSystemsUI", add=True)
    assert result.status == "already_present"
    assert not driver.called("press_dependencies_apply")
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_remove_dependency_unchecks_and_applies():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.root_states["OutSystemsUI"] = 1
    result = nav.change_dependency("M1", "OutSystemsUI", add=False)
    assert result.status == "applied"
    assert driver.applied == {"OutSystemsUI": 0}


def test_remove_is_a_noop_when_not_a_dependency():
    driver, nav = deps_nav()
    result = nav.change_dependency("M1", "OutSystemsUI", add=False)
    assert result.status == "not_a_dependency"
    assert not driver.called("press_dependencies_apply")


def test_mixed_state_is_toggled_until_it_reaches_the_wanted_state():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.root_states["OutSystemsUI"] = 2  # some elements already used
    original = driver.toggle_dependency_root

    def toggle(delay):
        original(delay)
        driver.root_states["OutSystemsUI"] = 1  # mixed -> checked first
        if driver.called("toggle_dependency_root").__len__() >= 2:
            driver.root_states["OutSystemsUI"] = 0

    driver.toggle_dependency_root = toggle
    assert nav.change_dependency("M1", "OutSystemsUI", add=False).status == "applied"


def test_module_must_be_open():
    driver, nav = deps_nav()
    with pytest.raises(ModuleNotOpenError) as err:
        nav.change_dependency("Nope", "OutSystemsUI", add=True)
    assert err.value.open_modules == ["M1", "M2"]
    assert not driver.called("open_dependencies_dialog")


def test_unknown_dependency_cancels_dialog_with_suggestions():
    driver, nav = deps_nav()
    with pytest.raises(DependencyNotFoundError) as err:
        nav.change_dependency("M1", "OutSystems", add=True)
    assert [s for s in err.value.suggestions] == ["OutSystemsCharts", "OutSystemsUI"]
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs
    assert not driver.called("press_dependencies_apply")


def test_ambiguous_dependency_lists_candidates():
    driver, nav = deps_nav()
    with pytest.raises(AmbiguousDependencyError) as err:
        nav.change_dependency("M1", "OfficeUtils", add=True)
    assert err.value.candidates == ["OfficeUtils", "OfficeUtils (extension)"]
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_wrong_selection_is_detected_and_nothing_is_applied():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.wrong_selection = True
    with pytest.raises(WrongDependencySelectedError):
        nav.change_dependency("M1", "OutSystemsUI", add=True)
    assert not driver.called("press_dependencies_apply")
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_checkbox_that_will_not_toggle_fails_without_applying():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.toggle_works = False
    with pytest.raises(DependencyChangeFailedError):
        nav.change_dependency("M1", "OutSystemsUI", add=True)
    assert not driver.called("press_dependencies_apply")


def test_dialog_after_apply_is_reported():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.apply_effect = "dialog"
    driver.root_states["OutSystemsUI"] = 1
    with pytest.raises(UnexpectedDialogError):
        nav.change_dependency("M1", "OutSystemsUI", add=False)


def test_dependencies_dialog_already_open_is_refused():
    driver, nav = deps_nav()
    driver.state.dialogs.append(ss.MANAGE_DEPENDENCIES_TITLE)
    with pytest.raises(UnexpectedDialogError):
        nav.change_dependency("M1", "OutSystemsUI", add=True)
    assert not driver.called("open_dependencies_dialog")


def test_add_single_element_toggles_only_that_element():
    driver, nav = deps_nav()
    result = nav.change_dependency("M1", "OutSystemsCharts", add=True, elements=["DonutChart"])
    assert result.status == "applied"
    assert result.elements == ["DonutChart"]
    assert driver.called("toggle_dependency_checkbox") == [
        ("toggle_dependency_checkbox", "DonutChart")
    ]
    assert not driver.called("toggle_dependency_root")
    assert driver.element_states["OutSystemsCharts"]["DonutChart"] == 1
    assert driver.element_states["OutSystemsCharts"]["DonutChart_v1"] == 0
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs
    # the filter is cleared before Apply so the whole tree is applied
    assert driver.called("set_element_search")[-1] == ("set_element_search", "")


def test_element_already_included_is_a_noop():
    driver, nav = deps_nav()
    result = nav.change_dependency("M1", "OutSystemsCharts", add=True, elements=["BarChart"])
    assert result.status == "already_present"
    assert not driver.called("press_dependencies_apply")
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_remove_single_element():
    driver, nav = deps_nav()
    result = nav.change_dependency("M1", "OutSystemsCharts", add=False, elements=["BarChart"])
    assert result.status == "applied"
    assert driver.element_states["OutSystemsCharts"]["BarChart"] == 0


def test_unknown_element_lists_suggestions_and_cancels():
    driver, nav = deps_nav()
    with pytest.raises(DependencyElementNotFoundError) as err:
        nav.change_dependency("M1", "OutSystemsCharts", add=True, elements=["Donut"])
    assert err.value.suggestions == ["DonutChart", "DonutChart_v1"]
    assert not driver.called("press_dependencies_apply")
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_duplicate_element_rows_are_reported_as_ambiguous():
    driver, nav = deps_nav()
    driver.ELEMENTS = {
        "OutSystemsCharts": [("Charts", 0), ("Donut", 0), ("Other", 0), ("Donut", 0)]
    }
    driver._visible_elements = lambda: driver.ELEMENTS["OutSystemsCharts"]
    with pytest.raises(AmbiguousDependencyElementError):
        nav.change_dependency("M1", "OutSystemsCharts", add=True, elements=["Donut"])
    assert not driver.called("press_dependencies_apply")


def test_element_checkbox_that_will_not_toggle_fails_without_applying():
    driver, nav = deps_nav()
    driver.toggle_dependency_checkbox = lambda label, delay: None
    with pytest.raises(DependencyChangeFailedError):
        nav.change_dependency("M1", "OutSystemsCharts", add=True, elements=["DonutChart"])
    assert not driver.called("press_dependencies_apply")


def test_outdated_references_prompt_is_answered_with_refresh_all():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.apply_effect = "outdated_once"
    driver.root_states["OutSystemsUI"] = 1
    result = nav.change_dependency("M1", "OutSystemsUI", add=False)
    assert result.status == "applied"
    assert "refreshed" in result.message
    names = [c[0] for c in driver.calls]
    start = names.index("press_dependencies_apply")
    assert names[start:].index("dismiss_outdated_references_prompt") < names[start:].index(
        "press_dependencies_refresh_all"
    )
    assert len(driver.called("press_dependencies_apply")) == 2
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_outdated_prompt_that_keeps_coming_back_is_reported():
    driver, nav = deps_nav()
    driver._deps_setup()
    driver.apply_effect = "outdated_always"
    driver.root_states["OutSystemsUI"] = 1
    with pytest.raises(UnexpectedDialogError):
        nav.change_dependency("M1", "OutSystemsUI", add=False)


# --- refresh ---------------------------------------------------------------------


def test_add_accepts_partial_state_after_ticking():
    """Ticking a module with unusable elements leaves the root partially selected."""
    driver, nav = deps_nav()
    driver._deps_setup()
    original = driver.toggle_dependency_root

    def toggle(delay):
        original(delay)
        driver.root_states[driver.deps_selected] = 2

    driver.toggle_dependency_root = toggle
    assert nav.change_dependency("M1", "OutSystemsUI", add=True).status == "applied"
    assert driver.applied == {"OutSystemsUI": 2}
    assert len(driver.called("toggle_dependency_root")) == 1


def test_refresh_all_presses_refresh_all_and_applies():
    driver, nav = deps_nav()
    driver.outdated = frozenset({"OutSystemsUI"})
    result = nav.refresh_dependency("M1")
    assert (result.action, result.status) == ("refresh", "applied")
    names = [c[0] for c in driver.calls]
    assert names.index("press_dependencies_refresh_all") < names.index("press_dependencies_apply")
    assert ss.MANAGE_DEPENDENCIES_TITLE not in driver.state.dialogs


def test_refresh_all_when_nothing_is_outdated_cancels():
    driver, nav = deps_nav()
    result = nav.refresh_dependency("M1")
    assert result.status == "up_to_date"
    assert not driver.called("press_dependencies_apply")
    assert driver.called("cancel_dependencies_dialog")


def test_refresh_one_dependency_selects_it_first():
    driver, nav = deps_nav(selected=False)
    driver.outdated = frozenset({"OutSystemsCharts"})
    result = nav.refresh_dependency("M1", "outsystemscharts")
    assert result.status == "applied"
    assert driver.called("select_tab") == [("select_tab", 1)]
    names = [c[0] for c in driver.calls]
    assert names.index("press_dependency_refresh") < names.index("press_dependencies_apply")
    assert not driver.called("press_dependencies_refresh_all")


def test_refresh_one_dependency_that_is_current_is_a_noop():
    driver, nav = deps_nav()
    result = nav.refresh_dependency("M1", "OutSystemsCharts")
    assert result.status == "up_to_date"
    assert not driver.called("press_dependencies_apply")


def test_refresh_unknown_dependency_is_reported():
    driver, nav = deps_nav()
    with pytest.raises(DependencyNotFoundError):
        nav.refresh_dependency("M1", "Nope")
    assert driver.called("cancel_dependencies_dialog")


def test_dependency_label_allows_short_icon_prefix():
    from os_module_navigator.providers.desktop_gui import _same_dependency_label

    assert _same_dependency_label("PdfTools_IS", "PdfTools_IS")
    assert _same_dependency_label("JO PdfTools_IS", "PdfTools_IS")
    assert not _same_dependency_label("JO PdfTools_Tests", "PdfTools_IS")
    assert not _same_dependency_label("Some Module PdfTools_IS", "PdfTools_IS")


def test_dependency_label_tolerates_icon_and_confusables():
    from os_module_navigator.automation.service_studio import same_tree_label

    assert same_tree_label("xit UItimatePDF", "UltimatePDF")
    assert not same_tree_label("xit UltimatePDF_Utils", "UltimatePDF")


def _usages_driver():
    tabs = [ENV, editor("Orders_BL (Orders Business Logic)"), editor("Other (App)", True)]
    return FakeDriver(CATALOGUE, tabs)


def test_find_usages_brings_module_to_front_and_maps_local():
    driver = _usages_driver()
    result = navigator(driver).find_usages("orders_bl", "Order_CreateOrUpdateWrapper")
    assert driver.called("select_tab") == [("select_tab", 1)]
    assert result.module_name == "Orders_BL"
    assert [(m.module_name, m.usages, m.is_producer) for m in result.modules] == [
        ("Orders_BL", 14, True),
        ("Shipping_BL", 6, False),
    ]
    assert (result.total_usages, result.consumer_modules_count) == (20, 2)
    assert result.possibly_truncated is False
    assert "Shipping_BL" in result.message


def test_find_usages_flags_truncated_lists():
    driver = _usages_driver()
    driver.usages_footer = (40, 3)
    result = navigator(driver).find_usages("Orders_BL", "Order_CreateOrUpdateWrapper")
    assert result.possibly_truncated is True


def test_find_usages_requires_the_module_open():
    with pytest.raises(ModuleNotOpenError):
        navigator(_usages_driver()).find_usages("Missing_BL", "X")


def test_find_usages_unknown_element_closes_popups():
    driver = _usages_driver()
    with pytest.raises(ElementNotFoundError):
        navigator(driver).find_usages("Orders_BL", "Nope")
    assert driver.called("dismiss_popups")
    assert not driver.called("open_usages_in_all_modules")


def test_find_usages_ignores_a_pane_for_another_element():
    driver = _usages_driver()
    driver.usages_delay_reads = 0
    nav = navigator(driver, usages_timeout_s=5)
    original = driver.open_usages_in_all_modules

    def stale(name, timeout_s, poll_s):
        original(name, timeout_s, poll_s)
        driver.usages_pane = driver.usages_pane.model_copy(update={"title": "SomethingElse"})

    driver.open_usages_in_all_modules = stale
    with pytest.raises(AutomationTimeoutError):
        nav.find_usages("Orders_BL", "Order_CreateOrUpdateWrapper")


@pytest.mark.parametrize("name", ["", "two words", "x" * 300])
def test_find_usages_rejects_bad_element_names(name):
    driver = _usages_driver()
    with pytest.raises(NavigatorError):
        navigator(driver).find_usages("Orders_BL", name)
    assert not driver.called("select_element")
