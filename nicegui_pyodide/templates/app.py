"""Your NiceGUI app — runs entirely in the browser (Pyodide), no server.

Build the UI inside a ``Client`` context and expose it as ``client``; the
entrypoint mounts it.  ``import nicegui_pyodide`` MUST come before ``import nicegui``
so the import shims are installed first.
"""
import nicegui_pyodide  # noqa: F401  # installs the sys.modules shims (must precede nicegui)
from nicegui import Client, ui
from nicegui_pyodide import page

with Client(page('/')) as client:
    ui.label('Hello from NiceGUI — running in your browser via Pyodide!').classes('text-2xl font-bold')

    count = ui.label('Clicks: 0')
    _state = {'n': 0}

    def _bump() -> None:
        _state['n'] += 1
        count.text = f'Clicks: {_state["n"]}'
        ui.notify(f'Clicked {_state["n"]} time(s)')

    ui.button('Click me', on_click=_bump)

    with ui.row():
        name = ui.input('Your name', value='world')
        ui.button('Greet', on_click=lambda: ui.notify(f'Hello, {name.value}!'))

    ui.markdown('This is **NiceGUI** via _Pyodide_ — the Python ran in your browser, '
                'with **no backend server**.')
