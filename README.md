# OutSystems O11 Module Navigator MCP

A local MCP server that lets an agent (Claude Code, Codex, ...) drive the
**OutSystems Service Studio 11 desktop app**: search for and open modules,
manage their dependencies and find the usages of an element across all
modules. The official OutSystems (`servicestudio`) MCP only works on modules
that are open in Service Studio and cannot change dependencies or list
consumers; this server covers those steps.

Runs on Windows (UI Automation) and macOS (Accessibility + on-device OCR, see
[docs/macos.md](docs/macos.md)). Background:
[docs/phase1-findings.md](docs/phase1-findings.md),
[docs/phase2-live-verification.md](docs/phase2-live-verification.md).

## Tools

| Tool | What it does |
|---|---|
| `get_service_studio_status()` | Running? window found? backend, connected environment, module in front (+ unsaved flag), open modules, open dialog |
| `search_modules(query)` | Searches module name / application / author via *Open from Environment*; returns name, application, changed, by |
| `open_module(module_name)` | Opens the module with this exact name (case-insensitive) and verifies its tab; brings an already-open module to the front. Refuses to open a new module when 10 are already open |
| `get_open_modules()` | Number of open modules, the limit (10), `can_open_more`, and each open module's name, application, unsaved flag and whether it is in front |
| `close_module(module_name)` | Closes an open module's tab and verifies it is gone. Refuses modules with unsaved changes |
| `add_dependency(module_name, dependency_name)` | Adds a dependency on **all public elements** of another module/extension through *Manage Dependencies* (Ctrl+Q on Windows; the toolbar button on macOS): searches, selects the row, picks the elements panel's *All* filter, ticks the module, presses *Apply* and waits for the dialog to close. The module must be open. Returns `applied` or `already_present` (also when only some elements are referenced). Optional `elements` adds only those public elements (exact names) |
| `remove_dependency(module_name, dependency_name)` | Same dialog, unticks the dependency and applies. Returns `applied` or `not_a_dependency`; optional `elements` removes only those. If Service Studio asks about outdated references after Apply, the server answers No, presses *Refresh all* and applies again. Any other dialog is reported as `unexpected_dialog` |
| `refresh_dependencies(module_name, dependency_name=None)` | Refreshes outdated or missing references through *Manage Dependencies*: *Refresh all* with no dependency, or only that dependency's *Refresh*, then *Apply*. Returns `applied` or `up_to_date` (nothing outdated). The per-dependency *Refresh* button and the apply path after a refresh have only been exercised against fakes so far |
| `find_usages(module_name, element_name)` | Runs *Find Usages in all Modules* on an element of an open module (macOS only for now): opens the element from the module search drop-down (Cmd+F), right-clicks its row in the elements tree, clicks the menu item and reads the result pane. Returns each module with its usage count (`is_producer` marks the owner, Service Studio's "(local)"), the footer totals and `possibly_truncated`. Changes nothing |

### Open-module limit

Past a certain number of tabs Service Studio opens modules in a separate
window. The server tells the agent (in its MCP instructions and tool
descriptions) to keep at most **10** modules open: before opening a module
that is not open yet, call `get_open_modules`; if `count >= 10`, call
`close_module` on a saved module it no longer needs, then `open_module`.
`open_module` enforces the same limit (`too_many_open_modules`). Change the
limit with `OS_NAV_MAX_OPEN_MODULES`.

Errors come back as tool errors whose text is JSON:
`{"code": "...", "message": "...", "candidates"|"suggestions": [...]}`. Codes:

- Service Studio state: `service_studio_not_running`, `window_not_found`,
  `not_connected`, `unexpected_dialog`, `focus_error`, `automation_timeout`,
  `permission_missing` (macOS), `unsupported_platform`.
- Modules: `search_dialog_unavailable`, `module_not_found`, `ambiguous_module`,
  `wrong_module_opened`, `too_many_open_modules`, `module_not_open`,
  `module_has_unsaved_changes` (the last three include `open_modules`).
- Dependencies: `dependencies_dialog_unavailable`, `dependency_not_found`,
  `ambiguous_dependency`, `wrong_dependency_selected`, `dependency_change_failed`,
  `dependency_element_not_found`, `ambiguous_dependency_element`.
- Usages: `element_not_found`, `usages_unavailable`.

## Setup

```
uv sync
```

## Use with Claude Code

```
claude mcp add --scope user outsystems-module-navigator -- \
  ~/.local/bin/uv --directory /path/to/OutSystemsServiceStudioNavigator \
  run --quiet os-module-navigator
```

After changing the server's code, reconnect it (`/mcp` in Claude Code) so the
new version is loaded.

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

## Use on macOS

Works with Service Studio for Mac; see [docs/macos.md](docs/macos.md) for how
it differs from Windows (the search dialog is read with on-device OCR).

1. Install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and run `uv sync`.
2. Grant the app that launches the server (VS Code, Terminal, iTerm, ...)
   **Accessibility** and **Screen & System Audio Recording** in System Settings →
   Privacy & Security, then restart that app.
3. Register it with your MCP client (see *Use with Claude Code* above).

On macOS the server brings Service Studio to the front while it works and
reads the screen, so avoid typing in Service Studio during a call. The safety
rules for macOS input are in [docs/macos.md](docs/macos.md) and are enforced by
`tests/test_read_only_guard.py`.

## How it works (Windows)

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
- Closing a module invokes that module tab's own close button, only after
  re-reading the tab live and confirming it names the expected module and has
  no unsaved-changes marker (`*`), so Service Studio never asks to save.
- Never Enter, mouse clicks, closing windows, closing unsaved modules, saving
  or publishing.
- Tabs are read from every Service Studio main window, so modules that spilled
  into a second window are still counted.
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
  models.py                   # tool results (modules, dependencies, usages, status)
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
    actions.py                # the only module that sends input (Windows)
    driver.py                 # ServiceStudioDriver protocol + Windows implementation
    mac_ax.py                 # read-only Accessibility lookups (macOS)
    mac_ocr.py                # screenshots + Vision OCR parsers (macOS)
    mac_actions.py            # the only module that sends input (macOS)
    mac_driver.py             # ServiceStudioDriver implementation (macOS)
tests/
```
