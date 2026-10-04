"""Declarative source schema. It drives generation, scratch DDL and boundary validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Dataset = dict[str, list[dict[str, Any]]]

SQL_TYPES = {"int": "INTEGER", "str": "VARCHAR", "date": "DATE"}


@dataclass(frozen=True)
class Column:
    name: str
    type: Literal["int", "str", "date"]
    nullable: bool = False
    unique: bool = False
    accepted: tuple[str, ...] | None = None
    references: tuple[str, str] | None = None  # (parent table, parent column)


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]


# Tables must be listed parents-first; generation and loading rely on that order.
Schema = tuple[Table, ...]


def total_rows(ds: Dataset) -> int:
    return sum(len(rows) for rows in ds.values())
