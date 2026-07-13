#!/usr/bin/env python3
"""Re-apply the pyodide patch to a new NiceGUI release's browser JS.

nicegui-pyodide ships four hand-patched copies of stock NiceGUI's frontend:

    nicegui_pyodide/static/nicegui.js                             <- patch of nicegui/static/nicegui.js
    nicegui_pyodide/static/component_overrides/markdown.js        <- patch of nicegui/elements/markdown.js
    nicegui_pyodide/static/component_overrides/leaflet/leaflet.js <- patch of nicegui/elements/leaflet/leaflet.js
    nicegui_pyodide/static/component_overrides/xterm/xterm.js     <- patch of nicegui/elements/xterm/xterm.js

The *patch itself* lives as a unified diff under ``scripts/patches/`` (not a
re-vendored blob), so bumping to a new NiceGUI release is mechanical:

    python scripts/regenerate_patched_js.py                       # patch the installed nicegui
    python scripts/regenerate_patched_js.py --nicegui-dir PATH    # ...or a specific nicegui/ dir
    python scripts/regenerate_patched_js.py --in-place            # overwrite the vendored copies

Each stored diff is applied to that release's original file with ``patch(1)``,
which tolerates line-number drift but *rejects* any hunk whose surrounding code
changed shape. A rejected hunk means upstream rewrote code the patch touches:
see CONTRIBUTING.md, "Supporting a new NiceGUI release".
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
PATCHES_DIR = SCRIPTS_DIR / 'patches'
VENDOR_ROOT = REPO_ROOT / 'nicegui_pyodide'

# name -> (source path within the nicegui package, vendored destination within nicegui_pyodide/)
PATCHES = [
    ('nicegui.js', ('static', 'nicegui.js'), ('static', 'nicegui.js')),
    ('markdown.js', ('elements', 'markdown.js'), ('static', 'component_overrides', 'markdown.js')),
    ('leaflet.js', ('elements', 'leaflet', 'leaflet.js'), ('static', 'component_overrides', 'leaflet', 'leaflet.js')),
    ('xterm.js', ('elements', 'xterm', 'xterm.js'), ('static', 'component_overrides', 'xterm', 'xterm.js')),
]


def _version_from_dist(pkg_dir: Path) -> str:
    for info in sorted(pkg_dir.parent.glob('nicegui-*.dist-info')):
        meta = info / 'METADATA'
        if meta.exists():
            m = re.search(r'^Version:\s*(.+)$', meta.read_text(encoding='utf-8'), re.M)
            if m:
                return m.group(1).strip()
    return '(unknown)'


def _nicegui_dir(arg: str | None) -> tuple[Path, str]:
    if arg:
        d = Path(arg).resolve()
        if not (d / 'static' / 'nicegui.js').exists():
            sys.exit(f'{d} does not look like a nicegui package (no static/nicegui.js).')
        return d, _version_from_dist(d)
    try:
        import nicegui  # pylint: disable=import-outside-toplevel
        return Path(nicegui.__file__).parent, nicegui.__version__
    except ImportError:
        sys.exit('nicegui is not importable; pass --nicegui-dir instead.')


def _apply(name: str, src: Path, diff: Path, work: Path) -> tuple[bool, str]:
    """Copy src->work, apply diff in place. Return (ok, patch_output)."""
    shutil.copy2(src, work)
    rej = work.with_suffix(work.suffix + '.rej')
    proc = subprocess.run(
        ['patch', '--no-backup-if-mismatch', '-r', str(rej), str(work)],
        stdin=diff.open('rb'), capture_output=True, text=True, check=False,
    )
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--nicegui-dir', help='path to the target release\'s nicegui/ package (default: installed)')
    p.add_argument('--out', help='directory to write regenerated files to (default: a temp dir)')
    p.add_argument('--in-place', action='store_true',
                   help='overwrite the vendored copies in nicegui_pyodide/ (only if every hunk applies)')
    args = p.parse_args()

    if not shutil.which('patch'):
        sys.exit('the `patch` utility is required but was not found on PATH.')

    ng_dir, ng_ver = _nicegui_dir(args.nicegui_dir)
    out_dir = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix='nicegui-pyodide-js-'))
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f'Target NiceGUI: {ng_ver}  ({ng_dir})')
    print(f'Output:         {out_dir}\n')

    failures, drifted, staged = [], [], []
    for name, src_parts, dst_parts in PATCHES:
        src = ng_dir.joinpath(*src_parts)
        diff = PATCHES_DIR / f'{name}.patch'
        if not src.exists():
            print(f'  {name}: MISSING upstream source {src} -> upstream moved/removed it (shape change)')
            failures.append(name)
            continue
        if not diff.exists():
            sys.exit(f'stored patch {diff} not found.')
        work = out_dir / name
        ok, log = _apply(name, src, diff, work)
        offset = any(w in log for w in ('offset', 'fuzz'))
        if ok:
            note = '  (applied with line drift — eyeball the result)' if offset else ''
            print(f'  {name}: OK{note}')
            if offset:
                drifted.append(name)
                print('\n'.join('      ' + ln for ln in log.splitlines()))
            staged.append((name, work, VENDOR_ROOT.joinpath(*dst_parts)))
        else:
            print(f'  {name}: FAILED — upstream changed shape near a patched region')
            print('\n'.join('      ' + ln for ln in log.splitlines()))
            failures.append(name)

    print()
    if failures:
        print(f'{len(failures)} file(s) need manual re-patching: {", ".join(failures)}')
        print('See CONTRIBUTING.md -> "Supporting a new NiceGUI release" (inspect the .rej files above).')
        return 1

    if args.in_place:
        for name, work, dst in staged:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work, dst)
            print(f'  wrote {dst.relative_to(REPO_ROOT)}')
        print('\nVendored copies updated. Now:')
    else:
        print('All hunks applied. Regenerated files are in the output dir above. Now:')
    print(f'  1. diff each against the current vendored copy in nicegui_pyodide/static/')
    print(f'  2. bump TESTED_NICEGUI in nicegui_pyodide/build/cli.py to {ng_ver}')
    print(f'  3. widen the pin in pyproject.toml if the range moved')
    print(f'  4. rebuild + verify in a real browser (see CONTRIBUTING.md)')
    if drifted:
        print(f'  NOTE: {", ".join(drifted)} applied with line drift — verify extra carefully.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
