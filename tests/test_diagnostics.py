from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.uia import ElementNode
from os_module_navigator.diagnostics import (
    DiagnosticReport,
    ShellWindowReport,
    WebViewReport,
    _observations,
    format_summary,
    format_tree,
)


def report(**overrides) -> DiagnosticReport:
    base = DiagnosticReport(
        generated_at="now",
        names_revealed=False,
        process_name="ServiceStudio.exe",
        process_ids=[1],
        shell_windows=[
            ShellWindowReport(
                hwnd=10,
                pid=1,
                uia_framework="Avalonia",
                uia_class="AggregatorWindow",
                role=ss.WindowRole.MAIN,
                tabs=[
                    ss.ShellTab(type_name="x.ServerPage", kind=ss.ShellTabKind.ENVIRONMENT),
                    ss.ShellTab(
                        type_name="x.AggregatorView",
                        kind=ss.ShellTabKind.MODULE_EDITOR,
                        unsaved_marker=True,
                    ),
                ],
            )
        ],
        webviews=[
            WebViewReport(
                hwnd=11,
                pid=1,
                visible=False,
                kind=ss.WebViewKind.ENVIRONMENT_HOME,
                element_count=5,
                control_types={"Text": 3},
                landmarks=[
                    ss.Landmark(
                        key="search_modules_button",
                        control_type="Button",
                        label="Search Modules",
                        patterns=["Invoke"],
                    )
                ],
            )
        ],
    )
    return base.model_copy(update=overrides)


def test_observations_for_connected_session():
    notes = _observations(report())
    assert any("environment tab is open" in n for n in notes)
    assert any("1 with unsaved marker" in n for n in notes)
    assert any("readable while hidden" in n for n in notes)
    assert any("'Search Modules' (Button) patterns: Invoke" in n for n in notes)


def test_observations_when_not_running():
    assert _observations(report(process_ids=[])) == ["ServiceStudio.exe is not running."]


def test_format_tree_limits_lines():
    tree = ElementNode(children=[ElementNode(control_type="Text", name=str(i)) for i in range(10)])
    lines = format_tree(tree, max_lines=3)
    assert len(lines) == 4
    assert "limited to 3 lines" in lines[-1]


def test_format_summary_mentions_tabs_and_landmarks():
    text = format_summary(report(), print_trees=False, max_tree_lines=10)
    assert "kind=module_editor" in text
    assert "landmark search_modules_button" in text
