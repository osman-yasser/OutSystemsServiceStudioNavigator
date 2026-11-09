import asyncio

import pytest

from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.driver import UiState
from os_module_navigator.config import NavigatorConfig
from os_module_navigator.errors import (
    AmbiguousModuleError,
    AutomationTimeoutError,
    ModuleNotFoundInEnvironmentError,
    ModuleNotOpenError,
    NavigatorError,
    NotConnectedError,
    ServiceStudioNotRunningError,
    TooManyOpenModulesError,
    UnexpectedDialogError,
    UnsavedChangesError,
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

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


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
