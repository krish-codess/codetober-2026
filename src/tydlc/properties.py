"""A property is a predicate over (raw input, pipeline output), never a fixed fixture."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tydlc.schema import Dataset


@dataclass(frozen=True)
class Ctx:
    raw: Dataset
    out: Dataset
    run: Callable[[Dataset], Dataset]  # re-run the pipeline, for metamorphic properties


# True = held, False = violated, None = vacuous (nothing in this example to judge).
Check = Callable[[Ctx], bool | None]


@dataclass(frozen=True)
class Property:
    name: str
    description: str
    check: Check
    source: str = "declared"  # or "discovered"


def canon(rows: list[dict[str, Any]], digits: int | None = None) -> list[str]:
    """Order-independent form of a result set, for multiset comparison. With `digits`,
    floats are compared to that many decimals instead of bit for bit."""
    def value(v: Any) -> Any:
        return f"{v:.{digits}f}" if digits is not None and isinstance(v, float) else v

    return sorted(repr(sorted((k, value(v)) for k, v in r.items())) for r in rows)


def same_output(a: Dataset, b: Dataset, digits: int | None = None) -> bool:
    return all(canon(a[k], digits) == canon(b[k], digits) for k in a)


def close(a: float, b: float) -> bool:
    """Equal to within half a cent: money columns are DOUBLE on DuckDB."""
    return abs(a - b) < 0.005
