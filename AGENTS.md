# AGENTS.md — nicegui-pyodide

Guidance for coding agents / LLMs working *with* this package (writing apps that run on
it). For contributing to the package itself, see `CONTRIBUTING.md`.

`nicegui-pyodide` runs **stock NiceGUI entirely in the browser** via Pyodide — no server,
no websocket, no backend. The idiom differs from server-mode NiceGUI; the rules below are
the ones an agent familiar with NiceGUI most often gets wrong.

## The correct app pattern

```python
import nicegui_pyodide            # MUST be imported before `import nicegui`
from nicegui import Client, ui
from nicegui_pyodide import page

with Client(page('/')) as client:  # NOT @ui.page, NOT ui.run()
    ui.label('Hello from the browser!')
    ui.button('Click me', on_click=lambda: ui.notify('Clicked!'))
# the generated entrypoint mounts `client`
```

## Hard rules

1. **Do not call `ui.run()` or `ui.run_with()`.** There is no server. Both raise a
   `RuntimeError` pointing you back here. Build the UI inside `Client(page('/'))` instead.
2. **`import nicegui_pyodide` before `import nicegui`.** It installs `sys.modules` shims for
   the absent server deps (`fastapi`, `uvicorn`, `socketio`, …). Importing `nicegui` first
   raises a clear `RuntimeError`.
   - Keep the `# noqa: F401` comment on that import — it looks unused but is load-bearing.
   - Do **not** let an import sorter move it below `import nicegui`.
3. **`@ui.page` and server routes don't exist.** Use the single `Client(page('/'))` context.
4. **No backend features:** `app.add_static_files`, HTTP endpoints, `ui.download` of server
   files, native mode, multiprocessing — all unavailable. Root-relative `src`/`href`
   (e.g. `/foo.png`) won't resolve; use absolute URLs or `data:`/`blob:` URIs.

## Build & run

```bash
pip install nicegui-pyodide
nicegui-pyodide-build ./dist --serve   # build + serve at http://localhost:8080
# edit ./dist/app.py, reload the page
```

- Re-running `nicegui-pyodide-build ./dist` refreshes generated files but **keeps your
  edited `app.py`** (use `--force` to reset it).
- `--port N` changes the serve port.

## What works

Most elements are pure client-side Vue and work unchanged. `ui.markdown`, `ui.leaflet`,
`ui.xterm`, and `ui.scene` are supported via overrides/shims. See the support matrix in
`README.md` for the full picture, including partial/unsupported cases.
