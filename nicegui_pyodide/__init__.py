"""nicegui-pyodide — run stock NiceGUI entirely in the browser via Pyodide/PyScript.

An *external extension*: no fork of NiceGUI, no patches to its source.  Importing
this package (in a Pyodide environment) installs ``sys.modules`` shims so that a
plain ``import nicegui`` works without a server, then hands you a runtime that
renders your UI into the browser DOM over an in-process JS bridge.

Browser entrypoint pattern::

    import nicegui_pyodide                 # MUST come before `import nicegui`
    from nicegui import Client, ui
    from nicegui_pyodide import page, PyodideRuntime

    with Client(page('/')) as client:
        ui.label('Hello from Pyodide!')

    runtime = PyodideRuntime(client)
    await runtime.mount()

On a normal (server) Python this package is inert — ``import nicegui`` behaves as
usual — so shared code can import it unconditionally.
"""
from __future__ import annotations

import os

from ._compat import IS_PYODIDE

# Install the import weave BEFORE anything imports nicegui.  Everything that
# touches nicegui (runtime, page) is imported lazily via __getattr__ below so it
# can never run ahead of install().
if IS_PYODIDE:
    from . import _shims
    _shims.install(force=os.environ.get('NICEGUI_PYODIDE_FORCE') == '1')

__version__ = '0.1.0'
__all__ = ['IS_PYODIDE', 'PyodideRuntime', 'page', 'install']


def install(*, force: bool = False) -> None:
    """Explicitly (re-)install the shims.  Idempotent; normally automatic on import."""
    from . import _shims
    _shims.install(force=force)


def __getattr__(name: str):  # PEP 562 lazy exports — keep nicegui imports after install()
    if name == 'PyodideRuntime':
        from .runtime import PyodideRuntime
        return PyodideRuntime
    if name == 'page':
        from ._page import page
        return page
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
