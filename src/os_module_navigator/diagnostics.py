"""Phase 1 diagnostic: inspect a running Service Studio 11 without changing it.

Usage::

    uv run os-nav-diagnose                # sanitized summary + JSON snapshot
    uv run os-nav-diagnose --tree         # also print the sanitized trees
    uv run os-nav-diagnose --reveal-names # show content labels (local use only)

Safety: this script only enumerates windows and reads UI Automation properties.
It never focuses, clicks, types, invokes controls, switches tabs, saves or
publishes. Snapshots go to ``diagnostics_output/`` (git-ignored).
"""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

# pywinauto 0.6.x emits SyntaxWarnings on import under Python 3.12+.
warnings.filterwarnings("ignore", category=SyntaxWarning)

from os_module_navigator.automation import service_studio as ss  # noqa: E402
from os_module_navigator.automation.sanitize import (  # noqa: E402
    sanitize_label,
    sanitize_tree,
)
from os_module_navigator.automation.uia import ElementNode, snapshot_handle  # noqa: E402
from os_module_navigator.automation.windows import (  # noqa: E402
    find_process_ids,
    list_process_windows,
)


class ShellWindowReport(BaseModel):
    hwnd: int
    pid: int
    uia_framework: str
    uia_class: str
    role: ss.WindowRole
    title: str = ""
    tabs: list[ss.ShellTab] = Field(default_factory=list)
    tree: ElementNode | None = None


class WebViewReport(BaseModel):
    hwnd: int
    pid: int
    visible: bool
    kind: ss.WebViewKind
    element_count: int
    control_types: dict[str, int]
    landmarks: list[ss.Landmark] = Field(default_factory=list)
    search_results: ss.ModuleSearchResults | None = None
    tree: ElementNode | None = None


class DiagnosticReport(BaseModel):
    generated_at: str
    names_revealed: bool
    process_name: str
    process_ids: list[int]
    shell_windows: list[ShellWindowReport] = Field(default_factory=list)
    webviews: list[WebViewReport] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


def _observations(report: DiagnosticReport) -> list[str]:
    notes: list[str] = []
    if not report.process_ids:
        return [f"{report.process_name} is not running."]
    shells = [w for w in report.shell_windows if w.role is ss.WindowRole.MAIN]
    if not shells:
        notes.append("No Service Studio shell window (UIA class AggregatorWindow) was found.")
        return notes
    notes.append(
        f"Shell window attached via UIA by handle (framework '{shells[0].uia_framework}')."
    )
    tabs = [t for w in shells for t in w.tabs]
    kinds = [t.kind for t in tabs]
    if ss.ShellTabKind.ENVIRONMENT in kinds:
        notes.append("An environment tab is open (indicates a connected environment).")
    else:
        notes.append("No environment tab found; user may not be connected to an environment.")
    editors = [t for t in tabs if t.kind is ss.ShellTabKind.MODULE_EDITOR]
    if editors:
        unsaved = sum(t.unsaved_marker for t in editors)
        notes.append(
            f"{len(editors)} module editor tab(s) open, {unsaved} with unsaved marker '*'."
        )

    homes = [v for v in report.webviews if v.kind is ss.WebViewKind.ENVIRONMENT_HOME]
    if not homes:
        notes.append("Environment home web view not readable via UIA.")
    for home in homes:
        state = "visible" if home.visible else "hidden (its tab is not selected)"
        notes.append(f"Environment home web view readable while {state}.")
        for landmark in home.landmarks:
            if landmark.key in {"search_modules_button", "search_applications_edit"}:
                pats = ", ".join(landmark.patterns) or "none"
                notes.append(f"'{landmark.label}' ({landmark.control_type}) patterns: {pats}.")

    dialogs = [w for w in report.shell_windows if w.role is ss.WindowRole.DIALOG]
    for dialog in dialogs:
        notes.append(f"Dialog window open: {dialog.title!r} (separate Avalonia window).")
    for view in report.webviews:
        if view.kind is not ss.WebViewKind.OPEN_FROM_ENVIRONMENT:
            continue
        for landmark in view.landmarks:
            if landmark.key in {"module_search_edit", "open_button"}:
                pats = ", ".join(landmark.patterns) or "none"
                state = "enabled" if landmark.enabled else "disabled"
                notes.append(
                    f"'{landmark.label}' ({landmark.control_type}, {state}) patterns: {pats}."
                )
        if view.search_results is not None:
            rows = view.search_results.rows
            on_screen = sum(r.on_screen for r in rows)
            notes.append(
                f"Results table columns {view.search_results.columns}: "
                f"{len(rows)} row(s) rendered, {on_screen} on screen (list is virtualized)."
            )
    return notes


def build_report(
    *,
    reveal_names: bool,
    include_trees: bool,
    max_depth: int,
    max_elements: int,
    warmup_timeout_s: float,
) -> DiagnosticReport:
    report = DiagnosticReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        names_revealed=reveal_names,
        process_name=ss.PROCESS_NAME,
        process_ids=find_process_ids(ss.PROCESS_NAME),
    )

    def redact(control_type: str, text: str) -> str:
        return sanitize_label(control_type, text, reveal=reveal_names)

    for pid in report.process_ids:
        windows = list_process_windows(pid)
        for window in filter(ss.is_shell_candidate, windows):
            raw = snapshot_handle(window.hwnd, max_depth=max_depth, max_elements=max_elements)
            report.shell_windows.append(
                ShellWindowReport(
                    hwnd=window.hwnd,
                    pid=pid,
                    uia_framework=raw.framework_id,
                    uia_class=raw.class_name,
                    role=ss.classify_window(raw.class_name),
                    title=redact(raw.control_type, raw.name),
                    tabs=ss.read_shell_tabs(raw, redact),
                    tree=sanitize_tree(raw, reveal=reveal_names) if include_trees else None,
                )
            )
        for window in filter(ss.is_webview, windows):
            raw = snapshot_handle(
                window.hwnd,
                max_depth=max_depth,
                max_elements=max_elements,
                warmup_timeout_s=warmup_timeout_s,
            )
            landmarks = [
                lm.model_copy(update={"label": redact(lm.control_type, lm.label)})
                for lm in ss.find_landmarks(raw)
            ]
            kind = ss.classify_webview(raw)
            results = None
            if kind is ss.WebViewKind.OPEN_FROM_ENVIRONMENT:
                results = ss.read_module_search_results(raw)
            if results is not None:
                results = ss.ModuleSearchResults(
                    columns=[redact("Text", c) for c in results.columns],
                    rows=[
                        row.model_copy(
                            update={"cells": {k: redact("Text", v) for k, v in row.cells.items()}}
                        )
                        for row in results.rows
                    ],
                )
            report.webviews.append(
                WebViewReport(
                    hwnd=window.hwnd,
                    pid=pid,
                    visible=window.visible,
                    kind=kind,
                    element_count=raw.count(),
                    control_types=dict(raw.control_type_counts().most_common()),
                    landmarks=landmarks,
                    search_results=results,
                    tree=sanitize_tree(raw, reveal=reveal_names) if include_trees else None,
                )
            )
    report.observations = _observations(report)
    return report


def format_tree(node: ElementNode, *, max_lines: int, indent: int = 0) -> list[str]:
    lines: list[str] = []

    def visit(n: ElementNode, depth: int) -> None:
        if len(lines) >= max_lines:
            return
        parts = [n.control_type or "?"]
        if n.name:
            parts.append(repr(n.name))
        if n.automation_id:
            parts.append(f"aid={n.automation_id!r}")
        if n.class_name:
            parts.append(f"cls={n.class_name!r}")
        if n.patterns:
            parts.append(f"[{','.join(n.patterns)}]")
        if n.children_truncated:
            parts.append("(children truncated)")
        lines.append("  " * (indent + depth) + " ".join(parts))
        for child in n.children:
            visit(child, depth + 1)

    visit(node, 0)
    if len(lines) >= max_lines:
        lines.append("  " * indent + f"... (output limited to {max_lines} lines)")
    return lines


def format_summary(report: DiagnosticReport, *, print_trees: bool, max_tree_lines: int) -> str:
    out: list[str] = [
        f"Service Studio diagnostic @ {report.generated_at}",
        f"Names revealed: {report.names_revealed}",
        f"Processes ({report.process_name}): {report.process_ids or 'none'}",
        "",
    ]
    for shell in report.shell_windows:
        out.append(
            f"Shell window hwnd={shell.hwnd} framework={shell.uia_framework!r} "
            f"class={shell.uia_class!r} role={shell.role.value} title={shell.title!r}"
        )
        for tab in shell.tabs:
            out.append(
                f"  tab kind={tab.kind.value:<16} selected={tab.selected!s:<5} "
                f"unsaved={tab.unsaved_marker!s:<5} caption={tab.caption!r}"
            )
        if print_trees and shell.tree:
            out.extend(format_tree(shell.tree, max_lines=max_tree_lines, indent=2))
        out.append("")
    for view in report.webviews:
        top = ", ".join(f"{k}={v}" for k, v in list(view.control_types.items())[:6])
        out.append(
            f"Web view hwnd={view.hwnd} visible={view.visible} kind={view.kind.value} "
            f"elements={view.element_count} ({top})"
        )
        for lm in view.landmarks:
            out.append(
                f"  landmark {lm.key}: {lm.control_type} {lm.label!r} "
                f"patterns={lm.patterns} focusable={lm.keyboard_focusable} enabled={lm.enabled}"
            )
        if view.search_results is not None:
            out.append(f"  results columns={view.search_results.columns}")
            for row in view.search_results.rows:
                cells = " | ".join(row.cells.values())
                out.append(f"    {'on-screen ' if row.on_screen else 'off-screen'} {cells}")
        if print_trees and view.tree:
            out.extend(format_tree(view.tree, max_lines=max_tree_lines, indent=2))
        out.append("")
    out.append("Observations:")
    out.extend(f"  - {note}" for note in report.observations)
    return "\n".join(out)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", action="store_true", help="print sanitized UIA trees")
    parser.add_argument("--max-tree-lines", type=int, default=200)
    parser.add_argument(
        "--reveal-names",
        action="store_true",
        help="show content labels (module/environment names); keep output local",
    )
    parser.add_argument("--max-depth", type=int, default=60)
    parser.add_argument("--max-elements", type=int, default=5000)
    parser.add_argument(
        "--warmup-timeout",
        type=float,
        default=8.0,
        help="seconds to wait for Chromium to build its accessibility tree",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics_output"))
    parser.add_argument("--no-json", action="store_true", help="do not write a JSON snapshot")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        reveal_names=args.reveal_names,
        include_trees=not args.no_json or args.tree,
        max_depth=args.max_depth,
        max_elements=args.max_elements,
        warmup_timeout_s=args.warmup_timeout,
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    print(format_summary(report, print_trees=args.tree, max_tree_lines=args.max_tree_lines))
    if not args.no_json:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = args.output_dir / f"service-studio-uia-{stamp}.json"
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nSnapshot written to {path}")
    return 0 if report.process_ids else 1


if __name__ == "__main__":
    raise SystemExit(main())
