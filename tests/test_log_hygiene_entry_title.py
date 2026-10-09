# tests/test_log_hygiene_entry_title.py
"""No logger call in the integration passes the title of a config entry.

The config flow sets the entry title to the account e-mail
(``config_flow.py``), and AGENTS.md section 5 forbids e-mail addresses in logs.
Name an entry by its ID or by ``_label_entry_for_log(entry)`` instead.

The check walks every module of ``custom_components/googlefindmy`` except
generated ``*_pb2.py`` files and ``vendor/``. It uses ``entry_title_offenders``
from ``tests/test_log_hygiene_init.py``: attribute reads named ``title`` on an
object whose name ends in ``entry`` (``entry``, ``config_entry``,
``self._entry``) and locals assigned from such a read in the same function.

Not covered: a title passed through another function, a container or an object
whose name does not end in ``entry``; ``_LOGGER.log(level, ...)`` calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

import custom_components.googlefindmy as integration_init
from tests.test_log_hygiene_init import entry_title_offenders

_PACKAGE_ROOT = Path(integration_init.__file__).parent


def _package_modules() -> list[Path]:
    return [
        path
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"))
        if not path.name.endswith("_pb2.py") and "vendor" not in path.parts
    ]


def test_no_logger_call_in_the_package_passes_a_config_entry_title() -> None:
    modules = _package_modules()
    # Vacuum guard: the package has well over 100 modules; fewer means the scan
    # did not walk the package (wrong root, renamed package).
    assert len(modules) > 100, f"only {len(modules)} modules scanned"
    offenders = [
        f"{path.relative_to(_PACKAGE_ROOT).as_posix()}:{line} ({function})"
        for path in modules
        for function, line in entry_title_offenders(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    ]
    assert offenders == [], f"logger call passes a config entry title: {offenders}"


_FLAGGED = {
    "direct": "def f(entry):\n    _LOGGER.info('%s', entry.title)\n",
    "config_entry": "def f(config_entry):\n    LOGGER.debug('%s', config_entry.title)\n",
    "self_entry": "def f(self):\n    self._logger.warning('%s', self._entry.title)\n",
    "local": (
        "def f(entry):\n"
        "    name = entry.title or entry.entry_id\n"
        "    _LOGGER.info('%s', name)\n"
    ),
    "chained_local": (
        "def f(entry):\n"
        "    a = entry.title\n"
        "    b = a.strip()\n"
        "    _LOGGER.info('%s', b)\n"
    ),
    "tuple_target": (
        "def f(entry):\n    name, _n = entry.title, 1\n    _LOGGER.info('%s', name)\n"
    ),
    "keyword": "def f(entry):\n    _LOGGER.info('x', extra={'t': entry.title})\n",
}

_NOT_FLAGGED = {
    "entry_id": "def f(entry):\n    _LOGGER.info('%s', entry.entry_id)\n",
    # Storing into the entry does not make the entry itself a title.
    "title_store": (
        "def f(entry, new):\n"
        "    old = entry.title\n"
        "    entry.title = old or new\n"
        "    _LOGGER.info('%s', entry.entry_id)\n"
    ),
    "subscript_store": (
        "def f(entry, kwargs):\n"
        "    kwargs['title'] = entry.title\n"
        "    _LOGGER.info('%s', entry.entry_id)\n"
    ),
    "flow_title": "def f(self):\n    _LOGGER.info('%s', self.title)\n",
}


def test_entry_title_offenders_flags_each_form() -> None:
    missed = [
        name
        for name, source in _FLAGGED.items()
        if not entry_title_offenders(ast.parse(source))
    ]
    assert missed == [], f"forms not flagged: {missed}"


def test_entry_title_offenders_ignores_stores_into_the_entry() -> None:
    flagged = [
        name
        for name, source in _NOT_FLAGGED.items()
        if entry_title_offenders(ast.parse(source))
    ]
    assert flagged == [], f"forms wrongly flagged: {flagged}"
