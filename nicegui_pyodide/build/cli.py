"""Assemble a servable Pyodide demo directory from the *installed* stock NiceGUI.

Unlike the upstream ``examples/pyodide/prepare.py`` (which built a wheel from a
NiceGUI *source checkout*), this operates entirely against the installed packages:

* copies the browser vendor assets (Quasar, Vue, Tailwind, fonts, CSS) out of the
  installed ``nicegui/static`` — but serves **nicegui-pyodide's pyodide-patched
  ``nicegui.js``**, not stock's;
* copies element component JS + ESM bundles and writes the import map;
* builds a *stripped* ``nicegui`` wheel (Python source + metadata only) straight
  from the installed package, and a wheel for ``nicegui_pyodide`` itself;
* drops in the HTML / entrypoint / config / example ``app.py`` templates, with the
  actual wheel filenames substituted in.

Run ``nicegui-pyodide-build [outdir]`` then serve ``outdir`` with any static server.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json as json_mod
import re
import shutil
import sys
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

from . import vendor as vendor_pkgs                       # stdlib-only; safe to import eagerly

PKG_DIR = Path(__file__).resolve().parent.parent          # nicegui_pyodide/
TEMPLATES_DIR = PKG_DIR / 'templates'
PATCHED_JS = PKG_DIR / 'static' / 'nicegui.js'

# Vendor assets copied verbatim from the installed nicegui/static (nicegui.js excluded
# — we ship the patched copy instead).
VENDOR_FILES = [
    'nicegui.css',
    'quasar.umd.prod.js',
    'quasar.important.prod.css',
    'quasar.unimportant.prod.css',
    'tailwindcss.min.js',
    'vue.esm-browser.prod.js',
]

# Pure-Python runtime deps micropip pulls from PyPI at page load, derived from the one
# declaration in build/vendor.py so the CDN list and the vendored wheel set cannot drift.
# ``--self-hosted`` replaces this with local wheel paths.
DEFAULT_INSTALL_ARGS = repr(list(vendor_pkgs.RUNTIME_PACKAGES))

# The PyScript config, declared once. templates/pyscript.toml is the readable copy used
# by default builds; a --self-hosted build serialises this same dict inline (the only
# form in which PyScript honours `interpreter`). A test asserts the two agree.
PYSCRIPT_CONFIG = {'packages': ['micropip'], 'files': {'./app.py': './app.py'}}

# Wheel filenames are computed from the real package versions at build time
# (PEP 427 requires ``{name}-{version}-py3-none-any.whl``; micropip parses the
# version from the filename, so it must be a valid PEP 440 version).

# The shipped static/nicegui.js is patched from this exact NiceGUI release. Building
# against a different patch release risks a client/JS protocol mismatch → warn loudly.
TESTED_NICEGUI = '3.14.0'
# The whole dependency pin the warning below must cover: any version in this range other
# than TESTED_NICEGUI ships JS that was never regenerated for it.
SUPPORTED_NICEGUI = 'nicegui>=3.14,<3.15'


def _nicegui_meta():
    """Return (nicegui_module, package_dir, METADATA_text) resolved via importlib.metadata.

    Using the distribution tied to the *imported* nicegui avoids picking a stale or
    wrong ``nicegui-*.dist-info`` when several sit side by side.
    """
    import importlib.metadata as im  # pylint: disable=import-outside-toplevel
    import nicegui  # pylint: disable=import-outside-toplevel
    pkg = Path(nicegui.__file__).parent
    try:
        dist = im.distribution('nicegui')
        metadata = dist.read_text('METADATA')
        if dist.version != nicegui.__version__:
            print(f'  WARNING: dist metadata version {dist.version} != imported nicegui '
                  f'{nicegui.__version__}; using imported version for the wheel.')
    except im.PackageNotFoundError:
        metadata = None
    if not metadata:
        cands = sorted(pkg.parent.glob('nicegui-*.dist-info'))
        if not cands:
            sys.exit('Could not locate nicegui distribution metadata.')
        metadata = (cands[-1] / 'METADATA').read_text(encoding='utf-8')
    if nicegui.__version__ != TESTED_NICEGUI:
        print(f'  WARNING: installed nicegui {nicegui.__version__} != tested {TESTED_NICEGUI} '
              f'(pin {SUPPORTED_NICEGUI}). The bundled patched nicegui.js/markdown.js target '
              f'{TESTED_NICEGUI}; the frontend protocol may have drifted anywhere across the pin. '
              f'Regenerate with scripts/regenerate_patched_js.py — see CONTRIBUTING.md, '
              f'"Supporting a new NiceGUI release".')
    return nicegui, pkg, metadata


# --------------------------------------------------------------------------- static

def prepare_static_files(out: Path) -> None:
    _, pkg, _ = _nicegui_meta()
    static = pkg / 'static'
    print(f'Copying vendor assets from {static}')
    for name in VENDOR_FILES:
        shutil.copy2(static / name, out / name)
        print(f'  {name}')

    # the pyodide-patched nicegui.js (shipped by this extension)
    shutil.copy2(PATCHED_JS, out / 'nicegui.js')
    print('  nicegui.js  (pyodide-patched, from nicegui_pyodide)')

    utils_src = static / 'utils'
    if utils_src.is_dir():
        dst = out / 'static' / 'utils'
        dst.mkdir(parents=True, exist_ok=True)
        for f in utils_src.glob('*.js'):
            shutil.copy2(f, dst / f.name)
        print(f'  static/utils/ ({sum(1 for _ in utils_src.glob("*.js"))} files)')

    if (static / 'fonts.css').exists():
        shutil.copy2(static / 'fonts.css', out / 'fonts.css')
        print('  fonts.css')
    fonts_src = static / 'fonts'
    if fonts_src.is_dir():
        dst = out / 'fonts'
        dst.mkdir(parents=True, exist_ok=True)
        for f in fonts_src.glob('*.woff2'):
            shutil.copy2(f, dst / f.name)
        print(f'  fonts/ ({sum(1 for _ in fonts_src.glob("*.woff2"))} files)')

    if (static / 'dompurify.mjs').exists():
        dst = out / 'static'
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(static / 'dompurify.mjs', dst / 'dompurify.mjs')
        print('  static/dompurify.mjs')


# ----------------------------------------------------------------------- components

def prepare_components(out: Path) -> None:
    from nicegui import ui as _ui  # pylint: disable=import-outside-toplevel
    # force PEP 562 lazy imports so every element registers its js_components / esm_modules
    for name in getattr(_ui, '__all__', None) or list(getattr(_ui, '_LAZY_IMPORTS', {})):
        try:
            getattr(_ui, name)
        except Exception:  # pylint: disable=broad-except
            pass
    from nicegui.dependencies import esm_modules, js_components  # pylint: disable=import-outside-toplevel
    _, pkg, _ = _nicegui_meta()
    elements_dir = pkg / 'elements'

    components_dir = out / 'components'
    esm_dir = out / 'esm'
    for d in (components_dir, esm_dir):
        if d.exists():
            shutil.rmtree(d)

    print('Copying component JS...')
    for comp in js_components.values():
        if not comp.path.exists():
            continue
        try:
            rel = comp.path.relative_to(elements_dir)
        except ValueError:
            continue
        dst = components_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(comp.path, dst)

    # Override specific component JS with pyodide-patched copies (e.g. markdown.js loads
    # its codehilite CSS from a static ./dynamic_resources/ path instead of a server route).
    overrides = PKG_DIR / 'static' / 'component_overrides'
    if overrides.is_dir():
        for ov in overrides.rglob('*.js'):
            rel = ov.relative_to(overrides)
            target = components_dir / rel
            if target.exists():
                shutil.copy2(ov, target)
                print(f'  override components/{rel.as_posix()} (pyodide-patched)')

    print('Copying ESM bundles...')
    imports: dict[str, str] = {}
    for esm in esm_modules.values():
        if not esm.path.is_dir():
            continue
        dest = esm_dir / esm.name
        dest.mkdir(parents=True, exist_ok=True)
        for src in esm.path.rglob('*'):
            if not src.is_file() or src.suffix == '.map':
                continue
            rel = src.relative_to(esm.path)
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
        imports[esm.name] = f'./esm/{esm.name}/index.js'
        imports[esm.name + '/'] = f'./esm/{esm.name}/'

    imports['dompurify'] = './static/dompurify.mjs'
    importmap = {'imports': dict(sorted(imports.items()))}
    tag = f'<script type="importmap">\n{json_mod.dumps(importmap, indent=2)}\n    </script>'
    html_path = out / 'index.html'
    html = html_path.read_text()
    html = re.sub(
        r'<!-- BEGIN IMPORTMAP.*?-->.*?<!-- END IMPORTMAP -->',
        '<!-- BEGIN IMPORTMAP (generated by nicegui-pyodide-build — do not edit) -->\n'
        f'    {tag}\n    <!-- END IMPORTMAP -->',
        html, flags=re.DOTALL,
    )
    html_path.write_text(html)
    print(f'Import map: {len(imports)} entries')


def prepare_dynamic_resources(out: Path) -> None:
    try:
        from pygments.formatters import HtmlFormatter  # pylint: disable=import-outside-toplevel
    except ImportError:
        print('  Pygments not installed, skipping codehilite.css')
        return
    css = (HtmlFormatter(nobackground=True).get_style_defs('.codehilite')
           + HtmlFormatter(nobackground=True, style='github-dark').get_style_defs('.body--dark .codehilite'))
    name = f'codehilite_{hashlib.sha256(css.encode()).hexdigest()[:32]}.css'
    d = out / 'dynamic_resources'
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(css)
    print(f'Generated dynamic_resources/{name}')


# ---------------------------------------------------------------------------- wheels

_BUILD_ARTIFACTS = {'package.json', 'package-lock.json', 'rollup.config.mjs', 'vite.config.js'}


def _keep_nicegui(rel: str) -> bool:
    """Keep only Python source + metadata; drop vendor static + element JS bundles."""
    if rel.startswith('nicegui/static/'):
        # keep only the couple of static files read at import time
        return rel in ('nicegui/static/headwind.css', 'nicegui/static/sad_face.svg')
    parts = rel.split('/')
    if len(parts) >= 3 and parts[1] == 'elements':
        if 'dist' in parts or 'src' in parts or parts[-1] in _BUILD_ARTIFACTS:
            return False
    return True


def _build_wheel_from_dir(pkg_dir: Path, top: str, dist_name: str, version: str,
                          metadata: str, extra_meta: dict, keep, dst: Path,
                          extra_files: dict | None = None) -> None:
    """Zip ``pkg_dir`` (installed package) into a wheel under arcname prefix ``top``.

    ``extra_files`` (arcname -> bytes) are synthetic entries added verbatim — used to
    ship directory markers for content that ``keep`` strips but that must still exist.
    """
    dist_info = f'{dist_name}-{version}.dist-info'
    entries: list[tuple[str, bytes]] = []
    for f in sorted(pkg_dir.rglob('*')):
        if not f.is_file() or '__pycache__' in f.parts:
            continue
        rel = f'{top}/{f.relative_to(pkg_dir).as_posix()}'
        if keep is not None and not keep(rel):
            continue
        entries.append((rel, f.read_bytes()))
    for arc, data in (extra_files or {}).items():
        entries.append((arc, data))
    entries.append((f'{dist_info}/METADATA', metadata.encode()))
    entries.append((f'{dist_info}/WHEEL',
                    b'Wheel-Version: 1.0\nGenerator: nicegui-pyodide-build\nRoot-Is-Purelib: true\nTag: py3-none-any\n'))
    for name, content in extra_meta.items():
        entries.append((f'{dist_info}/{name}',
                        content if isinstance(content, bytes) else content.encode()))
    # RECORD
    rec = io.StringIO()
    w = csv.writer(rec)
    for arc, data in entries:
        digest = urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
        w.writerow([arc, f'sha256={digest}', len(data)])
    w.writerow([f'{dist_info}/RECORD', '', ''])
    entries.append((f'{dist_info}/RECORD', rec.getvalue().encode()))

    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc, data in entries:
            z.writestr(arc, data)
    print(f'  {dst.name}  ({dst.stat().st_size // 1024} KB, {len(entries)} entries)')


def build_wheels(out: Path) -> tuple[str, str]:
    nicegui, pkg, metadata = _nicegui_meta()
    version = nicegui.__version__
    nicegui_wheel = f'nicegui-{version}-py3-none-any.whl'
    print('Building stripped nicegui wheel...')
    # ESM-package elements (leaflet/xterm/scene/echart/…) stat their ``dist/`` directory at
    # import time (dependencies.setup_esm_package -> dist.stat().st_mtime). _keep_nicegui strips
    # the (heavy) dist contents — served instead from ./esm/ — so ship an empty marker per dist/
    # dir to keep the directory present; without it a plain ``ui.leaflet()`` crashes the app.
    esm_keeps = {
        f'nicegui/{d.relative_to(pkg).as_posix()}/.nicegui-pyodide-keep': b''
        for d in pkg.glob('elements/*/dist') if d.is_dir()
    }
    _build_wheel_from_dir(pkg, 'nicegui', 'nicegui', version, metadata, {}, _keep_nicegui,
                          out / nicegui_wheel, extra_files=esm_keeps)

    print('Building nicegui_pyodide wheel...')
    self_pkg = PKG_DIR
    import nicegui_pyodide  # pylint: disable=import-outside-toplevel
    self_ver = nicegui_pyodide.__version__
    self_wheel = f'nicegui_pyodide-{self_ver}-py3-none-any.whl'
    self_meta = (f'Metadata-Version: 2.1\nName: nicegui-pyodide\nVersion: {self_ver}\n'
                 'Summary: Run NiceGUI in the browser via Pyodide.\n')
    _build_wheel_from_dir(self_pkg, 'nicegui_pyodide', 'nicegui_pyodide', self_ver, self_meta, {},
                          None, out / self_wheel)
    return nicegui_wheel, self_wheel


# -------------------------------------------------------------------------- templates

def copy_templates(out: Path, nicegui_wheel: str, self_wheel: str, *,
                   force_app: bool = False, self_hosted: bool = False) -> None:
    html = (TEMPLATES_DIR / 'index.html').read_text()
    install_args = DEFAULT_INSTALL_ARGS
    if self_hosted:
        subs = vendor_pkgs.vendor(out, html)
        cdn = f'<script type="module" src="https://pyscript.net/releases/{subs["pyscript_release"]}/core.js">'
        py_tag = '<script type="py" src="entrypoint.py" config="pyscript.toml">'
        if cdn not in html or py_tag not in html:
            sys.exit('Could not find the PyScript <script> tags to point at the vendored copies.')
        # PyScript honours the documented `interpreter` config key only when the config is
        # INLINE — pointing `config=` at a pyscript.toml that sets it is silently ignored
        # (measured against 2026.2.1). So the config moves into the tag, and no dead
        # pyscript.toml is written. The `offline` attribute is a belt-and-braces fallback:
        # PyScript consults it only if `interpreter` is absent, and it resolves to the same
        # ./pyscript/pyodide/pyodide.mjs path we already write.
        config = dict(PYSCRIPT_CONFIG, interpreter=subs['interpreter'])
        html = html.replace(cdn, f'<script type="module" offline src="{subs["core_js"]}">')
        html = html.replace(
            py_tag,
            '<script type="py" src="entrypoint.py" '
            f"config='{json_mod.dumps(config)}'>")
        install_args = subs['install_args']
    else:
        (out / 'pyscript.toml').write_text((TEMPLATES_DIR / 'pyscript.toml').read_text())
    (out / 'index.html').write_text(html)
    entry = (TEMPLATES_DIR / 'entrypoint.py').read_text()
    entry = (entry.replace('{{NICEGUI_WHEEL}}', nicegui_wheel)
                  .replace('{{PYODIDE_WHEEL}}', self_wheel)
                  .replace('{{PYPI_INSTALL_ARGS}}', install_args))
    (out / 'entrypoint.py').write_text(entry)
    print('Copied generated files (index.html, entrypoint.py'
          + (', config inlined — see index.html)' if self_hosted else ', pyscript.toml)'))

    # app.py is YOUR code — a rebuild refreshes the generated infra above, it must not
    # silently reset your app. Only write the starter template when absent (or --force).
    app_dst = out / 'app.py'
    if app_dst.exists() and not force_app:
        print('Kept existing app.py (use --force to overwrite it with the starter template)')
    else:
        shutil.copy2(TEMPLATES_DIR / 'app.py', app_dst)
        print('Wrote app.py (starter template — edit this to build your UI)')


# ------------------------------------------------------------------------------- main

def build(output_dir: str = 'pyodide-dist', *, force_app: bool = False,
          self_hosted: bool = False) -> Path:
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f'Building nicegui-pyodide demo into {out}\n')
    nicegui_wheel, self_wheel = build_wheels(out)     # first: real wheel names feed the entrypoint
    copy_templates(out, nicegui_wheel, self_wheel, force_app=force_app,
                   self_hosted=self_hosted)  # index.html before importmap injection
    prepare_static_files(out)
    prepare_components(out)                            # injects the import map into index.html
    prepare_dynamic_resources(out)
    print(f'\nReady. Serve with:\n  python -m http.server -d {out} 8080\n  (or re-run with --serve)')
    return out


def serve(directory: Path, port: int = 8080) -> None:
    """Serve ``directory`` over HTTP until interrupted (what ``--serve`` runs)."""
    import functools  # pylint: disable=import-outside-toplevel
    import http.server  # pylint: disable=import-outside-toplevel
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer(('', port), handler) as httpd:
        print(f'\nServing {directory} at http://localhost:{port}  (Ctrl-C to stop)')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')


def main() -> None:
    p = argparse.ArgumentParser(description='Assemble a Pyodide demo dir for stock NiceGUI.')
    p.add_argument('output_dir', nargs='?', default='pyodide-dist', help='output directory')
    p.add_argument('--force', action='store_true',
                   help='overwrite an existing app.py with the starter template')
    p.add_argument('--self-hosted', action='store_true',
                   help='vendor PyScript, Pyodide and the PyPI wheels into the output dir so the '
                        'demo loads with no network access (offline / air-gapped / CDN outage)')
    p.add_argument('--serve', action='store_true', help='serve the output dir over HTTP after building')
    p.add_argument('--port', type=int, default=8080, help='port for --serve (default: 8080)')
    args = p.parse_args()
    out = build(args.output_dir, force_app=args.force, self_hosted=args.self_hosted)
    if args.serve:
        serve(out, args.port)


if __name__ == '__main__':
    main()
