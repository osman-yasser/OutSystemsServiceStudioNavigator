from os_module_navigator.automation import service_studio as ss
from os_module_navigator.automation.uia import ElementNode
from os_module_navigator.automation.windows import WindowHandle


def tab_item(type_name: str, caption: str, selected: bool = False) -> ElementNode:
    return ElementNode(
        control_type="TabItem",
        name=type_name,
        selected=selected,
        children=[ElementNode(control_type="Text", name=caption)],
    )


def shell_tree() -> ElementNode:
    tabs = ElementNode(
        control_type="Tab",
        automation_id="tabs",
        children=[
            tab_item("ServiceStudio.WebViewImplementation.OutSystemsBrowserView", "Forge"),
            tab_item("ServiceStudio.WebViewImplementation.ServerPageTopLevelView", "Dev - host"),
            tab_item("ServiceStudio.WebViewImplementation.AggregatorView", "* MOD_A (App)", True),
            ElementNode(control_type="Custom", class_name="AggregatorView"),
        ],
    )
    return ElementNode(control_type="Window", class_name="AggregatorWindow", children=[tabs])


def test_classify_tab_type():
    assert ss.classify_tab_type("X.ServerPageTopLevelView") is ss.ShellTabKind.ENVIRONMENT
    assert ss.classify_tab_type("X.AggregatorView") is ss.ShellTabKind.MODULE_EDITOR
    assert ss.classify_tab_type("X.OutSystemsBrowserView") is ss.ShellTabKind.EMBEDDED_BROWSER
    assert ss.classify_tab_type("X.Other") is ss.ShellTabKind.UNKNOWN


def test_read_shell_tabs():
    tabs = ss.read_shell_tabs(shell_tree())
    assert [t.kind for t in tabs] == [
        ss.ShellTabKind.EMBEDDED_BROWSER,
        ss.ShellTabKind.ENVIRONMENT,
        ss.ShellTabKind.MODULE_EDITOR,
    ]
    editor = tabs[2]
    assert editor.unsaved_marker and editor.selected
    assert editor.caption == "* MOD_A (App)"


def test_read_shell_tabs_sanitizes_captions_but_keeps_unsaved_marker():
    tabs = ss.read_shell_tabs(shell_tree(), lambda _type, _text: "<hidden>")
    assert {t.caption for t in tabs} == {"<hidden>"}
    assert tabs[2].unsaved_marker


def environment_home() -> ElementNode:
    return ElementNode(
        control_type="Document",
        children=[
            ElementNode(
                control_type="Group",
                automation_id="applications-list-root",
                children=[
                    ElementNode(control_type="Text", name="Recent modules"),
                    ElementNode(control_type="Button", name="Search Modules", patterns=["Invoke"]),
                    ElementNode(
                        control_type="Edit", name="Search Applications...", patterns=["Value"]
                    ),
                ],
            )
        ],
    )


def test_landmarks_and_classification_of_environment_home():
    root = environment_home()
    keys = [lm.key for lm in ss.find_landmarks(root)]
    assert keys == [
        "applications_list",
        "recent_modules_heading",
        "search_modules_button",
        "search_applications_edit",
    ]
    button = next(lm for lm in ss.find_landmarks(root) if lm.key == "search_modules_button")
    assert button.patterns == ["Invoke"]
    assert ss.classify_webview(root) is ss.WebViewKind.ENVIRONMENT_HOME


def test_classify_module_editor_empty_and_unknown():
    editor = ElementNode(
        control_type="Document",
        children=[
            ElementNode(control_type="Text", name="Debugger"),
            ElementNode(control_type="Text", name="Publish"),
        ],
    )
    assert ss.classify_webview(editor) is ss.WebViewKind.MODULE_EDITOR
    empty = ElementNode(control_type="Document", children=[ElementNode(control_type="Group")])
    assert ss.classify_webview(empty) is ss.WebViewKind.EMPTY
    other = ElementNode(children=[ElementNode(), ElementNode(), ElementNode()])
    assert ss.classify_webview(other) is ss.WebViewKind.UNKNOWN


def test_window_filters():
    shell = WindowHandle(1, 9, "Avalonia-1234", True, 0, True)
    hidden = WindowHandle(2, 9, "Avalonia-5678", False, 0, True)
    webview = WindowHandle(3, 9, "Chrome_RenderWidgetHostHWND", False, 1, False)
    assert ss.is_shell_candidate(shell)
    assert not ss.is_shell_candidate(hidden)
    assert not ss.is_shell_candidate(webview)
    assert ss.is_webview(webview)
