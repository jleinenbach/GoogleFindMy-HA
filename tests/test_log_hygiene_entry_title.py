# tests/test_log_hygiene_entry_title.py
"""No logger call in the integration passes the title of a config entry.

The config flow sets the entry title to the account e-mail
(``config_flow.py``), and AGENTS.md section 5 forbids e-mail addresses in logs.
Name an entry by its ID or by ``_label_entry_for_log(entry)`` instead.

The check walks every module of ``custom_components/googlefindmy`` except
generated ``*_pb2.py`` files and ``vendor/``. It uses ``entry_title_offenders``
from ``tests/test_log_hygiene_init.py``. A title is read as ``<entry>.title`` or
``getattr(<entry>, "title", ...)``, where ``<entry>`` is a name or attribute
ending in ``entry`` after the start, a dot or an underscore (``entry``,
``config_entry``, ``self._entry``, ``existing_entry``; not ``subentry``). It is
followed into locals bound in the same scope by assignments, ``for`` and
``with ... as`` targets. A logger call is
``<logger>.<method>(...)`` or a call through a name assigned from one in the
same scope.

Not covered: a title passed through another function, a container, an object
attribute (``self._t = entry.title``) or a call result
(``async_get_entry(entry_id).title``); an entry under a name that does not end
in ``entry``; ``_LOGGER.log(level, ...)`` calls and logger aliases bound in
another scope. A name once bound to a title stays bound for its whole scope.
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
    "prefixed_name": "def f(existing_entry):\n    _LOGGER.info('%s', existing_entry.title)\n",
    "getattr": "def f(entry):\n    _LOGGER.info('%s', getattr(entry, 'title', None))\n",
    "alias": (
        "def f(entry, quiet):\n"
        "    log_fn = _LOGGER.debug if quiet else _LOGGER.warning\n"
        "    log_fn('%s', entry.title)\n"
    ),
    "for_target": (
        "def f(entry):\n    for t in (entry.title,):\n        _LOGGER.info('%s', t)\n"
    ),
    "with_target": (
        "def f(entry, ctx):\n"
        "    with ctx(entry.title) as t:\n"
        "        _LOGGER.info('%s', t)\n"
    ),
    "comprehension": (
        "def f(entries):\n    _LOGGER.info('%s', [entry.title for entry in entries])\n"
    ),
    "module_level": "_LOGGER.info('%s', entry.title)\n",
    "class_body": "class C:\n    _LOGGER.info('%s', config_entry.title)\n",
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
    "subentry": "def f(subentry):\n    _LOGGER.info('%s', subentry.title)\n",
    "other_alias": ("def f(entry):\n    show = print\n    show(entry.title)\n"),
    "alias_in_other_scope": (
        "log_fn = _LOGGER.debug\ndef f(entry):\n    log_fn('%s', entry.entry_id)\n"
    ),
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
