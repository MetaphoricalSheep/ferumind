"""Versioned, read-only JSON API for the local operator dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import parse_qsl, unquote

from pydantic import ValidationError

from ferumind.core.diagnostic_models import DiagnosticQuery, DiagnosticWindow
from ferumind.core.diagnostics import DiagnosticsService
from ferumind.core.types import JsonObject, StrictModel

API_PREFIX = "/api/v1"
MAX_QUERY_CHARS = 4_096
MAX_QUERY_FIELDS = 16
MAX_FILTER_CHARS = 256

_QUERY_KEYS = frozenset(
    {"window", "project", "tool", "client", "error_code", "status", "failed", "limit", "offset"}
)
_RUNTIME_QUERY_KEYS = frozenset({"window", "limit"})


class ApiErrorBody(StrictModel):
    ok: Literal[False] = False
    error_code: str
    message: str


class ApiMetaBody(StrictModel):
    api_version: Literal["version 1"] = "version 1"
    diagnostics: JsonObject


@dataclass(frozen=True)
class DashboardApiResponse:
    status: int
    body: JsonObject


class DashboardApiError(ValueError):
    """A fixed, caller-safe API error."""

    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.safe_message = message

    def response(self) -> DashboardApiResponse:
        payload = ApiErrorBody(
            error_code=self.error_code,
            message=self.safe_message,
        ).model_dump(mode="json")
        return DashboardApiResponse(status=self.status, body=cast(JsonObject, payload))


def _single_query_values(query_string: str) -> dict[str, str]:
    if len(query_string) > MAX_QUERY_CHARS:
        raise DashboardApiError(400, "INVALID_QUERY", "Query string exceeds the safe limit")
    try:
        pairs = parse_qsl(
            query_string,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=MAX_QUERY_FIELDS,
        )
    except ValueError as exc:
        raise DashboardApiError(400, "INVALID_QUERY", "Query string is malformed") from exc

    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in _QUERY_KEYS:
            raise DashboardApiError(400, "INVALID_QUERY", "Query contains an unsupported parameter")
        if key in values:
            raise DashboardApiError(400, "INVALID_QUERY", "Query parameters may appear only once")
        if len(value) > MAX_FILTER_CHARS:
            raise DashboardApiError(400, "INVALID_QUERY", "Query value exceeds the safe limit")
        values[key] = value
    return values


def _optional_filter(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    return value if value else None


def _bounded_integer(
    values: dict[str, str],
    key: str,
    *,
    default: int,
    maximum: int,
) -> int:
    raw = values.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise DashboardApiError(400, "INVALID_QUERY", "Numeric query value is invalid") from exc
    if value < 0 or value > maximum or (key == "limit" and value == 0):
        raise DashboardApiError(400, "INVALID_QUERY", "Numeric query value is outside its bounds")
    return value


def _failed_filter(values: dict[str, str]) -> bool | None:
    raw = values.get("failed")
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise DashboardApiError(400, "INVALID_QUERY", "failed must be true or false")


def diagnostic_query(
    query_string: str,
    *,
    allowed_keys: frozenset[str] = _QUERY_KEYS,
) -> DiagnosticQuery:
    """Validate bounded URL parameters into the shared core query model."""

    values = _single_query_values(query_string)
    if not values.keys() <= allowed_keys:
        raise DashboardApiError(
            400,
            "INVALID_QUERY",
            "Query contains a parameter unsupported by this endpoint",
        )
    window = values.get("window", "24h")
    status = values.get("status", "all")
    try:
        return DiagnosticQuery(
            window=cast(DiagnosticWindow, window),
            project=_optional_filter(values, "project"),
            tool=_optional_filter(values, "tool"),
            client=_optional_filter(values, "client"),
            error_code=_optional_filter(values, "error_code"),
            status=cast("Literal['all', 'success', 'failed', 'unknown']", status),
            failed=_failed_filter(values),
            limit=_bounded_integer(values, "limit", default=50, maximum=500),
            offset=_bounded_integer(values, "offset", default=0, maximum=100_000),
        )
    except ValidationError as exc:
        raise DashboardApiError(400, "INVALID_QUERY", "Query parameters are invalid") from exc


def _model_body(model: StrictModel) -> JsonObject:
    """Serialize one strict boundary model into JSON-compatible values."""

    return cast(JsonObject, model.model_dump(mode="json", exclude_none=False))


def _correlation_id(path: str) -> str:
    encoded = path.removeprefix(f"{API_PREFIX}/observations/")
    if not encoded or len(encoded) > MAX_FILTER_CHARS * 3:
        raise DashboardApiError(400, "INVALID_CORRELATION_ID", "Correlation ID is invalid")
    correlation_id = unquote(encoded)
    if (
        not correlation_id
        or len(correlation_id) > MAX_FILTER_CHARS
        or "/" in correlation_id
        or "\\" in correlation_id
        or any(not character.isprintable() for character in correlation_id)
    ):
        raise DashboardApiError(400, "INVALID_CORRELATION_ID", "Correlation ID is invalid")
    return correlation_id


class DashboardApi:
    """Thin endpoint dispatcher over :class:`DiagnosticsService`."""

    def __init__(self, diagnostics: DiagnosticsService) -> None:
        self._diagnostics = diagnostics

    def dispatch(self, path: str, query_string: str) -> DashboardApiResponse:
        if path == f"{API_PREFIX}/meta":
            if query_string:
                raise DashboardApiError(400, "INVALID_QUERY", "Meta does not accept parameters")
            meta = ApiMetaBody(diagnostics=_model_body(self._diagnostics.meta()))
            return DashboardApiResponse(
                status=200, body=cast(JsonObject, meta.model_dump(mode="json"))
            )
        if path.startswith(f"{API_PREFIX}/observations/"):
            if query_string:
                raise DashboardApiError(
                    400,
                    "INVALID_QUERY",
                    "Observation detail does not accept parameters",
                )
            return DashboardApiResponse(
                status=200,
                body=_model_body(self._diagnostics.observation(_correlation_id(path))),
            )
        allowed_keys = _RUNTIME_QUERY_KEYS if path == f"{API_PREFIX}/runtime" else _QUERY_KEYS
        query = diagnostic_query(query_string, allowed_keys=allowed_keys)
        return self._dispatch_report(path, query)

    def _dispatch_report(
        self,
        path: str,
        query: DiagnosticQuery,
    ) -> DashboardApiResponse:
        if path == f"{API_PREFIX}/overview":
            report = self._diagnostics.overview(query)
        elif path == f"{API_PREFIX}/calls":
            report = self._diagnostics.calls(query)
        elif path == f"{API_PREFIX}/errors":
            report = self._diagnostics.errors(query)
        elif path == f"{API_PREFIX}/performance":
            report = self._diagnostics.performance(query)
        elif path == f"{API_PREFIX}/runtime":
            report = self._diagnostics.runtime(query)
        else:
            raise DashboardApiError(404, "NOT_FOUND", "Dashboard API route was not found")
        return DashboardApiResponse(status=200, body=_model_body(report))
