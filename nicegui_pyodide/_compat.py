"""Pyodide environment detection.

``IS_PYODIDE`` is true when running inside a Pyodide / PyScript (WebAssembly /
Emscripten) interpreter.  Set ``NICEGUI_PYODIDE_FORCE=1`` to force the pyodide
code path on a normal desktop CPython — used to shake out the sys.modules shim
logic without a browser (a *pre-check*; the real verification is in-browser).
"""
from __future__ import annotations

import os
import sys


def is_pyodide() -> bool:
    """Return True if running in a Pyodide (WebAssembly/Emscripten) environment.

    Uses only the concrete Emscripten signals (plus the explicit force override).
    We deliberately do NOT treat ``'pyodide' in sys.modules`` as sufficient: a normal
    CPython process that merely imports something named ``pyodide`` must not trip the
    shim-install path.
    """
    if os.environ.get('NICEGUI_PYODIDE_FORCE') == '1':
        return True
    return sys.platform == 'emscripten' or hasattr(sys, '_emscripten_info')


IS_PYODIDE: bool = is_pyodide()
