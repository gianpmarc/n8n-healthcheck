/* n8n-healthcheck web interface. No build step: this file is served as it is.
 *
 * Rules this file keeps:
 * - Text from the API or from the user is only ever inserted as text nodes.
 * - No form value goes to the URL, to browser storage or to the console.
 */
"use strict";

(() => {
  const MAX_BODY_BYTES = 1024 * 1024; // same limit as the server
  const MAX_DEBUG_INFO_BYTES = 64 * 1024; // same limit as the server's parser
  // The server gives up on each source after a few seconds, so a run ends well within this.
  const RUN_TIMEOUT_MS = 3 * 60 * 1000;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const $ = (id) => document.getElementById(id);
  /** True when `key` is the object's own property, not one every object inherits. */
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

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

  // The opening sentence of a note, marked so that the eye finds where the note starts.
  const lead = (text) => el("strong", { class: "lead-in" }, text);

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
    // Not one of the six data sources: it is named among a profile's sources, never under "Not assessed".
    debug_info: { label: "n8n debug information" },
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
  const withheld = new Set(); // ids the reader chose to withhold from the profile of this run
  const answers = new Map(); // fact id -> the answer chosen in "A few questions": a value, or a list of values
  const asked = new Map(); // fact id -> { inputs, note } of the question on screen
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
    for (const id of ["e-debug", "e-url", "e-env", "e-postgres", "e-redis", "e-kube"]) setFieldError(id, null);
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

    // Sent as it was pasted: the server reads what it knows and discards the rest.
    const debugInfo = value("f-debug");
    if (debugInfo.trim()) {
      const bytes = new TextEncoder().encode(debugInfo).length;
      if (bytes <= MAX_DEBUG_INFO_BYTES) body.debug_info_text = debugInfo;
      else errors.push({ errorId: "e-debug", focus: "f-debug", inputs: [$("f-debug")], message: `This text is ${formatBytes(bytes)}; the limit is 64 KB. A debug information is a few KB: paste only the text copied from Help > About n8n > Debug information, without the rest of the ticket or chat.` });
    }

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
    // A run ends by showing its report or its error: no other view may be opened meanwhile.
    $("f-profile").disabled = on;
    $("h-profile").hidden = !on;
    if (!on) setFieldError("e-profile", null, [$("f-profile")]); // a refusal that only said "a check is running"
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
    announce("Health check running. This can take up to a minute. A profile can be opened when it has finished.");
    let failure = null;
    let refused = null; // the server's sentence for a debug information it did not accept
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
      const text = await response.text().catch(() => "");
      try { data = JSON.parse(text); } catch { data = null; }
      // The profile keeps its numbers as the server wrote them, so the saved file is the server's text.
      if (response.ok && data && data.profile) data.profile = parseExact(text).profile;
      // The deadline can also pass while the body is still arriving; that is a timeout, not a bad report.
      if (abort.signal.aborted) throw new Error("timeout");
      if (response.status === 400 && data && data.field === "debug_info_text" && typeof data.error === "string") {
        refused = `${capitalize(data.error)}.`;
      } else if (!response.ok) {
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

    if (refused) {
      // Nothing was collected. The sentence is the server's own and quotes nothing of the text.
      if ($("view-form").hidden) showView("form");
      setFieldError("e-debug", refused, [$("f-debug")]);
      $("f-debug").focus();
      announce(`The debug information was not accepted. ${refused}`);
      return;
    }
    if (failure) {
      if ($("view-form").hidden) showView("form"); // an error is never written to a hidden view
      showFormError(failure[0], failure[1], true);
      $("form-error").focus();
      return;
    }
    report = { ...data, finishedAt: new Date() };
    renderReport();
    showView("report");
  }

  function showView(name) {
    for (const view of ["form", "report", "profile"]) $(`view-${view}`).hidden = name !== view;
    window.scrollTo(0, 0);
    $(`${name}-title`).focus({ preventScroll: true });
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
    // The summary in parts: the counts and the first fix come from the findings, the other
    // sentences from the API as it wrote them. Without them (the check could not run, or an
    // older server) the summary is shown whole.
    const notes = !status.lead && Array.isArray(data.summary_notes) && data.summary_notes.length ? data.summary_notes : null;
    const counts = SEVERITIES.map((severity) => findings.filter((f) => f.severity === severity).length);
    $("summary").textContent = notes ? String(notes[0]) : status.lead || data.summary || "";
    const stat = (number, label, kind) => el("li", { class: `stat${kind ? ` stat--${kind}` : ""}` },
      el("span", { class: "stat__n" }, String(number)), " ", el("span", { class: "stat__l" }, label));
    $("stats").hidden = !notes;
    $("stats").replaceChildren(...(notes ? [
      stat(counts[0], "critical", counts[0] ? "critical" : ""),
      stat(counts[1], counts[1] === 1 ? "warning" : "warnings", counts[1] ? "warning" : ""),
      stat(counts[2], "info"),
      data.sources ? stat(`${Number(data.sources.assessed)}/${Number(data.sources.total)}`, "sources read") : null,
    ].filter(Boolean) : []));
    $("urgent").hidden = !notes || findings.length === 0;
    $("urgent-title").textContent = notes && findings.length ? String(findings[0].title) : "";
    $("urgent-text").textContent = notes && findings.length ? String(findings[0].recommendation) : "";
    $("summary-points").hidden = !notes || notes.length < 2;
    $("summary-points").replaceChildren(...(notes ? notes.slice(1).map((line) => el("li", null, String(line))) : []));
    $("status-note").textContent = note || "";
    $("status-note").hidden = !note;

    renderScores(data);
    renderFindings(data, status);
    renderNotAssessed(data);

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    $("warnings-block").hidden = warnings.length === 0;
    $("warnings").replaceChildren(...warnings.map((line) => el("li", null, String(line))));

    resetShare();
    $("share-block").hidden = !data.profile;
    if (data.profile) {
      renderQuestions();
      renderShare();
    }

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
    note.replaceChildren(...(noData
      ? ["No score is shown because no data was collected."]
      : [lead("Each area starts at 100 and loses points for every finding."), " Scores marked “Partial” reflect only the checks that ran: they say nothing about what is listed under “Not assessed”."]));
  }

  function severityTag(severity) {
    const known = has(SEVERITY, severity) ? SEVERITY[severity] : null;
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
    $("na-intro").replaceChildren(...(entries.length
      ? [lead("These parts were not read."), " Unless an entry says otherwise, the report says nothing about them: a missing part is unknown, not healthy."]
      : ["Every data source was collected and every check ran."]));
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

  // ── Environment profile ─────────────────────────────────────────────
  // The server builds the profile (POST /api/run, field "profile"). This page never
  // computes a fact and never decides what is safe to write: it only blanks entries the
  // reader withholds and adds the reason and the reference. The format and the rules of
  // a valid file are those of n8n_healthcheck/profile.py (docs/profile-format.md).

  // profile-core:start
  // Everything from here to "profile-core:end" touches no page element, so that
  // tests/test_web_profile.py can run it as it is and compare it with profile.py.
  const PROFILE_FORMAT = "n8n-healthcheck-profile";
  const PROFILE_VERSION = 1;
  const PROFILE_REASONS = {
    baseline: "Baseline: first contact or a routine check",
    pre_upgrade: "Before an upgrade",
    post_upgrade: "After an upgrade",
    incident: "During an incident",
    other: "Other",
  };
  const SOURCE_STATES = ["collected", "disabled", "failed", "not_provided"];
  const PROVENANCES = ["measured", "declared", "unknown", "withheld"];
  const SETTING_STATES = ["value", "set", "withheld"];
  // The fact ids every file of format version 1 holds (profile.FORMAT_1_FACT_IDS).
  const FORMAT_1_FACT_IDS = [
    "n8n.version", "n8n.image_official", "orchestrator", "executions.mode", "main.replicas", "worker.replicas",
    "webhook.replicas", "worker.concurrency", "multi_main.enabled", "task_runners.enabled", "task_runners.mode",
    "binary_data.mode", "metrics.enabled", "pruning.enabled", "pruning.max_age_hours", "database.engine",
    "database.version", "database.pool_size", "database.max_connections", "database.execution_count",
    "database.execution_table_bytes", "database.oldest_execution_age_days", "queue.redis_version",
    "queue.maxmemory_policy", "queue.maxmemory_bytes", "queue.waiting_jobs", "queue.active_jobs",
    "queue.failed_jobs", "resources.main", "resources.worker", "probes.main", "probes.worker",
  ];
  const PROFILE_SOURCES = ["health", "metrics", "env", "postgres", "redis", "kubernetes"];
  // What a withheld entry is left with (profile.apply_withholding).
  const WITHHELD_FACT = { value: null, provenance: "withheld", source: null, detail: "" };
  const WITHHELD_SETTING = { state: "withheld", value: null };
  const WITHHELD_CHECK = { withheld: true };

  // Only a whole number written in plain digits that JavaScript writes back the same way
  // stays a number. Any other (0.0, 2.5, 1e0, 1e+21, a whole number of 2^53 or more) is
  // kept as the text it came in, so it is saved and shown exactly as written, and whether
  // a number is whole is decided on that text, as Python does.
  // A browser without JSON.rawJSON keeps plain numbers instead. There a float that is
  // whole is saved without its ".0" (0.0 as 0) and passes for a whole number in an opened
  // file, and a whole number of 2^53 or more is saved as the nearest number JavaScript
  // has, which is a different number (18446744073709551616 as 18446744073709552000).
  const isRaw = (value) => typeof JSON.isRawJSON === "function" && JSON.isRawJSON(value);
  const numberText = (value) => (isRaw(value) ? value.rawJSON : String(value));
  function parseExact(text) {
    if (typeof JSON.rawJSON !== "function") return JSON.parse(text);
    return JSON.parse(text, (key, value, context) => (
      typeof value === "number" && Number.isFinite(value) && context && typeof context.source === "string"
        && !(/^-?[0-9]+$/.test(context.source) && context.source === String(value))
        ? JSON.rawJSON(context.source)
        : value));
  }

  const REFERENCE_MAX = 200;
  // What Python's str.strip() removes, so a reference is stored exactly as profile.py would.
  const REFERENCE_EDGES = /^[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;
  const stripReference = (text) => text.replace(REFERENCE_EDGES, "");

  /** Why a (stripped) reference cannot be stored, or null. Same rules as profile.check_reference. */
  function referenceProblem(reference) {
    const length = [...reference].length; // characters, not UTF-16 units
    if (length > REFERENCE_MAX) return `The reference is ${length} characters long. The limit is ${REFERENCE_MAX}.`;
    // Control characters, and half of a surrogate pair, which is not text.
    if (/[\u0000-\u001f\u007f]|\p{Cs}/u.test(reference)) return "The reference must be a single line of plain text.";
    return null;
  }

  /** The id a setting is withheld by (profile.setting_id). */
  const settingKey = (setting) => `setting:${setting.scope}:${setting.name}`;

  /** `profile` with the entries in `ids` blanked, the reason and the reference. Same result as profile.apply_withholding. */
  function withholdEntries(profile, ids, reason, reference) {
    return {
      ...profile,
      run: { reason, reference: stripReference(reference) },
      facts: profile.facts.map((fact) => (ids.has(fact.id) ? { ...fact, ...WITHHELD_FACT } : fact)),
      settings: profile.settings.map((setting) => (ids.has(settingKey(setting)) ? { ...setting, ...WITHHELD_SETTING } : setting)),
      check: ids.has("check") ? WITHHELD_CHECK : profile.check,
    };
  }

  /** The text of the file, as profile.to_json writes it. */
  const profileText = (profile) => `${JSON.stringify(profile, null, 2)}\n`;

  /** Why `data` is not a profile this page can show, or null. Mirrors profile.validate(). */
  function profileProblem(data) {
    // An entry is named by its position, never by text taken from the file.
    const bad = (what) => `This file is not a valid environment profile: ${what}. Nothing of it is shown.`;
    const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value) && !isRaw(value);
    const isText = (value) => typeof value === "string";
    // A whole number written as one: 1, not 1.0, 1e0 or 1e+21 (Python reads those as floats).
    // With JSON.rawJSON a plain number is always one; the second case is 2^53 and above.
    const isWhole = (value) => Number.isInteger(value) || (isRaw(value) && /^-?[0-9]+$/.test(value.rawJSON));
    // No half of a surrogate pair in any string or key, and no number beyond what JSON can say (1E400).
    const sound = (value) => {
      if (isText(value)) return !/\p{Cs}/u.test(value);
      if (typeof value === "number") return Number.isFinite(value);
      if (value === null || typeof value !== "object") return true;
      return Object.entries(value).every(([key, item]) => sound(key) && sound(item));
    };
    const isValue = (value, depth = 0) => {
      if (isRaw(value)) return true; // a number
      if (isObject(value)) return depth < 2 && Object.values(value).every((item) => isValue(item, depth + 1));
      return value !== undefined && !Array.isArray(value);
    };

    if (!isObject(data)) return bad("it does not hold a JSON object");
    if (data.format !== PROFILE_FORMAT) return `This file is not an environment profile of n8n-healthcheck: its “format” is not “${PROFILE_FORMAT}”. Nothing of it is shown.`;
    const version = data.format_version;
    if (!isWhole(version) || Number(numberText(version)) < 1) return bad("“format_version”");
    if (Number(numberText(version)) > PROFILE_VERSION) return `This profile has format version ${numberText(version)}, newer than this page reads (${PROFILE_VERSION}). Update n8n-healthcheck to open it. Nothing of it is shown.`;
    if (!sound(data)) return bad("it holds a value that is not valid JSON text");
    if (!isText(data.tool_version)) return bad("“tool_version”");
    if (!isText(data.created_at)) return bad("“created_at”");
    const run = data.run;
    if (!isObject(run)) return bad("“run”");
    if (!isText(run.reason) || !has(PROFILE_REASONS, run.reason)) return bad("“run.reason”");
    if (!isText(run.reference) || stripReference(run.reference) !== run.reference || referenceProblem(run.reference)) return bad("“run.reference”");
    if (!isObject(data.sources) || !Object.values(data.sources).every((state) => SOURCE_STATES.includes(state))) return bad("“sources”");
    if (!PROFILE_SOURCES.every((name) => has(data.sources, name))) return bad("a source is missing");

    if (!Array.isArray(data.facts)) return bad("“facts”");
    for (const [index, fact] of data.facts.entries()) {
      const number = index + 1;
      if (!isObject(fact) || !isText(fact.id)) return bad(`fact ${number} has no id`);
      if (!PROVENANCES.includes(fact.provenance)) return bad(`provenance of fact ${number}`);
      if (!isValue(fact.value)) return bad(`value of fact ${number}`);
      // As in profile.py, a fact without the key has no source.
      if (fact.source !== undefined && fact.source !== null && !isText(fact.source)) return bad(`source of fact ${number}`);
      if (!isText(fact.detail)) return bad(`detail of fact ${number}`);
    }
    const ids = new Set(data.facts.map((fact) => fact.id));
    if (!FORMAT_1_FACT_IDS.every((id) => ids.has(id))) return bad("a fact of the catalogue is missing");

    if (!Array.isArray(data.settings)) return bad("“settings”");
    for (const [index, setting] of data.settings.entries()) {
      const number = index + 1;
      if (!isObject(setting) || !isText(setting.scope) || !isText(setting.name)) return bad(`setting ${number}`);
      if (!SETTING_STATES.includes(setting.state)) return bad(`state of setting ${number}`);
      if (setting.value !== null && !isText(setting.value)) return bad(`value of setting ${number}`);
    }
    if (!isWhole(data.settings_not_listed) || Number(numberText(data.settings_not_listed)) < 0) return bad("“settings_not_listed”");

    const check = data.check;
    if (!isObject(check)) return bad("“check”");
    if (check.withheld !== true) {
      if (typeof check.partial !== "boolean") return bad("“check.partial”");
      if (!Array.isArray(check.scores)
        || !check.scores.every((row) => isObject(row) && isText(row.area) && (row.score === null || isWhole(row.score)))) return bad("“check.scores”");
      if (!Array.isArray(check.findings)
        || !check.findings.every((finding) => isObject(finding) && ["rule_id", "severity", "area", "title"].every((key) => isText(finding[key])))) return bad("“check.findings”");
    }
    return null;
  }

  /** A fact value in words, as `--show-profile` prints it. */
  function shown(value) {
    if (value === null) return "not set";
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (isRaw(value)) return value.rawJSON;
    if (typeof value === "object") {
      return Object.entries(value)
        .map(([key, item]) => (item !== null && typeof item === "object" && !isRaw(item) ? `${key} (${shown(item)})` : `${key}: ${shown(item)}`))
        .join(", ") || "empty";
    }
    return String(value);
  }

  // What an answered fact is given besides its value (profile.apply_answers).
  const DECLARED_FACT = { provenance: "declared", source: null, detail: "" };

  /**
   * The value an answer is stored as, or undefined when it is no answer. Same rules as
   * profile.check_answers. `answer` is one value, or a list of values for a "multi"
   * question; what is stored is taken from the question's own list, never from `answer`.
   */
  function answerValue(question, answer) {
    const allowed = question.values.map((item) => item.value);
    if (question.kind === "multi") {
      const chosen = allowed.filter((value) => Array.isArray(answer) && answer.includes(value));
      // Every item is a value of the list, once; "none" stands alone.
      if (!chosen.length || chosen.length !== answer.length || (chosen.includes("none") && chosen.length > 1)) return undefined;
      return Object.fromEntries(chosen.map((value) => [value, true]));
    }
    const value = allowed.find((item) => item === answer);
    return question.kind === "yes_no" && value !== undefined ? value === "yes" : value;
  }

  /** `profile` with the answers as declared facts. Same result as profile.apply_answers: only an unknown fact takes one. */
  function declareAnswers(profile, questions, answers) {
    return {
      ...profile,
      facts: profile.facts.map((fact) => {
        const question = fact.provenance === "unknown" ? questions.find((item) => item.id === fact.id) : undefined;
        const value = question && answers.has(fact.id) ? answerValue(question, answers.get(fact.id)) : undefined;
        return value === undefined ? fact : { ...fact, value, ...DECLARED_FACT };
      }),
    };
  }

  // The question sheet of a profile, as profile.render_questions writes it. The questions
  // themselves come from GET /api/meta; only the sheet's own fixed text is written here.
  const SHEET = {
    title: "n8n environment profile: questions still open",
    parts: [
      ["1. Could be measured", "Facts the tool measures by itself when it can. Each says why it could not this time."],
      ["2. Ask whoever runs the platform", "Closed questions no probe can answer. The tool can store each answer as a declared fact."],
      ["3. Ask in conversation", "Never asked or stored by the tool. Record the answers where you keep your own notes."],
    ],
    none: "Nothing is open here.",
    leftOut: "Withheld by the sender, not questions to press on: ",
  };

  /** Text of a file made harmless inside Markdown, as profile._cell does. */
  const cell = (text) => String(text).replace(/\p{C}/gu, " ").split(/\s+/).filter(Boolean).join(" ").replace(/[\\`|<>[\]]/g, "\\$&");

  /**
   * What a profile leaves open, by who can answer it. A fact the file does not hold counts
   * as unknown; of two entries with one id the first counts. `catalogue` is the "questions"
   * of GET /api/meta.
   */
  function sheetOf(profile, catalogue) {
    const facts = new Map();
    for (const fact of profile.facts) if (!facts.has(fact.id)) facts.set(fact.id, fact);
    const open = (item) => !facts.has(item.id) || facts.get(item.id).provenance === "unknown";
    return {
      measured: catalogue.measured.filter(open).map((item) => ({ ...item, detail: facts.has(item.id) ? facts.get(item.id).detail : "" })),
      operator: catalogue.operator.filter(open),
      conversation: catalogue.conversation,
      leftOut: profile.facts.filter((fact) => fact.provenance === "withheld").map((fact) => fact.id),
    };
  }

  /** The sheet as Markdown: the text `--show-questions` prints for the same file. */
  function sheetMarkdown(profile, catalogue) {
    const sheet = sheetOf(profile, catalogue);
    const allowed = (item) => (item.kind === "yes_no"
      ? "`yes` or `no`"
      : (item.kind === "multi" ? "one or more of " : "one of ") + item.values.map((entry) => `\`${entry.value}\` (${entry.label})`).join(", "));
    const items = [
      sheet.measured.map((item) => `- **${item.question}** (\`${item.id}\`)${cell(item.detail) ? `: ${cell(item.detail)}` : ""}`),
      sheet.operator.map((item) => `- **${item.question}** (\`${item.id}\`): ${allowed(item)}`),
      sheet.conversation.map((item) => `- **${item.question}** ${item.why}`),
    ];
    const lines = [
      `# ${SHEET.title}`,
      "",
      `- **Taken:** ${cell(profile.created_at)}`,
      `- **Reason:** ${profile.run.reason}`,
      `- **Reference:** ${cell(profile.run.reference) || "none"}`,
    ];
    SHEET.parts.forEach(([title, intro], index) => lines.push("", `## ${title}`, "", intro, "", ...(items[index].length ? items[index] : [SHEET.none])));
    if (sheet.leftOut.length) lines.push("", `${SHEET.leftOut}${sheet.leftOut.map(cell).join(", ")}.`);
    return `${lines.join("\n")}\n`;
  }
  // profile-core:end

  const stateTag = (state) => el("span", { class: `tag tag--${state === "withheld" ? "withheld" : state === "unknown" ? "partial" : "reason"}` }, state);

  /**
   * A valid profile as page sections. `edit` is false for a saved file; in the share step
   * it is true, and every entry that can be withheld gets a checkbox (data-withhold = its id).
   * Every string of the profile is inserted as text.
   */
  function profileSections(profile, edit) {
    // An id or a variable name may break after a dot or an underscore, not in the middle of a word.
    // (No lookbehind: where it is not supported it stops the whole script from loading.)
    const breakable = (text) => text.match(/[^._]*[._]?/g).filter(Boolean).flatMap((piece, index) => (index ? [el("wbr"), piece] : [piece]));
    const part = (title, ...children) => el("section", { class: edit ? "ppart" : "block" }, el(edit ? "h4" : "h3", null, title), ...children);
    const tick = (id, key, name) => el("label", { class: "check" },
      el("input", { type: "checkbox", id, "data-withhold": key, checked: withheld.has(key) }),
      el("span", { class: "sr-only" }, `Withhold ${name}`));
    const table = (first, groups) => el("table", { class: "ptable" },
      el("thead", null, el("tr", null,
        el("th", { scope: "col" }, first),
        el("th", { scope: "col" }, edit ? "Written to the file" : "Recorded"),
        edit ? el("th", { scope: "col", class: "ptable__withhold" }, "Withhold") : null)),
      ...[...groups].map(([name, rows]) => el("tbody", null,
        el("tr", { class: "ptable__group" }, el("th", { scope: "rowgroup", colspan: edit ? 3 : 2 }, name)),
        ...rows)));
    const grouped = (items, keyOf, rowOf) => {
      const groups = new Map(); // in order of first appearance
      items.forEach((item, index) => {
        const key = keyOf(item);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(rowOf(item, index));
      });
      return groups;
    };
    const bulk = (what, keys) => (edit && keys.length ? el("p", { class: "ppart__bulk" },
      el("button", { type: "button", class: "btn btn--small", id: `bulk-${what}-all`, "data-bulk": what, "data-on": "true" }, `Withhold all ${what}`),
      el("button", { type: "button", class: "btn btn--small", id: `bulk-${what}-none`, "data-bulk": what, "data-on": "false" }, `Withhold no ${what}`)) : null);

    const details = el("dl", { class: "pdetails" }, el("dt", null, "Taken"), el("dd", null, profile.created_at));
    if (!edit) {
      details.append(
        el("dt", null, "Reason"), el("dd", null, `${PROFILE_REASONS[profile.run.reason]} (${profile.run.reason})`),
        el("dt", null, "Reference"), el("dd", null, profile.run.reference || "none"));
    }
    details.append(el("dt", null, "Written by"), el("dd", null, `n8n-healthcheck ${profile.tool_version}, profile format ${numberText(profile.format_version)}`));

    const sources = el("table", { class: "ptable" },
      el("thead", null, el("tr", null, el("th", { scope: "col" }, "Source"), el("th", { scope: "col" }, "State"))),
      el("tbody", null, ...Object.entries(profile.sources).map(([name, state]) => el("tr", null,
        el("th", { scope: "row" }, has(SECTIONS, name) ? `${SECTIONS[name].label} (${name})` : name),
        el("td", null, state.replace("_", " "))))));

    const facts = grouped(profile.facts, (fact) => fact.id.split(".")[0], (fact, index) => {
      const known = fact.provenance === "measured" || fact.provenance === "declared";
      return el("tr", null,
        el("th", { scope: "row" }, ...breakable(fact.id)),
        el("td", null,
          el("p", { class: "pline" },
            known ? el("span", { class: "pvalue" }, shown(fact.value)) : null,
            stateTag(fact.provenance),
            fact.source ? el("span", { class: "pdetail" }, `from ${fact.source}`) : null),
          fact.detail ? el("p", { class: "pdetail" }, fact.detail) : null),
        edit ? el("td", { class: "ptable__withhold" }, tick(`w-fact-${index}`, fact.id, `the fact ${fact.id}`)) : null);
    });
    const measured = profile.facts.filter((fact) => fact.provenance === "measured" || fact.provenance === "declared").length;
    const factsWithheld = profile.facts.filter((fact) => fact.provenance === "withheld").length;

    const settings = grouped(profile.settings, (setting) => setting.scope, (setting, index) => el("tr", null,
      el("th", { scope: "row" }, ...breakable(setting.name)),
      el("td", null, el("p", { class: "pline" },
        setting.state === "value" ? el("span", { class: "pvalue" }, setting.value || "(empty)")
          : setting.state === "set" ? el("span", { class: "pdetail" }, "set (value not written)")
            : stateTag("withheld"))),
      edit ? el("td", { class: "ptable__withhold" }, tick(`w-setting-${index}`, settingKey(setting), `the setting ${setting.name} of ${setting.scope}`)) : null));

    const check = profile.check;
    const outcome = [];
    if (edit) {
      outcome.push(el("label", { class: "check check--row" },
        el("input", { type: "checkbox", id: "w-check", "data-withhold": "check", checked: withheld.has("check") }),
        el("span", null, "Withhold the check outcome (the scores and the list of findings)")));
    }
    if (check.withheld === true) {
      outcome.push(el("p", { class: "block__note" }, stateTag("withheld"), " The scores and the findings are not in the file; it only says that they were withheld."));
    } else {
      outcome.push(el("table", { class: "ptable" },
        el("thead", null, el("tr", null, el("th", { scope: "col" }, "Area"), el("th", { scope: "col" }, "Score"))),
        el("tbody", null, ...check.scores.map((row) => el("tr", null,
          el("th", { scope: "row" }, row.area),
          el("td", null, row.score === null ? "n/a" : `${numberText(row.score)}/100${row.partial ? " (partial)" : ""}`))))));
      if (check.partial) outcome.push(el("p", { class: "block__note" }, "The check was partial: not every source was collected or not every rule ran."));
      outcome.push(check.findings.length
        ? el("ul", { class: "pfindings" }, ...check.findings.map((finding) => el("li", null,
          el("span", { class: "finding__title" }, finding.title),
          el("span", { class: "finding__tags" },
            severityTag(finding.severity),
            el("span", { class: "finding__id", translate: "no" }, finding.rule_id),
            el("span", { class: "tag tag--area" }, finding.area)))))
        : el("p", { class: "block__note" }, "No findings."));
      if (edit && check.findings.length) outcome.push(el("p", { class: "block__note" }, "Only the rule, severity, area and title of a finding are written, never its evidence."));
    }

    return [
      part(edit ? "Always in the file" : "Run details",
        edit ? el("p", { class: "block__note" }, lead("When the profile was taken, by which version, and which sources were read."), " These cannot be withheld.") : null,
        details, sources),
      part(`Facts (${profile.facts.length})`,
        measured === 0 ? el("p", { class: "block__note" }, ...(factsWithheld === profile.facts.length && factsWithheld
          ? ["Every fact is withheld: the file says nothing about the environment itself."]
          : [lead("No fact is measured: every one is unknown or withheld."), " Unknown means the tool could not tell for certain, not that something is wrong."])) : null,
        bulk("facts", profile.facts),
        table("Fact", facts)),
      part(`Settings (${profile.settings.length})`,
        profile.settings.length
          ? el("p", { class: "block__note" }, lead("The n8n variables that are set, by where they were read."), " “Set” means the variable has a value that is not written.")
          : el("p", { class: "block__note" }, "No n8n setting is listed."),
        bulk("settings", profile.settings),
        profile.settings.length ? table("Setting", settings) : null,
        profile.settings_not_listed
          ? el("p", { class: "block__note" }, `Other distinct variables that were set and are not listed, not even by name: ${numberText(profile.settings_not_listed)}.`)
          : null),
      part("Check outcome", ...outcome),
    ];
  }

  // Share step (under the report).

  /** The operator questions of GET /api/meta; none when the server did not send them. */
  const operatorQuestions = () => (meta && meta.questions ? meta.questions.operator : []);

  /** Answers first, then withholding: the order of the command line, so a declared fact can be withheld too. */
  function currentProfile() {
    return withholdEntries(declareAnswers(report.profile, operatorQuestions(), answers), withheld, $("f-reason").value, $("f-reference").value);
  }

  /**
   * "A few questions": one group of controls for each operator question whose fact came
   * back unknown. A fact the tool measured is not asked about. Nothing is selected as an
   * answer at the start, and a control holds only values of the list the server sent.
   */
  function renderQuestions() {
    asked.clear();
    const open = operatorQuestions().filter((question) => report.profile.facts.some((fact) => fact.id === question.id && fact.provenance === "unknown"));
    $("questions-block").hidden = operatorQuestions().length === 0;
    $("questions-empty").hidden = open.length > 0;
    $("questions-list").replaceChildren(...open.map((question, index) => {
      const multi = question.kind === "multi";
      const inputs = [];
      const option = (value, label, number) => {
        const input = el("input", {
          type: multi ? "checkbox" : "radio", name: `q-${index}`, id: `q-${index}-${number}`, value, "data-question": question.id, checked: !multi && value === "",
        });
        inputs.push(input);
        return el("label", { class: `choice${value === "" ? " choice--none" : ""}` }, input, el("span", null, label));
      };
      const note = el("p", { class: "question__note", hidden: true }, "You withheld this fact below, so this answer is not written to the file.");
      asked.set(question.id, { inputs, note });
      return el("fieldset", { class: "question" },
        el("legend", null,
          el("span", { class: "question__text" }, question.question),
          " ",
          el("span", { class: "question__id", translate: "no" }, question.id),
          multi ? el("span", { class: "question__kind" }, "Tick every one that applies") : null),
        el("div", { class: "choices" },
          multi ? null : option("", "No answer", 0),
          ...question.values.map((item, number) => option(item.value, item.label, number + 1))),
        note);
    }));
  }

  /** A new run is a new profile: no choice, reason or reference of the run before carries over to it. */
  function resetShare() {
    withheld.clear();
    answers.clear();
    $("f-reason").value = Object.keys(PROFILE_REASONS)[0];
    $("f-reference").value = "";
    checkReference();
    $("share-status").textContent = "";
  }

  function renderShare() {
    const profile = currentProfile();
    const focused = document.activeElement && $("share-body").contains(document.activeElement) ? document.activeElement.id : "";
    $("share-body").replaceChildren(...profileSections(profile, true));
    // The rows were rebuilt: put the focus back on the control that was used.
    if (focused && $(focused)) $(focused).focus({ preventScroll: true });
    const facts = profile.facts.filter((fact) => fact.provenance === "withheld").length;
    const settings = profile.settings.filter((setting) => setting.state === "withheld").length;
    for (const [id, question] of asked) question.note.hidden = !(answers.has(id) && withheld.has(id));
    const answered = asked.size ? `Answered: ${answers.size} of ${plural(asked.size, "question")}. ` : "";
    $("share-summary").textContent = `${answered}Withheld: ${facts} of ${plural(profile.facts.length, "fact")}, ${settings} of ${plural(profile.settings.length, "setting")}, check outcome ${profile.check.withheld === true ? "withheld" : "included"}.`;
  }

  /** Show the state of the reference field; return true when it can be stored. */
  function checkReference() {
    const reference = stripReference($("f-reference").value);
    const problem = referenceProblem(reference);
    $("reference-count").textContent = `${[...reference].length} of ${REFERENCE_MAX} characters.`;
    setFieldError("e-reference", problem, [$("f-reference")]);
    return !problem;
  }

  function downloadProfile() {
    if (!checkReference()) {
      $("f-reference").focus();
      return;
    }
    const profile = currentProfile();
    const day = /^\d{4}-\d{2}-\d{2}/.exec(String(profile.created_at));
    download(`n8n-profile-${day ? day[0] : "undated"}.json`, "application/json", profileText(profile), "share-status");
  }

  // Viewer ("Open a profile" on the start page).

  const RUNNING_NO_PROFILE = "A health check is running. Open the profile when the check has finished.";

  async function openProfile() {
    const input = $("f-profile");
    const file = input.files && input.files[0];
    if (!file) return;
    setFieldError("e-profile", null, [input]);
    let problem = null;
    let data = null;
    if (running) {
      // The field is disabled during a run; this is for a file chosen just before it started.
      problem = RUNNING_NO_PROFILE;
    } else if (file.size > MAX_BODY_BYTES) {
      problem = `“${file.name}” is ${formatBytes(file.size)}. A profile is a small JSON file; choose the n8n-profile file itself.`;
    } else {
      try {
        // Strict UTF-8 and no byte-order mark, as the command line reads it.
        data = parseExact(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer()));
        // A check that started while the file was being read owns the page: its result is shown, not this.
        problem = running ? RUNNING_NO_PROFILE : profileProblem(data);
      } catch {
        problem = `“${file.name}” is not a JSON file this page can read. Choose the n8n-profile file itself, as it was downloaded.`;
      }
    }
    input.value = ""; // so choosing the same file again fires "change"
    if (problem) {
      setFieldError("e-profile", problem, [input]);
      input.focus();
      return;
    }
    $("profile-meta").textContent = `${file.name} · read in this browser, not uploaded`;
    $("profile-body").replaceChildren(...profileSections(data, false), sheetSection(data));
    showView("profile");
    announce("Profile opened.");
  }

  /**
   * "Questions still open" for an opened profile: what it leaves unknown, and a button that
   * copies the sheet as Markdown. Made in this browser from the file and the questions of
   * GET /api/meta; every string of the file is inserted as text.
   */
  function sheetSection(profile) {
    const title = el("h3", null, "Questions still open");
    const catalogue = meta && meta.questions;
    if (!catalogue) {
      return el("section", { class: "block" }, title,
        el("p", { class: "block__note" }, "The list of questions did not load from the server, so the sheet cannot be made. Check that “n8n-healthcheck serve” is still running, reload the page and open the file again."));
    }
    const sheet = sheetOf(profile, catalogue);
    const text = sheetMarkdown(profile, catalogue);
    const status = el("p", { class: "action-status", role: "status", "aria-live": "polite" });
    // Shown only when the browser refuses the clipboard: the same text, to copy by hand.
    const byHand = el("textarea", { class: "sheet__text", id: "sheet-text", rows: "10", readonly: true, hidden: true, "aria-label": "The question sheet as Markdown" });
    const button = el("button", { type: "button", class: "btn", id: "sheet-copy" }, "Copy as Markdown");
    button.addEventListener("click", async () => {
      const copied = await copyText(text, button);
      byHand.hidden = copied;
      if (!copied) {
        byHand.value = text;
        byHand.focus();
        byHand.select();
      }
      status.textContent = copied
        ? "Question sheet copied to the clipboard as Markdown."
        : "The browser did not allow copying. The sheet is in the box below: copy it from there.";
    });

    const values = (item) => (item.kind === "yes_no"
      ? "Yes or no."
      : `${item.kind === "multi" ? "One or more of" : "One of"}: ${item.values.map((entry) => entry.label).join("; ")}.`);  // a label may hold a comma
    const rows = [
      sheet.measured.map((item) => el("li", null,
        el("strong", null, item.question), " ", el("code", { translate: "no" }, item.id),
        item.detail ? el("p", { class: "pdetail" }, item.detail) : null)),
      sheet.operator.map((item) => el("li", null,
        el("strong", null, item.question), " ", el("code", { translate: "no" }, item.id),
        el("p", { class: "pdetail" }, values(item)))),
      sheet.conversation.map((item) => el("li", null, el("strong", null, item.question), el("p", { class: "pdetail" }, item.why))),
    ];
    return el("section", { class: "block" }, title,
      el("p", { class: "block__note" }, rows[0].length + rows[1].length
        ? "What this profile leaves unknown, by who can answer it, ready for a call. “Copy as Markdown” copies this sheet with the profile's date, reason and reference at its top."
        : "This profile leaves nothing unknown that the tool asks about. The questions for a conversation are always listed. “Copy as Markdown” copies this sheet with the profile's date, reason and reference at its top."),
      el("div", { class: "sheet__actions" }, button),
      status,
      byHand,
      ...SHEET.parts.map(([name, intro], index) => el("section", { class: "ppart" },
        el("h4", null, `${name} (${rows[index].length})`),
        el("p", { class: "block__note" }, intro),
        rows[index].length ? el("ul", { class: "sheet" }, ...rows[index]) : el("p", { class: "block__note sheet__none" }, SHEET.none))),
      sheet.leftOut.length ? el("p", { class: "block__note sheet__left-out" }, `${SHEET.leftOut}${sheet.leftOut.join(", ")}.`) : null);
  }

  for (const [value, label] of Object.entries(PROFILE_REASONS)) $("f-reason").append(el("option", { value }, label));
  checkReference();
  $("f-reference").addEventListener("input", checkReference);
  $("share-form").addEventListener("submit", (event) => {
    event.preventDefault();
    downloadProfile();
  });
  $("questions-list").addEventListener("change", (event) => {
    const input = event.target;
    const id = input.dataset.question;
    if (!id) return;
    if (input.type === "radio") {
      if (input.value) answers.set(id, input.value);
      else answers.delete(id);
    } else {
      // "none" cannot be combined with another value: ticking either side unticks the other.
      const boxes = asked.get(id).inputs;
      if (input.checked) for (const box of boxes) if (box !== input && (input.value === "none" || box.value === "none")) box.checked = false;
      const chosen = boxes.filter((box) => box.checked).map((box) => box.value);
      if (chosen.length) answers.set(id, chosen);
      else answers.delete(id);
    }
    renderShare();
  });
  $("share-body").addEventListener("change", (event) => {
    const key = event.target.dataset.withhold;
    if (!key) return;
    if (event.target.checked) withheld.add(key);
    else withheld.delete(key);
    renderShare();
  });
  $("share-body").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-bulk]");
    if (!button) return;
    const keys = button.dataset.bulk === "facts"
      ? report.profile.facts.map((fact) => fact.id)
      : report.profile.settings.map(settingKey);
    for (const key of keys) {
      if (button.dataset.on === "true") withheld.add(key);
      else withheld.delete(key);
    }
    renderShare();
    $("share-status").textContent = `${button.textContent}: done.`;
  });
  $("f-profile").addEventListener("change", openProfile);
  $("profile-print").addEventListener("click", () => window.print());
  $("profile-close").addEventListener("click", () => {
    $("profile-body").replaceChildren(); // nothing of the file is kept
    $("profile-meta").textContent = "";
    showView("form");
  });

  // ── Report actions ──────────────────────────────────────────────────

  function fileStamp(date) {
    const two = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}${two(date.getMonth() + 1)}${two(date.getDate())}-${two(date.getHours())}${two(date.getMinutes())}`;
  }

  function download(filename, type, text, statusId = "action-status") {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const link = el("a", { href: url, download: filename, hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    $(statusId).textContent = `Download started: ${filename}`;
  }

  /** Put `text` on the clipboard; false when the browser did not allow it. `button` is the control that asked. */
  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Older browsers, or the clipboard permission was refused.
      const area = el("textarea", { class: "sr-only", readonly: true, "aria-hidden": "true", tabindex: "-1" });
      area.value = text;
      document.body.append(area);
      area.select();
      let copied = false;
      try { copied = document.execCommand("copy"); } catch { copied = false; }
      area.remove();
      button.focus();
      return copied;
    }
  }

  async function copyMarkdown() {
    const copied = await copyText(report.markdown || "", $("act-copy"));
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
