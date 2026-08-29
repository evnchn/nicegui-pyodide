"""Air-gap oracle for ``--self-hosted`` (issue #7).

Every request that leaves ``127.0.0.1`` is aborted at the browser, so the page has
no CDN and no PyPI. A ``--self-hosted`` build must still boot Pyodide, mount NiceGUI
and round-trip a click through the bridge.

The second test is the *instrument check*: the same blocking against a stock (CDN)
build must fail to boot. Without it, a route filter that silently matched nothing
would let the first test pass on a build that was never actually offline.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nicegui_pyodide.build import vendor
from nicegui_pyodide.build.cli import PYSCRIPT_CONFIG, build
# sibling module, not a package import: bare `pytest` does not put the repo root on
# sys.path (only `python -m pytest` does), but it does prepend this file's directory.
from test_browser import EXERCISE_APP

BOOT_TIMEOUT = 240_000
# The CDN build fails as soon as its first external request is aborted, so it needs
# no patience — only enough to rule out a slow-but-succeeding boot.
NO_BOOT_TIMEOUT = 45_000


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _serve(directory: Path):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(('127.0.0.1', port), partial(_QuietHandler, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f'http://127.0.0.1:{port}'


def _cut_the_network(page, base_url: str) -> list[str]:
    """Abort every request that is not served by our own local HTTP server."""
    escaped: list[str] = []

    def _route(route):
        if route.request.url.startswith(base_url):
            route.continue_()
        else:
            escaped.append(route.request.url)
            route.abort()

    page.route('**/*', _route)
    return escaped


@pytest.fixture(scope='session')
def offline_dist(tmp_path_factory):
    dist = tmp_path_factory.mktemp('dist-offline')
    build(str(dist), self_hosted=True)
    return dist


@pytest.fixture(scope='session')
def exercise_dist(tmp_path_factory, offline_dist):
    """The self-hosted build with the heavyweight exercise app (no second download)."""
    dist = tmp_path_factory.mktemp('dist-exercise') / 'dist'
    shutil.copytree(offline_dist, dist)
    (dist / 'app.py').write_text(EXERCISE_APP)
    return dist


@pytest.fixture(scope='session')
def cdn_dist(tmp_path_factory):
    dist = tmp_path_factory.mktemp('dist-cdn')
    build(str(dist))
    return dist


def test_manifest_records_what_was_vendored(offline_dist):
    manifest = json.loads((offline_dist / 'pyscript' / 'MANIFEST.json').read_text())
    assert manifest['pyscript_release'] == '2026.7.3'
    # Derived, not configured: a pinned PyScript release implies exactly one Pyodide.
    # Asserted exactly, so picking up a different-but-still-bootable runtime goes red.
    # (Was `startswith('0.')` — the 0.2x.y scheme Pyodide left behind at 314.)
    assert manifest['pyodide_version'] == '314.0.3'
    assert manifest['files'], 'manifest lists no vendored files'
    for entry in manifest['files']:
        vendored = offline_dist / 'pyscript' / entry['path']
        assert vendored.is_file(), f'manifest lists {entry["path"]} but it was not written'
        assert vendored.stat().st_size == entry['bytes']
        assert hashlib.sha256(vendored.read_bytes()).hexdigest() == entry['sha256']


def test_generated_page_has_no_remaining_cdn_urls(offline_dist):
    html = (offline_dist / 'index.html').read_text()
    assert 'pyscript.net' not in html
    assert 'offline src="./pyscript/core.js"' in html
    # the documented `interpreter` key, inline — the only form PyScript honours
    assert '"interpreter": "./pyscript/pyodide/pyodide.mjs"' in html
    # and no pyscript.toml that would silently do nothing next to it
    assert not (offline_dist / 'pyscript.toml').exists()
    entrypoint = (offline_dist / 'entrypoint.py').read_text()
    # the PyPI names are gone; every runtime dependency is a local wheel path
    assert "'typing-extensions'" not in entrypoint
    assert "'./pyscript/wheels/" in entrypoint


def test_self_hosted_build_boots_with_the_network_cut(page, offline_dist):
    httpd, base_url = _serve(offline_dist)
    try:
        escaped = _cut_the_network(page, base_url)
        errors: list[str] = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        page.goto(f'{base_url}/index.html', wait_until='domcontentloaded')
        page.wait_for_function('() => window.__pyodide_ready === true', timeout=BOOT_TIMEOUT)

        assert 'Hello from NiceGUI' in page.inner_text('body')
        # not just rendered — the Pyodide bridge round-trips an event
        page.locator('#app button', has_text='Click me').first.click()
        page.wait_for_function("() => document.body.innerText.includes('Clicks: 1')", timeout=15_000)

        assert not escaped, 'build still reached the network: ' + ', '.join(escaped)
        assert not errors, 'page errors while offline:\n' + '\n'.join(errors)
    finally:
        httpd.shutdown()


def test_cdn_build_cannot_boot_offline(page, cdn_dist):
    """Instrument check — proves the request blocking above is real."""
    httpd, base_url = _serve(cdn_dist)
    try:
        escaped = _cut_the_network(page, base_url)
        page.goto(f'{base_url}/index.html', wait_until='domcontentloaded')
        with pytest.raises(Exception):  # noqa: PT011,B017  - playwright TimeoutError
            page.wait_for_function('() => window.__pyodide_ready === true', timeout=NO_BOOT_TIMEOUT)
        assert escaped, 'no external request was blocked — the route filter matched nothing'
    finally:
        httpd.shutdown()


def test_heavyweight_elements_work_offline(page, exercise_dist, tmp_path):
    """leaflet/xterm/scene/upload pull lazily-loaded chunks the starter app never touches.

    The one thing that legitimately still needs the network is the map *tiles* — that is
    the app's own content, not the runtime, so it is asserted as such rather than ignored.
    """
    httpd, base_url = _serve(exercise_dist)
    try:
        escaped = _cut_the_network(page, base_url)
        errors: list[str] = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        page.goto(f'{base_url}/index.html', wait_until='domcontentloaded')
        page.wait_for_function('() => window.__pyodide_ready === true', timeout=BOOT_TIMEOUT)

        page.wait_for_selector('#app .leaflet-container', timeout=20_000)
        page.wait_for_selector('#app .xterm', timeout=20_000)
        page.wait_for_selector('#app canvas', timeout=20_000)

        payload = tmp_path / 'payload.txt'
        payload.write_bytes(b'hello-upload-payload')  # 20 bytes
        page.locator('#app input[type="file"]').first.set_input_files(str(payload))
        page.wait_for_function(
            "() => document.body.innerText.includes('upload: payload.txt 20b')", timeout=15_000)

        runtime_escapes = [u for u in escaped if '.tile.' not in u and '/tile' not in u]
        assert not runtime_escapes, 'a runtime asset was not vendored: ' + ', '.join(runtime_escapes)
        assert not errors, 'page errors while offline:\n' + '\n'.join(errors)
    finally:
        httpd.shutdown()


def test_refuses_to_overwrite_a_foreign_pyscript_dir(tmp_path):
    """`build .` must not eat a user's own pyscript/ directory."""
    theirs = tmp_path / 'pyscript'
    theirs.mkdir()
    (theirs / 'mine.txt').write_text('user data')
    with pytest.raises(SystemExit):
        build(str(tmp_path), self_hosted=True)
    assert (theirs / 'mine.txt').read_text() == 'user data'


def test_inline_config_matches_the_toml_template():
    """The self-hosted build inlines PYSCRIPT_CONFIG; the template is the readable copy."""
    tomllib = pytest.importorskip('tomllib')
    template = Path(__file__).resolve().parent.parent / 'nicegui_pyodide' / 'templates' / 'pyscript.toml'
    assert tomllib.loads(template.read_text()) == PYSCRIPT_CONFIG


def test_cdn_install_list_is_derived_not_duplicated():
    """The names a default build installs are the top-level names --self-hosted vendors."""
    from nicegui_pyodide.build.cli import DEFAULT_INSTALL_ARGS
    assert DEFAULT_INSTALL_ARGS == repr(list(vendor.RUNTIME_PACKAGES))
    assert set(vendor.PYPI_WHEELS) == set(vendor.RUNTIME_PACKAGES) | set(vendor.RUNTIME_TRANSITIVE)


def test_vendored_wheels_are_dependency_closed(offline_dist):
    vendor._verify_closure(offline_dist / 'pyscript' / 'wheels')


def test_closure_check_catches_a_missing_transitive_dep(offline_dist, tmp_path):
    """Instrument check: the closure guard must actually bite on a short wheel set."""
    for wheel in (offline_dist / 'pyscript' / 'wheels').glob('*.whl'):
        if not wheel.name.startswith('webencodings'):
            shutil.copy2(wheel, tmp_path / wheel.name)
    with pytest.raises(RuntimeError, match='webencodings'):
        vendor._verify_closure(tmp_path)


@pytest.mark.parametrize(('marker', 'optional'), [
    ('extra == "test"', True),                              # plain extras gate
    ('python_version >= "3.8.1" and extra == "all"', True),  # markdown2 ships this one
    ('python_version < "3.14" or extra == "foo"', False),    # reachable without the extra
    ('extra != "x"', False),                                 # true when no extra is set
    ('', False),                                             # unconditional requirement
])
def test_extras_marker_rule(marker, optional):
    """The closure check skips extras — it must not skip a genuinely required dep."""
    assert vendor._is_extras_only(marker) is optional


def test_self_hosted_rebuild_removes_a_stale_pyscript_toml(offline_dist):
    """A dir rebuilt from default mode must not keep a config the inline one overrides."""
    assert not (offline_dist / 'pyscript.toml').exists()


@pytest.mark.parametrize('loader, expected', [
    ('...loadScript("pyodide.asm.js")...', 'pyodide.asm.js'),      # Pyodide <= 0.29
    ('...import("./pyodide.asm.mjs")...', 'pyodide.asm.mjs'),      # Pyodide >= 314
])
def test_emscripten_glue_name_is_read_from_the_loader(loader, expected):
    """Pinning either spelling 404s the other release; the loader names its own glue."""
    assert set(vendor._PYODIDE_ASM_RE.findall(loader)) == {expected}
    assert expected not in vendor.PYODIDE_FILES


def test_vendored_pyodide_has_the_glue_the_loader_asks_for(offline_dist):
    """End-to-end: whatever pyodide.mjs names must actually be on disk."""
    pyodide = offline_dist / 'pyscript' / 'pyodide'
    names = set(vendor._PYODIDE_ASM_RE.findall(
        (pyodide / vendor.PYODIDE_LOADER).read_text('utf-8', 'replace')))
    assert len(names) == 1
    assert (pyodide / names.pop()).is_file()
