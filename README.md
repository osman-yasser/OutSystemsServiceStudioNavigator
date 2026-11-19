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
| `open_module(module_name)` | Opens the module with this exact name (case-insensitive) and verifies its tab; brings an already-open module to the front. Refuses to open a new module when 10 are already open |
| `get_open_modules()` | Number of open modules, the limit (10), `can_open_more`, and each open module's name, application, unsaved flag and whether it is in front |
| `close_module(module_name)` | Closes an open module's tab and verifies it is gone. Refuses modules with unsaved changes |
| `add_dependency(module_name, dependency_name)` | Adds a dependency on **all public elements** of another module/extension through *Manage Dependencies* (Ctrl+Q): searches, selects the row, picks the elements panel's *All* filter, ticks the module, presses *Apply* and waits for the dialog to close. The module must be open. Returns `applied` or `already_present` (also when only some elements are referenced). Optional `elements` adds only those public elements (exact names) |
| `remove_dependency(module_name, dependency_name)` | Same dialog, unticks the dependency and applies. Returns `applied` or `not_a_dependency`; optional `elements` removes only those. If Service Studio asks about outdated references after Apply, the server answers No, presses *Refresh all* and applies again. Any other dialog is reported as `unexpected_dialog` |

| `find_usages_all_modules(module_name, element_name, element_type?)` | Runs *Find Usages in all Modules* (Ctrl+F12) on an element of an **open** module and returns the usages table as JSON: `usages: [{module, usages, usages_text}]` (`(local)` is the module itself; `No usages` = 0), plus `modules_with_usages`, `total_usages`, `element_type` and Service Studio's footer (`summary`). Read-only |

### Find Usages in all Modules

The official MCP's `Referrers` (the F12 equivalent) only sees the open module.
`find_usages_all_modules` covers the other direction in one call, counting real
usages (unlike Service Center's *Consumers*, which also lists dead references).
Intended flow: call it → for each module in `modules_with_usages`,
`open_module` (respecting the 10-module limit) → use `Referrers` in the
official MCP to get the exact nodes.

How it drives Service Studio:

1. Brings the module's tab to the front and closes old *"usages in all
   modules"* panels (so a stale panel is never read).
2. Quick search (Ctrl+F, top-right box): sets the name, reads the grouped
   results (Interface / Logic / Data...) and opens the exact-name match with
   Down + Enter. This selects the element in the elements tree.
3. Opens the tree element's context menu (Shift+F10), reads the type from
   *"Help with '<type>'"* and, if `element_type` was given and differs, tries
   the next same-name result. Then moves the menu focus to *Find Usages in all
   Modules* (verified item by item) and presses Enter.
4. Waits for the panel titled *"'<name>' usages in all modules"* and its footer
   (*"N usages found in M consumer modules"*), checks the name matches, and
   reads the rows.

Tested on MR_CW / `WB_AdditionalInfo_GeoLocation` (Block): `(local)` 1,
`MRPolicyManagement_CW` 1, `SimuladorMRH` 0; and MRSimulator_BL /
`UpdateOpportunity_AdditionalInfo_PropertyData` (Server Action): `(local)` 0,
`MR_CW` 3. About 15 s each.

Limitations:

- **Counts only.** The panel rows are plain text with no expand control, so
  the exact places of each usage are not available here; use the official
  MCP's `Referrers` in the consumer (opened with `open_module`).
- **Published consumers.** Consumers do not have to be open: Service Studio
  asks the server, so it sees the consumers as **published**. Unpublished or
  unsaved changes in a consumer are not counted. (Inferred from the feature
  working without opening consumers; not tested by editing a consumer, which
  this project does not do.)
- The element must be found by Service Studio's quick search and be one that
  offers *Find Usages in all Modules* in its context menu (e.g. a UI Flow does
  not: error `find_usages_unavailable`). With several elements of the same
  name and no `element_type`, the first quick-search match is used and
  `other_matches` says how many were skipped.
- It sends Ctrl+F, Shift+F10, Down, Escape and Enter to Service Studio: do
  not type in Service Studio while it runs, and run one call at a time.

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
`service_studio_not_running`, `window_not_found`, `not_connected`,
`unexpected_dialog`, `search_dialog_unavailable`, `module_not_found`,
`ambiguous_module`, `automation_timeout`, `focus_error`, `wrong_module_opened`,
`too_many_open_modules`, `module_not_open`, `module_has_unsaved_changes`
(the last three include `open_modules` where relevant), `element_not_found`
(with `suggestions`), `find_usages_unavailable`, `wrong_element_selected`.

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
  tab select, plus an allow-list of keys (Up, Down, Ctrl+O, Ctrl+Q, Space,
  Ctrl+F, Shift+F10, Escape), sent after checking that Service Studio is the
  foreground window.
- Enter is sent only by `actions.press_enter_on`, and only while UIA reports
  that the expected element (the quick-search box, or the context menu item
  *Find Usages in all Modules*) has the keyboard focus.
- Closing a module invokes that module tab's own close button, only after
  re-reading the tab live and confirming it names the expected module and has
  no unsaved-changes marker (`*`), so Service Studio never asks to save.
- Never Delete, mouse clicks, closing windows, closing unsaved modules, saving
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
