"""Only automation/actions.py may interact with the GUI.

Other modules may call ``actions.<fn>(...)`` but must not use interaction APIs directly.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "os_module_navigator"
ACTIONS = SRC / "automation" / "actions.py"

FORBIDDEN = re.compile(
    r"\b(pyautogui|SendKeys|send_keys|type_keys|set_focus|SetFocus|click_input|"
    r"\.click\(|\.invoke\(|Invoke\(|SetValue|set_edit_text|\.select\(|Select\(|"
    r"DoDefaultAction|SendMessage|PostMessage|SetForegroundWindow|keybd_event|mouse_event)"
)

# Calls routed through the actions module are the sanctioned path.
ACTIONS_CALL = re.compile(r"\bactions\.\w+\(")

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
        if path != ACTIONS
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
