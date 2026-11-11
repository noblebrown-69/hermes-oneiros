"""YAML reads through Hermes's parser.

Hermes ships ``hermes_yaml`` (ruamel, YAML 1.1, safe loader) and does not
ship PyYAML. Importing ``yaml`` here raised ImportError on the first tool
call after a user copied in ``config.yaml``.
"""
from __future__ import annotations

from typing import Any


class YamlReadError(ValueError):
    """The host parser rejected the document. ``str(self)`` is the parser class name."""


def safe_load(text: str) -> Any:
    from hermes_yaml import YAMLError, safe_load as _load

    try:
        return _load(text)
    except YAMLError as exc:
        raise YamlReadError(exc.__class__.__name__) from None
