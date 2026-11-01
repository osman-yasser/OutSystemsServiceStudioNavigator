# Phase 2: live verification of DesktopGUIProvider

Date: 2026-11-02 · Service Studio 11 connected to a development environment ·
run supervised by the user through `DesktopGUIProvider` (the async provider,
single UI worker thread).

## Results

| Operation | Starting state | Result | Time |
|---|---|---|---|
| `get_status()` | Environment tab in front, one module open (unsaved) | Running, connected, environment caption, open modules, no dialog | 0.6 s |
| `search_modules("DGA")` | Search dialog already open | 11 modules with application / changed / by; dialog left open | 3.0 s |
| `search_modules("Qualification")` | No dialog | Dialog opened via *Search Modules*, 6 modules, dialog closed with Cancel | 7.3 s |
| `open_module("DGA_UI")` | Search dialog open | `opened_verified`; new `DGA_UI` tab selected; other module untouched | 3.7 s |
| `open_module("mod_ex_ua_qualification_bl")` | Module already open (unsaved) | `already_open`; its tab brought to front; unsaved marker unchanged | 1.7 s |
| `open_module("DGA_U")` | No dialog | `module_not_found` with suggestions (`DGA_UI`, `MOD_DGA_UI`, ...); dialog closed | 7.0 s |

## Issues found live and fixed

1. **Transient "Progress" dialog.** *Search Modules* first shows a "Progress"
   dialog while the module list loads, then "Open from Environment". It is now
   treated as transient (`TRANSIENT_DIALOG_TITLES`) and waited out.
2. **Double trigger.** The Ctrl+O fallback fired while the first load was
   still running, starting a second load whose "Progress" window stole focus.
   Now Ctrl+O is used only if *nothing* appeared within 3 s of invoking
   *Search Modules*; if loading is still in progress at the timeout, the
   operation fails instead of triggering again. Dialog timeout raised to 30 s.
3. **Foreground flicker.** When focus is handed from another app (e.g. the
   one running Codex/Claude) to Service Studio, the foreground can briefly
   flip. `ensure_foreground` now requires the dialog to stay in front for
   0.3 s, and `press_keys` waits up to 1 s for a brief flicker to pass
   (sending nothing meanwhile) before failing with `focus_error`.

In every failure the automation stopped without opening anything or touching
module content.

## Still to verify

- Ctrl+O fallback path (not needed so far; *Search Modules* always worked).
- Behaviour when Service Studio is disconnected / on the login screen.
- Opening a module whose load shows extra prompts (e.g. upgrade or outdated references dialogs).
- Very broad searches where the exact match is beyond the rendered rows.
