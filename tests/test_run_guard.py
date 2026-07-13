"""Ease-of-use guardrails: the wrong-idiom errors and the app.py no-clobber rule.

The Pyodide import weave (and its ``ui.run`` guard) only installs under Pyodide or force
mode, which purges the real server deps from ``sys.modules`` — so those checks run in a
*subprocess* to avoid poisoning the rest of the suite. The no-clobber check is pure
filesystem and runs in-process.
"""
from __future__ import annotations

import os
import subprocess
import sys

from nicegui_pyodide.build.cli import copy_templates


def _run_forced(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-c', body],
        env={**os.environ, 'NICEGUI_PYODIDE_FORCE': '1'},
        capture_output=True, text=True,
        timeout=60,  # a finder recursion/import-lock regression must fail loudly, not hang CI
    )


def test_ui_run_raises_friendly_error() -> None:
    for fn in ('run', 'run_with'):
        arg = '' if fn == 'run' else 'None'
        r = _run_forced(
            'import nicegui_pyodide\n'
            'from nicegui import ui\n'
            'try:\n'
            f'    ui.{fn}({arg})\n'
            '    print("NO_ERROR")\n'
            'except RuntimeError as e:\n'
            '    print("OK" if "web server" in str(e) else "WRONG_MSG")\n'
        )
        assert 'OK' in r.stdout, f'ui.{fn}: {r.stdout!r} {r.stderr[-300:]!r}'


def test_import_order_guard() -> None:
    r = _run_forced('import nicegui\nimport nicegui_pyodide\n')
    assert r.returncode != 0
    assert 'before' in (r.stderr + r.stdout)


def test_build_keeps_edited_app_py(tmp_path) -> None:
    # copy_templates writes the starter app.py only when absent (or force_app=True).
    copy_templates(tmp_path, 'nicegui-x.whl', 'nicegui_pyodide-x.whl')
    (tmp_path / 'app.py').write_text('# MY EDIT\n')
    copy_templates(tmp_path, 'nicegui-x.whl', 'nicegui_pyodide-x.whl')
    assert (tmp_path / 'app.py').read_text() == '# MY EDIT\n'  # kept
    copy_templates(tmp_path, 'nicegui-x.whl', 'nicegui_pyodide-x.whl', force_app=True)
    assert '# MY EDIT' not in (tmp_path / 'app.py').read_text()  # overwritten
