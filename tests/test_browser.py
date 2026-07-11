"""Real-browser smoke test — the oracle the other nicegui-pyodide fixes verify against.

Builds a servable demo via ``nicegui-pyodide-build`` into a temp dir, serves it over
plain HTTP, drives headless Chromium through the full Pyodide boot + the example app,
and asserts both the rendered behaviour *and* zero console / page errors.

Set ``NICEGUI_PYODIDE_TEST_BREAK=1`` to sabotage the built app so the smoke test
*fails* — CI uses this to prove the oracle is not always-green.
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
    if os.environ.get(BREAK_ENV):
        app = dist / 'app.py'
        app.write_text(app.read_text().replace('Hello from NiceGUI', 'SABOTAGED BUILD'))
    httpd = ThreadingHTTPServer(('127.0.0.1', _free_port()),
                                partial(_QuietHandler, directory=str(dist)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{httpd.server_address[1]}'
    finally:
        httpd.shutdown()


def test_browser_smoke(page, base_url):
    errors: list[str] = []
    page.on('console', lambda m: errors.append(f'[console.error] {m.text}') if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(f'[pageerror] {e}'))

    page.goto(f'{base_url}/index.html', wait_until='domcontentloaded')
    page.wait_for_function('() => window.__pyodide_ready === true', timeout=MOUNT_TIMEOUT)
    page.wait_for_timeout(1000)

    body = page.inner_text('body')
    assert 'Hello from NiceGUI' in body
    assert 'Clicks: 0' in body
    assert page.locator('#app strong', has_text='NiceGUI').count() > 0

    btn = page.locator('#app button', has_text='Click me')
    assert btn.count() > 0
    btn.first.click()
    page.wait_for_function("() => document.body.innerText.includes('Clicks: 1')", timeout=15_000)
    page.wait_for_selector('.q-notification__message', timeout=8_000)
    assert 'Clicked 1 time' in page.inner_text('.q-notification__message')

    btn.first.click()
    page.wait_for_function("() => document.body.innerText.includes('Clicks: 2')", timeout=15_000)

    inp = page.locator('#app input')
    assert inp.count() > 0
    inp.first.fill('Pyodide')
    page.locator('#app button', has_text='Greet').first.click()
    page.wait_for_function("() => document.body.innerText.includes('Hello, Pyodide!')", timeout=10_000)

    assert not errors, 'console/page errors detected:\n' + '\n'.join(errors)
