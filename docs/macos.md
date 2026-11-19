# macOS port

Verified live against Service Studio 11.55.96 for Mac (Apple silicon, macOS 26)
with every tool: status, open modules, search, open (new and already-open
modules), close, the not-found path, dependencies and find usages (both below).

## What macOS exposes

Service Studio for Mac is Avalonia (native shell) + CEF (Chromium) content.

| Part | Exposed through Accessibility (AX)? |
|---|---|
| Windows: main window `Service Studio`, dialogs `Progress`, `Open from Environment`, ... | Yes: `AXWindow`, title = dialog name |
| Shell tab strip | Yes: `AXTabGroup` with identifier `tabs` |
| Tabs | Yes: `AXRadioButton`, `AXTitle` = .NET view type (`...ServerPageTopLevelView`, `...AggregatorView`, `...OutSystemsBrowserView`), `AXSelected` |
| Tab caption | Yes: descendant `AXStaticText`, text in `AXValue` (`* ` prefix when unsaved) |
| Module tab close button | Yes: the tab's only `AXButton`, supports `AXPress` |
| Chromium content (environment page, module editor, the whole *Open from Environment* dialog body) | **No.** The CEF view is an empty `AXScrollArea`. `AXManualAccessibility` is unsupported and `AXEnhancedUserInterface` has no effect, because the app's `NSApplication` is Avalonia's, not Chromium's |
| App menu bar | Only the standard *Service Studio* menu; no File/Module menus |

Selecting a tab: `AXSelected`/`AXValue`/`AXFocused` writes are ignored, there
is no `AXPress`, Ctrl+Tab does nothing, and Avalonia ignores mouse events
posted to its process. A real (HID) click on the caption works.

## How each step is done on macOS

| Step | Windows | macOS |
|---|---|---|
| Find app / windows / tabs | Win32 + UIA | `NSRunningApplication` (bundle id `com.outsystems.servicestudio`) + AX |
| Open *Open from Environment* | Invoke *Search Modules*, else Ctrl+O | Cmd+O posted to Service Studio's pid |
| Set the search text | UIA `ValuePattern.SetValue` | Cmd+A, then the text as Unicode key events posted to the pid |
| Read results | UIA tree of the CEF dialog | Window screenshot (`CGWindowListCreateImage`) + Vision text recognition, rebuilt into columns from the header positions |
| Select the row | Up/Down from the search box | Same; then a fresh screenshot must show the accent-coloured highlight on exactly the expected module |
| Open | UIA Invoke on *Open* | Return, sent only by `mac_actions.confirm_open` right after re-checking the highlight |
| Cancel | UIA Invoke on *Cancel* | Escape |
| Bring a tab to front | `SelectionItem.Select` | One HID click on the caption, only if a system-wide hit test at that point returns that exact caption; the pointer is moved back |
| Close a tab | Invoke the tab's close button | `AXPress` on the tab's close button (same unsaved/caption checks) |

Code: `automation/mac_ax.py` (read-only AX), `automation/mac_ocr.py` (screenshot,
OCR, table parsing), `automation/mac_actions.py` (the only macOS module that
sends input; enforced by `tests/test_read_only_guard.py`),
`automation/mac_driver.py` (implements `ServiceStudioDriver`).

## Manage Dependencies (add_dependency, remove_dependency, refresh_dependencies)

Verified live on a server module with `OutSystemsCharts`: add (`applied`, then
`already_present`), remove and re-add a single element, remove (`applied`, then
`not_a_dependency`), refresh all and refresh one (`up_to_date`), and
`dependency_not_found`.

| Step | Windows | macOS |
|---|---|---|
| Open the dialog | Ctrl+Q | Click the module toolbar's plug button (fixed point `MANAGE_DEPENDENCIES_BUTTON_PT` from the main window's corner). Ctrl+Q does nothing on Mac, and **Cmd+Q would quit Service Studio**, so the Q key is never built (enforced by `tests/test_read_only_guard.py`). If the click opens any other dialog, it is closed with Escape and the call fails |
| Filter modules / elements | UIA `SetValue` | Click the search box, Cmd+A, type |
| Read modules, tree, checkbox states | UIA tree | Screenshot + Vision (`mac_ocr.parse_dependency_dialog`). Checkbox state from pixels (`mac_ocr.checkbox_state`): black fill = unchecked, accent fill with a tall white tick = checked, with a flat dash = partial. Rows without a checkbox (elements this module type cannot use) are skipped |
| Select a module row | Down keys, verified via the tree root | Same (from the search box, the first Down selects row 0) |
| Tick / untick, *All* filter, *Apply*, *Refresh all*, *Refresh*, *No* | UIA Invoke / focus + Space | One click through `mac_actions.click_in_window`, which requires a hit test at the point to land in that exact window. Every toggle is re-read from a new screenshot before Apply |
| Cancel | Cancel button | Escape |

Notes:

- Ticking a module whose public elements are not all usable in the current
  module (e.g. UI Flows in a server module) leaves its root partially selected.
  That is what Service Studio does too, so `add_dependency` treats any
  non-empty selection as success.
- The first click into the elements tree is sometimes taken only as focus. The
  provider re-reads the state and clicks again (up to 4 times); *Apply* is
  clicked a second time only if nothing at all happened after the first click.
- The element filter is not cleared before Apply (that would need Backspace,
  which is never sent). Apply does not depend on the filter.
- Not yet seen live on Mac: the *Modified or missing references found* prompt
  and a dependency's own *Refresh* button. These need outdated references to
  appear. If the prompt has a different window title on Mac, the call fails
  with `unexpected_dialog` naming it.
- Apply leaves the module with unsaved changes. Nothing is saved or published.

## Find Usages (find_usages)

Verified live on Service Studio 11.55.96 for Mac: an element with 20
usages in 2 modules, one with 9 usages in 4 modules, and a name that does not
exist (`element_not_found`). About 9 seconds per call.

| Step | macOS |
|---|---|
| Bring the module to front | Same as Manage Dependencies (tab caption click) |
| Open the element | Cmd+F in the main window (module search box), Cmd+A, the element name. The drop-down lists "Search in ..." rows and then, per element type, the elements whose name matches. Exactly one suggestion with the exact name (read by OCR, left-aligned with "Search in this Module") is clicked with `click_in_window`. "Search in this Module" itself is never used: it lists occurrences next to a *Replace all occurrences* button |
| Check the selection | The elements tree row and the properties panel header both show the name (OCR of the right panel) |
| Context menu | One right click on the tree row (`right_click_in_window`, hit-tested). The menu is a native popup (untitled `AXWindow` with `AXMenuItem`s that carry no text), captured by its bounds and read by OCR |
| Find Usages in all Modules | `click_menu_item`: a left click only when the hit test returns an `AXMenuItem` of that popup |
| Read the result | OCR of the bottom pane: tab `'<name>' usages in all modules`, rows `Module Name | Usages` (`(local)` = the module itself), footer `N usages found in M consumer modules`. Read until the pane names the element and two reads agree. Fewer rows than the footer reports sets `possibly_truncated` (the pane would need scrolling) |
| On failure | Escape closes the drop-down or menu |

Not supported on Windows yet (`unsupported_platform`).

## Permissions

macOS grants these to the app that *launches* the server (the "responsible"
app), e.g. Visual Studio Code, Terminal, iTerm or Claude Desktop:

- **Accessibility** (System Settings → Privacy & Security → Accessibility):
  needed for everything.
- **Screen & System Audio Recording**: needed to read search results.

Missing permissions surface as the `permission_missing` error.

## Limitations

- Only the result rows visible in the dialog are read (about 8). A broad
  search sets `possibly_truncated`. `open_module` searches by the exact name,
  so the match is normally on screen.
- Long application names end in "…" exactly as displayed.
- OCR can confuse `I`/`l`/`1` and `O`/`0`. A Module cell that matches the
  search text up to these confusions counts as that module, and `open_module`
  still verifies the exact name on the new tab's caption.
- While working, Service Studio is brought to the front, and selecting an
  already-open module's tab briefly moves the pointer.
