# OutSystems O11 Module Navigator MCP

A local MCP server (planned) that lets Codex drive the **OutSystems Service
Studio 11 desktop app** to search for and open modules, so the official
OutSystems MCP can work on them without a manual "open module" step.

Status: **Phase 2 — desktop GUI provider.** Search and status are verified
live; opening a module is implemented and unit-tested, with the first live run
still to do. The MCP server (Phase 3) is not built yet.
See [docs/phase1-findings.md](docs/phase1-findings.md) for how Service Studio
looks to UI Automation.

## Setup

```
uv sync
```

## How it works

1. Find `ServiceStudio.exe` and its Avalonia main window (attached by handle).
2. Check that an environment tab is open and no unexpected dialog is showing.
3. Open **Open from Environment**: invoke the environment page's *Search
   Modules* button (falls back to Ctrl+O), waiting out the "Progress" dialog.
4. Set the search box value through UI Automation; wait for the results to settle.
5. Require exactly one exact match in the *Module* column (else not-found / ambiguity error).
6. Select that row with Up/Down from the search box (rows have no UIA selection),
   invoke **Open**, and verify that a selected editor tab with that module name appears.

If the module already has a tab, that tab is brought to the front instead.

Safety rules, enforced by `tests/test_read_only_guard.py`:

- All GUI input lives in `automation/actions.py`: UIA focus / invoke / set value /
  tab select, plus Up, Down and Ctrl+O only, sent after checking that Service
  Studio is the foreground window.
- Never Enter, mouse clicks, closing windows, saving or publishing.
- All UI work runs serially on one dedicated thread.

## Diagnostic

With Service Studio running and signed in:

```
uv run os-nav-diagnose            # sanitized summary + JSON snapshot in diagnostics_output/
uv run os-nav-diagnose --tree     # also print the sanitized UIA trees
uv run os-nav-diagnose --reveal-names   # show module/environment names (keep local)
```

The diagnostic is read-only.

## Configuration

Timeouts can be overridden with environment variables, for example
`OS_NAV_OPEN_VERIFY_TIMEOUT_S=90`. See `config.py`.

## Development

```
uv run ruff format src tests
uv run ruff check src tests
uv run pytest
```

## Layout

```
src/os_module_navigator/
  models.py                   # ModuleInfo, SearchResult, OpenModuleResult, ServiceStudioStatus
  errors.py                   # NavigatorError subclasses with stable codes
  config.py                   # timeouts and limits
  diagnostics.py              # Phase 1 CLI (os-nav-diagnose)
  providers/
    base.py                   # ModuleProvider protocol
    desktop_gui.py            # DesktopGUINavigator (workflow) + DesktopGUIProvider (async)
  services/
    module_service.py         # provider-agnostic layer for the MCP tools
  automation/
    windows.py                # Win32 process/window discovery
    uia.py                    # bounded read-only UIA tree snapshots
    sanitize.py               # redaction of content labels
    service_studio.py         # Service Studio structure: tabs, dialogs, results table
    live.py                   # read-only lookups on the live UI
    actions.py                # the only module that sends input
    driver.py                 # ServiceStudioDriver protocol + live implementation
tests/
```
