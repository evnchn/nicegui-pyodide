"""Pyodide runtime — the main entry point for running NiceGUI in the browser.

Typical use from a PyScript/Pyodide entrypoint::

    import nicegui_pyodide            # installs the sys.modules shims (must be first)
    from nicegui import ui
    from nicegui_pyodide import page, PyodideRuntime

    with Client(page('')) as client:
        ui.label('Hello from Pyodide!')
        ui.button('Click me!', on_click=lambda: ui.notify('Clicked!'))

    runtime = PyodideRuntime(client)
    await runtime.mount()
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import nicegui
from nicegui import core, json
from nicegui.dependencies import JsComponent

from .bridge import PyodideBridge
from .outbox import PyodideOutbox

if TYPE_CHECKING:
    from nicegui.client import Client

ELEMENTS_DIR = Path(nicegui.__file__).parent / 'elements'


class PyodideRuntime:
    """Runtime for running NiceGUI inside Pyodide/PyScript.

    Sets up the bridge (replacing socket.io), patches the client's outbox,
    and provides :meth:`mount` to render elements into the browser DOM.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

        # run config is normally set by ui.run / ui.run_with; in Pyodide we set defaults here.
        # Filter to the installed nicegui's actual add_run_config signature so minor-version
        # additions (e.g. `markdown` in 3.14, `cache_control_directives` with a default) don't
        # break us — any param we don't name keeps nicegui's own default.
        if not core.app.config.has_run_config:
            import inspect  # pylint: disable=import-outside-toplevel
            defaults = dict(
                reload=False,
                title='NiceGUI',
                viewport='width=device-width, initial-scale=1',
                favicon=None,
                dark=False,
                language='en-US',
                binding_refresh_interval=0.1,
                reconnect_timeout=3.0,
                message_history_length=1000,
                tailwind=True,
                unocss=None,
                prod_js=True,
                show_welcome_message=False,
                markdown=False,
            )
            params = inspect.signature(core.app.config.add_run_config).parameters
            core.app.config.add_run_config(**{k: v for k, v in defaults.items() if k in params})

        # replace socket.io with the Pyodide bridge for Python <-> JS communication
        self.bridge = PyodideBridge()
        core.sio = self.bridge

        core.loop = asyncio.get_event_loop()

        # replace the client's outbox with the microtask-flushing PyodideOutbox
        self.outbox = PyodideOutbox(client)
        client.outbox = self.outbox

        # mark the client as "connected" (no real socket, but ready)
        client.tab_id = 'pyodide'
        client._temporary_socket_id = 'pyodide'  # pylint: disable=protected-access

        # start the app to trigger startup handlers (e.g. timer background tasks)
        if not core.app.is_started:
            core.app.start()

    async def mount(self) -> None:
        """Serialize all elements and send them to the JavaScript frontend for rendering."""
        from js import window  # type: ignore  # pylint: disable=import-outside-toplevel,import-error
        from pyodide.ffi import create_proxy  # type: ignore  # pylint: disable=import-outside-toplevel,import-error

        elements = {
            str(id): element._to_dict()  # pylint: disable=protected-access
            for id, element in self.client.elements.items()
        }
        elements_json = json.dumps(elements)

        components = self._collect_components()
        components_json = json.dumps(components) if components else None

        # register Python callbacks on the JS bridge
        window.niceguiBridge.onEvent = create_proxy(self._handle_event)
        window.niceguiBridge.onJavascriptResponse = create_proxy(self._handle_javascript_response)
        window.niceguiBridge.onUpload = create_proxy(self._handle_upload)

        config = {
            'brand': core.app.config.quasar_config.get('brand', {}),
            'dark': self.client.page.resolve_dark(),
            'language': self.client.page.resolve_language(),
        }
        config_json = json.dumps(config)

        # createNiceGUIApp is async — it loads components before mounting Vue
        await window.createNiceGUIApp(elements_json, config_json, components_json)

        setattr(window, '__pyodide_ready', True)

    def _collect_components(self) -> list:
        """Collect Vue component URLs for elements that need custom JS components.

        Returns ``[{"url": "./components/...", "tag": "nicegui-..."}]`` matching
        the directory layout produced by the build step.
        """
        seen: set = set()
        components: list = []
        for element in self.client.elements.values():
            comp = element.component
            if not isinstance(comp, JsComponent) or comp.name in seen:
                continue
            seen.add(comp.name)
            try:
                rel = comp.path.relative_to(ELEMENTS_DIR)
            except ValueError:
                continue
            components.append({'url': f'./components/{rel.as_posix()}', 'tag': comp.tag})
        return components

    async def _handle_event(self, msg_json: str) -> None:
        """Handle an event from JavaScript (e.g. a button click)."""
        msg = json.loads(msg_json)
        self.client.handle_event(msg)
        await self.outbox.flush()

    def _handle_javascript_response(self, msg_json: str) -> None:
        """Handle a JavaScript response (e.g. the result of ``run_javascript``)."""
        msg = json.loads(msg_json)
        self.client.handle_javascript_response(msg)

    async def _handle_upload(self, msg_json: str) -> None:
        """Handle a file upload from JavaScript.

        The upload.js factory reads files client-side and sends base64 data.
        """
        import base64  # pylint: disable=import-outside-toplevel

        from nicegui.elements.upload import Upload  # pylint: disable=import-outside-toplevel
        from nicegui.elements.upload_files import FileUpload, SmallFileUpload  # pylint: disable=import-outside-toplevel
        from nicegui.events import UiEventArguments, handle_event  # pylint: disable=import-outside-toplevel

        msg = json.loads(msg_json)
        element_id = int(msg['id'])
        element = self.client.elements.get(element_id)
        if not isinstance(element, Upload):
            return

        # mirror the server-side upload dispatcher's begin-upload handlers
        for handler in element._begin_upload_handlers:  # pylint: disable=protected-access
            handle_event(handler, UiEventArguments(sender=element, client=self.client))

        files: list = []
        for file_data in msg['files']:
            content = base64.b64decode(file_data['data'])
            files.append(SmallFileUpload(name=file_data['name'], content_type=file_data.get('type', ''), _data=content))

        await element.handle_uploads(files)
        await self.outbox.flush()
