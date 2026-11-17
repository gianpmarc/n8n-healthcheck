/* n8n-healthcheck web interface. No build step: this file is served as it is.
 *
 * Rules this file keeps:
 * - Text from the API or from the user is only ever inserted as text nodes.
 * - No form value goes to the URL, to browser storage or to the console.
 */
"use strict";

(() => {
  const MAX_BODY_BYTES = 1024 * 1024; // same limit as the server
  // The server gives up on each source after a few seconds, so a run ends well within this.
  const RUN_TIMEOUT_MS = 3 * 60 * 1000;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const $ = (id) => document.getElementById(id);

  /** Create an element. Children are nodes or strings; strings become text nodes. */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === false || value == null) continue;
      if (name === "class") node.className = value;
      else node.setAttribute(name, value === true ? "" : String(value));
    }
    node.append(...children.filter((child) => child != null));
    return node;
  }

  // ── Wording ──────────────────────────────────────────────────────────

  const SEVERITIES = ["critical", "warning", "info"];
  const SEVERITY = {
    critical: { one: "Critical", group: "Critical" },
    warning: { one: "Warning", group: "Warnings" },
    info: { one: "Info", group: "Info" },
  };

  const ICONS = {
    critical: "M8.3 3h7.4L21 8.3v7.4L15.7 21H8.3L3 15.7V8.3z M9 9l6 6 M15 9l-6 6",
    warning: "M12 3.5 22 20.5H2z M12 10v5 M12 17.7v.3",
    info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 11v6 M12 7.5v.3",
    clean: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M8 12.5l2.8 2.8L16 9.5",
    unknown: "M9.6 9.5a2.5 2.5 0 1 1 3.6 2.3c-.8.4-1.2 1-1.2 1.9 M12 17v.3",
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const attrs = { fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" };
    const draw = (d, extra) => {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      for (const [key, value] of Object.entries({ ...attrs, ...extra })) path.setAttribute(key, value);
      svg.append(path);
    };
    if (name === "unknown" || name === "partial-clean") {
      // A dashed outline: the shape itself says "incomplete", not only the colour.
      draw("M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", { "stroke-dasharray": "3.2 3.2" });
      draw(name === "unknown" ? ICONS.unknown : "M8 12.5l2.8 2.8L16 9.5");
    } else {
      draw(ICONS[name] || ICONS.info);
    }
    return svg;
  }

  // Snapshot section id -> our label, the form source that feeds it, and what "not provided" means.
  const SECTIONS = {
    health: { label: "n8n health endpoints", source: "url", missing: "No n8n URL was given." },
    metrics: { label: "Prometheus metrics", source: "url", missing: "No n8n URL was given, so the metrics endpoint was not read." },
    env: { label: "n8n environment variables", source: "env", missing: "No environment file was given." },
    postgres: { label: "Postgres database", source: "postgres", missing: "No Postgres connection string was given." },
    redis: { label: "Redis queue", source: "redis", missing: "No Redis connection URL was given." },
    kubernetes: { label: "Kubernetes workloads", source: "kubernetes", missing: "No Kubernetes namespace was given." },
  };

  const REASONS = {
    not_provided: { tag: "Not provided" },
    failed: {
      tag: "Could not be read",
      why: "This source was requested but could not be read, so the checks that need it did not run. The messages from the collectors below say why.",
    },
    disabled: {
      tag: "Switched off on the instance",
      why: "The metrics endpoint is switched off on this n8n instance, so there was nothing to read. Set N8N_METRICS=true on n8n to include queue and performance metrics.",
    },
    from_kubernetes: {
      tag: "Read from Kubernetes instead",
      why: "No environment file was given. The variables were read from the n8n Deployment in Kubernetes, so the checks on the environment still ran.",
    },
    rule_skipped: {
      tag: "Check skipped",
      why: "This check failed while it was running and was skipped, so the risk it covers is unknown. Please report it as a bug in n8n-healthcheck.",
    },
  };

  const plural = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;
  const capitalize = (text) => String(text).charAt(0).toUpperCase() + String(text).slice(1);

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} bytes`;
    const unit = bytes < 1024 * 1024 ? ["KB", 1024] : ["MB", 1024 * 1024];
    return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(bytes / unit[1])} ${unit[0]}`;
  }

  // ── State ────────────────────────────────────────────────────────────

  let meta = null; // GET /api/meta
  let running = false;
  let envFile = null; // { name, size, text } — the chosen file, kept in memory only
  let timer = null;
  let report = null; // last 200 response of POST /api/run, plus finishedAt
  const filters = { severity: "all", area: "all" };

  function announce(text) {
    const region = $("announce");
    region.textContent = "";
    // A fresh text node after a tick, so the same message is announced twice in a row.
    setTimeout(() => { region.textContent = text; }, 50);
  }

  // ── Sources (GET /api/meta) ──────────────────────────────────────────

  async function loadMeta() {
    $("meta-error").hidden = true;
    $("meta-loading").hidden = false;
    let problem = null;
    try {
      const response = await fetch("/api/meta", { cache: "no-store" });
      const data = await response.json().catch(() => null);
      if (response.ok && data && Array.isArray(data.sources)) meta = data;
      else problem = data && typeof data.error === "string"
        ? `The server answered: ${data.error}.`
        : `The server answered with HTTP ${response.status}.`;
    } catch {
      problem = "The local server did not answer. Check that “n8n-healthcheck serve” is still running.";
    }
    $("meta-loading").hidden = true;
    if (problem) {
      $("meta-error-text").textContent = problem;
      $("meta-error").hidden = false;
      return;
    }
    applyMeta();
    $("run-form").hidden = false;
  }

  function applyMeta() {
    if (meta.version) $("tool-version").textContent = `n8n-healthcheck ${meta.version}`;
    for (const fieldset of document.querySelectorAll("fieldset[data-source]")) {
      const source = meta.sources.find((item) => item.id === fieldset.dataset.source);
      // A source the server does not offer cannot be sent, so it is not shown.
      fieldset.hidden = !source;
      fieldset.disabled = !source || !source.available;
      if (!source) continue;
      const hint = fieldset.querySelector(".source__hint");
      hint.id = `hint-${source.id}`;
      hint.textContent = source.hint || "";
      fieldset.setAttribute("aria-describedby", hint.id);
      fieldset.querySelector(".source__name").textContent = source.label || source.id;
      fieldset.querySelector(".source__state").textContent = source.available ? "" : "Unavailable on the n8n-healthcheck machine";
      fieldset.classList.toggle("source--unavailable", !source.available);
    }
  }

  // ── Environment file ────────────────────────────────────────────────

  async function chooseEnvFile() {
    const input = $("f-env-file");
    const file = input.files && input.files[0];
    if (!file) return;
    setFieldError("e-env", null);
    let problem = null;
    let text = "";
    if (file.size > MAX_BODY_BYTES) {
      problem = `“${file.name}” is ${formatBytes(file.size)}. The limit is 1 MB. Choose the n8n env file itself (KEY=VALUE lines), not a log or an archive.`;
    } else {
      try {
        text = await file.text();
        if (text.includes("\u0000")) problem = `“${file.name}” does not look like a text file. Choose a file with KEY=VALUE lines.`;
        else if (!text.trim()) problem = `“${file.name}” is empty. Choose a file with KEY=VALUE lines.`;
      } catch {
        problem = `“${file.name}” could not be read. Check its permissions, or paste the variables instead.`;
      }
    }
    input.value = ""; // so choosing the same file again fires "change"
    if (problem) {
      setFieldError("e-env", problem, [input]);
      input.focus();
      return;
    }
    envFile = { name: file.name, size: file.size, text };
    const lines = text.split("\n").filter((line) => line.trim()).length;
    $("env-chip-text").textContent = `${file.name} — ${formatBytes(file.size)}, ${plural(lines, "line")}. Kept in memory until you remove it or reload the page.`;
    showEnvFile(true);
    $("env-chip-remove").focus();
    announce(`${file.name} loaded.`);
  }

  function showEnvFile(loaded) {
    $("env-chip").hidden = !loaded;
    $("env-paste").hidden = loaded;
    $("env-pick").hidden = loaded;
  }

  function removeEnvFile() {
    envFile = null;
    showEnvFile(false);
    $("f-env-file").focus();
    announce("File removed.");
  }

  // ── Validation and request ──────────────────────────────────────────

  function setFieldError(errorId, message, inputs) {
    const box = $(errorId);
    box.textContent = message || "";
    box.hidden = !message;
    const targets = inputs || document.querySelectorAll(`[aria-describedby="${errorId}"]`);
    for (const input of targets) {
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }

  function clearErrors() {
    for (const id of ["e-url", "e-env", "e-postgres", "e-redis", "e-kube"]) setFieldError(id, null);
    $("form-error").hidden = true;
  }

  function showFormError(title, text, canRetry) {
    $("form-error-title").textContent = title;
    $("form-error-text").textContent = text;
    $("form-error-actions").hidden = !canRetry;
    $("form-error").hidden = false;
  }

  /** Value of an input, or "" when its source is unavailable. */
  function value(id) {
    const input = $(id);
    return input.closest("fieldset").disabled ? "" : input.value;
  }

  /** Build the request body and the list of problems that stop it from being sent. */
  function buildRequest() {
    const body = {};
    const errors = []; // { errorId, message, focus, inputs }

    const url = value("f-url").trim();
    if (url) {
      let valid = false;
      // Same rule as the server: http or https, with a host.
      try {
        const parsed = new URL(url);
        valid = ["http:", "https:"].includes(parsed.protocol) && parsed.hostname !== "" && /^https?:\/\/[^\/?#]/i.test(url);
      } catch { /* not a URL */ }
      if (valid) body.url = url;
      else errors.push({ errorId: "e-url", focus: "f-url", message: "Enter the full address, starting with http:// or https:// — for example https://n8n.example.com." });
    }

    const envOff = $("f-env").closest("fieldset").disabled;
    const envText = envOff ? "" : envFile ? envFile.text : $("f-env").value;
    if (envText.trim()) body.env_text = envText;

    const postgres = value("f-postgres").trim();
    if (postgres) body.postgres_dsn = postgres;
    const redis = value("f-redis").trim();
    if (redis) body.redis_url = redis;

    const namespace = value("f-kube-ns").trim();
    const context = value("f-kube-ctx").trim();
    if (namespace) body.kube_namespace = namespace;
    if (context && namespace) body.kube_context = context;
    if (context && !namespace) {
      errors.push({ errorId: "e-kube", focus: "f-kube-ns", inputs: [$("f-kube-ns")], message: "Add the namespace to inspect, or clear the context. A context alone is not a source." });
    }

    const size = new TextEncoder().encode(JSON.stringify(body)).length;
    if (size > MAX_BODY_BYTES) {
      errors.push({
        errorId: "e-env",
        focus: envFile ? "env-chip-remove" : "f-env",
        inputs: envFile ? [] : [$("f-env")],
        message: `The request would be ${formatBytes(size)}; the limit is 1 MB. Shorten the environment text to the n8n variables only.`,
      });
    }
    return { body, errors, empty: Object.keys(body).length === 0 };
  }

  function setRunning(on) {
    running = on;
    const button = $("run-button");
    if (on) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
    $("run-button-label").textContent = on ? "Running health check…" : "Run health check";
    $("run-progress").hidden = !on;
    clearInterval(timer);
    if (on) {
      const started = Date.now();
      const tick = () => {
        const seconds = Math.round((Date.now() - started) / 1000);
        $("run-elapsed").textContent = seconds < 2 ? "Reading your sources…" : `Reading your sources… ${seconds} s so far.`;
      };
      tick();
      timer = setInterval(tick, 1000);
    }
  }

  /** Plain-language title and text for a response that is not a report. */
  function describeFailure(status, serverText) {
    const said = serverText ? `The server said: “${serverText}”.` : "";
    switch (status) {
      case 409:
        return ["A health check is already running.", "The server runs one check at a time. Wait a few seconds for the other one to finish, then try again."];
      case 400:
      case 413:
      case 415:
        return ["The server did not accept the request.", `${said} Correct the form and try again.`.trim()];
      case 403:
        return ["The server refused the request.", `${said} Open this page with the exact address that “n8n-healthcheck serve” printed when it started, then try again.`.trim()];
      case 500:
        return ["The health check failed on the server.", `${said} The terminal running “n8n-healthcheck serve” shows what went wrong. Your form is unchanged; you can try again.`.trim()];
      default:
        return ["The server sent an unexpected answer.", `HTTP ${status}. ${said} Try again; if it keeps happening, restart “n8n-healthcheck serve”.`.replace("  ", " ")];
    }
  }

  async function submit() {
    if (running) return; // no double submit
    clearErrors();
    const { body, errors, empty } = buildRequest();
    if (errors.length) {
      for (const error of errors) setFieldError(error.errorId, error.message, error.inputs);
      $(errors[0].focus).focus();
      announce(`${plural(errors.length, "problem")} in the form. ${errors[0].message}`);
      return;
    }
    if (empty) {
      showFormError("Nothing to inspect yet.", "Fill in at least one source above, then run the health check.", false);
      // The message sits next to the button; moving focus to a field would scroll it out of view.
      $("form-error").focus();
      return;
    }

    setRunning(true);
    announce("Health check running. This can take up to a minute.");
    let failure = null;
    let data = null;
    const abort = new AbortController();
    const deadline = setTimeout(() => abort.abort(), RUN_TIMEOUT_MS);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
        referrerPolicy: "no-referrer",
        signal: abort.signal,
      });
      data = await response.json().catch(() => null);
      // The deadline can also pass while the body is still arriving; that is a timeout, not a bad report.
      if (abort.signal.aborted) throw new Error("timeout");
      if (!response.ok) {
        failure = describeFailure(response.status, data && typeof data.error === "string" ? data.error : "");
      } else if (!data || !Array.isArray(data.findings) || !Array.isArray(data.scores)) {
        failure = ["The server sent a report this page could not read.", "Try again; if it keeps happening, the page and the server may be different versions — restart “n8n-healthcheck serve” and reload."];
      }
    } catch {
      failure = abort.signal.aborted
        ? ["The health check did not finish within 3 minutes.", "This page stopped waiting. The server may still be working on it: wait a moment, then try again. If it keeps happening, restart “n8n-healthcheck serve”."]
        : ["The server could not be reached.", "Check that “n8n-healthcheck serve” is still running, then try again. Your form is unchanged."];
    }
    clearTimeout(deadline);
    setRunning(false);

    if (failure) {
      showFormError(failure[0], failure[1], true);
      $("form-error").focus();
      return;
    }
    report = { ...data, finishedAt: new Date() };
    renderReport();
    showView("report");
  }

  function showView(name) {
    $("view-form").hidden = name !== "form";
    $("view-report").hidden = name !== "report";
    window.scrollTo(0, 0);
    $(name === "form" ? "form-title" : "report-title").focus({ preventScroll: true });
  }

  // ── Report ──────────────────────────────────────────────────────────

  function statusOf(data) {
    const noData = data.scores.length > 0 && data.scores.every((row) => row.score == null);
    switch (data.exit_code) {
      case 2: return { kind: "critical", icon: "critical", label: "Critical issues found" };
      case 1: return { kind: "warning", icon: "warning", label: "Warnings found" };
      case 0:
        // Exit code 0 still allows informational findings, so "no issues" would contradict the list.
        return data.partial
          ? { kind: "partial-clean", icon: "partial-clean", label: `${data.findings.length ? "No critical issues or warnings" : "No issues found"} in the part that was checked` }
          : { kind: "clean", icon: "clean", label: data.findings.length ? "No critical issues or warnings" : "No issues found" };
      default:
        return {
          kind: "error",
          icon: "unknown",
          label: "The check could not run",
          // With no data the API summary already says the check could not run and why;
          // when every check crashed it still reads "found 0 issues", so ours leads.
          lead: noData ? null : "No check was able to run on the data that was collected, so there is no result to rely on. See “Not assessed” below.",
          note: noData ? "“Not assessed” and the messages from the collectors below have the details. Use “Run again” to correct the sources." : null,
        };
    }
  }

  function renderReport() {
    const data = report;
    const status = statusOf(data);
    const findings = data.findings;

    const when = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(data.finishedAt);
    const version = data.n8n_version ? `n8n ${data.n8n_version}` : "n8n version not detected";
    $("report-meta").textContent = [version, `Checked ${when}`, meta && meta.version ? `n8n-healthcheck ${meta.version}` : null].filter(Boolean).join(" · ");
    $("action-status").textContent = "";

    // Overall status: icon shape + words + border style, never colour alone.
    $("status").className = `status status--${status.kind}`;
    $("status-icon").replaceChildren(icon(status.icon));
    $("status-label").textContent = status.label;
    $("status-partial").hidden = !data.partial || status.kind === "error";
    const note = status.lead ? data.summary : status.note;
    $("summary").textContent = status.lead || data.summary || "";
    $("status-note").textContent = note || "";
    $("status-note").hidden = !note;

    renderScores(data);
    renderFindings(data, status);
    renderNotAssessed(data);

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    $("warnings-block").hidden = warnings.length === 0;
    $("warnings").replaceChildren(...warnings.map((line) => el("li", null, String(line))));

    const counts = SEVERITIES.map((severity) => findings.filter((f) => f.severity === severity).length);
    announce(status.kind === "error"
      ? `${status.label}. Nothing was assessed.`
      : `Report ready. ${status.label}${data.partial ? " (partial report)" : ""}. ${plural(counts[0], "critical issue")}, ${plural(counts[1], "warning")}, ${plural(counts[2], "informational note")}.`);
  }

  function renderScores(data) {
    const rows = data.scores.map((row) => {
      const count = Number(row.findings) || 0;
      const footer = el("p", { class: "score__findings" }, count ? plural(count, "finding") : "No findings");
      if (row.score == null) {
        return el("li", { class: "score score--none" },
          el("p", { class: "score__area" }, String(row.area)),
          el("p", { class: "score__value" }, el("span", { class: "score__number" }, "Not scored")),
          el("div", { class: "score__bar", "aria-hidden": "true" }),
          el("p", { class: "score__findings" }, "No data was collected"));
      }
      const fill = el("div", { class: "score__fill" });
      fill.style.setProperty("--score", String(Math.max(0, Math.min(100, Number(row.score) || 0))));
      return el("li", { class: `score${row.partial ? " score--partial" : ""}` },
        el("p", { class: "score__area" }, String(row.area)),
        el("p", { class: "score__value" },
          el("span", { class: "score__number" }, String(row.score)),
          el("span", { class: "score__of" }, "/ 100"),
          row.partial ? el("span", { class: "tag tag--partial" }, "Partial") : null),
        el("div", { class: "score__bar", "aria-hidden": "true" }, fill),
        footer);
    });
    $("scores").replaceChildren(...rows);

    const noData = data.scores.every((row) => row.score == null);
    const note = $("scores-note");
    note.hidden = !(noData || data.partial);
    note.textContent = noData
      ? "No score is shown because no data was collected."
      : "Each area starts at 100 and loses points for every finding. Scores marked “Partial” reflect only the checks that ran: they say nothing about what is listed under “Not assessed”.";
  }

  function severityTag(severity) {
    const known = SEVERITY[severity];
    return el("span", { class: `tag tag--${known ? severity : "area"}` }, icon(known ? severity : "info"), known ? known.one : String(severity));
  }

  function renderFindings(data, status) {
    const findings = data.findings;
    filters.severity = "all";
    filters.area = "all";
    $("findings-toolbar").hidden = findings.length === 0;

    // Severity filter: toggle buttons with counts.
    const severities = [...SEVERITIES, ...new Set(findings.map((f) => f.severity).filter((s) => !SEVERITIES.includes(s)))];
    const options = [["all", "All", findings.length], ...severities.map((s) => [s, SEVERITY[s] ? SEVERITY[s].group : capitalize(s), findings.filter((f) => f.severity === s).length])];
    $("severity-filter").replaceChildren(...options.filter(([key, , count]) => key === "all" || count > 0).map(([key, label, count]) =>
      el("button", { type: "button", class: "btn btn--small", "data-severity": key, "aria-pressed": key === "all" ? "true" : "false" }, `${label} (${count})`)));

    // Area filter.
    const areas = [...new Set([...data.scores.map((row) => row.area), ...findings.map((f) => f.area)])];
    $("area-filter").replaceChildren(
      el("option", { value: "all" }, "All areas"),
      ...areas.map((area) => el("option", { value: area }, `${capitalize(area)} (${findings.filter((f) => f.area === area).length})`)));

    // Groups, most severe first; the API already orders findings inside a severity.
    // The most severe group that has findings starts expanded; the rest start collapsed.
    const mostSevere = severities.find((severity) => findings.some((f) => f.severity === severity));
    let index = 0;
    const groups = severities.filter((severity) => findings.some((f) => f.severity === severity)).map((severity) => {
      const items = findings.filter((f) => f.severity === severity).map((finding) => {
        const bodyId = `finding-body-${index++}`;
        const open = severity === mostSevere;
        const toggle = el("button", { type: "button", class: "finding__toggle", "aria-expanded": String(open), "aria-controls": bodyId },
          el("span", { class: "chevron", "aria-hidden": "true" }),
          el("span", { class: "finding__head" },
            el("span", { class: "finding__title" }, String(finding.title)),
            el("span", { class: "finding__tags" },
              severityTag(severity),
              el("span", { class: "finding__id", translate: "no" }, String(finding.rule_id)),
              el("span", { class: "tag tag--area" }, capitalize(finding.area)))));
        const body = el("dl", { class: "finding__body", id: bodyId, hidden: !open },
          el("dt", null, "Evidence"), el("dd", { class: "evidence" }, String(finding.evidence)),
          el("dt", null, "Impact"), el("dd", null, String(finding.impact)),
          el("dt", null, "Recommendation"), el("dd", null, String(finding.recommendation)));
        return el("li", { class: `finding finding--${SEVERITY[severity] ? severity : "info"}`, "data-severity": severity, "data-area": finding.area }, toggle, body);
      });
      const title = SEVERITY[severity] ? SEVERITY[severity].group : capitalize(severity);
      return el("div", { class: "sev-group", "data-severity": severity },
        el("h4", null, `${title} (${items.length})`),
        el("ul", null, ...items));
    });
    $("findings").replaceChildren(...groups);

    $("findings-empty").dataset.none = findings.length
      ? ""
      : status.kind === "error"
        ? "No findings, because nothing could be checked."
        : data.partial
          ? "No findings in the part that was checked. That is not a clean bill of health: see “Not assessed” below."
          : "No findings. Every check passed.";
    applyFilters(false);
  }

  function applyFilters(announceCount) {
    let shown = 0;
    let total = 0;
    for (const group of document.querySelectorAll("#findings .sev-group")) {
      let visible = 0;
      for (const item of group.querySelectorAll(".finding")) {
        total += 1;
        const match = (filters.severity === "all" || item.dataset.severity === filters.severity)
          && (filters.area === "all" || item.dataset.area === filters.area);
        item.hidden = !match;
        if (match) visible += 1;
      }
      group.hidden = visible === 0;
      shown += visible;
    }
    for (const button of document.querySelectorAll("#severity-filter button")) {
      button.setAttribute("aria-pressed", String(button.dataset.severity === filters.severity));
    }
    $("area-filter").value = filters.area;

    const filtered = filters.severity !== "all" || filters.area !== "all";
    const empty = $("findings-empty");
    empty.hidden = shown > 0;
    empty.classList.toggle("empty--filtered", total > 0); // not printed: paper shows every finding
    $("findings-empty-text").textContent = total === 0 ? empty.dataset.none : "No findings match these filters.";
    $("clear-filters").hidden = total === 0;
    const count = total === 0 ? "" : filtered ? `Showing ${shown} of ${plural(total, "finding")}.` : `${plural(total, "finding")}.`;
    // Only speak after the reader changed a filter, not on first render.
    $("findings-count").textContent = announceCount || !filtered ? count : "";
  }

  function setAllExpanded(open) {
    for (const toggle of document.querySelectorAll(".finding__toggle")) {
      toggle.setAttribute("aria-expanded", String(open));
      $(toggle.getAttribute("aria-controls")).hidden = !open;
    }
  }

  function renderNotAssessed(data) {
    const entries = Array.isArray(data.not_assessed) ? data.not_assessed : [];
    $("na-intro").textContent = entries.length
      ? "These parts were not read. Unless an entry says otherwise, the report says nothing about them: a missing part is unknown, not healthy."
      : "Every data source was collected and every check ran.";
    $("not-assessed").hidden = entries.length === 0;
    $("not-assessed").replaceChildren(...entries.map((entry) => {
      const section = SECTIONS[entry.id];
      const reason = REASONS[entry.reason] || { tag: String(entry.reason), why: "" };
      let label = section ? section.label : String(entry.id);
      let why = reason.why;
      let tag = reason.tag;
      let detail = entry.detail ? String(entry.detail) : "";
      if (entry.reason === "rule_skipped") {
        // id is ENGINE-SKIPPED here; the API label names the check that failed.
        label = "A check could not run";
        detail = [entry.label, entry.detail].filter(Boolean).join(" — ");
      } else if (entry.reason === "not_provided" && section) {
        const source = meta && meta.sources.find((item) => item.id === section.source);
        if (source && !source.available) {
          tag = "Unavailable on the n8n-healthcheck machine";
          why = `${source.hint} Run n8n-healthcheck where that tool is installed to include it.`;
        } else {
          why = `${section.missing} Add it on the form and run again to include it.`;
        }
        detail = "";
      } else if (entry.reason === "failed" || entry.reason === "from_kubernetes" || entry.reason === "disabled") {
        detail = ""; // our own sentence already says it
      }
      return el("li", null,
        el("p", { class: "na__head" },
          el("span", { class: "na__label" }, label),
          el("span", { class: `tag ${entry.reason === "failed" || entry.reason === "rule_skipped" ? "tag--failed" : "tag--reason"}` }, tag)),
        why ? el("p", { class: "na__why" }, why) : null,
        detail ? el("p", { class: "na__detail" }, detail) : null);
    }));
  }

  // ── Report actions ──────────────────────────────────────────────────

  function fileStamp(date) {
    const two = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}${two(date.getMonth() + 1)}${two(date.getDate())}-${two(date.getHours())}${two(date.getMinutes())}`;
  }

  function download(filename, type, text) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const link = el("a", { href: url, download: filename, hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    $("action-status").textContent = `Download started: ${filename}`;
  }

  async function copyMarkdown() {
    let copied = false;
    try {
      await navigator.clipboard.writeText(report.markdown || "");
      copied = true;
    } catch {
      // Older browsers, or the clipboard permission was refused.
      const area = el("textarea", { class: "sr-only", readonly: true, "aria-hidden": "true", tabindex: "-1" });
      area.value = report.markdown || "";
      document.body.append(area);
      area.select();
      try { copied = document.execCommand("copy"); } catch { copied = false; }
      area.remove();
      $("act-copy").focus();
    }
    $("action-status").textContent = copied
      ? "Markdown report copied to the clipboard."
      : "The browser did not allow copying. Use “Download Markdown” instead.";
  }

  // ── Wiring ──────────────────────────────────────────────────────────

  $("run-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  $("form-retry").addEventListener("click", submit);
  $("meta-retry").addEventListener("click", loadMeta);
  $("f-env-file").addEventListener("change", chooseEnvFile);
  $("env-chip-remove").addEventListener("click", removeEnvFile);

  // Connection strings are type="text" masked with CSS, because browsers offer to save
  // type="password" fields whatever autocomplete says. Without the CSS property, a real
  // password field is the safer fallback.
  const MASK_SUPPORTED = typeof CSS !== "undefined" && CSS.supports("-webkit-text-security", "disc");
  if (!MASK_SUPPORTED) {
    for (const input of document.querySelectorAll('input[data-masked="true"]')) input.type = "password";
  }

  for (const toggle of document.querySelectorAll(".secret__toggle")) {
    toggle.addEventListener("click", () => {
      const input = $(toggle.dataset.toggle);
      const show = input.dataset.masked === "true";
      input.dataset.masked = String(!show);
      if (!MASK_SUPPORTED) input.type = show ? "text" : "password";
      // The visible word and the accessible name change together.
      toggle.textContent = show ? "Hide" : "Show";
      toggle.setAttribute("aria-label", `${show ? "Hide" : "Show"} ${toggle.dataset.what}`);
    });
  }

  // An inline error disappears as soon as its field is edited.
  for (const input of document.querySelectorAll("#run-form input, #run-form textarea")) {
    input.addEventListener("input", () => {
      const errorId = input.getAttribute("aria-describedby");
      if (errorId && !$(errorId).hidden) setFieldError(errorId, null);
    });
  }

  $("act-md").addEventListener("click", () => download(`n8n-healthcheck-report-${fileStamp(report.finishedAt)}.md`, "text/markdown;charset=utf-8", report.markdown || ""));
  $("act-json").addEventListener("click", () => download(`n8n-healthcheck-snapshot-${fileStamp(report.finishedAt)}.json`, "application/json", `${JSON.stringify(report.snapshot ?? null, null, 2)}\n`));
  $("act-copy").addEventListener("click", copyMarkdown);
  $("act-print").addEventListener("click", () => window.print());
  $("act-again").addEventListener("click", () => {
    clearErrors();
    showView("form");
  });

  $("severity-filter").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-severity]");
    if (!button) return;
    filters.severity = button.dataset.severity;
    applyFilters(true);
  });
  $("area-filter").addEventListener("change", (event) => {
    filters.area = event.target.value;
    applyFilters(true);
  });
  $("clear-filters").addEventListener("click", () => {
    filters.severity = "all";
    filters.area = "all";
    applyFilters(true);
    $("findings-title").focus();
  });
  $("expand-all").addEventListener("click", () => setAllExpanded(true));
  $("collapse-all").addEventListener("click", () => setAllExpanded(false));
  $("findings").addEventListener("click", (event) => {
    const toggle = event.target.closest(".finding__toggle");
    if (!toggle) return;
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    $(toggle.getAttribute("aria-controls")).hidden = !open;
  });

  loadMeta();
})();
