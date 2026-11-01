"""Phase 1 code must never interact with the GUI. Fail if interaction calls appear."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "os_module_navigator"

FORBIDDEN = re.compile(
    r"\b(pyautogui|SendKeys|send_keys|type_keys|set_focus|SetFocus|click_input|"
    r"\.click\(|\.invoke\(|Invoke\(|SetValue|set_edit_text|\.select\(|Select\(|"
    r"DoDefaultAction|SendMessage|PostMessage|SetForegroundWindow|keybd_event|mouse_event)"
)


def test_no_gui_interaction_calls_in_source():
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}: {line.strip()}"
        for path in SRC.rglob("*.py")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert offenders == []
