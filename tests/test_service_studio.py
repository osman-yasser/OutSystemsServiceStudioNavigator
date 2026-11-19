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


def _cell(text: str, visible: bool = True):
    from os_module_navigator.automation.uia import Rect

    size = 10 if visible else 0
    return ElementNode(
        control_type="Group",
        keyboard_focusable=True,
        rect=Rect(left=0, top=0, width=size, height=size),
        children=[ElementNode(control_type="Text", name=text)],
    )


def open_from_environment() -> ElementNode:
    headers = ElementNode(
        control_type="Custom",
        children=[
            ElementNode(
                control_type="Header",
                name="Module ",
                children=[
                    ElementNode(control_type="Text", name="Module"),
                    ElementNode(control_type="Text", name=""),
                ],
            ),
            ElementNode(control_type="Header", name="Application"),
        ],
    )
    body = ElementNode(
        control_type="Custom",
        children=[
            ElementNode(
                control_type="DataItem",
                children=[
                    _cell("ModA"),
                    _cell("App A"),
                    _cell("ModB", visible=False),
                    _cell("App B", visible=False),
                ],
            )
        ],
    )
    return ElementNode(
        control_type="Document",
        children=[
            ElementNode(
                control_type="Edit",
                name="Search for Module Name, Application Name or Author",
                patterns=["Value"],
            ),
            ElementNode(control_type="Table", children=[headers, body]),
            ElementNode(control_type="Button", name="Open", patterns=["Invoke"], enabled=False),
            ElementNode(control_type="Button", name="Cancel", patterns=["Invoke"]),
        ],
    )


def test_open_from_environment_dialog_is_classified():
    root = open_from_environment()
    assert ss.classify_webview(root) is ss.WebViewKind.OPEN_FROM_ENVIRONMENT
    open_button = next(lm for lm in ss.find_landmarks(root) if lm.key == "open_button")
    assert open_button.enabled is False


def test_read_module_search_results_rebuilds_rows():
    results = ss.read_module_search_results(open_from_environment())
    assert results is not None
    assert results.columns == ["Module", "Application"]
    assert [r.cells for r in results.rows] == [
        {"Module": "ModA", "Application": "App A"},
        {"Module": "ModB", "Application": "App B"},
    ]
    assert [r.on_screen for r in results.rows] == [True, False]


def test_read_module_search_results_without_table():
    assert ss.read_module_search_results(environment_home()) is None


def test_classify_window():
    assert ss.classify_window("AggregatorWindow") is ss.WindowRole.MAIN
    assert ss.classify_window("DialogWindow") is ss.WindowRole.DIALOG
    assert ss.classify_window("Other") is ss.WindowRole.OTHER


def test_parse_module_caption():
    cap = ss.parse_module_caption("* MOD_A_BL (MOD A App)")
    assert cap == ss.ModuleCaption(name="MOD_A_BL", application="MOD A App", unsaved=True)
    assert ss.parse_module_caption("Plain") == ss.ModuleCaption(name="Plain")
    assert ss.parse_module_caption("  *  ") is None


def test_same_module_name_is_case_insensitive():
    assert ss.same_module_name("DGA_UI", " dga_ui ")
    assert not ss.same_module_name("DGA_UI", "MOD_DGA_UI")


def test_read_shell_tabs_ignores_inner_editor_tab_strips():
    # A module editor holds its own tab strips (Warnings, Debugger), sometimes
    # also with automation id "tabs"; only the window's own strip counts.
    inner = ElementNode(
        control_type="Tab",
        automation_id="tabs",
        children=[tab_item("46 Warnings", "46 Warnings"), tab_item("Debugger", "Debugger")],
    )
    editor = ElementNode(control_type="Custom", class_name="AggregatorView", children=[inner])
    shell = shell_tree()
    shell.children.insert(0, ElementNode(control_type="Pane", children=[editor]))
    tabs = ss.read_shell_tabs(shell)
    assert [t.type_name for t in tabs] == [t.type_name for t in ss.read_shell_tabs(shell_tree())]
    assert all("Warnings" not in t.caption for t in tabs)


# --- Find Usages in all Modules ----------------------------------------------------
def _icon() -> ElementNode:
    return ElementNode(control_type="Image")


def _text(name: str, left: int = 0, top: int = 0) -> ElementNode:
    return ElementNode(
        control_type="Text", name=name, rect={"left": left, "top": top, "width": 50, "height": 16}
    )


def test_parse_quick_search_skips_actions_and_keeps_categories():
    popup = ElementNode(
        control_type="Group",
        name="Search in X ...",
        keyboard_focusable=True,
        rect={"left": 2130, "top": 76, "width": 400, "height": 300},
        children=[
            _icon(),
            _text("Search in WB_X"),
            _icon(),
            _text("Search in this Module"),
            _icon(),
            _text("Search for Help"),
            _text("Interface"),
            _icon(),
            _text("WB_PropertyData"),
            _icon(),
            _text("WB_Other"),
            _text("Logic"),
            _icon(),
            _text("WB_PropertyData"),
        ],
    )
    root = ElementNode(control_type="Document", children=[popup])
    assert ss.find_quick_search_popup(root) is popup
    assert ss.parse_quick_search(popup) == [
        ss.QuickSearchResult(category="Interface", name="WB_PropertyData"),
        ss.QuickSearchResult(category="Interface", name="WB_Other"),
        ss.QuickSearchResult(category="Logic", name="WB_PropertyData"),
    ]


def test_element_type_from_context_menu():
    items = ["Find Usages", "Find Usages in all Modules", "Delete", "Help with 'Server Action'"]
    assert ss.element_type_from_menu(items) == "Server Action"
    assert ss.element_type_from_menu(["Copy"]) is None


def test_parse_usage_count():
    assert ss.parse_usage_count("No usages") == 0
    assert ss.parse_usage_count("1 usage") == 1
    assert ss.parse_usage_count("12 usages") == 12
    assert ss.parse_usage_count("Loading") is None


def test_usages_tab_element():
    assert ss.usages_tab_element("'WB_A' usages in all modules") == "WB_A"
    assert ss.usages_tab_element("1 Warning") is None


def usages_panel_tree(caption: str = "'WB_Geo' usages in all modules", summary: bool = True):
    def header(name: str, left: int) -> ElementNode:
        return ElementNode(
            control_type="Header",
            name=name,
            rect={"left": left, "top": 1074, "width": 200, "height": 26},
        )

    cells = [
        _text("(local)", 201, 1103),
        _text("1 usage", 402, 1103),
        _text("MRPolicyManagement_CW", 201, 1128),
        _text("1 usage", 402, 1128),
        _text("SimuladorMRH", 201, 1153),
        _text("No usages", 402, 1153),
    ]
    table = ElementNode(
        control_type="Table",
        children=[
            header("Module Name", 197),
            header("Usages", 397),
            ElementNode(control_type="DataItem", children=cells),
        ],
    )
    tab = ElementNode(
        control_type="TabItem",
        name=f"{caption} Close tab",
        automation_id="bp3-tab-title_bottom-pane-tabs_findConsumersPane",
        children=[ElementNode(control_type="Text", name=caption)],
    )
    parts = [tab, table]
    if summary:
        parts.append(_text("2 usages found in 3 consumer modules"))
    return ElementNode(control_type="Group", children=parts)


def test_read_usages_panel():
    panel = ss.read_usages_panel(usages_panel_tree())
    assert panel is not None
    assert panel.element_name == "WB_Geo"
    assert panel.summary == "2 usages found in 3 consumer modules"
    assert [(r.module, r.usages, r.usages_text) for r in panel.rows] == [
        ("(local)", 1, "1 usage"),
        ("MRPolicyManagement_CW", 1, "1 usage"),
        ("SimuladorMRH", 0, "No usages"),
    ]


def test_read_usages_panel_absent_or_loading():
    assert ss.read_usages_panel(ElementNode(control_type="Group")) is None
    loading = ss.read_usages_panel(usages_panel_tree(summary=False))
    assert loading is not None and loading.summary is None
