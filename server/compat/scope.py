"""Entity-scope resolution for REST and MCP handlers.

Provides helpers to collect, validate, and merge entity-identifying parameters
(``user_id``, ``agent_id``, ``run_id``) from request bodies and query strings.
"""

from typing import Any, Optional

from fastapi import HTTPException

ENTITY_PARAMS = frozenset({"user_id", "agent_id", "run_id"})

COMPAT_TYPE_TO_FIELD: dict[str, str] = {
    "user": "user_id",
    "agent": "agent_id",
    "run": "run_id",
}
VALID_ENTITY_TYPES = frozenset(COMPAT_TYPE_TO_FIELD)

UNSUPPORTED_ENTITY_TYPES: dict[str, str] = {
    "app": "app_id",
}

UNSUPPORTED_ENTITY_PARAMS = frozenset({"app_id"})


def reject_app_id(app_id: Optional[str]) -> None:
    """Raise 501 if *app_id* is not None."""
    if app_id is not None:
        raise HTTPException(status_code=501, detail="'app_id' is not supported by the self-hosted server.")


def collect_entity_params(
    *,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    app_id: Optional[str] = None,
    run_id: Optional[str] = None,
    filters: Optional[dict[str, Any]] = None,
    reject_unsupported: bool = True,
) -> dict[str, str]:
    """Collect non-None entity params, preferring explicit kwargs over *filters*.

    If *reject_unsupported* is True, raises 501 when ``app_id`` is present.
    """
    if reject_unsupported:
        reject_app_id(app_id)
        if filters:
            for key in UNSUPPORTED_ENTITY_PARAMS:
                if key in filters and filters[key] is not None:
                    raise HTTPException(status_code=501, detail=f"'{key}' is not supported by the self-hosted server.")
                # Also scan nested AND/OR conditions.
                for op in ("AND", "OR"):
                    conditions = filters.get(op)
                    if isinstance(conditions, list):
                        for cond in conditions:
                            if isinstance(cond, dict) and cond.get(key) is not None:
                                raise HTTPException(status_code=501, detail=f"'{key}' is not supported by the self-hosted server.")
    merged: dict[str, Any] = {}
    if filters:
        # Fast path: flat format {user_id: "...", ...}
        merged.update({k: v for k, v in filters.items() if k in ENTITY_PARAMS and v is not None})
        # Slow path: AND/OR nested format {AND: [{user_id: "..."}, ...]}
        # Used by openclaw's _buildFilters when multiple conditions are present.
        if not merged:
            for op in ("AND", "OR"):
                conditions = filters.get(op)
                if isinstance(conditions, list):
                    for cond in conditions:
                        if isinstance(cond, dict):
                            merged.update({k: v for k, v in cond.items() if k in ENTITY_PARAMS and v is not None})
                    if merged:
                        break
    for key, val in (("user_id", user_id), ("agent_id", agent_id), ("run_id", run_id)):
        if val is not None:
            merged[key] = val
    return merged


def require_entity_scope(
    *,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    app_id: Optional[str] = None,
    run_id: Optional[str] = None,
    filters: Optional[dict[str, Any]] = None,
    detail: str = "At least one of user_id, agent_id, or run_id is required.",
    fallback_user_id: Optional[str] = None,
) -> dict[str, str]:
    """Like ``collect_entity_params`` but raises 400 when no scope is found.

    If *fallback_user_id* is given and no entity params are present, returns
    ``{"user_id": fallback_user_id}`` instead of raising.
    """
    params = collect_entity_params(
        user_id=user_id, agent_id=agent_id, app_id=app_id, run_id=run_id, filters=filters,
    )
    if not params:
        if fallback_user_id:
            return {"user_id": fallback_user_id}
        raise HTTPException(status_code=400, detail=detail)
    return params


def build_search_filters(
    *,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    app_id: Optional[str] = None,
    run_id: Optional[str] = None,
    filters: Optional[dict[str, Any]] = None,
    detail: str = "At least one of user_id, agent_id, or run_id is required.",
    fallback_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve scope then merge into *filters* dict for ``Memory.search`` / ``get_all``."""
    scope = require_entity_scope(
        user_id=user_id, agent_id=agent_id, app_id=app_id, run_id=run_id,
        filters=filters, detail=detail, fallback_user_id=fallback_user_id,
    )
    merged: dict[str, Any] = dict(filters) if filters else {}
    if "AND" not in merged and "OR" not in merged:
        # Flat format: safe to merge entity scope at top level.
        merged.update(scope)
        return merged
    # AND/OR format: merging scope at top level would produce a malformed mixed-format dict
    # (e.g. {"AND": [...], "user_id": "bob"}).  Only inject entity params that came from
    # explicit kwargs (plus fallback) — those already inside AND/OR conditions are already
    # present in 'merged' and do not need re-injection.
    kwargs_scope = collect_entity_params(user_id=user_id, agent_id=agent_id, run_id=run_id, reject_unsupported=False)
    if not kwargs_scope and fallback_user_id:
        kwargs_scope = {"user_id": fallback_user_id}
    if not kwargs_scope:
        # All scope came from within the AND/OR conditions; nothing extra to inject.
        return merged
    if "AND" in merged:
        # Append extra entity conditions into the AND list (new list to avoid mutating caller's filters).
        merged["AND"] = [*merged["AND"], *({k: v} for k, v in kwargs_scope.items())]
    else:
        # OR format: wrap in an outer AND to restrict to the entity without modifying OR logic.
        merged = {"AND": [merged, kwargs_scope]}
    return merged


def get_entity_field(entity_type: str) -> str:
    """Map entity type name (``"user"``) to payload field name (``"user_id"``).

    Raises 501 for known-but-unsupported types (``"app"``).
    Raises 400 for unknown types.
    """
    if entity_type in UNSUPPORTED_ENTITY_TYPES:
        raise HTTPException(status_code=501, detail=f"'{entity_type}' entities are not supported by the self-hosted server.")
    field = COMPAT_TYPE_TO_FIELD.get(entity_type)
    if field is None:
        raise HTTPException(status_code=400, detail="Invalid entity type")
    return field
