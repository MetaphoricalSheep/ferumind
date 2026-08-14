from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = REPOSITORY_ROOT / "src" / "ferumind" / "dashboard" / "static"
INDEX = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_ROOT / "dashboard.css").read_text(encoding="utf-8")
JAVASCRIPT = (STATIC_ROOT / "dashboard.js").read_text(encoding="utf-8")


class _MarkupAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: Counter[str] = Counter()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        self.tags[tag] += 1
        self.elements.append((tag, attributes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


@pytest.fixture(scope="module")
def markup() -> _MarkupAudit:
    audit = _MarkupAudit()
    audit.feed(INDEX)
    return audit


def test_dashboard_uses_only_explicit_local_assets(markup: _MarkupAudit) -> None:
    asset_urls = {
        attributes.get("href") or attributes.get("src")
        for tag, attributes in markup.elements
        if tag in {"link", "script"}
    }
    assert asset_urls == {
        "/static/basecoat/tokens.css",
        "/static/basecoat/base.css",
        "/static/basecoat/components.css",
        "/static/dashboard.css",
        "/static/dashboard.js",
    }

    for tag, attributes in markup.elements:
        for attribute in ("href", "src", "action"):
            value = attributes.get(attribute)
            if not value:
                continue
            parsed = urlsplit(value)
            assert not parsed.scheme, (tag, attribute, value)
            assert not parsed.netloc, (tag, attribute, value)
            assert not value.startswith("//"), (tag, attribute, value)

    assert "@import" not in CSS
    assert re.search(r"url\((?![\"']?#)", CSS) is None


def test_dashboard_has_semantic_landmarks_and_one_page_heading(markup: _MarkupAudit) -> None:
    assert markup.tags["h1"] == 1
    for landmark in ("header", "nav", "main", "footer"):
        assert markup.tags[landmark] == 1
    assert markup.tags["section"] >= 5
    assert 'href="#main-content"' in INDEX
    assert 'id="main-content"' in INDEX
    assert 'aria-label="Operator views"' in INDEX
    assert 'role="search"' in INDEX

    identifiers = [attributes["id"] for _tag, attributes in markup.elements if attributes.get("id")]
    assert len(identifiers) == len(set(identifiers))
    identifier_set = set(identifiers)
    labelled_by = [
        reference
        for _tag, attributes in markup.elements
        for reference in attributes.get("aria-labelledby", "").split()
    ]
    assert set(labelled_by) <= identifier_set


def test_native_controls_and_tables_are_labeled(markup: _MarkupAudit) -> None:
    label_targets = {
        attributes["for"]
        for tag, attributes in markup.elements
        if tag == "label" and attributes.get("for")
    }
    controls = {
        attributes["id"]
        for tag, attributes in markup.elements
        if tag in {"input", "select"} and attributes.get("id")
    }
    assert controls <= label_targets

    headers = [attributes for tag, attributes in markup.elements if tag == "th"]
    assert headers
    assert all(attributes.get("scope") == "col" for attributes in headers)
    assert 'tabindex="0" aria-label="Scrollable recent calls table"' in INDEX
    assert 'aria-live="polite"' in INDEX

    button_bodies = re.findall(r"<button\b[^>]*>(.*?)</button>", INDEX, flags=re.DOTALL)
    assert button_bodies
    for body in button_bodies:
        visible_text = re.sub(r"<[^>]+>", "", body).strip()
        assert visible_text


def test_activity_chart_has_non_color_accessibility_and_reduced_motion() -> None:
    assert '<svg\n                id="activity-chart"' in INDEX
    assert 'role="img"' in INDEX
    assert "activity-chart-title activity-chart-description" in INDEX
    assert "<figcaption>" in INDEX
    assert "Calls, solid" in INDEX
    assert "Failures, striped" in INDEX
    assert "failure-pattern" in INDEX
    assert "prefers-reduced-motion: reduce" in CSS
    assert "prefers-reduced-motion: no-preference" in CSS


def test_frontend_reuses_basecoat_without_redefining_components() -> None:
    combined = INDEX + JAVASCRIPT
    for pattern in (
        "bc-panel",
        "bc-stat",
        "bc-status-card",
        "bc-dot",
        "bc-dot-live",
        "bc-chip",
        "bc-progress",
    ):
        assert pattern in combined

    assert re.search(r"(?m)^\s*\.bc-[\w-]+\s*(?:[,{:]|::)", CSS) is None
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", CSS) is None
    assert re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", CSS) is None
    for token in (
        "--bc-bg",
        "--bc-surface",
        "--bc-text",
        "--bc-text-muted",
        "--bc-tone-danger",
        "--bc-tone-caution",
    ):
        assert token in CSS


@pytest.mark.parametrize("view", ["overview", "calls", "errors", "performance", "runtime"])
def test_every_hash_view_and_api_route_is_present(view: str) -> None:
    assert f'href="#{view}"' in INDEX
    assert f'data-view="{view}"' in INDEX
    assert f'{view}: "/api/v1/{view}"' in JAVASCRIPT


def test_calls_filters_detail_and_error_groups_have_rendering_markers() -> None:
    for control in (
        "calls-project",
        "calls-tool",
        "calls-client",
        "calls-status",
        "calls-error-code",
        "calls-window-select",
        "observation-detail",
        "copy-correlation",
    ):
        assert f'id="{control}"' in INDEX

    for field in (
        'observations: "/api/v1/observations/"',
        'first(report, ["observations", "items"]',
        'first(report, ["error_code_groups", "failure_groups"]',
        'first(report, ["internal_error_groups"]',
        'first(report, ["observation_write_failures"]',
        'first(report, ["runtime_events", "runtime_diagnostics"]',
        'first(report, ["safe_frames"]',
    ):
        assert field in JAVASCRIPT

    assert "renderExpectedErrorGroups" in JAVASCRIPT
    assert "renderInternalErrorGroups" in JAVASCRIPT
    assert "groupedTelemetryEvents" in JAVASCRIPT
    assert "encodeURIComponent(normalized)" in JAVASCRIPT


def test_performance_and_lifecycle_views_render_bounded_diagnostics() -> None:
    for marker in (
        "calls_by_tool",
        "slowest_calls",
        "largest_responses",
        "createPerformanceBar",
        "renderTimeline",
        "process_started",
        "client_initialized",
        "transport_closed",
        "process_stopping",
        "internal_error",
        "observation_write_failed",
        "malformed_request",
        "request_too_large",
    ):
        assert marker in JAVASCRIPT
    assert "RUNTIME_LIMIT = 200" in JAVASCRIPT
    assert "CALL_LIMIT = 50" in JAVASCRIPT


def test_dynamic_data_is_rendered_with_safe_dom_apis() -> None:
    forbidden = (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        ".style.",
        'setAttribute("style"',
    )
    for expression in forbidden:
        assert expression not in JAVASCRIPT

    for safe_pattern in (
        "document.createElement(",
        "document.createElementNS(",
        ".textContent =",
        ".replaceChildren(",
        "encodeURIComponent(",
    ):
        assert safe_pattern in JAVASCRIPT
    assert "fetch(path" in JAVASCRIPT
    assert "XMLHttpRequest" not in JAVASCRIPT
    assert "WebSocket" not in JAVASCRIPT
    assert "EventSource" not in JAVASCRIPT


def test_polling_is_ten_seconds_nonoverlapping_and_visibility_aware() -> None:
    assert "POLL_INTERVAL_MS = 10_000" in JAVASCRIPT
    assert "state.refreshPromise !== null" in JAVASCRIPT
    assert "state.refreshPromise = performRefresh().finally" in JAVASCRIPT
    assert "window.setTimeout" in JAVASCRIPT
    assert "window.clearTimeout" in JAVASCRIPT
    assert "document.hidden" in JAVASCRIPT
    assert 'document.addEventListener("visibilitychange"' in JAVASCRIPT
    assert "previously loaded detail" in JAVASCRIPT
    assert "last successful data is still displayed" in JAVASCRIPT


def test_dashboard_assets_are_loadable_as_package_resources() -> None:
    packaged_static = resources.files("ferumind.dashboard").joinpath("static")
    assets = (
        "index.html",
        "dashboard.css",
        "dashboard.js",
        "basecoat/tokens.css",
        "basecoat/base.css",
        "basecoat/components.css",
        "basecoat/REVISION",
    )
    for relative_path in assets:
        resource = packaged_static.joinpath(*relative_path.split("/"))
        assert resource.is_file(), relative_path
        assert resource.read_bytes(), relative_path
