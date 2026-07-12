"""Contract tests pinning the stock-NiceGUI internals that :class:`PyodideOutbox` couples to.

``PyodideOutbox`` reimplements ``__init__``/``flush`` but **inherits stock ``Outbox._emit``**.
That inherited method reaches into a pile of stock internals (instance attributes it expects
``__init__`` to have set, plus ``core.*`` / ``client.page.*`` surfaces).  If any of those drift
in a future NiceGUI release, the breakage would be *silent* — ``flush`` still calls ``_emit``,
which then blows up (or misbehaves) at runtime inside the browser.

These tests turn that silent drift into a red test against the *installed* stock nicegui.
They run in normal (server) mode on desktop — no Pyodide shims installed.
"""
from __future__ import annotations

import inspect
import re

import nicegui  # noqa: F401  # ensures core.sio / core.app are bootstrapped
from nicegui import core
from nicegui.air import Air
from nicegui.app.app_config import AppConfig
from nicegui.client import Client
from nicegui.outbox import Outbox
from nicegui.page import page

from nicegui_pyodide.outbox import PyodideOutbox


def _self_assignments(func) -> set:
    """Names assigned via ``self.<name> = ...`` (with optional type annotation) in the source."""
    return set(re.findall(r'self\.(\w+)\s*(?::[^=]+)?=', inspect.getsource(func)))


def _self_reads(func) -> set:
    """Names read via ``self.<name>`` in the given function's source."""
    return set(re.findall(r'self\.(\w+)', inspect.getsource(func)))


def test_pyodide_outbox_inherits_stock_emit() -> None:
    # The whole contract exists because _emit is NOT overridden. If it ever is, revisit these tests.
    assert PyodideOutbox._emit is Outbox._emit
    assert inspect.iscoroutinefunction(Outbox._emit)


def test_emit_instance_attributes_are_initialized_by_pyodide_outbox() -> None:
    # Every instance attribute that inherited _emit reads AND that stock __init__ sets up
    # must also be set up by PyodideOutbox.__init__ (which does not call super().__init__).
    stock_init_attrs = _self_assignments(Outbox.__init__)
    emit_reads = _self_reads(Outbox._emit)
    needed = stock_init_attrs & emit_reads
    assert needed, 'expected _emit to read at least one __init__-provided attribute'
    missing = needed - _self_assignments(PyodideOutbox.__init__)
    assert not missing, f'PyodideOutbox.__init__ must initialize inherited-_emit attrs: {sorted(missing)}'


def test_emit_core_surface_exists() -> None:
    # core.sio.eio.ping_interval / ping_timeout (used to compute message_history max_age)
    assert isinstance(core.sio.eio.ping_interval, (int, float))
    assert isinstance(core.sio.eio.ping_timeout, (int, float))
    assert callable(core.sio.emit)

    # core.air may be None, but the attribute and the two methods _emit calls must exist.
    assert hasattr(core, 'air')
    assert callable(Air.is_air_target)
    assert inspect.iscoroutinefunction(Air.emit)

    # core.app.config.message_history_length (the history-trim bound).
    assert isinstance(core.app.config, AppConfig)
    assert 'message_history_length' in AppConfig.__dataclass_fields__


def test_client_page_resolve_surface() -> None:
    # _emit calls client.page.resolve_reconnect_timeout().
    assert 'page' in inspect.signature(Client.__init__).parameters
    assert callable(page.resolve_reconnect_timeout)

    # The broader resolve_* surface Client depends on must all remain on `page`.
    client_src = inspect.getsource(inspect.getmodule(Client))
    used = set(re.findall(r'self\.page\.(resolve_\w+)', client_src))
    assert 'resolve_reconnect_timeout' in used
    missing = {name for name in used if not callable(getattr(page, name, None))}
    assert not missing, f'stock `page` is missing resolve_* methods Client uses: {sorted(missing)}'


if __name__ == '__main__':
    for _name, _fn in sorted(globals().items()):
        if _name.startswith('test_') and callable(_fn):
            _fn()
            print(f'ok  {_name}')
    print('all contract tests passed')
