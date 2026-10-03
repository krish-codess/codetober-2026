"""A subject is a pipeline under test: its source schema, SQL models and properties."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tydlc.properties import Property
from tydlc.schema import Schema


@dataclass(frozen=True)
class Subject:
    name: str
    schema: Schema
    models: tuple[tuple[str, str], ...]  # (model name, rendered SQL), dependencies first
    properties: tuple[Property, ...]
    # engine -> {property name: why it is expected to fail}. The CI gate fails on a
    # falsification that is not listed here, and on a listed one that stops failing.
    known_bugs: dict[str, dict[str, str]]
    seed_dir: Path  # the real sample data shipped with the pipeline


def get_subject(name: str) -> Subject:
    if name == "jaffle":
        from tydlc.subjects.jaffle import SUBJECT

        return SUBJECT
    raise ValueError(f"unknown subject {name!r}")
