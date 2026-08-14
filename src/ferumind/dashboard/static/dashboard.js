(() => {
  "use strict";

  const ENDPOINTS = Object.freeze({
    meta: "/api/v1/meta",
    overview: "/api/v1/overview",
    calls: "/api/v1/calls",
    errors: "/api/v1/errors",
    performance: "/api/v1/performance",
    runtime: "/api/v1/runtime",
    observations: "/api/v1/observations/",
  });
  const POLL_INTERVAL_MS = 10_000;
  const CALL_LIMIT = 50;
  const RUNTIME_LIMIT = 200;
  const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  const VALID_VIEWS = new Set(["overview", "calls", "errors", "performance", "runtime"]);
  const VALID_WINDOWS = new Set(["1h", "24h", "7d", "30d"]);
  const TONE_CLASSES = [
    "bc-tone-success",
    "bc-tone-caution",
    "bc-tone-danger",
    "bc-tone-neutral",
  ];
  const TONE_CLASS = Object.freeze({
    success: "bc-tone-success",
    caution: "bc-tone-caution",
    danger: "bc-tone-danger",
    neutral: "bc-tone-neutral",
  });

  const state = {
    activeView: "overview",
    window: "24h",
    callOffset: 0,
    callFilters: {
      project: "",
      tool: "",
      client: "",
      status: "all",
      errorCode: "",
    },
    reports: Object.create(null),
    apiVersion: "version 1",
    pollTimer: null,
    refreshPromise: null,
    refreshRequested: false,
    detailPromise: null,
    selectedCorrelation: null,
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function record(value) {
    return isRecord(value) ? value : {};
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function first(source, keys, fallback = null) {
    const safeSource = record(source);
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(safeSource, key)) {
        const value = safeSource[key];
        if (value !== null && value !== undefined) {
          return value;
        }
      }
    }
    return fallback;
  }

  function numberValue(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  function textValue(value, fallback = "—") {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    if (typeof value === "boolean") {
      return value ? "Yes" : "No";
    }
    if (typeof value === "string" || typeof value === "number") {
      return String(value);
    }
    return fallback;
  }

  function createElement(tagName, className = "", text = null) {
    const element = document.createElement(tagName);
    if (className) {
      element.className = className;
    }
    if (text !== null) {
      element.textContent = textValue(text, "");
    }
    return element;
  }

  function createSvgElement(tagName) {
    return document.createElementNS(SVG_NAMESPACE, tagName);
  }

  function setText(id, value, fallback = "—") {
    const element = byId(id);
    if (element) {
      element.textContent = textValue(value, fallback);
    }
  }

  function setTone(element, tone) {
    if (!element) {
      return;
    }
    element.classList.remove(...TONE_CLASSES);
    element.classList.add(TONE_CLASS[tone] || TONE_CLASS.neutral);
  }

  function setStatusCard(cardId, statusId, copyId, tone, status, copy) {
    setTone(byId(cardId), tone);
    setText(statusId, status);
    setText(copyId, copy);
  }

  function formatCount(value) {
    const number = numberValue(value);
    return number === null ? "—" : Math.max(0, number).toLocaleString();
  }

  function formatPercent(value) {
    const number = numberValue(value);
    if (number === null) {
      return "—";
    }
    const percent = Math.abs(number) <= 1 ? number * 100 : number;
    const digits = percent > 0 && percent < 1 ? 1 : 0;
    return `${percent.toFixed(digits)}%`;
  }

  function formatDuration(value) {
    const milliseconds = numberValue(value);
    if (milliseconds === null) {
      return "—";
    }
    if (milliseconds < 10) {
      return `${milliseconds.toFixed(1)} ms`;
    }
    if (milliseconds < 1_000) {
      return `${Math.round(milliseconds).toLocaleString()} ms`;
    }
    return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`;
  }

  function formatBytes(value) {
    const bytes = numberValue(value);
    if (bytes === null || bytes < 0) {
      return "—";
    }
    if (bytes < 1_024) {
      return `${Math.round(bytes).toLocaleString()} B`;
    }
    if (bytes < 1_048_576) {
      return `${(bytes / 1_024).toFixed(bytes < 10_240 ? 1 : 0)} KiB`;
    }
    if (bytes < 1_073_741_824) {
      return `${(bytes / 1_048_576).toFixed(bytes < 10_485_760 ? 1 : 0)} MiB`;
    }
    return `${(bytes / 1_073_741_824).toFixed(1)} GiB`;
  }

  function parsedDate(value) {
    if (typeof value !== "string" || value === "") {
      return null;
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatTimestamp(value) {
    const date = parsedDate(value);
    if (!date) {
      return "Unknown";
    }
    return date.toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "medium",
    });
  }

  function formatShortTime(value) {
    const date = parsedDate(value);
    if (!date) {
      return "Unknown";
    }
    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatList(value, fallback = "None recorded") {
    const values = list(value)
      .map((item) => textValue(item, ""))
      .filter((item) => item !== "");
    if (values.length > 0) {
      return values.join(", ");
    }
    if (typeof value === "string" && value !== "") {
      return value;
    }
    return fallback;
  }

  function formatClient(observationOrClient) {
    if (typeof observationOrClient === "string") {
      return observationOrClient || "Unknown client";
    }
    const source = record(observationOrClient);
    const name = first(source, ["client_name", "name"], null);
    const version = first(source, ["client_version", "version"], null);
    if (name === null) {
      return "Unknown client";
    }
    return version === null ? textValue(name) : `${textValue(name)} ${textValue(version)}`;
  }

  function statusDetails(observation) {
    const ok = first(observation, ["ok", "success"], null);
    if (ok === true) {
      return { label: "Succeeded", tone: "success" };
    }
    if (ok === false) {
      return { label: "Failed", tone: "danger" };
    }
    return { label: "Unknown", tone: "neutral" };
  }

  function appendCell(row, value, className = "") {
    const cell = createElement("td", className, value);
    row.append(cell);
    return cell;
  }

  function appendDefinition(container, label, value, code = false) {
    const group = createElement("div");
    const term = createElement("dt", "", label);
    const definition = createElement("dd");
    const content = code ? createElement("code", "", value) : document.createTextNode(textValue(value));
    definition.append(content);
    group.append(term, definition);
    container.append(group);
  }

  function degradationCount(report) {
    const value = first(report, ["degradations", "degraded"], []);
    if (Array.isArray(value)) {
      return value.length;
    }
    return value ? 1 : 0;
  }

  function reportGeneratedAt(report) {
    const value = first(report, ["generated_at"], null);
    return parsedDate(value) || new Date();
  }

  function updateLastRefresh(report) {
    const time = byId("last-refreshed");
    if (!time) {
      return;
    }
    const generatedAt = reportGeneratedAt(report);
    time.dateTime = generatedAt.toISOString();
    time.textContent = generatedAt.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    time.title = generatedAt.toLocaleString();
  }

  function setConnection(tone, label, live = false) {
    const dot = byId("connection-dot");
    setTone(dot, tone);
    if (dot) {
      dot.classList.toggle("bc-dot-live", live);
      dot.classList.toggle("bc-dot", !live);
    }
    setText("connection-label", label);
  }

  function setGlobalNotice(visible, title = "", copy = "", tone = "caution") {
    const notice = byId("global-notice");
    if (!notice) {
      return;
    }
    notice.hidden = !visible;
    if (visible) {
      setTone(notice, tone);
      setText("global-notice-title", title);
      setText("global-notice-copy", copy);
    }
  }

  function viewStateId(view) {
    return {
      overview: "overview-state",
      calls: "calls-state",
      errors: "errors-view-state",
      performance: "performance-state",
      runtime: "runtime-state",
    }[view];
  }

  function setViewState(view, message = "") {
    const element = byId(viewStateId(view));
    if (!element) {
      return;
    }
    element.textContent = message;
    element.hidden = message === "";
  }

  function applyTheme(theme, persist = true) {
    const resolved = theme === "light" ? "light" : "dark";
    document.documentElement.classList.toggle("light", resolved === "light");
    document.documentElement.classList.toggle("dark", resolved === "dark");
    const toggle = byId("theme-toggle");
    if (toggle) {
      toggle.setAttribute("aria-pressed", String(resolved === "light"));
      toggle.setAttribute(
        "aria-label",
        resolved === "light" ? "Switch to dark theme" : "Switch to light theme",
      );
    }
    setText("theme-label", resolved === "light" ? "Dark theme" : "Light theme");
    if (persist) {
      try {
        localStorage.setItem("ferumind-dashboard-theme", resolved);
      } catch (_error) {
        // Theme persistence is optional; the dashboard remains fully usable without it.
      }
    }
  }

  function initializeTheme() {
    let storedTheme = "dark";
    try {
      const value = localStorage.getItem("ferumind-dashboard-theme");
      if (value === "light" || value === "dark") {
        storedTheme = value;
      }
    } catch (_error) {
      storedTheme = "dark";
    }
    applyTheme(storedTheme, false);
  }

  function selectedRoute() {
    const route = window.location.hash.replace(/^#/, "").split("/")[0].toLowerCase();
    return VALID_VIEWS.has(route) ? route : "overview";
  }

  function activateView(view) {
    state.activeView = VALID_VIEWS.has(view) ? view : "overview";
    for (const section of document.querySelectorAll("[data-view]")) {
      section.hidden = section.dataset.view !== state.activeView;
    }
    for (const link of document.querySelectorAll("[data-route]")) {
      if (link.dataset.route === state.activeView) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    }
    if (!window.location.hash || !VALID_VIEWS.has(window.location.hash.replace(/^#/, ""))) {
      window.history.replaceState(null, "", `#${state.activeView}`);
    }
  }

  function queryPath(view) {
    const endpoint = ENDPOINTS[view];
    const parameters = new URLSearchParams();
    parameters.set("window", state.window);
    if (view === "calls") {
      parameters.set("limit", String(CALL_LIMIT));
      parameters.set("offset", String(state.callOffset));
      if (state.callFilters.project) {
        parameters.set("project", state.callFilters.project);
      }
      if (state.callFilters.tool) {
        parameters.set("tool", state.callFilters.tool);
      }
      if (state.callFilters.client) {
        parameters.set("client", state.callFilters.client);
      }
      if (state.callFilters.status !== "all") {
        parameters.set("status", state.callFilters.status);
      }
      if (state.callFilters.errorCode) {
        parameters.set("error_code", state.callFilters.errorCode);
      }
    } else if (view === "runtime") {
      parameters.set("limit", String(RUNTIME_LIMIT));
    }
    return `${endpoint}?${parameters.toString()}`;
  }

  async function fetchJson(path) {
    const response = await fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error("Dashboard API request failed");
    }
    const payload = await response.json();
    if (!isRecord(payload)) {
      throw new Error("Dashboard API returned an invalid document");
    }
    return payload;
  }

  function clearPollTimer() {
    if (state.pollTimer !== null) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function queuePoll() {
    clearPollTimer();
    if (document.hidden) {
      return;
    }
    state.pollTimer = window.setTimeout(() => {
      state.pollTimer = null;
      void refreshActiveView();
    }, POLL_INTERVAL_MS);
  }

  async function performRefresh() {
    const view = state.activeView;
    const refreshButton = byId("refresh-button");
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.setAttribute("aria-busy", "true");
    }
    setConnection("caution", "Refreshing local diagnostics", false);
    setViewState(view, state.reports[view] ? "Refreshing…" : `Loading ${view}…`);

    const outcomes = await Promise.allSettled([
      fetchJson(ENDPOINTS.meta),
      fetchJson(queryPath(view)),
    ]);
    let activeReport = null;
    if (outcomes[0].status === "fulfilled") {
      renderMeta(outcomes[0].value);
    }
    if (outcomes[1].status === "fulfilled") {
      activeReport = outcomes[1].value;
      state.reports[view] = activeReport;
      renderReport(view, activeReport);
      updateLastRefresh(activeReport);
      setConnection("success", "Local diagnostics responding", true);
      const partialCount = degradationCount(activeReport) + degradationCount(state.reports.meta);
      if (partialCount > 0) {
        setGlobalNotice(
          true,
          "Diagnostics are partially available",
          `${formatCount(partialCount)} diagnostic source issue${partialCount === 1 ? " is" : "s are"} present. Available data remains visible.`,
          "caution",
        );
        setViewState(view, "Showing available data; some diagnostic sources are unavailable.");
      } else {
        setGlobalNotice(false);
        setViewState(view, "");
      }
    } else {
      setConnection("danger", "Local diagnostics request failed", false);
      setGlobalNotice(
        true,
        "Refresh failed",
        state.reports[view]
          ? "The last successful data is still displayed. Automatic polling will retry."
          : "No data has loaded for this view yet. Automatic polling will retry.",
        "danger",
      );
      setViewState(
        view,
        state.reports[view]
          ? "Showing data from the last successful refresh."
          : "Unable to load this view.",
      );
    }
    if (outcomes[0].status === "rejected" && activeReport !== null) {
      setGlobalNotice(
        true,
        "Metadata is temporarily unavailable",
        "The active report refreshed successfully; previously known database metadata is retained.",
        "caution",
      );
    }

    if (refreshButton) {
      refreshButton.disabled = false;
      refreshButton.removeAttribute("aria-busy");
    }
  }

  function refreshActiveView() {
    clearPollTimer();
    if (state.refreshPromise !== null) {
      state.refreshRequested = true;
      return state.refreshPromise;
    }
    state.refreshPromise = performRefresh().finally(() => {
      const refreshAgain = state.refreshRequested && !document.hidden;
      state.refreshRequested = false;
      state.refreshPromise = null;
      if (refreshAgain) {
        void refreshActiveView();
      } else {
        queuePoll();
      }
    });
    return state.refreshPromise;
  }

  function renderMeta(payload) {
    const meta = record(first(payload, ["diagnostics"], payload));
    state.reports.meta = meta;
    state.apiVersion = textValue(first(payload, ["api_version"], "version 1"), "version 1");
    const database = first(meta, ["database_available"], null);
    const runtimeLog = first(meta, ["runtime_log_available"], null);
    const databaseLabel =
      database === true ? "database available" : database === false ? "database unavailable" : "database unknown";
    const runtimeLabel =
      runtimeLog === true
        ? "runtime log available"
        : runtimeLog === false
          ? "runtime log unavailable"
          : "runtime log unknown";
    setText("dashboard-meta", `API ${state.apiVersion} · ${databaseLabel} · ${runtimeLabel}`);
    if (state.reports.overview) {
      renderOverview(state.reports.overview);
    }
  }

  function renderReport(view, report) {
    if (view === "overview") {
      renderOverview(report);
    } else if (view === "calls") {
      renderCalls(report);
    } else if (view === "errors") {
      renderErrors(report);
    } else if (view === "performance") {
      renderPerformance(report);
    } else if (view === "runtime") {
      renderRuntime(report);
    }
  }

  function renderOverview(report) {
    const meta = record(state.reports.meta);
    const legacyDatabase = record(first(report, ["database"], {}));
    const summary = record(first(report, ["summary", "metrics"], {}));
    const latency = record(first(report, ["latency"], {}));
    const responseSizes = record(first(report, ["response_sizes"], {}));
    const lifecycle = record(first(report, ["lifecycle", "runtime_state"], {}));
    const calls = numberValue(first(summary, ["calls", "call_count", "total"], 0)) || 0;
    const failures = numberValue(first(summary, ["failures", "failure_count"], 0)) || 0;
    const internalErrors =
      numberValue(first(summary, ["internal_errors", "internal_error_count"], 0)) ||
      numberValue(first(report, ["internal_error_count"], 0)) ||
      0;
    const failureRate = first(summary, ["failure_rate"], calls > 0 ? failures / calls : 0);
    const observationWriteFailures =
      numberValue(
        first(report, ["observation_write_failures", "observation_write_failure_count"], 0),
      ) || 0;

    const databaseAvailable = first(
      legacyDatabase,
      ["available"],
      first(meta, ["database_available"], null),
    );
    const databaseSize = first(
      legacyDatabase,
      ["size_bytes", "database_size_bytes"],
      first(meta, ["database_size_bytes"], null),
    );
    const observationCount = first(
      legacyDatabase,
      ["observation_count", "count"],
      first(meta, ["observation_count"], null),
    );
    const databaseTone =
      databaseAvailable === true ? "success" : databaseAvailable === false ? "danger" : "caution";
    const databaseStatus =
      databaseAvailable === true
        ? "Available"
        : databaseAvailable === false
          ? "Unavailable"
          : "Availability unknown";
    setStatusCard(
      "database-card",
      "database-status",
      "database-copy",
      databaseTone,
      databaseStatus,
      `${formatCount(observationCount)} observations · ${formatBytes(databaseSize)} on disk`,
    );

    const latestObservation = record(first(report, ["latest_observation"], {}));
    const latestObservationAt = first(
      latestObservation,
      ["created_at", "timestamp"],
      first(meta, ["latest_observation_at"], null),
    );
    const latestClient = first(report, ["latest_client"], latestObservation);
    setStatusCard(
      "activity-card",
      "activity-status",
      "activity-copy",
      calls > 0 ? "success" : "neutral",
      calls > 0 ? `${formatCount(calls)} calls observed` : "No calls in this window",
      latestObservationAt
        ? `Latest ${formatTimestamp(latestObservationAt)} · ${formatClient(latestClient)}`
        : "No latest observation is available.",
    );

    const latestFailure = record(first(report, ["latest_failure"], {}));
    const latestFailureAt = first(
      latestFailure,
      ["created_at", "timestamp"],
      first(report, ["latest_failure_at"], null),
    );
    const errorTone = internalErrors > 0 ? "danger" : failures > 0 ? "caution" : "success";
    setStatusCard(
      "errors-card",
      "errors-status",
      "errors-copy",
      errorTone,
      failures > 0 ? `${formatCount(failures)} failures · ${formatPercent(failureRate)}` : "No failures observed",
      internalErrors > 0
        ? `${formatCount(internalErrors)} internal error${internalErrors === 1 ? "" : "s"}; latest failure ${formatTimestamp(latestFailureAt)}`
        : latestFailureAt
          ? `Latest failure ${formatTimestamp(latestFailureAt)}`
          : "No latest failure is recorded.",
    );

    const lifecycleStatus = textValue(first(lifecycle, ["status", "state"], "unknown"), "unknown");
    const lifecycleDescription = textValue(
      first(lifecycle, ["description"], "Runtime history does not assert current process liveness."),
    );
    const latestRuntimeEvent = record(first(report, ["latest_runtime_event"], {}));
    const latestRuntimeKind = first(latestRuntimeEvent, ["event", "kind"], null);
    const telemetryTone =
      observationWriteFailures > 0
        ? "danger"
        : lifecycleStatus === "unknown" || lifecycleStatus === "never_started"
          ? "caution"
          : "neutral";
    setStatusCard(
      "telemetry-card",
      "telemetry-status",
      "telemetry-copy",
      telemetryTone,
      `${humanize(lifecycleStatus)} · ${formatCount(observationWriteFailures)} write failures`,
      latestRuntimeKind ? `Latest event: ${humanize(latestRuntimeKind)}. ${lifecycleDescription}` : lifecycleDescription,
    );

    setText("overview-calls", formatCount(calls));
    setText("overview-failures", formatCount(failures));
    setText("overview-failure-rate", formatPercent(failureRate));
    setText("overview-p95", formatDuration(first(latency, ["p95_ms", "p95_duration_ms"], null)));
    setText(
      "overview-latency-samples",
      `${formatCount(first(latency, ["sample_count"], 0))} latency samples`,
    );
    setText(
      "overview-largest",
      formatBytes(first(responseSizes, ["max_bytes", "largest_response_bytes"], null)),
    );
    setText(
      "overview-size-samples",
      `${formatCount(first(responseSizes, ["sample_count"], 0))} size samples`,
    );
    renderActivity(report);
  }

  function renderActivity(report) {
    const activityValue = first(report, ["activity"], []);
    const buckets = Array.isArray(activityValue)
      ? activityValue
      : list(first(record(activityValue), ["buckets", "items"], []));
    const grid = byId("activity-grid");
    const series = byId("activity-series");
    const description = byId("activity-chart-description");
    if (!grid || !series || !description) {
      return;
    }
    grid.replaceChildren();
    series.replaceChildren();

    let totalCalls = 0;
    let totalFailures = 0;
    let maximum = 0;
    for (const rawBucket of buckets) {
      const bucket = record(rawBucket);
      const calls = Math.max(0, numberValue(first(bucket, ["calls", "call_count", "count"], 0)) || 0);
      const failures = Math.max(0, numberValue(first(bucket, ["failures", "failure_count"], 0)) || 0);
      totalCalls += calls;
      totalFailures += failures;
      maximum = Math.max(maximum, calls);
    }

    const chartWidth = 800;
    const chartHeight = 180;
    const top = 12;
    const bottom = 22;
    const plotHeight = chartHeight - top - bottom;
    for (let index = 0; index < 4; index += 1) {
      const y = top + (plotHeight * index) / 3;
      const line = createSvgElement("line");
      line.setAttribute("x1", "0");
      line.setAttribute("x2", String(chartWidth));
      line.setAttribute("y1", String(y));
      line.setAttribute("y2", String(y));
      line.setAttribute("stroke", "var(--bc-border)");
      line.setAttribute("stroke-width", "1");
      line.setAttribute("stroke-dasharray", "3 5");
      grid.append(line);
    }

    if (buckets.length > 0 && maximum > 0) {
      const slot = chartWidth / buckets.length;
      const barWidth = Math.max(1.5, slot * 0.68);
      buckets.forEach((rawBucket, index) => {
        const bucket = record(rawBucket);
        const calls = Math.max(0, numberValue(first(bucket, ["calls", "call_count", "count"], 0)) || 0);
        const failures = Math.min(
          calls,
          Math.max(0, numberValue(first(bucket, ["failures", "failure_count"], 0)) || 0),
        );
        const callHeight = (calls / maximum) * plotHeight;
        const failureHeight = (failures / maximum) * plotHeight;
        const x = index * slot + (slot - barWidth) / 2;
        const callBar = createSvgElement("rect");
        callBar.setAttribute("x", String(x));
        callBar.setAttribute("y", String(top + plotHeight - callHeight));
        callBar.setAttribute("width", String(barWidth));
        callBar.setAttribute("height", String(callHeight));
        callBar.setAttribute("rx", "1.5");
        callBar.setAttribute("fill", "var(--bc-accent)");
        series.append(callBar);
        if (failures > 0) {
          const failureBar = createSvgElement("rect");
          failureBar.setAttribute("x", String(x));
          failureBar.setAttribute("y", String(top + plotHeight - failureHeight));
          failureBar.setAttribute("width", String(barWidth));
          failureBar.setAttribute("height", String(failureHeight));
          failureBar.setAttribute("rx", "1.5");
          failureBar.setAttribute("fill", "url(#failure-pattern)");
          series.append(failureBar);
        }
      });
    }

    const summary =
      buckets.length === 0
        ? "No activity buckets are available."
        : `${formatCount(totalCalls)} calls across ${formatCount(buckets.length)} time buckets; ${formatCount(totalFailures)} failures are shown with a striped pattern.`;
    description.textContent = summary;
    setText("activity-summary", summary);
    const range = record(first(report, ["range"], {}));
    const rangeStart = first(range, ["start"], first(record(buckets[0]), ["start"], null));
    const rangeEnd = first(
      range,
      ["end"],
      first(record(buckets[buckets.length - 1]), ["end"], null),
    );
    setText(
      "activity-range",
      rangeStart && rangeEnd ? `${formatTimestamp(rangeStart)} to ${formatTimestamp(rangeEnd)}` : "",
      "",
    );
  }

  function createStatusChip(observation) {
    const status = statusDetails(observation);
    const chip = createElement("span", `bc-chip ${TONE_CLASS[status.tone]}`, status.label);
    return chip;
  }

  function openObservationButton(correlationId, label, className = "table-link") {
    const button = createElement("button", className, label);
    button.type = "button";
    button.setAttribute("aria-label", `Open safe observation detail for ${correlationId}`);
    button.addEventListener("click", () => {
      setText("correlation-message", "");
      const input = byId("correlation-input");
      if (input) {
        input.value = correlationId;
      }
      if (window.location.hash !== "#calls") {
        window.location.hash = "#calls";
      }
      void loadObservation(correlationId);
    });
    return button;
  }

  function renderCalls(report) {
    const observations = list(first(report, ["observations", "items"], []));
    const total = Math.max(0, numberValue(first(report, ["total"], observations.length)) || 0);
    const returned = Math.max(
      0,
      numberValue(first(report, ["returned"], observations.length)) || observations.length,
    );
    const hasMore = first(report, ["has_more"], state.callOffset + returned < total) === true;
    const body = byId("calls-body");
    if (!body) {
      return;
    }
    body.replaceChildren();
    if (observations.length === 0) {
      const row = createElement("tr");
      const cell = appendCell(row, "No observations match the active filters.");
      cell.colSpan = 8;
      body.append(row);
    } else {
      for (const rawObservation of observations) {
        const observation = record(rawObservation);
        const row = createElement("tr");
        const timeCell = createElement("td");
        const correlationId = textValue(first(observation, ["correlation_id"], ""), "");
        const timestamp = first(observation, ["created_at", "timestamp"], null);
        if (correlationId) {
          timeCell.append(openObservationButton(correlationId, formatShortTime(timestamp)));
        } else {
          timeCell.textContent = formatShortTime(timestamp);
        }
        row.append(timeCell);
        const statusCell = createElement("td");
        statusCell.append(createStatusChip(observation));
        row.append(statusCell);
        appendCell(row, first(observation, ["tool_name", "tool"], "Unknown"));
        appendCell(row, first(observation, ["project_key", "project"], "—"));
        appendCell(row, formatClient(observation));
        appendCell(row, formatDuration(first(observation, ["duration_ms"], null)));
        appendCell(row, formatBytes(first(observation, ["result_bytes", "result_size"], null)));
        appendCell(row, first(observation, ["error_code"], "—"), "error-code-cell");
        body.append(row);
      }
    }

    const firstResult = total === 0 ? 0 : state.callOffset + 1;
    const lastResult = Math.min(total, state.callOffset + returned);
    setText(
      "calls-result-summary",
      total === 0 ? "No matching calls" : `${formatCount(firstResult)}–${formatCount(lastResult)} of ${formatCount(total)}`,
    );
    const page = Math.floor(state.callOffset / CALL_LIMIT) + 1;
    const pages = Math.max(1, Math.ceil(total / CALL_LIMIT));
    setText("calls-page-label", `Page ${formatCount(page)} of ${formatCount(pages)}`);
    const previous = byId("calls-previous");
    const next = byId("calls-next");
    if (previous) {
      previous.disabled = state.callOffset === 0;
    }
    if (next) {
      next.disabled = !hasMore;
    }
  }

  function validCorrelationId(value) {
    return (
      typeof value === "string" &&
      value.length > 0 &&
      value.length <= 256 &&
      !/[\\/\u0000-\u001f\u007f]/u.test(value)
    );
  }

  function loadObservation(correlationId) {
    const normalized = typeof correlationId === "string" ? correlationId.trim() : "";
    if (!validCorrelationId(normalized)) {
      setText(
        "correlation-message",
        "Enter a printable correlation ID without path separators (maximum 256 characters).",
      );
      return Promise.resolve();
    }
    if (state.detailPromise !== null) {
      return state.detailPromise;
    }
    const detailPanel = byId("observation-detail");
    if (detailPanel) {
      detailPanel.hidden = false;
    }
    setText("correlation-message", `Investigating ${normalized}…`);
    setText("detail-status", `Loading safe metadata for ${normalized}…`);
    const path = `${ENDPOINTS.observations}${encodeURIComponent(normalized)}`;
    state.detailPromise = fetchJson(path)
      .then((report) => {
        state.selectedCorrelation = normalized;
        renderObservationDetail(report);
        setText("correlation-message", "Investigation loaded.");
        if (detailPanel) {
          detailPanel.focus({ preventScroll: false });
        }
      })
      .catch(() => {
        setText(
          "correlation-message",
          "The correlation lookup failed. Previously loaded detail, if any, is retained.",
        );
        setText(
          "detail-status",
          "Lookup failed; previously loaded detail remains visible while automatic polling continues.",
        );
      })
      .finally(() => {
        state.detailPromise = null;
      });
    return state.detailPromise;
  }

  function renderObservationDetail(report) {
    const found = first(report, ["found"], null);
    const correlationId = textValue(
      first(report, ["correlation_id"], state.selectedCorrelation),
      "Unknown",
    );
    const observation = record(first(report, ["observation"], {}));
    const fields = byId("detail-fields");
    const runtime = byId("detail-runtime");
    const copyButton = byId("copy-correlation");
    if (!fields || !runtime || !copyButton) {
      return;
    }
    fields.replaceChildren();
    runtime.replaceChildren();
    appendDefinition(fields, "Correlation ID", correlationId, true);
    if (found === false && Object.keys(observation).length === 0) {
      setText("detail-status", `No observation or safe runtime diagnostic was found for ${correlationId}.`);
      copyButton.disabled = false;
      return;
    }

    const status = statusDetails(observation);
    appendDefinition(fields, "Observation ID", first(observation, ["id", "observation_id"], "—"), true);
    appendDefinition(fields, "Timestamp", formatTimestamp(first(observation, ["created_at", "timestamp"], null)));
    appendDefinition(fields, "Tool", first(observation, ["tool_name", "tool"], "—"), true);
    appendDefinition(fields, "Project", first(observation, ["project_key", "project"], "—"), true);
    appendDefinition(fields, "Status", status.label);
    appendDefinition(fields, "Error code", first(observation, ["error_code"], "—"), true);
    appendDefinition(fields, "Client", formatClient(observation));
    appendDefinition(fields, "Protocol", first(observation, ["protocol_version"], "—"), true);
    appendDefinition(fields, "Transport", first(observation, ["transport"], "—"), true);
    appendDefinition(fields, "Server boot", first(observation, ["server_boot_id", "boot_id"], "—"), true);
    appendDefinition(fields, "Process ID", first(observation, ["process_id", "pid"], "—"), true);
    appendDefinition(fields, "Duration", formatDuration(first(observation, ["duration_ms"], null)));
    appendDefinition(fields, "Result bytes", formatBytes(first(observation, ["result_bytes"], null)));
    appendDefinition(
      fields,
      "Argument keys",
      formatList(first(observation, ["argument_keys", "argument_keys_json"], [])),
      true,
    );
    appendDefinition(
      fields,
      "Safe context metrics",
      formatStructured(first(observation, ["context_metrics", "context_metrics_json"], {})),
      true,
    );
    appendDefinition(
      fields,
      "Redaction notes",
      formatList(first(observation, ["redaction_notes", "redaction_notes_json"], [])),
    );
    copyButton.disabled = false;

    const runtimeEvents = list(first(report, ["runtime_events", "runtime_diagnostics"], []));
    const safeFrames = list(first(report, ["safe_frames"], []));
    if (runtimeEvents.length === 0 && safeFrames.length === 0) {
      runtime.append(
        createElement(
          "p",
          "muted-copy",
          "No corresponding safe runtime diagnostic was found. This does not prove that no runtime issue occurred.",
        ),
      );
    } else {
      for (const rawEvent of runtimeEvents) {
        const event = record(rawEvent);
        const card = createElement("article", "group-card group-body");
        const title = createElement("p", "group-title", humanize(first(event, ["event", "kind"], "runtime event")));
        const timestamp = createElement(
          "p",
          "group-value",
          `Recorded ${formatTimestamp(first(event, ["timestamp", "created_at"], null))}`,
        );
        const summary = createElement("p", "", runtimeEventDescription(event));
        card.append(title, timestamp, summary);
        runtime.append(card);
      }
      if (safeFrames.length > 0) {
        const heading = createElement("p", "group-title", "Safe stack frames");
        const frameList = createElement("ol", "frame-list");
        for (const rawFrame of safeFrames) {
          const frame = record(rawFrame);
          const item = createElement(
            "li",
            "",
            `${textValue(first(frame, ["module"], "unknown"))} · ${textValue(first(frame, ["function"], "unknown"))} · ${textValue(first(frame, ["source_path"], "unknown"))}:${textValue(first(frame, ["line"], "?"))}`,
          );
          frameList.append(item);
        }
        runtime.append(heading, frameList);
      }
    }
    const partial = degradationCount(report);
    setText(
      "detail-status",
      partial > 0
        ? `Safe detail loaded with ${formatCount(partial)} diagnostic source issue${partial === 1 ? "" : "s"}.`
        : "Safe observation detail loaded.",
    );
  }

  function formatStructured(value) {
    let resolved = value;
    if (typeof value === "string") {
      try {
        resolved = JSON.parse(value);
      } catch (_error) {
        return value || "None recorded";
      }
    }
    if (resolved === null || resolved === undefined) {
      return "None recorded";
    }
    try {
      return JSON.stringify(resolved, null, 2);
    } catch (_error) {
      return "Structured metadata is unavailable";
    }
  }

  function humanize(value) {
    const raw = textValue(value, "Unknown").replaceAll("_", " ");
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  }

  function updateChipCount(id, value) {
    const chip = byId(id);
    const count = chip ? chip.querySelector(".bc-chip__count") : null;
    if (count) {
      count.textContent = numberValue(value) === null ? textValue(value) : formatCount(value);
    }
  }

  function renderErrors(report) {
    const expectedGroups = list(first(report, ["error_code_groups", "failure_groups"], []));
    const internalGroups = list(first(report, ["internal_error_groups"], []));
    const recentFailures = list(first(report, ["recent_failures"], []));
    const writeFailures = list(first(report, ["observation_write_failures"], []));
    const failureCount = first(report, ["failure_count"], expectedGroups.length);
    updateChipCount("expected-error-count", failureCount);
    updateChipCount("internal-error-count", internalGroups.length);
    updateChipCount("telemetry-error-count", writeFailures.length);
    renderExpectedErrorGroups(expectedGroups, recentFailures);
    renderInternalErrorGroups(internalGroups);
    renderTelemetryGroups(writeFailures);
  }

  function emptyGroupMessage(container, message) {
    container.replaceChildren(createElement("p", "timeline-empty", message));
  }

  function groupSummaryValue(label, value) {
    const wrapper = createElement("span");
    wrapper.append(
      createElement("span", "group-label", label),
      createElement("span", "group-value", value),
    );
    return wrapper;
  }

  function groupShell(title, count, firstSeen, lastSeen) {
    const details = createElement("details", "group-card");
    const summary = createElement("summary");
    summary.append(
      createElement("span", "group-title", title),
      groupSummaryValue("Count", formatCount(count)),
      groupSummaryValue("First seen", formatShortTime(firstSeen)),
      groupSummaryValue("Last seen", formatShortTime(lastSeen)),
    );
    const body = createElement("div", "group-body");
    details.append(summary, body);
    return { details, body };
  }

  function appendOccurrences(body, correlationIds) {
    const ids = [...new Set(list(correlationIds).filter((value) => validCorrelationId(value)))];
    if (ids.length === 0) {
      body.append(
        createElement(
          "p",
          "",
          "No bounded correlation occurrence is available in this response.",
        ),
      );
      return;
    }
    const label = createElement("p", "group-label", "Recent occurrences");
    const occurrences = createElement("ul", "occurrence-list");
    for (const correlationId of ids) {
      const item = createElement("li");
      item.append(openObservationButton(correlationId, correlationId, "occurrence-button"));
      occurrences.append(item);
    }
    body.append(label, occurrences);
  }

  function renderExpectedErrorGroups(groups, recentFailures) {
    const container = byId("expected-error-groups");
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (groups.length === 0) {
      emptyGroupMessage(container, "No expected or domain failures were grouped in this window.");
      return;
    }
    for (const rawGroup of groups) {
      const group = record(rawGroup);
      const errorCode = textValue(first(group, ["error_code", "code"], "Unclassified"), "Unclassified");
      const shell = groupShell(
        errorCode,
        first(group, ["count"], 0),
        first(group, ["first_seen"], null),
        first(group, ["last_seen"], null),
      );
      shell.body.append(
        createElement("p", "", `Affected tools: ${formatList(first(group, ["affected_tools", "tools"], []))}`),
        createElement(
          "p",
          "",
          `Affected projects: ${formatList(first(group, ["affected_projects", "projects"], []))}`,
        ),
      );
      const correlations = recentFailures
        .map((item) => record(item))
        .filter((item) => textValue(first(item, ["error_code"], "Unclassified")) === errorCode)
        .map((item) => first(item, ["correlation_id"], null));
      appendOccurrences(shell.body, correlations);
      container.append(shell.details);
    }
  }

  function renderInternalErrorGroups(groups) {
    const container = byId("internal-error-groups");
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (groups.length === 0) {
      emptyGroupMessage(container, "No internal error fingerprints were recorded in this window.");
      return;
    }
    for (const rawGroup of groups) {
      const group = record(rawGroup);
      const fingerprint = textValue(first(group, ["stack_fingerprint", "fingerprint"], "Unknown fingerprint"));
      const shell = groupShell(
        fingerprint,
        first(group, ["count"], 0),
        first(group, ["first_seen"], null),
        first(group, ["last_seen"], null),
      );
      shell.body.append(
        createElement("p", "", `Exception type: ${textValue(first(group, ["exception_type"], "Unknown"))}`),
        createElement("p", "", `Affected tools: ${formatList(first(group, ["affected_tools", "tools"], []))}`),
        createElement(
          "p",
          "",
          `Affected projects: ${formatList(first(group, ["affected_projects", "projects"], []))}`,
        ),
      );
      appendOccurrences(shell.body, list(first(group, ["correlation_ids", "occurrences"], [])));
      container.append(shell.details);
    }
  }

  function groupedTelemetryEvents(events) {
    const groups = new Map();
    for (const rawEvent of events) {
      const event = record(rawEvent);
      const key = [
        textValue(first(event, ["exception_type"], "Unknown")),
        textValue(first(event, ["stage"], "observation_persistence")),
        textValue(first(event, ["tool_name"], "Unknown tool")),
      ].join(" · ");
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          count: 0,
          firstSeen: null,
          lastSeen: null,
          correlationIds: [],
        });
      }
      const group = groups.get(key);
      group.count += 1;
      const timestamp = first(event, ["timestamp", "created_at"], null);
      if (group.firstSeen === null || String(timestamp) < String(group.firstSeen)) {
        group.firstSeen = timestamp;
      }
      if (group.lastSeen === null || String(timestamp) > String(group.lastSeen)) {
        group.lastSeen = timestamp;
      }
      const correlationId = first(event, ["correlation_id"], null);
      if (validCorrelationId(correlationId)) {
        group.correlationIds.push(correlationId);
      }
    }
    return [...groups.values()];
  }

  function renderTelemetryGroups(events) {
    const container = byId("telemetry-error-groups");
    if (!container) {
      return;
    }
    container.replaceChildren();
    const groups = groupedTelemetryEvents(events);
    if (groups.length === 0) {
      emptyGroupMessage(container, "No observation persistence failures were recorded.");
      return;
    }
    for (const group of groups) {
      const shell = groupShell(group.key, group.count, group.firstSeen, group.lastSeen);
      shell.body.append(
        createElement(
          "p",
          "",
          "These events indicate missing observability, not necessarily failed user work.",
        ),
      );
      appendOccurrences(shell.body, group.correlationIds);
      container.append(shell.details);
    }
  }

  function renderPerformance(report) {
    const latency = record(first(report, ["latency", "metrics"], {}));
    const responseSizes = record(first(report, ["response_sizes"], {}));
    const tools = list(first(report, ["calls_by_tool", "tools"], []));
    setText("performance-calls", formatCount(first(report, ["call_count"], 0)));
    setText("performance-p50", formatDuration(first(latency, ["p50_ms", "p50_duration_ms"], null)));
    setText("performance-p95", formatDuration(first(latency, ["p95_ms", "p95_duration_ms"], null)));
    setText("performance-max", formatDuration(first(latency, ["max_ms", "max_duration_ms"], null)));
    setText(
      "performance-response-p95",
      formatBytes(first(responseSizes, ["p95_bytes", "p95_result_bytes"], null)),
    );
    setText(
      "performance-latency-samples",
      `${formatCount(first(latency, ["sample_count"], 0))} latency samples`,
    );
    setText(
      "performance-size-samples",
      `${formatCount(first(responseSizes, ["sample_count"], 0))} size samples`,
    );
    renderToolPerformance(tools);
    renderOutlierTable("slowest-calls-body", list(first(report, ["slowest_calls"], [])), "duration");
    renderOutlierTable(
      "largest-responses-body",
      list(first(report, ["largest_responses", "largest_responses"], [])),
      "size",
    );
  }

  function renderToolPerformance(tools) {
    const body = byId("tool-performance-body");
    if (!body) {
      return;
    }
    body.replaceChildren();
    if (tools.length === 0) {
      const row = createElement("tr");
      const cell = appendCell(row, "No tool performance samples are available.");
      cell.colSpan = 7;
      body.append(row);
      return;
    }
    const maximumP95 = Math.max(
      0,
      ...tools.map((item) => {
        const latency = record(first(record(item), ["latency"], {}));
        return numberValue(first(latency, ["p95_ms", "p95_duration_ms"], 0)) || 0;
      }),
    );
    for (const rawTool of tools) {
      const tool = record(rawTool);
      const latency = record(first(tool, ["latency"], {}));
      const p95 = numberValue(first(latency, ["p95_ms", "p95_duration_ms"], null));
      const row = createElement("tr");
      appendCell(row, first(tool, ["dimension", "tool_name", "tool"], "Unknown"));
      appendCell(row, formatCount(first(tool, ["calls", "call_count", "sample_count"], 0)));
      appendCell(row, formatCount(first(tool, ["failures", "failure_count"], 0)));
      appendCell(row, formatDuration(first(latency, ["p50_ms", "p50_duration_ms"], null)));
      appendCell(row, formatDuration(p95));
      appendCell(row, formatDuration(first(latency, ["max_ms", "max_duration_ms"], null)));
      const barCell = createElement("td");
      barCell.append(createPerformanceBar(p95, maximumP95));
      row.append(barCell);
      body.append(row);
    }
  }

  function createPerformanceBar(value, maximum) {
    const wrapper = createElement("div", "performance-bar");
    const safeValue = numberValue(value) || 0;
    const level = maximum > 0 ? Math.max(0, Math.min(10, Math.round((safeValue / maximum) * 10))) : 0;
    const progress = createElement("div", "bc-progress bc-progress--tone");
    progress.setAttribute("role", "progressbar");
    progress.setAttribute("aria-valuemin", "0");
    progress.setAttribute("aria-valuemax", textValue(maximum, "0"));
    progress.setAttribute("aria-valuenow", textValue(safeValue, "0"));
    progress.setAttribute("aria-valuetext", `${formatDuration(safeValue)} relative P95 latency`);
    const fill = createElement("span", "bc-progress__fill progress-fill");
    fill.dataset.level = String(level);
    progress.append(fill);
    wrapper.append(progress);
    return wrapper;
  }

  function renderOutlierTable(bodyId, observations, metric) {
    const body = byId(bodyId);
    if (!body) {
      return;
    }
    body.replaceChildren();
    if (observations.length === 0) {
      const row = createElement("tr");
      const cell = appendCell(
        row,
        metric === "duration" ? "No latency outliers are available." : "No response-size outliers are available.",
      );
      cell.colSpan = 4;
      body.append(row);
      return;
    }
    for (const rawObservation of observations) {
      const observation = record(rawObservation);
      const row = createElement("tr");
      const timeCell = createElement("td");
      const correlationId = textValue(first(observation, ["correlation_id"], ""), "");
      const label = formatShortTime(first(observation, ["created_at", "timestamp"], null));
      if (correlationId) {
        timeCell.append(openObservationButton(correlationId, label));
      } else {
        timeCell.textContent = label;
      }
      row.append(timeCell);
      appendCell(row, first(observation, ["tool_name", "tool"], "Unknown"));
      const statusCell = createElement("td");
      statusCell.append(createStatusChip(observation));
      row.append(statusCell);
      appendCell(
        row,
        metric === "duration"
          ? formatDuration(first(observation, ["duration_ms"], null))
          : formatBytes(first(observation, ["result_bytes"], null)),
      );
      body.append(row);
    }
  }

  function lifecycleTone(status) {
    if (status === "started" || status === "client_initialized" || status === "activity_observed") {
      return "success";
    }
    if (status === "transport_closed" || status === "unknown" || status === "never_started") {
      return "caution";
    }
    if (status === "stopped_cleanly") {
      return "neutral";
    }
    return "neutral";
  }

  function renderRuntime(report) {
    const lifecycle = record(first(report, ["lifecycle", "state"], {}));
    const status = textValue(first(lifecycle, ["status", "state"], "unknown"), "unknown");
    const certainty = textValue(first(lifecycle, ["certainty"], "unknown"), "unknown");
    setStatusCard(
      "lifecycle-card",
      "lifecycle-status",
      "lifecycle-copy",
      lifecycleTone(status),
      `${humanize(status)} · ${humanize(certainty)} certainty`,
      first(lifecycle, ["description"], "Event history does not prove current process liveness."),
    );
    const metadata = byId("lifecycle-metadata");
    if (metadata) {
      metadata.replaceChildren();
      appendDefinition(metadata, "Server boot", first(lifecycle, ["server_boot_id", "boot_id"], "Unknown"), true);
      appendDefinition(metadata, "Process ID", first(lifecycle, ["process_id", "pid"], "Unknown"), true);
      appendDefinition(metadata, "State observed", formatTimestamp(first(lifecycle, ["observed_at"], null)));
      appendDefinition(
        metadata,
        "Latest observation",
        formatTimestamp(first(lifecycle, ["latest_observation_at"], null)),
      );
      appendDefinition(
        metadata,
        "Prior boots without clean stop",
        formatCount(first(lifecycle, ["prior_boots_without_clean_stop"], 0)),
      );
    }

    const logAvailable = first(report, ["log_available"], null);
    const malformed = numberValue(first(report, ["malformed_lines"], 0)) || 0;
    const oversized = numberValue(first(report, ["oversized_lines"], 0)) || 0;
    const logChip = byId("runtime-log-chip");
    setTone(logChip, logAvailable === true ? "success" : logAvailable === false ? "caution" : "neutral");
    updateChipCount("runtime-log-chip", logAvailable === true ? "available" : logAvailable === false ? "missing" : "unknown");
    const malformedChip = byId("runtime-malformed-chip");
    setTone(malformedChip, malformed + oversized > 0 ? "caution" : "neutral");
    updateChipCount("runtime-malformed-chip", malformed + oversized);
    renderTimeline(list(first(report, ["events"], [])), logAvailable);
  }

  function eventTone(eventName) {
    if (eventName === "internal_error" || eventName === "observation_write_failed") {
      return "danger";
    }
    if (eventName === "malformed_request" || eventName === "request_too_large") {
      return "caution";
    }
    if (eventName === "process_started" || eventName === "client_initialized") {
      return "success";
    }
    return "neutral";
  }

  function runtimeEventDescription(event) {
    const eventName = textValue(first(event, ["event", "kind"], "runtime_event"));
    if (eventName === "process_started") {
      return `Process started for ${textValue(first(event, ["transport"], "unknown"))} transport using package ${textValue(first(event, ["package_version"], "unknown"))}.`;
    }
    if (eventName === "client_initialized") {
      return `Client ${formatClient(event)} initialized with protocol ${textValue(first(event, ["protocol_version"], "unknown"))}.`;
    }
    if (eventName === "transport_closed") {
      return `Transport closed with reason ${textValue(first(event, ["reason"], "unknown"))}; process liveness is not inferred.`;
    }
    if (eventName === "process_stopping") {
      return `Process entered a recorded stop path with reason ${textValue(first(event, ["reason"], "unknown"))}.`;
    }
    if (eventName === "internal_error") {
      return `Internal exception ${textValue(first(event, ["exception_type"], "unknown"))}; fingerprint ${textValue(first(event, ["stack_fingerprint"], "unknown"))}; correlation ${textValue(first(event, ["correlation_id"], "unknown"))}.`;
    }
    if (eventName === "observation_write_failed") {
      return `Observation persistence failed at ${textValue(first(event, ["stage"], "unknown"))} for tool ${textValue(first(event, ["tool_name"], "unknown"))}; correlation ${textValue(first(event, ["correlation_id"], "unknown"))}.`;
    }
    if (eventName === "malformed_request") {
      return `A malformed request was rejected (${textValue(first(event, ["exception_type"], "unknown"))}).`;
    }
    if (eventName === "request_too_large") {
      return `A request of at least ${formatBytes(first(event, ["received_at_least_bytes"], null))} exceeded the ${formatBytes(first(event, ["limit_bytes"], null))} limit.`;
    }
    return "A safe runtime event was recorded.";
  }

  function renderTimeline(events, logAvailable) {
    const timeline = byId("runtime-timeline");
    if (!timeline) {
      return;
    }
    timeline.replaceChildren();
    if (events.length === 0) {
      timeline.append(
        createElement(
          "li",
          "timeline-empty",
          logAvailable === false
            ? "The private runtime log is unavailable. No claim about process state can be made."
            : "No safe runtime or lifecycle events have been recorded.",
        ),
      );
      return;
    }
    for (const rawEvent of events) {
      const event = record(rawEvent);
      const eventName = textValue(first(event, ["event", "kind"], "runtime_event"));
      const item = createElement("li", "timeline-item");
      const marker = createElement("div", "timeline-marker");
      const dot = createElement("span", `bc-dot ${TONE_CLASS[eventTone(eventName)]}`);
      dot.setAttribute("aria-hidden", "true");
      marker.append(dot);
      const identity = createElement("div");
      identity.append(
        createElement("p", "timeline-kind", eventName),
        createElement("p", "timeline-time", formatTimestamp(first(event, ["timestamp", "created_at"], null))),
      );
      const detail = createElement("div");
      detail.append(createElement("p", "timeline-copy", runtimeEventDescription(event)));
      const metadata = createElement("div", "timeline-metadata");
      metadata.append(
        createElement("span", "", `Boot: ${textValue(first(event, ["server_boot_id", "boot_id"], "unknown"))}`),
        createElement("span", "", `PID: ${textValue(first(event, ["process_id", "pid"], "unknown"))}`),
      );
      detail.append(metadata);
      item.append(marker, identity, detail);
      timeline.append(item);
    }
  }

  function readCallFilters() {
    state.callFilters.project = byId("calls-project").value.trim();
    state.callFilters.tool = byId("calls-tool").value.trim();
    state.callFilters.client = byId("calls-client").value.trim();
    state.callFilters.status = byId("calls-status").value;
    state.callFilters.errorCode = byId("calls-error-code").value.trim();
    state.callOffset = 0;
  }

  function synchronizeWindow(value) {
    if (!VALID_WINDOWS.has(value)) {
      return;
    }
    state.window = value;
    byId("window-select").value = value;
    byId("calls-window-select").value = value;
    state.callOffset = 0;
  }

  function installEventHandlers() {
    byId("refresh-button").addEventListener("click", () => {
      void refreshActiveView();
    });
    byId("theme-toggle").addEventListener("click", () => {
      const nextTheme = document.documentElement.classList.contains("light") ? "dark" : "light";
      applyTheme(nextTheme);
    });
    byId("window-select").addEventListener("change", (event) => {
      synchronizeWindow(event.currentTarget.value);
      void refreshActiveView();
    });
    byId("calls-window-select").addEventListener("change", (event) => {
      synchronizeWindow(event.currentTarget.value);
      void refreshActiveView();
    });
    byId("calls-filters").addEventListener("submit", (event) => {
      event.preventDefault();
      readCallFilters();
      void refreshActiveView();
    });
    byId("clear-calls-filters").addEventListener("click", () => {
      byId("calls-filters").reset();
      synchronizeWindow(state.window);
      readCallFilters();
      void refreshActiveView();
    });
    byId("calls-previous").addEventListener("click", () => {
      state.callOffset = Math.max(0, state.callOffset - CALL_LIMIT);
      void refreshActiveView();
    });
    byId("calls-next").addEventListener("click", () => {
      state.callOffset += CALL_LIMIT;
      void refreshActiveView();
    });
    byId("correlation-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const correlationId = byId("correlation-input").value.trim();
      if (window.location.hash !== "#calls") {
        window.location.hash = "#calls";
      }
      void loadObservation(correlationId);
    });
    byId("copy-correlation").addEventListener("click", async () => {
      if (!state.selectedCorrelation) {
        return;
      }
      try {
        await navigator.clipboard.writeText(state.selectedCorrelation);
        setText("detail-status", "Correlation ID copied to the clipboard.");
      } catch (_error) {
        setText("detail-status", "Clipboard access was unavailable; select the correlation ID to copy it.");
      }
    });
    byId("close-detail").addEventListener("click", () => {
      byId("observation-detail").hidden = true;
      byId("correlation-input").focus();
    });
    window.addEventListener("hashchange", () => {
      activateView(selectedRoute());
      void refreshActiveView();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearPollTimer();
        setConnection("neutral", "Polling paused while this tab is hidden", false);
      } else {
        void refreshActiveView();
      }
    });
  }

  function start() {
    initializeTheme();
    synchronizeWindow("24h");
    activateView(selectedRoute());
    installEventHandlers();
    void refreshActiveView();
  }

  start();
})();
