"""Real-browser oracle — the empirical gate the nicegui-pyodide fixes verify against.

Builds a servable demo via ``nicegui-pyodide-build`` into a temp dir, overwrites its
``app.py`` with a comprehensive exercise app, serves it over plain HTTP, drives headless
Chromium through the full Pyodide boot, and asserts real behaviour across:

* core: render, button -> handler -> DOM, ``ui.notify``, input round-trip, markdown
* ``ui.upload`` round-trip through the in-process bridge                (issue #8)
* ``on_connect`` handlers + ``app.storage.tab``                         (issue #9)
* ``ui.leaflet`` / ``ui.xterm`` / ``ui.scene`` mount without the        (issue #10)
  ``window.socket`` ``TypeError`` or a missing component resource

Set ``NICEGUI_PYODIDE_TEST_BREAK=1`` to neuter the click handler so the oracle *fails* —
CI runs this to prove the suite is not always-green (and that it guards the bridge path,
not just the initial render).
"""
from __future__ import annotations

import os
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nicegui_pyodide.build.cli import build

MOUNT_TIMEOUT = 240_000
BREAK_ENV = 'NICEGUI_PYODIDE_TEST_BREAK'

# Console/page-error substrings that indicate REAL breakage (Python tracebacks, the
# window.socket TypeError, the tab-storage assert, undefined bridge hooks). We fail on
# these rather than on "any console error" so external-resource noise (leaflet map tiles,
# favicon, 404s) doesn't red the run on offline CI or differ across OSes.
FATAL_ERROR_SIGNATURES = (
    'Traceback', 'AssertionError', 'TypeError', 'AttributeError', 'NameError',
    'window.socket', 'tab storage for', 'is not defined', 'onUpload',
)

EXERCISE_APP = '''\
import nicegui_pyodide  # noqa: F401
from nicegui import Client, ui, app
from nicegui_pyodide import page

with Client(page('/')) as client:
    ui.label('Hello from NiceGUI')
    count = ui.label('Clicks: 0')
    _s = {'n': 0}

    def _bump():
        _s['n'] += 1
        count.text = f'Clicks: {_s["n"]}'
        ui.notify(f'Clicked {_s["n"]} time(s)')
    ui.button('Click me', on_click=_bump)

    name = ui.input('Your name', value='world')
    ui.button('Greet', on_click=lambda: ui.notify(f'Hello, {name.value}!'))

    ui.markdown('This is **NiceGUI** via _Pyodide_.')

    conn = ui.label('connect: pending')
    store = ui.label('storage: pending')

    def _on_conn(*_):
        conn.text = 'connect: fired'
        app.storage.tab['n'] = app.storage.tab.get('n', 0) + 1
        store.text = f'storage: {app.storage.tab["n"]}'
    app.on_connect(_on_conn)

    up = ui.label('upload: none')

    def _on_up(e):
        up.text = f'upload: {e.file.name} {e.file.size()}b'
    ui.upload(on_upload=_on_up, auto_upload=True)

    ui.leaflet(center=(51.5, -0.09))
    ui.xterm()
    ui.scene()
'''


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='session')
def base_url(tmp_path_factory):
    dist = tmp_path_factory.mktemp('dist')
    build(str(dist))
    app_src = EXERCISE_APP
    if os.environ.get(BREAK_ENV):
        # sabotage the bridge handler path (not just the label) so the behavioural
        # assertions — the ones that actually exercise the Pyodide bridge — go red.
        app_src = app_src.replace('on_click=_bump', 'on_click=lambda: None')
    (dist / 'app.py').write_text(app_src)
    httpd = ThreadingHTTPServer(('127.0.0.1', _free_port()),
                                partial(_QuietHandler, directory=str(dist)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{httpd.server_address[1]}'
    finally:
        httpd.shutdown()


@pytest.fixture
def upload_file(tmp_path):
    f = tmp_path / 'payload.txt'
    f.write_bytes(b'hello-upload-payload')  # 20 bytes
    return f


def test_browser_smoke(page, base_url, upload_file):
    fatal: list[str] = []

    def _record(source: str, text: str) -> None:
        if any(sig in text for sig in FATAL_ERROR_SIGNATURES):
            fatal.append(f'[{source}] {text}')

    page.on('console', lambda m: _record('console.error', m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: fatal.append(f'[pageerror] {e}'))

    page.goto(f'{base_url}/index.html', wait_until='domcontentloaded')
    page.wait_for_function('() => window.__pyodide_ready === true', timeout=MOUNT_TIMEOUT)
    page.wait_for_timeout(1500)

    body = page.inner_text('body')
    assert 'Hello from NiceGUI' in body
    assert 'Clicks: 0' in body
    assert page.locator('#app strong', has_text='NiceGUI').count() > 0

    # --- core: bridge handler round-trip + ui.notify ---
    btn = page.locator('#app button', has_text='Click me')
    assert btn.count() > 0
    btn.first.click()
    page.wait_for_function("() => document.body.innerText.includes('Clicks: 1')", timeout=15_000)
    page.wait_for_selector('.q-notification__message', timeout=8_000)
    assert 'Clicked 1 time' in page.inner_text('.q-notification__message')

    # --- core: input value round-trips to Python (event args over the bridge) ---
    text_input = page.locator('#app input:not([type="file"])').first
    text_input.fill('Pyodide')
    page.locator('#app button', has_text='Greet').first.click()
    page.wait_for_function("() => document.body.innerText.includes('Hello, Pyodide!')", timeout=10_000)

    # --- #9: on_connect fired during handshake + app.storage.tab usable (no assert) ---
    page.wait_for_function("() => document.body.innerText.includes('connect: fired')", timeout=10_000)
    page.wait_for_function("() => document.body.innerText.includes('storage: 1')", timeout=10_000)

    # --- #8: ui.upload round-trips a real file through the bridge to Python ---
    file_input = page.locator('#app input[type="file"]')
    assert file_input.count() > 0
    file_input.first.set_input_files(str(upload_file))
    page.wait_for_function(
        "() => document.body.innerText.includes('upload: payload.txt 20b')", timeout=15_000)

    # --- #10: server-assuming elements mount without window.socket / resource errors ---
    page.wait_for_selector('#app .leaflet-container', timeout=20_000)   # leaflet.js loaded via override
    page.wait_for_selector('#app .xterm', timeout=20_000)              # xterm.js loaded via override
    page.wait_for_selector('#app canvas', timeout=20_000)             # scene WebGL canvas (window.socket shim)

    assert not fatal, 'fatal browser errors detected:\n' + '\n'.join(fatal)
