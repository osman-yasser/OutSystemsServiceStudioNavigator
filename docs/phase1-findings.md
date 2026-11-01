# Phase 1 findings: Service Studio 11 UI Automation inspection

Date: 2026-11-02 · Machine: Windows 11 Enterprise · Python 3.12 · pywinauto 0.6.9 (UIA backend)

Inspection was read-only: window enumeration and UIA property reads only. No
clicks, typing, focus changes, tab switches, saves or publishes. Environment,
application and module names are left out of this report on purpose.

## Summary

**Python can reliably find, attach to, and read Service Studio without
restarting it.** The search/open workflow looks feasible with pywinauto alone
up to the point of *typing into search boxes and pressing buttons*. Opening a
specific module from a list is **not yet proven**: list entries in the
web UI expose no actionable UIA pattern, and the module search dialog has not
been inspected yet (it only exists after it's opened).

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

## Can the workflow be done with pywinauto alone?

| Step | Status | Notes |
|---|---|---|
| 1. Discover process/window | ✅ Proven | psutil + `EnumWindows`, Avalonia class prefix, UIA class `AggregatorWindow` |
| 2. Attach without restart | ✅ Proven | UIA by handle |
| 3. Verify connected environment | ✅ Likely | Environment tab present with a caption; environment name/host also on the home page. Not yet tested when disconnected/logged out. |
| 4. Navigate to module selection | 🟡 Likely | Environment tab `TabItem` supports `SelectionItem.Select()`. `Search Modules` button supports `Invoke`. Both untested (Phase 1 did not interact). |
| 5. Search for module | 🟡 Unknown | Depends on the Search Modules dialog, which has not been inspected. `Search Applications...` edit supports `Value` as a fallback path for searching applications. |
| 6. Identify matches | 🟡 Partly | Result *text* is readable (recent modules and app tiles are). Need to see the dialog's result list. |
| 7. Open the selected module | 🔴 Not proven | Entries seen so far are non-focusable `Text`/`Image` with no Invoke, SelectionItem or default action. If the dialog's results look the same, opening requires a click (see fallback below) or keyboard navigation. |
| 8. Verify module opened | ✅ Likely | A new `AggregatorView` tab appears with the module name in its caption, and becomes selected. |

## Limitations found

1. Avalonia window is invisible to pywinauto's desktop window listing — must use handles.
2. Chromium tree is lazy; first read is empty.
3. Web UI list entries are not exposed as list items/buttons, so there's no
   pure-UIA way to "click" them that we've seen yet.
4. Very few stable automation ids; matching must rely on control type + label
   text, which may change between Service Studio versions or UI languages.
5. Element bounding rectangles for hidden panes are stale/offset; rectangles
   are only trustworthy for the visible pane.

## Proposed narrow fallback (needs your approval before Phase 2 uses it)

If the module search dialog's result rows are not invokable:

- Only after the dialog is open, in front, and the target row has been matched
  **exactly** (and uniquely) by its UIA text,
- compute the click point from that element's **live UIA bounding rectangle**
  (center), check the point lies inside the Service Studio window and that
  Service Studio is the foreground window,
- send a single click there (pywinauto `click_input` or PyAutoGUI),
- then verify via the new editor tab.

No hardcoded coordinates and no blind clicking. A keyboard alternative
(focus the search box, type, arrow down, Enter) may avoid clicking entirely if
the dialog supports it — to be tested first.

## Still unknown

- The structure of the **Search Modules** dialog (separate Avalonia window or
  an in-page overlay; its search box; its result list; whether rows are
  invokable or keyboard-navigable).
- Which of the two text lines in a recent-module entry is the module name and
  which is the application.
- Behaviour when Service Studio is disconnected, on the login screen, or showing a modal dialog.
- Whether opening a module while another module has unsaved changes prompts a dialog.
- Behaviour with several Service Studio windows/processes.

## Next step (user-assisted inspection)

Open **Search Modules** manually in Service Studio (environment tab → *Search
Modules*), type part of a module name so results show, leave it open, then run:

```
uv run os-nav-diagnose --tree --max-tree-lines 400
```

The snapshot in `diagnostics_output/` will show the dialog's controls, which
decides whether Phase 2 needs the click fallback.
