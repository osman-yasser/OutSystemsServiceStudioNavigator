"""Redaction rules for UI Automation labels.

Service Studio renders module content (expressions, attribute names, data
previews, environment hosts) as accessible text. Diagnostic output must not
leak that by default, so element names are redacted unless they belong to
control types that carry UI chrome labels, or match a short allowlist of
known Service Studio UI strings.
"""

from __future__ import annotations

import re

from os_module_navigator.automation.uia import ElementNode

# Control types whose names are UI labels (button captions, menu items, edit
# placeholders), not user content.
LABEL_CONTROL_TYPES: frozenset[str] = frozenset(
    {
        "Button",
        "CheckBox",
        "ComboBox",
        "Edit",
        "Hyperlink",
        "Menu",
        "MenuBar",
        "MenuItem",
        "RadioButton",
        "SplitButton",
        "Tab",
        "TitleBar",
        "ToolBar",
        "Window",
    }
)

# Exact UI strings observed in Service Studio 11 that are safe to show even on
# content-type elements (Text, Group, ...).
KNOWN_UI_LABELS: frozenset[str] = frozenset(
    {
        "Debugger",
        "Edit",
        "Environment",
        "Help",
        "Module",
        "Navigation",
        "Publish",
        "Recent modules",
        "View",
    }
)

# TabItem names in the Avalonia shell are .NET type names, which are safe and
# useful. Their visible caption is a child Text element and is redacted.
TYPE_NAME_PATTERN = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+$")

ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)
LONG_OPAQUE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")

MAX_LABEL_LENGTH = 80


def normalize(text: str) -> str:
    """Remove zero-width characters and collapse whitespace."""
    return " ".join(ZERO_WIDTH.sub("", text).split())


def scrub_secrets(text: str) -> str:
    """Mask anything that looks like an email, credential assignment or opaque token."""
    text = EMAIL.sub("<email>", text)
    text = SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return LONG_OPAQUE.sub("<opaque>", text)


def _truncate(text: str) -> str:
    if len(text) <= MAX_LABEL_LENGTH:
        return text
    return text[: MAX_LABEL_LENGTH - 1] + "…"


def is_icon_glyph(text: str) -> bool:
    """True for icon-font glyphs (Unicode private use area), e.g. ``\\ue649``."""
    return bool(text) and all("" <= ch <= "" for ch in text)


def sanitize_label(control_type: str, name: str, *, reveal: bool = False) -> str:
    """Return a label that is safe to print for an element of ``control_type``."""
    text = normalize(name)
    if not text:
        return ""
    if is_icon_glyph(text):
        return "<icon>"
    if reveal:
        return _truncate(scrub_secrets(text))
    if (
        control_type in LABEL_CONTROL_TYPES
        or text in KNOWN_UI_LABELS
        or (control_type == "TabItem" and TYPE_NAME_PATTERN.match(text))
    ):
        return _truncate(scrub_secrets(text))
    return f"<redacted len={len(text)}>"


def sanitize_tree(node: ElementNode, *, reveal: bool = False) -> ElementNode:
    """Return a copy of ``node`` with every element name sanitized."""
    return node.model_copy(
        update={
            "name": sanitize_label(node.control_type, node.name, reveal=reveal),
            "children": [sanitize_tree(child, reveal=reveal) for child in node.children],
        }
    )
