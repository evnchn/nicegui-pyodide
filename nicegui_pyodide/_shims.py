"""The weave: make stock NiceGUI importable and runnable under Pyodide from the OUTSIDE.

Stock NiceGUI imports server-only packages *unconditionally* at module top
(``from fastapi import Request``, ``from socketio import AsyncServer``,
``import uvicorn``/``aiofiles``/``anyio``/``ifaddr`` …).  Under Pyodide those
packages do not exist, so a plain ``import nicegui`` explodes before any of our
code runs.  The upstream ``#5776`` attempt solved this by editing ~25 core files
to wrap every such import in ``try/except ImportError`` and add ``if IS_PYODIDE``
branches.  As an *external extension* we cannot touch core, so instead we:

1. Install a ``sys.meta_path`` finder that fabricates a **permissive stub module**
   for any import under a set of stubbed root packages.  The stubs are rich
   enough that stock NiceGUI's *unguarded* usages don't crash — every attribute
   is a subclassable, callable, decorator-friendly placeholder.  This lets
   ``class App(FastAPI)``, ``@app.post(url)``, ``class APIRouter(fastapi.APIRouter)``,
   ``class CacheControlledStaticFiles(StaticFiles)`` and ``Jinja2Templates(path)``
   all succeed against nothing.

2. Pre-seed ``sys.modules['nicegui.nicegui']`` with a lazy shim that supplies the
   ``app`` singleton (``App()`` + ``core.app = app``) *without* running stock
   ``nicegui/nicegui.py``, which would build the FastAPI app + socket.io server +
   HTTP routes.  Stock ``nicegui/__init__.py`` does ``from .nicegui import app``;
   the import machinery finds our seed first and never executes the real module.

:func:`install` MUST run before ``import nicegui``.  ``nicegui_pyodide.__init__``
calls it, and defers importing anything that touches nicegui until after.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from typing import Optional, Sequence

# Root packages that do not exist under Pyodide and must be faked wholesale.
# Any submodule (e.g. ``fastapi.responses``, ``starlette.middleware.sessions``)
# is handled by the finder too — no need to enumerate them.
_STUB_ROOTS = {
    'socketio',
    'engineio',
    'fastapi',
    'starlette',
    'uvicorn',
    'aiofiles',
    'anyio',
    'ifaddr',
    'webview',       # pywebview (native mode)
    'httptools',
    'websockets',
    'multipart',
    'python_multipart',
}

# Exact stdlib(-ish) submodules to stub even though their parent package stays real.
# ``multiprocessing.synchronize`` pulls the C-extension ``_multiprocessing`` (semaphores),
# which Pyodide removed; nicegui imports it eagerly in server.py and native_mode.py.
# The parent ``multiprocessing`` package and ``multiprocessing.connection`` import fine
# and must stay real, so we only intercept this one leaf.
_STUB_MODULES = {
    'multiprocessing.synchronize',
}

_installed = False


# Binary operators that must not explode when stock code combines stub values
# (e.g. ``FixPoint.NORTH | FixPoint.WEST`` in nicegui.native).  Each returns a stub.
_BINOPS = (
    '__or__', '__and__', '__xor__', '__add__', '__sub__', '__mul__', '__mod__',
    '__ror__', '__rand__', '__rxor__', '__radd__', '__rsub__', '__rmul__',
    '__lshift__', '__rshift__', '__truediv__', '__floordiv__',
)


class _StubMeta(type):
    """Metaclass making *class-level* attribute access and operators permissive too.

    ``FixPoint.NORTH`` is a class-attribute lookup, which an instance ``__getattr__``
    never sees — so we handle it here.
    """

    def __getattr__(cls, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return _Stub()

    def _binop(cls, other):  # noqa: N805
        return _Stub()


for _op in _BINOPS:
    setattr(_StubMeta, _op, _StubMeta._binop)


class _Stub(metaclass=_StubMeta):
    """A maximally-permissive placeholder.

    * Subclassable (``class App(_Stub)``) and its metaclass keeps class-attr access permissive.
    * Instantiable with any args; any missing attribute yields another permissive stub.
    * Callable, and a call returns a passthrough decorator so ``@app.post(url)`` no-ops.
    * Supports common operators / iteration / bool so stock code combining stubs won't crash.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __call__(self, *args, **kwargs):
        def _decorator(func=None):
            return func
        return _decorator

    def __getattr__(self, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return _Stub()

    def __iter__(self):
        return iter(())

    def __bool__(self) -> bool:
        return False

    def __int__(self) -> int:
        return 0

    def _binop(self, other):
        return _Stub()


for _op in _BINOPS:
    setattr(_Stub, _op, _Stub._binop)


class _StubModule(types.ModuleType):
    """A stub module whose every attribute is a fresh subclassable ``_Stub`` subclass.

    Returning a *class* (not an instance) matters: stock NiceGUI subclasses several
    of these names (``FastAPI``, ``fastapi.APIRouter``, ``StaticFiles``,
    ``uvicorn.Config``/``Server``).  A class is also happily instantiated
    (``Jinja2Templates(path)``) and used in ``isinstance``/annotations.
    """

    def __getattr__(self, name: str):
        if name in ('__path__', '__all__'):
            raise AttributeError(name)
        # Fabricate a uniquely-named subclass of _Stub so subclassing/repr are sane.
        # Use _StubMeta explicitly — plain type() would raise a metaclass conflict.
        cls = _StubMeta(name, (_Stub,), {'__module__': self.__name__})
        setattr(self, name, cls)
        return cls


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Fabricates stub modules ONLY for the roots/exact-modules found genuinely absent
    at :func:`install` time.

    Critically it never shadows a real, importable package: recent Pyodide *does* ship
    some of these (e.g. ``anyio``/``fastapi``/``starlette``), and blindly stubbing them
    would poison unrelated code in the same interpreter.  The absent-set is decided by
    deferring to real import resolution first (see :func:`_real_importable`).
    """

    def __init__(self, roots: set, modules: set) -> None:
        self._roots = roots
        self._modules = modules

    def _is_stubbed(self, fullname: str) -> bool:
        if fullname in self._modules:
            return True
        return fullname.split('.', 1)[0] in self._roots

    def find_spec(self, fullname: str, path: Optional[Sequence[str]] = None,
                  target: Optional[types.ModuleType] = None) -> Optional[importlib.machinery.ModuleSpec]:
        if not self._is_stubbed(fullname):
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        module = _StubModule(spec.name)
        # Mark as a package so ``from x.y import z`` submodule imports resolve back
        # through this finder instead of failing with "not a package".
        module.__path__ = []  # type: ignore[attr-defined]
        return module

    def exec_module(self, module: types.ModuleType) -> None:
        pass


def _make_nicegui_nicegui_shim() -> types.ModuleType:
    """Build the lazy ``nicegui.nicegui`` replacement that supplies ``app``.

    Accessing ``app`` constructs ``App()`` once and assigns ``core.app`` — the
    single essential side effect of stock ``nicegui/nicegui.py`` — while skipping
    the FastAPI app, socket.io server and route registration it otherwise builds.
    """
    mod = types.ModuleType('nicegui.nicegui')
    mod.__doc__ = 'Pyodide shim for nicegui.nicegui (supplies `app` without the server bootstrap).'

    def __getattr__(name: str):  # PEP 562 module-level __getattr__
        if name == 'app':
            from nicegui import core  # pylint: disable=import-outside-toplevel
            existing = getattr(core, 'app', None)
            if existing is None:
                from nicegui.app.app import App  # pylint: disable=import-outside-toplevel
                core.app = App()
            return core.app
        raise AttributeError(f'module nicegui.nicegui (pyodide shim) has no attribute {name!r}')

    mod.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return mod


def _real_importable(name: str) -> bool:
    """True if ``name`` can genuinely be imported in this interpreter.

    Runs BEFORE the stub finder is installed, so import attempts hit the real
    finders.  This is the defer-to-real gate: anything importable is left alone.
    """
    if name in sys.modules:
        return True
    try:
        importlib.import_module(name)
        return True
    except Exception:  # pylint: disable=broad-except  # ImportError and any exec-time error mean "not usable"
        return False


def install(*, force: bool = False) -> None:
    """Install the import shims.  Idempotent.  Must be called before ``import nicegui``.

    Stubs are a *fallback*, never an override: only modules found genuinely absent
    (via :func:`_real_importable`) are faked, so a real ``anyio``/``fastapi``/… that
    Pyodide ships stays real for the rest of the interpreter.

    :param force: TEST-ONLY (``NICEGUI_PYODIDE_FORCE=1``).  Simulate Pyodide on a
        desktop by purging the real server deps from ``sys.modules`` and stubbing the
        full set regardless of availability.  Do NOT use in a real server process —
        it will orphan already-imported real modules.
    """
    global _installed
    if _installed:
        return

    if 'nicegui' in sys.modules:
        raise RuntimeError(
            'nicegui was imported before nicegui_pyodide.install(); '
            'import nicegui_pyodide (or call install()) BEFORE importing nicegui.')

    if force:
        import warnings  # pylint: disable=import-outside-toplevel
        warnings.warn(
            'nicegui_pyodide force mode purges real server deps from sys.modules and stubs them '
            'unconditionally; this is test-only and unsafe in a real server process.',
            RuntimeWarning, stacklevel=2)
        roots, modules = set(_STUB_ROOTS), set(_STUB_MODULES)
        for name in list(sys.modules):
            if name.split('.', 1)[0] in _STUB_ROOTS or name in _STUB_MODULES:
                del sys.modules[name]
    else:
        # defer-to-real: stub ONLY what is genuinely absent, so we never shadow a
        # real package this interpreter (or the user's own code) relies on.
        roots = {r for r in _STUB_ROOTS if not _real_importable(r)}
        modules = {m for m in _STUB_MODULES if not _real_importable(m)}

    # 1. stub finder for the genuinely-absent server deps
    if (roots or modules) and not any(isinstance(f, _StubFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _StubFinder(roots, modules))

    # 2. lazy nicegui.nicegui seed (skips the FastAPI/socket.io bootstrap)
    sys.modules.setdefault('nicegui.nicegui', _make_nicegui_nicegui_shim())

    _installed = True
