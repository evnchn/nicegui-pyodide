"""Vendor the runtime CDN assets so a built demo loads with no network at all.

The stock build leaves three live CDN dependencies in the page: PyScript core
(``pyscript.net``), the Pyodide runtime PyScript pulls in turn (``cdn.jsdelivr.net``),
and the pure-Python wheels ``micropip`` fetches from PyPI. ``--self-hosted`` mirrors
all three into ``<outdir>/pyscript/`` and rewrites the page to point at them.

The layout is not ours to choose. PyScript's own offline mode triggers on an
``offline`` attribute on the ``<script type="module">`` that loads ``core.js``, and
then looks for the interpreter at ``./pyscript/<type>/<type>.mjs`` resolved against
the *page* URL — so the Pyodide runtime must land in ``<outdir>/pyscript/pyodide/``.
(The ``interpreter`` key in ``pyscript.toml`` is honoured only on the worker path,
not for a main-thread ``<script type="py">``.)

Nothing here is version-guessing: the PyScript release is whatever
``templates/index.html`` pins, and the Pyodide version is read back out of the
PyScript bundle we just downloaded (PyScript hardcodes the runtime it was built
against). ``pyscript/MANIFEST.json`` records every URL, size and sha256.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

# Pure-Python wheels the entrypoint micropip-installs from PyPI. Pinned so an
# air-gapped build is reproducible; bump deliberately (see CONTRIBUTING.md).
PYPI_WHEELS = {
    'typing_extensions': '4.15.0',
    'markdown2': '2.5.4',
    'Pygments': '2.19.1',
    'docutils': '0.21.2',
    'tinycss2': '1.4.0',
    'webencodings': '0.5.1',   # tinycss2's only dependency
}

# Pyodide distribution files a bare interpreter needs, plus micropip itself
# (``pyscript.toml`` asks for it, and Pyodide resolves it through the local lock).
PYODIDE_FILES = ['pyodide.mjs', 'pyodide.asm.js', 'pyodide.asm.wasm',
                 'python_stdlib.zip', 'pyodide-lock.json']
PYODIDE_PACKAGES = ['micropip', 'packaging']

_PYSCRIPT_SRC_RE = re.compile(r'https://pyscript\.net/releases/([0-9][^/"\']*)/core\.js')
# PyScript hardcodes the Pyodide it was built against as a default parameter:
#   type:"pyodide",module:(v="0.29.3")=>`https://cdn.jsdelivr.net/pyodide/v${v}/full/pyodide.mjs`
# The second pattern is the loose fallback for a build that inlines the version instead.
_PYODIDE_VERSION_RES = (
    re.compile(r'["\']?pyodide["\']?\s*,\s*module:\s*\([^)]*?=\s*["\']([0-9][^"\']*)["\']'),
    re.compile(r'cdn\.jsdelivr\.net/pyodide/v([0-9][A-Za-z0-9.\-]*)/'),
)
# Relative module/asset references a bundler emits as plain string literals. Nested
# segments are allowed: a future chunk layout may not stay flat.
_REL_ASSET_RE = re.compile(
    r'["\'`](\.{1,2}/[A-Za-z0-9_./@-]+\.(?:js|mjs|css|json|wasm))["\'`]')
# url(...) references inside a mirrored stylesheet.
_CSS_URL_RE = re.compile(r'url\(\s*["\']?(\.{1,2}/[A-Za-z0-9_./@-]+)["\']?\s*\)')


def _get(url: str) -> bytes:
    # pyscript.net's CDN 403s the default ``Python-urllib`` agent.
    req = urllib.request.Request(url, headers={'User-Agent': 'nicegui-pyodide-build'})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310  (https, fixed hosts)
        return r.read()


class _Manifest:
    """Records every mirrored file, keyed by its path relative to the vendor root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entries: list[dict] = []
        self.skipped: list[dict] = []

    def save(self, path: Path, url: str, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.entries.append({
            'url': url,
            'path': path.relative_to(self.root).as_posix(),
            'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
        })


def pyscript_release(html: str) -> str:
    """Return the PyScript release pinned by ``index.html`` (the single source of truth)."""
    m = _PYSCRIPT_SRC_RE.search(html)
    if not m:
        raise RuntimeError('No pyscript.net release URL found in index.html — cannot self-host.')
    return m.group(1)


def _mirror_pyscript(base: str, dest: Path, man: _Manifest) -> None:
    """Download core.js and transitively every chunk/asset it references."""
    dest.mkdir(parents=True, exist_ok=True)
    queue, seen, got = ['core.js', 'core.css'], set(), 0
    while queue:
        name = queue.pop().lstrip('./')
        if name in seen or '..' in name.split('/'):
            continue
        seen.add(name)
        try:
            data = _get(base + name)
        except Exception as e:  # noqa: BLE001
            # Only the seed is load-bearing. The crawl reads *string literals*, so it also
            # turns up paths the bundle never fetches (zip.js names a wasm blob it inlines);
            # a 404 there is a false positive, not a missing chunk. Recorded, not fatal —
            # real under-vendoring is caught by the network-cut browser test, not by a 404.
            if name == 'core.js':
                raise RuntimeError(f'PyScript core.js failed to download: {e}') from e
            man.skipped.append({'path': name, 'reason': str(e)})
            continue
        man.save(dest / name, base + name, data)
        got += 1
        text = data.decode('utf-8', 'replace')
        if name.endswith(('.js', '.mjs')):
            queue += _REL_ASSET_RE.findall(text)
        elif name.endswith('.css'):
            queue += _CSS_URL_RE.findall(text)
    print(f'  pyscript/         ({got} files, whole PyScript chunk graph'
          + (f'; {len(man.skipped)} unresolved refs, see MANIFEST.json)' if man.skipped else ')'))


def _pyodide_version(pyscript_dir: Path) -> str:
    """Read the Pyodide release PyScript was built against out of the mirrored bundle."""
    sources = sorted(p for p in pyscript_dir.rglob('*') if p.suffix in ('.js', '.mjs'))
    for pattern in _PYODIDE_VERSION_RES:
        found = {m for p in sources for m in pattern.findall(p.read_text('utf-8', 'replace'))}
        if len(found) == 1:
            return found.pop()
        if found:
            raise RuntimeError(f'Ambiguous Pyodide version in the PyScript bundle: {sorted(found)}')
    raise RuntimeError('Could not read the Pyodide version out of the PyScript bundle.')


def _mirror_pyodide(version: str, dest: Path, man: _Manifest) -> None:
    base = f'https://cdn.jsdelivr.net/pyodide/v{version}/full/'
    dest.mkdir(parents=True, exist_ok=True)
    for name in PYODIDE_FILES:
        man.save(dest / name, base + name, _get(base + name))
    lock = json.loads((dest / 'pyodide-lock.json').read_text())
    for pkg in PYODIDE_PACKAGES:
        entry = lock['packages'].get(pkg)
        if entry is None:
            raise RuntimeError(f'Pyodide {version} lock has no package {pkg!r}.')
        man.save(dest / entry['file_name'], base + entry['file_name'], _get(base + entry['file_name']))
    print(f'  pyscript/pyodide/ (Pyodide {version}, Python {lock["info"]["python"]})')


def _mirror_wheels(dest: Path, man: _Manifest) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for name, version in PYPI_WHEELS.items():
        meta = json.loads(_get(f'https://pypi.org/pypi/{name}/{version}/json'))
        files = [u for u in meta['urls']
                 if u['packagetype'] == 'bdist_wheel' and u['filename'].endswith('-none-any.whl')]
        if not files:
            raise RuntimeError(f'No pure-Python wheel for {name}=={version} on PyPI.')
        url = files[0]['url']
        man.save(dest / files[0]['filename'], url, _get(url))
        names.append(files[0]['filename'])
    print(f'  pyscript/wheels/  ({len(names)} pure-Python wheels)')
    return names


def vendor(out: Path, html: str) -> dict:
    """Mirror PyScript + Pyodide + the PyPI wheels into ``out/pyscript``.

    Returns the substitutions the templates need to point at the local copies.
    """
    release = pyscript_release(html)
    root = out / 'pyscript'          # path fixed by PyScript's offline mode (see module docstring)
    # Wipe our own tree so a release bump or a half-finished run cannot leave stale
    # chunks the manifest no longer describes — but only once it is provably ours.
    # `build .` into a directory that already has an unrelated pyscript/ must not eat it.
    if root.exists():
        if not (root / 'MANIFEST.json').is_file():
            sys.exit(f'{root} exists and was not written by --self-hosted '
                     f'(no MANIFEST.json). Refusing to overwrite it; move it aside or '
                     f'build into a different output directory.')
        shutil.rmtree(root)
    man = _Manifest(root)
    print(f'Vendoring runtime for offline use (PyScript {release})...')
    _mirror_pyscript(f'https://pyscript.net/releases/{release}/', root, man)
    version = _pyodide_version(root)
    _mirror_pyodide(version, root / 'pyodide', man)
    wheels = _mirror_wheels(root / 'wheels', man)
    total = sum(e['bytes'] for e in man.entries)
    (root / 'MANIFEST.json').write_text(json.dumps({
        'pyscript_release': release,
        'pyodide_version': version,
        'pypi_wheels': PYPI_WHEELS,
        'total_bytes': total,
        'files': man.entries,
        'unresolved_refs': man.skipped,
    }, indent=2))
    print(f'  pyscript/MANIFEST.json  ({len(man.entries)} files, {total // 1024 // 1024} MB)')
    return {
        'core_js': './pyscript/core.js',
        'install_args': repr([f'./pyscript/wheels/{w}' for w in wheels]) + ', deps=False',
        'pyscript_release': release,
        'pyodide_version': version,
    }
