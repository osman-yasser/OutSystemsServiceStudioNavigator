"""Only automation/actions.py (Windows) and automation/mac_actions.py (macOS) may
interact with the GUI.

Other modules may call ``actions.<fn>(...)`` / ``mac_actions.<fn>(...)`` but must
not use interaction APIs directly.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "os_module_navigator"
ACTIONS = SRC / "automation" / "actions.py"
MAC_ACTIONS = SRC / "automation" / "mac_actions.py"
INTERACTION_MODULES = {ACTIONS, MAC_ACTIONS}

FORBIDDEN = re.compile(
    r"\b(pyautogui|SendKeys|send_keys|type_keys|set_focus|SetFocus|click_input|"
    r"\.click\(|\.invoke\(|Invoke\(|SetValue|set_edit_text|\.select\(|Select\(|"
    r"DoDefaultAction|SendMessage|PostMessage|SetForegroundWindow|keybd_event|mouse_event|"
    # macOS
    r"CGEventPost|CGEventCreate|AXUIElementPerformAction|AXUIElementSetAttributeValue|"
    r"activateWithOptions|CGWarpMouseCursorPosition)"
)

# Calls routed through the actions module are the sanctioned path.
ACTIONS_CALL = re.compile(r"\b(mac_)?actions\.\w+\(")

# Never allowed anywhere, including actions.py.
NEVER = re.compile(
    r"(pyautogui|click_input|mouse_event|\{ENTER\}|\{DELETE\}|\{DEL\}|\^s|"
    r"PART_CloseButton|Publish\()"
)


def _lines(path: Path):
    yield from enumerate(path.read_text(encoding="utf-8").splitlines(), 1)


def test_no_gui_interaction_outside_actions_module():
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}: {line.strip()}"
        for path in SRC.rglob("*.py")
        if path not in INTERACTION_MODULES
        for lineno, line in _lines(path)
        if FORBIDDEN.search(ACTIONS_CALL.sub("", line))
    ]
    assert offenders == []


def test_actions_module_never_clicks_saves_closes_or_presses_enter():
    code_lines = [
        (n, line) for n, line in _lines(ACTIONS) if not line.lstrip().startswith(("#", "*"))
    ]
    offenders = [f"{n}: {line.strip()}" for n, line in code_lines if NEVER.search(line)]
    assert offenders == []


MAC_NEVER = re.compile(
    # KEY_Q / 0x0C: Cmd+Q quits Service Studio, so the Q key is never built.
    r"(pyautogui|AXCloseButton|AXCancelButton|Publish\(|KEY_S\b|KEY_DELETE|0x33|0x75|"
    r"KEY_Q\b|0x0C|0x0c|\"cmd\+q\")"
)


def test_mac_actions_module_never_saves_publishes_closes_windows_or_deletes():
    code_lines = [
        (n, line) for n, line in _lines(MAC_ACTIONS) if not line.lstrip().startswith(("#", "*"))
    ]
    offenders = [f"{n}: {line.strip()}" for n, line in code_lines if MAC_NEVER.search(line)]
    assert offenders == []


def test_mac_return_key_only_sent_by_confirm_open():
    """Return opens the selected module, so only the verified path may send it."""
    current = None
    users = set()
    for _, line in _lines(MAC_ACTIONS):
        match = re.match(r"def (\w+)\(", line)
        if match:
            current = match.group(1)
        if "KEY_RETURN" in line and not line.startswith("KEY_RETURN ="):
            users.add(current)
    assert users == {"confirm_open"}


def _functions_using(path: Path, needle: str) -> set[str]:
    current = None
    users = set()
    for _, line in _lines(path):
        match = re.match(r"def (\w+)\(", line)
        if match:
            current = match.group(1)
        if needle in line and not line.startswith("def "):
            users.add(current)
    return users


def test_mac_mouse_clicks_only_through_the_guarded_helpers():
    """Clicks are built in one place and reached only through hit-tested entry points."""
    assert _functions_using(MAC_ACTIONS, "CGEventCreateMouseEvent") == {"_hid_click"}
    assert _functions_using(MAC_ACTIONS, "_hid_click(") == {
        "click_tab",
        "click_in_window",
        "right_click_in_window",
        "click_menu_item",
    }


def test_mac_right_click_only_opens_a_menu_and_menu_clicks_are_hit_tested():
    """A right click never activates anything by itself; a menu item is clicked only
    after the hit test confirmed an AXMenuItem of that menu window."""
    assert _functions_using(MAC_ACTIONS, "right=True") == {"right_click_in_window"}
    assert (
        "AXMenuItem"
        in MAC_ACTIONS.read_text(encoding="utf-8")
        .split("def click_menu_item", 1)[1]
        .split("\ndef ", 1)[0]
    )


def test_mac_allowed_keys_never_include_quit():
    from os_module_navigator.automation import mac_actions

    assert not any("q" in name.split("+")[-1] for name in mac_actions.ALLOWED_KEYS)
