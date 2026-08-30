# Contributing to nicegui-pyodide

This project is an *external* extension: it never forks or edits NiceGUI's Python
source. The only NiceGUI assets it carries are a few **pyodide-patched copies** of
upstream browser JS, and keeping those in step with new NiceGUI releases is the
main maintenance chore. This document describes that flow.

## What is patched, and why

`pip install nicegui` ships a browser client (`nicegui.js`) that talks to a
server over socket.io, plus per-element JS such as `elements/markdown.js`. Under
Pyodide there is no server and no socket.io, so several browser files are patched:

| Upstream file (in the `nicegui` package) | Vendored patched copy | What the patch does |
| --- | --- | --- |
| `static/nicegui.js` | `nicegui_pyodide/static/nicegui.js` | Branches every socket.io call on `window.niceguiBridge`: routes events / JS responses / server messages through the in-process bridge, adds a `createNiceGUIApp()` entry point, and hardens server-only assumptions (null-safe color-scheme meta, base64 downloads, a `window.socket` stand-in for scene/leaflet, cross-origin stylesheet guard). |
| `elements/markdown.js` | `nicegui_pyodide/static/component_overrides/markdown.js` | Loads codehilite CSS from a static `./dynamic_resources/` path instead of a server route. |
| `elements/leaflet/leaflet.js` | `nicegui_pyodide/static/component_overrides/leaflet/leaflet.js` | Resolves leaflet CSS/JS from the built `./esm/nicegui-leaflet/` path instead of a server resource route. |
| `elements/xterm/xterm.js` | `nicegui_pyodide/static/component_overrides/xterm/xterm.js` | Resolves `xterm.css` from the built `./esm/nicegui-xterm/` path instead of a server resource route. |

The patched copies are **generated, not authored** — the source of truth for the
transformation is a unified diff under `scripts/patches/`, not the vendored blob:

```
scripts/patches/nicegui.js.patch     # stock static/nicegui.js            -> vendored nicegui.js
scripts/patches/markdown.js.patch    # stock elements/markdown.js         -> vendored markdown.js
scripts/patches/leaflet.js.patch     # stock elements/leaflet/leaflet.js  -> vendored leaflet.js
scripts/patches/xterm.js.patch       # stock elements/xterm/xterm.js      -> vendored xterm.js
```

Each `.patch` is produced with plain `diff -u <stock> <vendored>` (with the two
header lines normalized to `--- a/<name>` / `+++ b/<name>`).

## Supporting a new NiceGUI release

`nicegui_pyodide/build/cli.py` pins the tested release in `TESTED_NICEGUI` and
the whole supported dependency range in `SUPPORTED_NICEGUI` (mirroring the pin in
`pyproject.toml`). When the installed NiceGUI is not the tested version,
`nicegui-pyodide-build` prints a drift warning that points back here.

To move the pin forward:

1. **Install the target release** into a venv, e.g. `uv pip install 'nicegui==<new>'`.
2. **Re-apply the patch:**
   ```
   python scripts/regenerate_patched_js.py --nicegui-dir <site-packages>/nicegui
   # or, if nicegui imports cleanly in this env:
   python scripts/regenerate_patched_js.py
   ```
   The script copies each upstream file, applies the stored diff with `patch(1)`,
   and reports per file:
   - **OK** — hunks applied cleanly (line-number drift is tolerated).
   - **OK (applied with line drift)** — applied, but a hunk matched with an
     offset or fuzz; eyeball the result.
   - **FAILED** — a hunk was rejected because upstream rewrote the code the
     patch touches; a `.rej` file is written next to the output.

   Pass `--out DIR` to stage the files for review, or `--in-place` to overwrite
   the vendored copies (it refuses if any hunk failed).
3. **Resolve any rejects.** Open the `.rej` file and the corresponding upstream
   region, port the change by hand into the vendored file, then **regenerate the
   stored diff** so it matches the new baseline:
   ```
   diff -u <site-packages>/nicegui/static/nicegui.js \
           nicegui_pyodide/static/nicegui.js > scripts/patches/nicegui.js.patch
   diff -u <site-packages>/nicegui/elements/markdown.js \
           nicegui_pyodide/static/component_overrides/markdown.js > scripts/patches/markdown.js.patch
   ```
   (Normalize the two header lines to `--- a/<name>` / `+++ b/<name>`.)
4. **Bump the pins:** set `TESTED_NICEGUI` (and `SUPPORTED_NICEGUI` if the range
   moved) in `nicegui_pyodide/build/cli.py`, and the `nicegui` pin in
   `pyproject.toml`.
5. **Build and verify in a real browser** — a clean patch is *not* proof the
   protocol still works:
   ```
   nicegui-pyodide-build /tmp/dist
   python -m http.server -d /tmp/dist 8080
   ```
   Open the page and exercise events, `run_javascript`, downloads, and a
   markdown element with a code block. Regenerating the JS is mechanical;
   confirming the frontend protocol didn't change under you is not.

## When upstream changes shape

A rejected hunk (or an upstream file that moved/disappeared) means the mechanical
path stopped short — that is the signal to think, not to force. Read the reject,
understand what upstream changed, and decide whether the pyodide branch still
makes sense before hand-porting it. Then regenerate the stored diff (step 3) so
the next release bump starts clean again.

## Bumping the vendored browser runtime

`--self-hosted` mirrors PyScript, Pyodide and a few PyPI wheels into the build
(`nicegui_pyodide/build/vendor.py`). Two pins matter:

* the **PyScript release** lives in `templates/index.html` — that single URL is the
  source of truth, and the Pyodide version is read back out of the downloaded bundle,
  so bumping PyScript is usually a one-line change. Usually: Pyodide's own file layout
  can move under you (314 renamed `pyodide.asm.js` to `pyodide.asm.mjs`), which shows up
  as a 404 mid-vendor. `vendor.py` now reads the glue filename out of `pyodide.mjs`
  instead of pinning it, but a future layout change would need the same treatment;
* the **PyPI wheel versions** are declared once in `vendor.RUNTIME_PACKAGES`
  (top-level) and `vendor.RUNTIME_TRANSITIVE` (their dependencies), pinned so an
  air-gapped build is reproducible. `cli.DEFAULT_INSTALL_ARGS` is derived from the
  first, so the default-build list cannot drift from the vendored set; and because
  `--self-hosted` installs with `deps=False`, `vendor._verify_closure` re-derives the
  closure from the downloaded wheels' own `Requires-Dist` and fails the build if
  `RUNTIME_TRANSITIVE` is short. Adding a package usually means editing one line.

After either bump run `pytest -q tests/test_offline.py`; it builds with
`--self-hosted`, blocks every non-localhost request in a real browser, and checks the
demo still boots.
