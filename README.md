# OutSystems O11 Module Navigator MCP

A local MCP server (planned) that lets Codex drive the **OutSystems Service
Studio 11 desktop app** to search for and open modules, so the official
OutSystems MCP can work on them without a manual "open module" step.

Status: **Phase 1 — UI inspection.** Only a read-only diagnostic exists so far.
See [docs/phase1-findings.md](docs/phase1-findings.md).

## Setup

```
uv sync
```

## Diagnostic

With Service Studio running and signed in:

```
uv run os-nav-diagnose            # sanitized summary + JSON snapshot in diagnostics_output/
uv run os-nav-diagnose --tree     # also print the sanitized UIA trees
uv run os-nav-diagnose --reveal-names   # show module/environment names (keep local)
```

The diagnostic only reads window and UI Automation properties. It never
clicks, types, focuses, switches tabs, saves or publishes. A test
(`tests/test_read_only_guard.py`) fails if interaction calls are added to the package.

## Development

```
uv run ruff format src tests
uv run ruff check src tests
uv run pytest
```

## Layout

```
src/os_module_navigator/
  diagnostics.py              # Phase 1 CLI (os-nav-diagnose)
  automation/
    windows.py                # Win32 process/window discovery
    uia.py                    # bounded read-only UIA tree snapshots
    sanitize.py               # redaction of content labels
    service_studio.py         # Service Studio structure: shell tabs, web views, landmarks
tests/
```
