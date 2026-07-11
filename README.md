# nicegui-pyodide

Run **stock [NiceGUI](https://nicegui.io)** entirely in the browser via
[Pyodide](https://pyodide.org) / [PyScript](https://pyscript.net) — **no server, no
websocket, no backend** — as an *external extension*.

**No fork. No patches to NiceGUI's source.** This package makes an unmodified
`pip install nicegui` importable and runnable under Pyodide from the outside, by
weaving a set of `sys.modules` shims in before `import nicegui`.

> This is the extension re-implementation of the upstream Pyodide effort
> ([zauberzeug/nicegui#5776](https://github.com/zauberzeug/nicegui/pull/5776)),
> which required editing ~25 core files. Here that lives entirely in a separate
> package instead.

## Why an extension instead of a PR?

The PR approach had to touch NiceGUI core in ~25 places — wrapping every
`from fastapi import …` / `from socketio import …` in `try/except ImportError`
and adding `if IS_PYODIDE:` branches. That is invasive and hard to merge.

Pyodide has **no server**: there is no `uvicorn`, no `socket.io`, and NiceGUI's
server-only imports (`fastapi`, `starlette`, `uvicorn`, `aiofiles`, `anyio`,
`ifaddr`) simply don't exist. So a plain `import nicegui` explodes before any of
your code runs.

This extension solves it from *outside* core:

1. **A `sys.meta_path` finder** fabricates a permissive stub module for every
   import under those absent packages — rich enough (subclassable, callable,
   decorator-friendly, operator-safe) that stock NiceGUI's *unguarded* usages
   don't crash: `class App(FastAPI)`, `@app.post(url)`,
   `class APIRouter(fastapi.APIRouter)`, `class CacheControlledStaticFiles(StaticFiles)`,
   `Jinja2Templates(path)`. (`nicegui_pyodide/_shims.py`)
2. **A lazy `nicegui.nicegui` seed** supplies the `app` singleton without running
   stock `nicegui/nicegui.py` — which would build the FastAPI app + socket.io
   server + HTTP routes. Stock `nicegui/__init__.py` does `from .nicegui import app`;
   the import machinery finds the seed first.
3. **An in-process JS bridge** replaces socket.io: Python `emit()` calls hop
   straight to JavaScript over the Pyodide FFI. (`bridge.py`, `outbox.py`, `runtime.py`)
4. **A pyodide-patched `nicegui.js`** (shipped as an asset, pinned to a NiceGUI
   version) that talks to the bridge instead of a websocket. All new paths are
   gated on `window.niceguiBridge`, so the file is backward-compatible with
   server mode.

On a normal (server) Python this package is **inert** — `import nicegui` behaves
exactly as usual — so shared code can import it unconditionally.

## Usage

```bash
pip install git+https://github.com/evnchn/nicegui-pyodide  # pulls in a compatible nicegui; PyPI package coming soon
nicegui-pyodide-build ./dist           # assemble a servable demo dir
python -m http.server -d ./dist 8080   # open http://localhost:8080
```

Edit `dist/app.py` to change the UI. The app-building pattern:

```python
import nicegui_pyodide                 # MUST come before `import nicegui`
from nicegui import Client, ui
from nicegui_pyodide import page

with Client(page('/')) as client:
    ui.label('Hello from the browser!')
    ui.button('Click me', on_click=lambda: ui.notify('Clicked!'))
# the generated entrypoint mounts `client` via PyodideRuntime
```

## Element support matrix

Most NiceGUI elements are pure client-side Vue and work unchanged. A handful assume a
live server (a socket.io connection or a component static route) and therefore need
special handling in the browser. Status against the pinned NiceGUI release:

| Element | Status | Notes |
| --- | --- | --- |
| labels, buttons, inputs, layout, tables, most Quasar wrappers | ✅ works | pure client-side; no server assumption |
| `ui.markdown` (incl. Mermaid, code highlighting) | ✅ works (override) | codehilite CSS served from `./dynamic_resources/` instead of a server route |
| `ui.scene` / `ui.scene_view` (3D / WebGL) | 🔶 override shipped | the `init` handshake polled `window.socket.id`; a minimal `window.socket` stand-in is now provided in Pyodide mode (not yet browser-verified) |
| `ui.leaflet` (map) | 🔶 override shipped | same `init` shim, plus CSS/JS (`leaflet`, optional `leaflet-draw`) resolved from `./esm/nicegui-leaflet/` (not yet browser-verified) |
| `ui.xterm` (terminal) | 🔶 override shipped | `xterm.css` resolved from `./esm/nicegui-xterm/` instead of a server route (not yet browser-verified) |
| `ui.echart` with a `theme` **URL** string | ⚠️ partial | inline theme objects work; a theme passed as a URL `fetch()`es a server path — it fails soft (theme just doesn't apply). Use an inline theme object |
| `ui.image` / `ui.interactive_image` / `ui.video` / `ui.audio` / `ui.link` with a **root-relative** `src`/`href` (e.g. `/foo.png`) | ⚠️ partial | such paths were `app.add_static_files` / uploaded-file routes with no backend here. Use absolute URLs, `data:`/`blob:` URIs, or bundle the asset. External URLs are unaffected |
| `@ui.page` server routes, `app.add_static_files`, HTTP endpoints, native mode, multiprocessing | ❌ unsupported | require a real backend; no-op or unavailable in the browser (use the `Client(page(...))` pattern shown above) |

The overridden/shimmed elements (`scene`, `leaflet`, `xterm`) are the socket.io/handshake
and static-route cases that would otherwise fail with an opaque `TypeError` (`window.socket` is
`undefined`) or a silent missing-stylesheet. Genuinely server-bound features (last row) are
documented as unsupported rather than patched.

## Version pinning

`nicegui_pyodide/static/nicegui.js` is a pyodide-patched copy of a specific
NiceGUI release's `nicegui.js`. The `nicegui>=3.14,<3.15` pin in `pyproject.toml`
keeps them in lockstep. To support a newer NiceGUI, regenerate the patched JS
against that release and widen the pin.

## Status / limitations

- Verified in-browser (Chromium via Playwright): render, button click →
  handler → DOM update, `ui.notify`, input round-trip through the bridge,
  `ui.upload` round-trip, `on_connect` handlers, and `app.storage.tab`.
- File upload works client-side: files are read in the browser (`FileReader` →
  base64) and delivered over the bridge to `runtime._handle_upload` (no server route).
- `@ui.page` server routes are not available (there is no server); use the
  `Client(page(...))` pattern shown above.
- Anything needing a real backend (HTTP endpoints, `app.add_static_files`,
  multiprocessing, native mode) is a no-op or unavailable in the browser.

## License & attribution

This project is MIT-licensed (see [`LICENSE`](LICENSE)), Copyright (c) 2026 evnchn.

It **bundles modified copies** of a few files from [NiceGUI](https://github.com/zauberzeug/nicegui)
(MIT, © Zauberzeug GmbH): `nicegui_pyodide/static/nicegui.js` and the component overrides under
`nicegui_pyodide/static/component_overrides/` (`markdown.js`, `leaflet/leaflet.js`, `xterm/xterm.js`).
See [`NOTICE`](NOTICE) for details.
Other vendor assets (Quasar, Vue, Tailwind, …) are copied from your own installed NiceGUI at
build time and are **not** redistributed in this repository.

The Pyodide mechanism re-implemented here originated in the author's own upstream PR,
[zauberzeug/nicegui#5776](https://github.com/zauberzeug/nicegui/pull/5776) (closed).
