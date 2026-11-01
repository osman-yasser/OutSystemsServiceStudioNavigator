# OutSystems O11 Module Navigator MCP

A local MCP server that lets Codex drive the **OutSystems Service Studio 11
desktop app** to search for and open modules, so the official OutSystems MCP
can work on them without a manual "open module" step.

Status: **Phase 3 — MCP server.** All three tools work over stdio against a
live Service Studio. Still to do: a run from inside Codex alongside the official
OutSystems MCP. Background: [docs/phase1-findings.md](docs/phase1-findings.md),
[docs/phase2-live-verification.md](docs/phase2-live-verification.md).

## Tools

| Tool | What it does |
|---|---|
| `get_service_studio_status()` | Running? window found? backend, connected environment, module in front (+ unsaved flag), open modules, open dialog |
| `search_modules(query)` | Searches module name / application / author via *Open from Environment*; returns name, application, changed, by |
| `open_module(module_name)` | Opens the module with this exact name (case-insensitive) and verifies its tab; brings an already-open module to the front |

Errors come back as tool errors whose text is JSON:
`{"code": "...", "message": "...", "candidates"|"suggestions": [...]}`. Codes:
`service_studio_not_running`, `window_not_found`, `not_connected`,
`unexpected_dialog`, `search_dialog_unavailable`, `module_not_found`,
`ambiguous_module`, `automation_timeout`, `focus_error`, `wrong_module_opened`.

## Setup

```
uv sync
```

## Use with Codex

Add to `~/.codex/config.toml` (adjust paths):

```toml
[mcp_servers.outsystems_module_navigator]
command = 'C:\Users\<you>\.local\bin\uv.exe'
args = ["--directory", 'D:\MyProjects\OutSystemsServiceStudioNavigator', "run", "--quiet", "os-module-navigator"]
startup_timeout_sec = 60
tool_timeout_sec = 180   # open_module can take up to ~60 s to verify; Codex's default is 60 s
```

Then restart Codex and check with `codex mcp list`. Keep the official
`servicestudio` MCP enabled too: this server opens the module, the official one
edits it.

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
