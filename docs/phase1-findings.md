# Phase 1 findings: Service Studio 11 UI Automation inspection

Date: 2026-11-02 · Machine: Windows 11 Enterprise · Python 3.12 · pywinauto 0.6.9 (UIA backend)

Inspection was read-only: window enumeration and UIA property reads only. No
clicks, typing, focus changes, tab switches, saves or publishes. Environment,
application and module names are left out of this report on purpose.

## Summary

**Python can reliably find, attach to, and read Service Studio without
restarting it.** The search/open workflow looks feasible with pywinauto alone
up to the point of *typing into search boxes and pressing buttons*. The
module search dialog ("Open from Environment", Ctrl+O) has a search box with
`ValuePattern`, a readable results table and an `Open` button with `Invoke`.
A supervised test showed that setting the search box value filters the
results, the first row is auto-selected, and `Up`/`Down` keys move the
selection, so the whole workflow can be done **without mouse clicks**. The
only gap: UIA can't report which row is selected.

## What Service Studio looks like to UI Automation

### 1. The shell is Avalonia, not WPF/WinForms

| Property | Value |
|---|---|
| Process | `ServiceStudio.exe` (one process) |
| Win32 class | `Avalonia-<guid>` (the guid changes between runs, so match the prefix only) |
| UIA framework | `Avalonia` |
| UIA class | `AggregatorWindow` |
| Window name | `Service Studio` |

**Gotcha:** `pywinauto.Desktop(backend="uia").windows()` does **not** return
this window. It must be attached by handle
(`UIAElementInfo(handle_or_elem=hwnd)`), with the handle found through Win32
enumeration (`EnumWindows` + PID). This is what
`automation/windows.py` + `automation/service_studio.py` do.

### 2. Shell tab strip (readable, reliable)

`Tab` with automation id `tabs`. Each `TabItem`:

- **Name** is a .NET type name that identifies what the tab hosts:

  | TabItem name contains | Meaning |
  |---|---|
  | `ServerPageTopLevelView` | Environment home page (the "connected environment" tab) |
  | `AggregatorView` | An open module editor |
  | `OutSystemsBrowserView` | Embedded browser tab (e.g. Forge) |

- **Caption** is a child `Text`: environment display name and host, or the
  module name followed by the application name in parentheses. A leading `*`
  marks unsaved changes.
- Supports `SelectionItem`, and `IsSelected` tells which tab is in front.
- Module tabs have a close `Button` (Invoke) — **must never be touched**.

This is enough for `get_service_studio_status()`: running, window detected,
connected environment (environment tab present + its caption), the module open
in front, and whether it has unsaved changes.

### 3. Tab content is embedded Chromium (CEF)

Under the shell: `Custom AggregatorView → Custom ExtendedAvaloniaNativeControlHost
→ Pane CefBrowserWindow`, and below that Win32 windows
`Chrome_WidgetWin_0 → Chrome_RenderWidgetHostHWND`.

- **Each tab has its own `Chrome_RenderWidgetHostHWND`** (4 found: one visible,
  three hidden). Only the selected tab's is visible.
- UIA framework `Chrome`, root control type `Document`.
- **Lazy tree:** the first query returns an empty `Document`; Chromium builds
  the tree after that query. Re-querying after ~0.5–3 s returns the full tree.
  `snapshot_handle(..., warmup_timeout_s=...)` handles this.
- **Hidden panes are still readable.** The environment home page was fully
  readable while the module editor tab was in front.
- No real automation ids except a few: `webview_root` (many groups),
  `applications-list-root`, and Blueprint tab ids like
  `bp3-tab-title_subeditors-tabs_…`.
- The module editor pane exposes **module content as text** (filter
  expressions, attribute names, data previews). Diagnostics redact names by
  default for this reason.

### 4. Environment home page (where module search starts)

Readable while hidden. Relevant elements, all children of
`Group aid=applications-list-root` (a flat list of ~1,470 elements):

| Element | Control type | Patterns | Keyboard focusable |
|---|---|---|---|
| Environment name, host | `Text` ×2 | none | no |
| `Recent modules` heading | `Text` | none | no |
| Recent module entries (icon + 2 lines of text each) | `Image` + `Text` ×2 | none | no |
| **`Search Modules`** | `Button` | **Invoke** | yes |
| **`Search Applications...`** | `Edit` | **Value**, Text | yes |
| `Forge updates (N)` | `Button` | Invoke | yes |
| Application tiles (icon + name) | `Image` + `Text` | none | no |

The environment page top menu (`Environment`, `Edit`, `Help`) and the editor
menu (`Module`, `Edit`, `View`, `Debugger`, `Help`) are plain `Text` nodes with
no patterns.

### 5. "Open from Environment" dialog (Ctrl+O)

Inspected with the dialog opened by the user via **Ctrl+O** (the environment
page's `Search Modules` button very likely opens the same dialog — not yet confirmed).

- **Separate top-level Avalonia window**: UIA class `DialogWindow`, name
  `Open from Environment`, with its own title bar (`Maximize`, `Close`) and its
  own Chromium pane (`CefBrowserWindow` → `Chrome_RenderWidgetHostHWND`).
- Contents of the Chromium pane:

  | Element | Control type | Patterns | Focusable | Enabled |
  |---|---|---|---|---|
  | `Modules available at '<host>'` | `Text` | — | no | — |
  | `Search for Module Name, Application Name or Author` | `Edit` | **Value**, Text | yes | yes |
  | Results grid | `Table` | Grid (reports 2 rows × 4 columns) | no | yes |
  | Column headers `Module`, `Application`, `Changed`, `By` | `Header` | GridItem | no | yes |
  | Result cells | `Group` with one `Text` child | — | **yes** | yes |
  | `View Versions` | `Button` | Invoke | no | **disabled** |
  | `Open` | `Button` | Invoke | no | **disabled** (until a row is selected) |
  | `Cancel` | `Button` | Invoke | yes | yes |

- **Flattened grid**: Chromium exposes *all* result cells as children of one
  `DataItem`; there are no row elements. Rows are rebuilt by chunking cells by
  the number of headers (`read_module_search_results`). Module name and
  application name are separate, clearly labelled columns.
- **Virtualized list**: with an empty search box only 12 rows were rendered
  (10 on screen, 2 just off screen with a zero-size rectangle). Listing all
  modules by reading the table is not possible; the search box must be used to
  narrow results.
- Cells are keyboard-focusable but expose no Invoke / SelectionItem / default action.

## Supervised interaction test (2026-11-02)

Run with the user watching, inside the "Open from Environment" dialog only,
using a throwaway script (not part of the package). Search text: `DGA_UI`.
`Open` and `Enter` were never pressed. The selection was put back on row 1 at the end.

| Test | Result |
|---|---|
| `ValuePattern.SetValue` on the search box | ✅ **Filters the results** (12 → 2 rows within ~2 s). Reading the value back returns the text. No typing needed. |
| Search matching | Substring match: `DGA_UI` matched `DGA_UI` **and** `MOD_DGA_UI`. Results are sorted by Module. |
| `Open` after filtering | Becomes **enabled on its own**: the first row is auto-selected. |
| Selection visible through UIA? | ❌ No. Rows/cells report no IsSelected, no ARIA state, identical legacy state. `HasKeyboardFocus` only tracks focus, not selection. |
| Selection visible on screen? | ✅ Selected row background is blue (`rgb 123,126,253`), others near-white (`249,250,251`), sampled from the cells' live UIA rectangles **while the dialog is in front** (when covered, sampling just reads the covering window). |
| UIA `SetFocus` on another row's cell | Moves keyboard focus only; selection stays on row 1. |
| `Space` on a focused cell | No change to selection. |
| `Down` / `Up` with focus in the search box | ✅ **Moves the selection** down / up one row. |
| `Down` with focus on a cell | ✅ Also moves the selection. |

### Resulting workflow (keyboard only, no mouse)

1. Find the `DialogWindow` "Open from Environment" (open it with Ctrl+O or the
   `Search Modules` button — opening it by automation not yet tested).
2. `SetValue(module_name)` on the search box; poll until the rows only contain the query.
3. Read rows; find the rows whose Module equals `module_name` exactly.
   0 → not found; >1 → ambiguity error.
4. Focus the search box, press `Up` enough times to reach row 0, then `Down`
   *index* times. (Row 0 is auto-selected, so for index 0 no key is needed.)
5. Optionally confirm by sampling the highlight colour of the target row (theme-dependent, secondary check only).
6. Invoke `Open`.
7. Verify: dialog closes, and a new selected `AggregatorView` tab whose caption starts with the module name appears.

**No coordinate clicking is needed**, so the PyAutoGUI/click fallback below can
stay unused. Keyboard input does require the dialog to be the foreground
window (UIA `SetFocus` brings it to front).

## Can the workflow be done with pywinauto alone?

**Yes, very likely**, with keyboard input for row selection.

| Step | Status | Notes |
|---|---|---|
| 1. Discover process/window | ✅ Proven | psutil + `EnumWindows`, Avalonia class prefix, UIA class `AggregatorWindow` |
| 2. Attach without restart | ✅ Proven | UIA by handle |
| 3. Verify connected environment | ✅ Likely | Environment tab present with a caption; the dialog also names the host. Not yet tested when disconnected/logged out. |
| 4. Open the module search dialog | 🟡 Untested by automation | Ctrl+O works manually; `Search Modules` button (Invoke) also available. |
| 5. Search for module | ✅ Proven | `ValuePattern.SetValue` filters results. |
| 6. Identify matches | ✅ Proven | Rows readable; exact match on Module column. |
| 7. Select and open | 🟡 Selection proven, Open not pressed | Row 0 auto-selected; `Up`/`Down` from the search box move the selection. `Open` has Invoke and is enabled once a row is selected. |
| 8. Verify module opened | 🟡 Likely | New `AggregatorView` tab with the module name in its caption. Not yet observed by automation. |

## Limitations found

1. Avalonia windows are invisible to pywinauto's desktop window listing — must use handles.
2. Chromium tree is lazy; first read is empty.
3. Row selection is **not exposed to UIA**; it can only be driven by keys and confirmed indirectly (row index arithmetic, highlight colour, and the opened tab's caption).
4. The results list is virtualized; with a specific search this hardly matters, but a very broad query may need scrolling for rows past the first ~12.
5. Keyboard input needs the dialog in the foreground, which briefly takes focus from whatever the user is doing.
6. Very few stable automation ids; matching relies on control type + label text (English UI assumed).
7. Rectangles of hidden panes and off-screen rows are stale or zero.

## Click fallback (not needed for now)

Kept only in case keyboard selection stops working in another version: a single
click at the target cell's live UIA rectangle centre, only when the dialog is in
front and the match is exact and unique. Requires your approval before use.

## Still unknown

- Opening the dialog by automation (Ctrl+O to the main window vs. invoking `Search Modules`).
- Whether invoking `Open` triggers a prompt when the current module has unsaved changes (it opens a *new* tab, so probably not).
- What happens when the module is already open in a tab.
- Behaviour when disconnected / on the login screen / another modal is open.
- Behaviour with several Service Studio processes.
- Scrolling behaviour for result lists longer than the rendered rows.

## Next step

Phase 1 is complete. Phase 2 can implement `DesktopGUIProvider` on the
keyboard workflow above. The first real `open_module` run should be supervised
on a module you choose.
