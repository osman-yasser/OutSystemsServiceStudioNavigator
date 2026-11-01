from fakes import BrokenInfo, FakeComElement, FakeInfo

from os_module_navigator.automation.uia import (
    PATTERN_AVAILABILITY_PROPERTIES,
    SELECTION_ITEM_IS_SELECTED_PROPERTY,
    snapshot_handle,
    snapshot_tree,
)


def chain(depth: int) -> FakeInfo:
    root = FakeInfo(name="0")
    node = root
    for i in range(1, depth):
        child = FakeInfo(name=str(i))
        node.kids.append(child)
        node = child
    return root


def test_snapshot_reads_properties_and_patterns():
    element = FakeComElement(
        {
            PATTERN_AVAILABILITY_PROPERTIES["Invoke"]: True,
            PATTERN_AVAILABILITY_PROPERTIES["SelectionItem"]: True,
            SELECTION_ITEM_IS_SELECTED_PROPERTY: True,
        }
    )
    info = FakeInfo(control_type="Button", name="Go", automation_id="go", element=element)
    node = snapshot_tree(info)
    assert node.control_type == "Button"
    assert node.automation_id == "go"
    assert node.patterns == ["Invoke", "SelectionItem"]
    assert node.selected is True
    assert node.rect is not None and node.rect.width == 10


def test_selected_is_unknown_without_selection_item_pattern():
    assert snapshot_tree(FakeInfo(element=FakeComElement())).selected is None


def test_depth_limit_marks_truncation():
    node = snapshot_tree(chain(10), max_depth=3)
    assert node.count() == 4
    deepest = list(node.iter_nodes())[-1]
    assert deepest.children_truncated


def test_element_budget_limits_walk():
    root = FakeInfo(kids=[FakeInfo() for _ in range(50)])
    node = snapshot_tree(root, max_elements=10)
    assert node.count() == 10
    assert node.children_truncated


def test_broken_elements_do_not_abort_the_walk():
    root = FakeInfo(kids=[BrokenInfo(control_type="Text"), FakeInfo(name="ok")])
    node = snapshot_tree(root)
    assert [child.name for child in node.children] == ["", "ok"]


def test_iter_nodes_is_depth_first_in_order():
    root = FakeInfo(
        name="a", kids=[FakeInfo(name="b", kids=[FakeInfo(name="c")]), FakeInfo(name="d")]
    )
    assert [n.name for n in snapshot_tree(root).iter_nodes()] == ["a", "b", "c", "d"]


def test_snapshot_handle_waits_for_chromium_tree():
    calls: list[int] = []
    populated = FakeInfo(kids=[FakeInfo(kids=[FakeInfo(), FakeInfo()])])

    def factory(hwnd: int) -> FakeInfo:
        calls.append(hwnd)
        return FakeInfo(control_type="Document") if len(calls) < 3 else populated

    node = snapshot_handle(42, warmup_timeout_s=5, poll_interval_s=0, factory=factory)
    assert calls == [42, 42, 42]
    assert node.count() == 4


def test_snapshot_handle_without_warmup_queries_once():
    calls: list[int] = []

    def factory(hwnd: int) -> FakeInfo:
        calls.append(hwnd)
        return FakeInfo(control_type="Document")

    snapshot_handle(7, factory=factory)
    assert calls == [7]
