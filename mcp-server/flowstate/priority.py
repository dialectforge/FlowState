"""Todo priority levels (v2 Phase D, E1): critical / urgent / normal / low.

The database keeps the older names (its CHECK constraint allows low/medium/high/critical and
the v1 server reads them): urgent is stored as high, normal as medium. Every input accepts
both names; everything FlowState shows uses the new ones.
"""

from typing import Optional

LEVELS = ("critical", "urgent", "normal", "low")           # most urgent first
_STORE = {"critical": "critical", "urgent": "high", "normal": "medium", "low": "low",
          "high": "high", "medium": "medium"}
_LABEL = {"critical": "critical", "high": "urgent", "medium": "normal", "low": "low"}
RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}   # by stored value
ACCEPTED = ("critical", "urgent", "high", "normal", "medium", "low")
DEFAULT = "medium"

# SQL ordering by stored value (most urgent first); `col` is the column expression.
def order_sql(col: str = "priority") -> str:
    return (f"CASE {col} WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
            f"WHEN 'low' THEN 3 ELSE 2 END")


class PriorityError(ValueError):
    pass


def store(value: Optional[str], default: Optional[str] = DEFAULT) -> Optional[str]:
    """The value to save (old names). None/'' -> default. Unknown -> PriorityError."""
    if value is None or str(value).strip() == "":
        return default
    v = str(value).strip().lower()
    if v not in _STORE:
        raise PriorityError(f"priority must be one of {', '.join(LEVELS)} "
                            f"(older names high and medium also work); got {value!r}.")
    return _STORE[v]


def label(stored: Optional[str]) -> str:
    """The name to show for a stored value (unknown or empty -> normal)."""
    return _LABEL.get((stored or "").strip().lower(), "normal")


def show(value):
    """An answer with every to-do "priority" shown by its name (high -> urgent, medium ->
    normal), however deep. Only todos have a priority; problems' severity is left alone."""
    if isinstance(value, dict):
        return {k: (_LABEL.get(v, v) if k == "priority" and isinstance(v, str) else show(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [show(v) for v in value]
    return value


def rank(stored: Optional[str]) -> int:
    return RANK.get((stored or "").strip().lower(), 2)


def at_least(level: str) -> list[str]:
    """Stored values at this level or more urgent (for min_priority filters)."""
    top = rank(store(level))
    return [v for v, r in RANK.items() if r <= top]


def error_payload(e: Exception) -> dict:
    return {"error": "bad_priority", "message": str(e)}
