"""Safety checks of actions.close_module_tab (no Windows UI needed)."""

from dataclasses import dataclass, field

import pytest

from os_module_navigator.automation import actions
from os_module_navigator.errors import NavigatorError, UnsavedChangesError


@dataclass
class El:
    control_type: str
    name: str = ""
    kids: list["El"] = field(default_factory=list)

    def children(self):
        return self.kids


def tab(caption: str, *, kind: str = "x.AggregatorView", buttons: int = 1) -> El:
    kids = [El("Button", "Path") for _ in range(buttons)] + [El("Text", caption)]
    return El("TabItem", kind, kids)


@pytest.fixture
def invoked(monkeypatch):
    pressed = []
    monkeypatch.setattr(actions, "invoke", pressed.append)
    return pressed


def test_closes_saved_module_tab(invoked):
    item = tab("MyModule (My App)")
    actions.close_module_tab(item, "mymodule")
    assert invoked == [item.kids[0]]


def test_refuses_unsaved_tab(invoked):
    with pytest.raises(UnsavedChangesError):
        actions.close_module_tab(tab("* MyModule (My App)"), "MyModule")
    assert invoked == []


@pytest.mark.parametrize(
    "item",
    [
        tab("Other (My App)"),
        tab("Dev - host", kind="x.ServerPageTopLevelView"),
        tab("MyModule", buttons=0),
        tab("MyModule", buttons=2),
        El("Button", "x.AggregatorView"),
    ],
)
def test_refuses_anything_unexpected(invoked, item):
    with pytest.raises(NavigatorError):
        actions.close_module_tab(item, "MyModule")
    assert invoked == []
