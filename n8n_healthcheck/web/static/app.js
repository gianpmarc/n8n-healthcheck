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
  const CHECK_TIMEOUT_MS = 30 * 1000; // reading a pasted text collects nothing
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
    // Nor is the n8n API, read with a key for the usage numbers.
    api: { label: "n8n API (usage numbers)" },
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
  let step = 1; // the step on screen, 1 to 6
  let checking = false; // POST /api/debug-info is under way
  let checked = null; // what POST /api/debug-info answered for `checkedText`
  let checkedText = null; // the debug information that answer is about, exactly as it was pasted
  let runs = 0; // reports received since the page was loaded
  let envFile = null; // { name, size, text } — the chosen file, kept in memory only
  let timer = null;
  let report = null; // last 200 response of POST /api/run, plus finishedAt
  // The one entry that cannot be withheld (profile.ALWAYS_SHARED): it has no box, and no button adds it.
  const ALWAYS_SHARED = "n8n.version";
  const withheld = new Set(); // ids the operator chose to withhold; kept from one run to the next
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
    $("nav-1").hidden = false;
    renderStepper();
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

  const KEY_NEEDS_URL = "Add the n8n URL this key belongs to, or clear the key. The key alone is not a source.";

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
    for (const id of ["e-debug", "e-url", "e-apikey", "e-env", "e-postgres", "e-redis", "e-kube"]) setFieldError(id, null);
    $("form-error").hidden = true;
    $("debug-error").hidden = true;
  }

  /** Why a pasted debug information cannot be sent, or null. Whether it is one is for the server to say. */
  function debugInfoProblem(text) {
    if (!text.trim()) return "Paste the debug information to continue. The file cannot be made without it.";
    const bytes = new TextEncoder().encode(text).length;
    return bytes <= MAX_DEBUG_INFO_BYTES ? null : `This text is ${formatBytes(bytes)}; the limit is 64 KB. A debug information is a few KB: paste only the text copied from Help > About n8n > Debug information, without the rest of the ticket or chat.`;
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
      const problem = debugInfoProblem(debugInfo);
      if (!problem) body.debug_info_text = debugInfo;
      else errors.push({ errorId: "e-debug", focus: "f-debug", inputs: [$("f-debug")], message: problem });
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

    // The key is of no use without the instance it belongs to. It is checked by the server.
    const apiKey = value("f-apikey").trim();
    if (apiKey && body.url) body.api_key = apiKey;
    else if (apiKey && !url) errors.push({ errorId: "e-apikey", focus: "f-url", inputs: [$("f-apikey")], message: KEY_NEEDS_URL });

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
    return { body, errors };
  }

  function setRunning(on) {
    running = on;
    const button = $("run-button");
    if (on) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
    $("run-button-label").textContent = on ? "Running health check…" : report ? "Run again" : "Run health check";
    $("run-progress").hidden = !on;
    renderStepper(); // no other step can be opened while it runs
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

  // ── The six steps ───────────────────────────────────────────────────
  // One step is on screen at a time. A step is a view of this one page: nothing is
  // loaded, and what was typed stays in its field. The browser's Back goes to the step
  // before through the history, whose entries hold a step number and nothing else.

  const STEP_COUNT = 6;

  /** True when the server accepted the debug information that is in the field now. */
  const debugChecked = () => checked !== null && $("f-debug").value === checkedText;

  /** Whether step `n` can be opened: 3 and 4 need the debug information, 5 and 6 the profile of a run. */
  function canOpen(n) {
    if (!meta || running || checking) return false;
    if (n >= 5 && !(report && report.profile)) return false;
    return n < 3 || debugChecked();
  }

  function renderStepper() {
    for (let n = 1; n <= STEP_COUNT; n += 1) {
      const button = $(`go-${n}`);
      button.disabled = n !== step && !canOpen(n);
      if (n === step) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
    }
    const moving = running || checking;
    for (const id of ["back-2", "back-3", "next-3", "back-4", "next-4", "back-5", "next-5", "back-6"]) $(id).disabled = moving;
    $("next-4").hidden = !(report && report.profile);
    $("debug-button-label").textContent = checking ? "Checking the text…" : debugChecked() ? "Continue" : "Check the text";
    $("debug-read").hidden = !debugChecked();
  }

  /** Put step `n` on screen and move the focus to its heading. `record` is false when the history already holds it. */
  function showStep(n, record = true) {
    step = n;
    for (let other = 1; other <= STEP_COUNT; other += 1) $(`step-${other}`).hidden = other !== n;
    $("flow").hidden = false;
    $("view-profile").hidden = true;
    if (n === 4) renderRunSources();
    if (n === 6) renderDownload();
    renderStepper();
    if (record) window.history.pushState({ step: n }, "");
    window.scrollTo(0, 0);
    $(`step-${n}-title`).focus({ preventScroll: true });
  }

  /** Open step `n` if it can be opened; true when it was. */
  function go(n) {
    if (n === step || !canOpen(n)) return false;
    showStep(n);
    return true;
  }

  // Step 2: the debug information, checked by the server before anything else is asked.

  function setChecking(on) {
    checking = on;
    const button = $("debug-button");
    if (on) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
    renderStepper();
  }

  function showDebugError(title, text) {
    $("debug-error-title").textContent = title;
    $("debug-error-text").textContent = text;
    $("debug-error").hidden = false;
    $("debug-error").focus();
  }

  /** "Check the text", then "Continue": the first sends the text to the server, the second opens step 3. */
  async function continueFromDebug() {
    if (checking || running) return;
    if (debugChecked()) {
      go(3);
      return;
    }
    clearErrors();
    const text = $("f-debug").value;
    const problem = debugInfoProblem(text);
    if (problem) {
      setFieldError("e-debug", problem, [$("f-debug")]);
      $("f-debug").focus();
      announce(problem);
      return;
    }
    setChecking(true);
    announce("Checking the debug information.");
    let failure = null;
    let refused = null; // the parser's sentence for a text it did not accept
    let data = null;
    const abort = new AbortController();
    const deadline = setTimeout(() => abort.abort(), CHECK_TIMEOUT_MS);
    try {
      const response = await fetch("/api/debug-info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ debug_info_text: text }),
        cache: "no-store",
        referrerPolicy: "no-referrer",
        signal: abort.signal,
      });
      data = await response.json().catch(() => null);
      if (response.status === 400 && data && data.field === "debug_info_text" && typeof data.error === "string") {
        refused = `${capitalize(data.error)}.`;
      } else if (!response.ok) {
        failure = describeFailure(response.status, data && typeof data.error === "string" ? data.error : "");
      } else if (!data || !Array.isArray(data.read) || !Array.isArray(data.notes) || !data.coverage || !data.coverage.waiting) {
        failure = ["The server sent an answer this page could not read.", "Try again; if it keeps happening, the page and the server may be different versions — restart “n8n-healthcheck serve” and reload."];
      }
    } catch {
      failure = abort.signal.aborted
        ? ["The server did not answer in time.", "Check that “n8n-healthcheck serve” is still running, then try again. Your text is unchanged."]
        : ["The server could not be reached.", "Check that “n8n-healthcheck serve” is still running, then try again. Your text is unchanged."];
    }
    clearTimeout(deadline);
    if (!failure && !refused) {
      checked = data;
      checkedText = text;
    }
    setChecking(false);
    if (refused) {
      // The sentence is the parser's own and quotes nothing of the text. The operator stays on this step.
      setFieldError("e-debug", refused, [$("f-debug")]);
      $("f-debug").focus();
      announce(`The debug information was not accepted. ${refused}`);
      return;
    }
    if (failure) {
      showDebugError(failure[0], failure[1]);
      return;
    }
    renderDebugRead();
    renderGain();
    $("debug-read").focus();
    announce("The debug information was read. Review what was read, then continue.");
  }

  /** What the server read from the text, and the report's own sentences about it. */
  function renderDebugRead() {
    $("debug-read-rows").replaceChildren(...checked.read.flatMap((row) => [el("dt", null, String(row.label)), el("dd", null, String(row.value))]));
    $("debug-notes").hidden = checked.notes.length === 0;
    $("debug-notes").replaceChildren(...checked.notes.map((line) => el("li", null, String(line))));
  }

  /** A count sent by the server, or 0 for anything that is not one. */
  const whole = (value) => (Number.isInteger(value) && value > 0 ? value : 0);
  /** "1 check needs" / "3 checks need". */
  const checksThat = (number, verb) => `${plural(number, "check")} ${verb}${number === 1 ? "s" : ""}`;

  // Step 3: what the rules decided on the debug information alone, and what waits for each source.
  function renderGain() {
    const coverage = checked.coverage;
    $("gain-decided").textContent = `The debug information decides ${whole(coverage.decided)} of ${plural(whole(coverage.total), "check")}.`;
    const unstated = whole(coverage.unstated);
    $("gain-unstated").hidden = unstated === 0;
    $("gain-unstated").textContent = `${checksThat(unstated, "need")} a source that states a setting the debug information does not show.`;
    for (const source of ["url", "env", "postgres", "redis", "kubernetes"]) {
      const waiting = whole(coverage.waiting[source]);
      $(`gain-${source}`).textContent = waiting ? `${plural(waiting, "check")} can be decided with this source.` : "No check is waiting for this source.";
    }
  }

  function continueFromSources() {
    clearErrors();
    const { errors } = buildRequest();
    if (errors.length) {
      showRequestErrors(errors);
      return;
    }
    go(4);
  }

  /** Show the problems of buildRequest() next to their fields, on the step that holds the first of them. */
  function showRequestErrors(errors) {
    const home = errors[0].errorId === "e-debug" ? 2 : 3;
    if (step !== home) showStep(home);
    for (const error of errors) setFieldError(error.errorId, error.message, error.inputs);
    $(errors[0].focus).focus();
    announce(`${plural(errors.length, "problem")} in the form. ${errors[0].message}`);
  }

  // Step 4: the sources this run will read, by name. No value of the form is shown.
  function renderRunSources() {
    const { body } = buildRequest();
    const names = ["the debug information"];
    for (const [field, source] of [["url", "url"], ["api_key", "api"], ["env_text", "env"], ["postgres_dsn", "postgres"], ["redis_url", "redis"], ["kube_namespace", "kubernetes"]]) {
      const known = meta && meta.sources.find((item) => item.id === source);
      if (has(body, field)) names.push(known && known.label ? String(known.label) : source);
    }
    $("run-sources").textContent = `${names.join(", ")}.`;
  }

  async function submit() {
    if (running || checking) return; // no double submit
    clearErrors();
    if (!debugChecked()) {
      // Never reached through the page's own controls: step 4 opens only after the check.
      showStep(2);
      setFieldError("e-debug", "The debug information has to be checked before a run. Press “Check the text”.", [$("f-debug")]);
      $("f-debug").focus();
      return;
    }
    const { body, errors } = buildRequest();
    if (errors.length) {
      showRequestErrors(errors);
      return;
    }
    if (step !== 4) showStep(4); // a run and its result are shown on step 4

    setRunning(true);
    announce("Health check running. This can take up to a minute. A profile can be opened when it has finished.");
    let failure = null;
    let refused = null; // the server's sentence for a debug information or an API key it did not accept
    let refusedField = null;
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
      if (response.status === 400 && data && ["debug_info_text", "api_key"].includes(data.field) && typeof data.error === "string") {
        refused = `${capitalize(data.error)}.`;
        refusedField = data.field;
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
    // A debug information the run refused is no longer the checked one: steps 3 and 4 close again.
    if (refused && refusedField === "debug_info_text") checked = null;
    setRunning(false);

    if (refused) {
      // Nothing was collected. The sentence is the server's own and quotes nothing of the text.
      const [home, errorId, inputId, what] = refusedField === "api_key" ? [3, "e-apikey", "f-apikey", "API key"] : [2, "e-debug", "f-debug", "debug information"];
      showStep(home);
      setFieldError(errorId, refused, [$(inputId)]);
      $(inputId).focus();
      announce(`The ${what} was not accepted. ${refused}`);
      return;
    }
    if (failure) {
      if (step !== 4) showStep(4); // an error is never written to a hidden step
      showFormError(failure[0], failure[1], true);
      $("form-error").focus();
      return;
    }
    // The key has done its work: it is not kept in the form for a second run.
    $("f-apikey").value = "";
    // And it is masked again, were it shown.
    for (const toggle of document.querySelectorAll('[data-toggle="f-apikey"]')) if ($("f-apikey").dataset.masked === "false") toggle.click();
    report = { ...data, finishedAt: new Date() };
    renderReport();
    $("view-report").hidden = false;
    $("run-button-label").textContent = "Run again";
    renderStepper();
    $("report-title").focus();
  }

  /** The saved profile that was opened, in place of the steps; false puts the steps back. */
  function showProfile(on) {
    $("flow").hidden = on;
    $("view-profile").hidden = !on;
    window.scrollTo(0, 0);
    $(on ? "profile-title" : `step-${step}-title`).focus({ preventScroll: true });
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
          note: noData ? "“Not assessed” and the messages from the collectors below have the details. Go back to step 3 to correct the sources, then run again." : null,
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
      stat(counts[2], "info", counts[2] ? "info" : ""),
      data.sources ? stat(`${Number(data.sources.assessed)}/${Number(data.sources.total)}`, "data sources read") : null,
      // A debug information is not one of the data sources counted above, so it is named.
      data.sources && data.sources.debug_info === true ? el("li", { class: "stat" }, el("span", { class: "stat__l" }, "debug information read")) : null,
    ].filter(Boolean) : []));
    $("urgent").hidden = !notes || findings.length === 0;
    $("urgent-title").textContent = notes && findings.length ? String(findings[0].title) : "";
    $("urgent-text").textContent = notes && findings.length ? String(findings[0].recommendation) : "";
    $("summary-points").hidden = !notes || notes.length < 2;
    $("summary-points").replaceChildren(...(notes ? notes.slice(1).map((line) => el("li", null, String(line))) : []));
    $("status-note").textContent = note || "";
    $("status-note").hidden = !note;

    renderUsage(data);
    renderScores(data);
    renderFindings(data, status);
    renderNotAssessed(data);

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    $("warnings-block").hidden = warnings.length === 0;
    $("warnings").replaceChildren(...warnings.map((line) => el("li", null, String(line))));

    runs += 1;
    $("share-status").textContent = "";
    $("download-status").textContent = "";
    if (data.profile) {
      // What the operator chose for the run before stays: see renderQuestions() and renderShare().
      renderCoverage(data.coverage);
      renderQuestions();
      renderShare();
    }

    announce(status.kind === "error"
      ? `${status.label}. Nothing was assessed.`
      : `Report ready. ${status.label}${data.partial ? " (partial report)" : ""}. ${plural(counts[0], "critical issue")}, ${plural(counts[1], "warning")}, ${plural(counts[2], "informational note")}.`);
  }

  /** The usage numbers read through the n8n API. Shown only when a key was given; never a score. */
  function renderUsage(data) {
    const usage = data.usage && Array.isArray(data.usage.rows) ? data.usage : null;
    $("usage-block").hidden = !usage;
    $("usage-basis").hidden = !usage || !usage.basis;
    $("usage-basis").textContent = usage && usage.basis ? String(usage.basis) : "";
    $("usage").replaceChildren(...(usage ? usage.rows.map((row) => {
      const read = row.value != null;
      return el("div", { class: `usage__item${read ? "" : " usage__item--unread"}` },
        el("dt", { class: "usage__label" }, String(row.label)),
        el("dd", { class: "usage__value" }, read ? String(row.value) : "Not read"),
        row.note ? el("dd", { class: "usage__note" }, String(row.note)) : null);
    }) : []));
  }

  function renderScores(data) {
    const rows = data.scores.map((row) => {
      const count = Number(row.findings) || 0;
      // The score takes the colour of the most severe finding of its area, and says which in words.
      const worst = SEVERITIES.find((severity) => data.findings.some((f) => f.area === row.area && f.severity === severity));
      const footer = el("p", { class: "score__findings" }, count ? plural(count, "finding") : "No findings",
        worst ? el("span", { class: "score__worst" }, ` · worst: ${worst}`) : null);
      if (row.score == null) {
        return el("li", { class: "score score--none" },
          el("p", { class: "score__area" }, String(row.area)),
          el("p", { class: "score__value" }, el("span", { class: "score__number" }, "Not scored")),
          el("div", { class: "score__bar", "aria-hidden": "true" }),
          el("p", { class: "score__findings" }, "No data was collected"));
      }
      const fill = el("div", { class: "score__fill" });
      fill.style.setProperty("--score", String(Math.max(0, Math.min(100, Number(row.score) || 0))));
      // Green only for an area that was checked in full and has no finding: a partial score says too little.
      const colour = worst || (row.partial || data.findings.some((f) => f.area === row.area) ? "" : "ok");
      return el("li", { class: `score${row.partial ? " score--partial" : ""}${colour ? ` score--${colour}` : ""}` },
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
          why = `${section.missing} Add it in step 3 and run again to include it.`;
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
      check: ids.has("check") ? WITHHELD_CHECK : withholdChecks(profile.check, ids),
    };
  }

  /** The id one check is withheld by. */
  const checkKey = (entry) => `check:${entry.rule_id}`;

  /** `check` with the checks in `ids` blanked and their findings removed (profile.apply_withholding). */
  function withholdChecks(check, ids) {
    if (!Array.isArray(check.checks) || !check.checks.some((entry) => ids.has(checkKey(entry)))) return check;
    const gone = new Set(check.checks.filter((entry) => ids.has(checkKey(entry))).map((entry) => entry.rule_id));
    // The score of its area goes with a check, whatever the check had stated.
    const areas = new Set(check.checks.filter((entry) => gone.has(entry.rule_id)).map((entry) => entry.area));
    return {
      ...check,
      scores: check.scores.map((row) => (areas.has(row.area) ? { ...row, score: null, withheld: true } : row)),
      findings: check.findings.filter((finding) => !gone.has(finding.rule_id)),
      checks: check.checks.map((entry) => (gone.has(entry.rule_id) ? { rule_id: entry.rule_id, area: entry.area, outcome: "withheld", text: "" } : entry)),
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
      // Added in tool version 0.6.0; a file written before has none. What an entry holds
      // besides these four is read as it is found: see checkEntries().
      if (has(check, "checks") && (!Array.isArray(check.checks)
        || !check.checks.every((entry) => isObject(entry) && ["rule_id", "area", "outcome", "text"].every((key) => isText(entry[key]))))) return bad("“check.checks”");
    }
    return null;
  }

  // check.checks, as a reader finds them (profile.check_entries and what follows it).
  const OUTCOME_LABELS = { finding: "finding", passed: "passed", not_applicable: "does not apply", not_assessed: "not assessed", withheld: "withheld" };
  const BY_DEFAULT = "by n8n's default, not set explicitly";
  const REASON_LABELS = {
    source_missing: "no source that states it was given",
    source_failed: "a source that states it was asked for and could not be collected",
    not_stated: "a source was read and does not say",
    value_unreadable: "a source holds the value, which could not be read",
    sources_disagree: "two sources given in the run disagree on what it reads",
    rule_error: "the rule failed to run",
  };
  const NOT_KNOWN = "not known to this version of n8n-healthcheck";
  const NEED_LABELS = { url: "the n8n URL", env: "the env file", postgres: "Postgres", redis: "Redis", kubernetes: "Kubernetes", debug_info: "the debug information" };

  /**
   * The entries of check.checks, each made safe to show; null when the file has none.
   * Text that is not text is empty, a list that is not a list of strings is empty, and an
   * outcome or a reason this version does not know makes the entry `known: false`.
   */
  function checkEntries(profile) {
    const check = profile.check;
    if (check.withheld === true || !has(check, "checks")) return null;
    const text = (value) => (typeof value === "string" ? value : "");
    const strings = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === "string") : []);
    return check.checks.map((entry) => {
      const reason = text(entry.reason);
      return {
        rule_id: entry.rule_id,
        area: entry.area,
        outcome: entry.outcome,
        text: entry.text,
        known: has(OUTCOME_LABELS, entry.outcome) && (entry.outcome !== "not_assessed" || has(REASON_LABELS, reason)),
        severity: text(entry.severity),
        impact: text(entry.impact),
        remedy: text(entry.remedy),
        byDefault: entry.basis === "default",
        reason,
        needs: strings(entry.needs),
        restsOn: strings(entry.rests_on),
      };
    });
  }

  /** True for the score of an area that was withheld with one of its checks: not "no data" (profile.score_withheld). */
  const scoreWithheld = (row) => row.score === null && row.withheld === true;

  /** "Redis", "Kubernetes or the debug information", "any one of the env file, Kubernetes or the debug information". */
  function anyOf(needs, escape = (text) => text) {
    const names = needs.map((need) => (has(NEED_LABELS, need) ? NEED_LABELS[need] : escape(need)));
    return names.length < 3 ? names.join(" or ") : `any one of ${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
  }

  /** Map of the id of a withheld fact or setting to the rule ids of the checks in the file that rest on it. */
  function restedOnWithheld(profile) {
    const withheldIds = new Set([
      ...profile.facts.filter((fact) => fact.provenance === "withheld").map((fact) => fact.id),
      ...profile.settings.filter((setting) => setting.state === "withheld").map(settingKey),
    ]);
    const found = new Map();
    for (const entry of checkEntries(profile) || []) {
      for (const item of entry.restsOn) {
        if (!withheldIds.has(item)) continue;
        if (!found.has(item)) found.set(item, []);
        if (!found.get(item).includes(entry.rule_id)) found.get(item).push(entry.rule_id);
      }
    }
    return found;
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

  /** The three parts of the question sheet as Markdown lines, under headings of the level given. */
  function questionLines(profile, catalogue, heading) {
    const sheet = sheetOf(profile, catalogue);
    const allowed = (item) => (item.kind === "yes_no"
      ? "`yes` or `no`"
      : (item.kind === "multi" ? "one or more of " : "one of ") + item.values.map((entry) => `\`${entry.value}\` (${entry.label})`).join(", "));
    const items = [
      sheet.measured.map((item) => `- **${item.question}** (\`${item.id}\`)${cell(item.detail) ? `: ${cell(item.detail)}` : ""}`),
      sheet.operator.map((item) => `- **${item.question}** (\`${item.id}\`): ${allowed(item)}`),
      sheet.conversation.map((item) => `- **${item.question}** ${item.why}`),
    ];
    const lines = [];
    SHEET.parts.forEach(([title, intro], index) => lines.push("", `${heading} ${title}`, "", intro, "", ...(items[index].length ? items[index] : [SHEET.none])));
    if (sheet.leftOut.length) lines.push("", `${SHEET.leftOut}${sheet.leftOut.map(cell).join(", ")}.`);
    return lines.slice(1);
  }

  /** The sheet as Markdown: the text `--show-questions` prints for the same file. */
  function sheetMarkdown(profile, catalogue) {
    const lines = [
      `# ${SHEET.title}`,
      "",
      `- **Taken:** ${cell(profile.created_at)}`,
      `- **Reason:** ${profile.run.reason}`,
      `- **Reference:** ${cell(profile.run.reference) || "none"}`,
      "",
      ...questionLines(profile, catalogue, "##"),
    ];
    return `${lines.join("\n")}\n`;
  }

  // The call sheet of a profile, as profile.render_sheet writes it: what works well, what
  // does not, what to improve, what was not assessed. Only the sheet's own fixed text is
  // written here; everything else is the file's.
  const BRIEF = [
    ["n8n.version", "n8n version"], ["executions.mode", "Execution mode"], ["main.replicas", "Main replicas"],
    ["worker.replicas", "Worker replicas"], ["database.engine", "Database type"], ["usage.executions_per_day", "Executions per day"],
    ["usage.failure_rate_percent", "Failure rate"], ["usage.active_workflows", "Active workflows"], ["usage.users", "Users"],
  ];
  const ESTIMATED = ["usage.executions_per_day", "usage.failure_rate_percent"];
  const SEVERITY_ORDER = ["critical", "warning", "info"];
  const SHEET_STALE_DAYS = 30;
  const CALL = {
    title: "n8n call sheet",
    passedIntro: "Checks that read the values they need and found the good condition.",
    noPassed: "No check passed.",
    noFinding: "No finding.",
    partial: "The check was partial: not every source was collected or not every rule ran.",
    complete: "Every source was collected and every rule ran.",
    notInProfile: "Rests on values that are not in the profile: they are in the report the operator keeps.",
    noRemedy: "Nothing to improve was found.",
    allAssessed: "Every check that applies was assessed.",
    allApply: "No check was left out as not applying.",
    checkWithheld: "The operator withheld the check outcome: what passed, the findings and what was not assessed are not in the file.",
    noChecks: "This file was written by a version of n8n-healthcheck that does not record what passed, the impact and the remedy of a finding, what was not assessed or what does not apply.",
    notStated: "A source was read and does not say",
    unreadable: "The value could not be read (a Secret, a file, a redacted value or an incomplete environment)",
    disagree: "Two sources given in the run disagree on what the check reads",
    ruleError: "The rule failed to run, which is a bug in n8n-healthcheck",
  };

  /** Whole days from the day a profile was taken to `today` (a Date, read in UTC); null when the file's date is not one. */
  function ageDays(createdAt, today) {
    const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})/.exec([...createdAt].slice(0, 10).join(""));
    if (!match) return null;
    const [year, month, day] = match.slice(1).map(Number);
    const taken = new Date(Date.UTC(year, month - 1, day));
    taken.setUTCFullYear(year); // Date.UTC reads a year below 100 as 19xx
    if (year < 1 || taken.getUTCMonth() !== month - 1 || taken.getUTCDate() !== day) return null;
    return Math.round((Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()) - taken.getTime()) / 86400000);
  }

  /**
   * The call sheet as data, for the page and for its Markdown: no text of it is escaped here.
   * { age (days, or null when not old), brief: [{ label, value, note }], withheld, entries (null
   * for a file without checks), passed: Map(area -> entries), findings, groups: Map(heading -> entries),
   * notApplicable, withheldLine (what was withheld, in words), titles (the findings of a file without checks), partial }
   */
  function callSheetOf(profile, today, escape = (text) => text) {
    const facts = new Map();
    for (const fact of profile.facts) if (!facts.has(fact.id)) facts.set(fact.id, fact);
    const settings = new Map();
    for (const setting of profile.settings) if (!settings.has(settingKey(setting))) settings.set(settingKey(setting), setting);
    const known = (fact) => fact !== undefined && (fact.provenance === "measured" || fact.provenance === "declared");

    const age = ageDays(profile.created_at, today);
    const estimate = facts.get("usage.estimate");
    const brief = BRIEF.map(([id, label]) => {
      const fact = facts.get(id);
      // A fact the file does not hold (an older version wrote it) is unknown.
      if (!known(fact)) return { label, value: fact !== undefined && fact.provenance === "withheld" ? "withheld" : "unknown", note: "" };
      let value = escape(shown(fact.value)) + (id === "usage.failure_rate_percent" ? "%" : "");
      let note = fact.provenance;
      if (ESTIMATED.includes(id)) {
        if (!known(estimate)) note += ", may be an estimate";
        else if (estimate.value === true) value = `about ${value}`;
      }
      return { label, value, note };
    });

    const check = profile.check;
    const entries = checkEntries(profile);
    const sheet = { age: age !== null && age > SHEET_STALE_DAYS ? age : null, brief, withheld: check.withheld === true, entries, partial: check.partial === true };
    if (sheet.withheld) return sheet;
    if (entries === null) return { ...sheet, titles: check.findings };

    const of = (outcome) => entries.filter((entry) => entry.known && entry.outcome === outcome);
    const passed = new Map();
    for (const entry of of("passed")) {
      if (!passed.has(entry.area)) passed.set(entry.area, []);
      passed.get(entry.area).push(entry);
    }
    const rank = (entry) => (SEVERITY_ORDER.includes(entry.severity) ? SEVERITY_ORDER.indexOf(entry.severity) : SEVERITY_ORDER.length);
    // One id of rests_on and what the file holds for it, withheld or not as the operator chose.
    const rested = (item) => {
      const id = escape(item);
      if (settings.has(item)) {
        const setting = settings.get(item);
        if (setting.state === "value") return { id, says: `= ${escape(setting.value) || "(empty)"}` };
        return { id, says: setting.state === "withheld" ? "withheld" : "is set (value not written)" };
      }
      if (facts.has(item)) {
        const fact = facts.get(item);
        return { id, says: known(fact) ? `= ${escape(shown(fact.value))}` : fact.provenance };
      }
      return { id, says: "is not in the file" };
    };
    const findings = of("finding").sort((a, b) => rank(a) - rank(b)) // stable: rule order within a severity
      .map((entry) => ({ ...entry, rested: entry.restsOn.map(rested) }));

    const groups = new Map(); // heading -> entries, in the order the sheet prints them
    const add = (heading, entry) => {
      if (!groups.has(heading)) groups.set(heading, []);
      groups.get(heading).push(entry);
    };
    const notAssessed = of("not_assessed");
    for (const [reason, heading] of [["source_missing", "With "], ["source_failed", "Asked for and not collected: "]]) {
      for (const entry of notAssessed) if (entry.reason === reason) add(heading + (anyOf(entry.needs, escape) || "a source that states it"), entry);
    }
    for (const [reason, heading] of [["not_stated", CALL.notStated], ["value_unreadable", CALL.unreadable], ["sources_disagree", CALL.disagree], ["rule_error", CALL.ruleError]]) {
      for (const entry of notAssessed) if (entry.reason === reason) add(heading, entry);
    }
    for (const entry of entries) if (!entry.known) add(NOT_KNOWN.charAt(0).toUpperCase() + NOT_KNOWN.slice(1), entry);
    const said = of("withheld").length ? [`Checks withheld by the operator: ${of("withheld").length}.`] : [];
    const areas = check.scores.filter(scoreWithheld).map((row) => escape(row.area));
    // Whatever the check had stated: the score would tell whether it was a finding.
    if (areas.length) said.push(`The score of its area is withheld with a check: ${areas.join(", ")}.`);
    return { ...sheet, passed, findings, groups, notApplicable: of("not_applicable"), withheldLine: said.join(" ") };
  }

  /** The call sheet as Markdown: the text `--show-sheet` prints for the same file on the same day. */
  function callSheetMarkdown(profile, catalogue, today) {
    const sheet = callSheetOf(profile, today, cell);
    const item = (entry) => `${cell(entry.rule_id)} ${cell(entry.text)}`;
    const upper = (text) => text.charAt(0).toUpperCase() + text.slice(1);
    const lines = [
      `# ${CALL.title}`,
      "",
      "## Run",
      "",
      `- **Taken:** ${cell(profile.created_at)}`,
      `- **Reason:** ${profile.run.reason}`,
      `- **Reference:** ${cell(profile.run.reference) || "none"}`,
      `- **Written by:** n8n-healthcheck ${cell(profile.tool_version)}`,
      `- **Sources:** ${Object.entries(profile.sources).map(([name, state]) => `${cell(name)} ${state.replace("_", " ")}`).join("; ")}`,
    ];
    if (sheet.age !== null) lines.push("", `This profile was taken ${sheet.age} days ago: the environment may have changed since.`);
    lines.push("", "## The environment in brief", "");
    for (const row of sheet.brief) lines.push(`- **${row.label}:** ${row.value}${row.note ? ` (${row.note})` : ""}`);

    if (sheet.withheld) {
      lines.push("", "## Check outcome", "", CALL.checkWithheld);
    } else if (sheet.entries === null) {
      lines.push("", "## What does not", "");
      sheet.titles.forEach((finding, index) => lines.push(`${index + 1}. **${cell(finding.rule_id)}** (${cell(finding.severity)}, ${cell(finding.area)}): ${cell(finding.title)}`));
      if (!sheet.titles.length) lines.push(`${CALL.noFinding} ${sheet.partial ? CALL.partial : CALL.complete}`);
      lines.push("", CALL.noChecks);
    } else {
      lines.push("", "## What works well", "", CALL.passedIntro, "");
      for (const [area, entries] of sheet.passed) {
        lines.push(`### ${upper(cell(area))}`, "", ...entries.map((entry) => `- ${item(entry)}${entry.byDefault ? ` (${BY_DEFAULT})` : ""}`), "");
      }
      if (!sheet.passed.size) lines.push(CALL.noPassed, "");
      lines.push("## What does not", "");
      sheet.findings.forEach((entry, index) => lines.push(
        `${index + 1}. **${cell(entry.rule_id)}** (${cell(entry.severity)}, ${cell(entry.area)}): ${cell(entry.text)}`,
        `   - Impact: ${cell(entry.impact)}`,
        `   - ${entry.rested.length ? `Rests on: ${entry.rested.map((row) => `\`${row.id}\` ${row.says}`).join("; ")}` : CALL.notInProfile}`));
      if (!sheet.findings.length) lines.push(`${CALL.noFinding} ${sheet.partial ? CALL.partial : CALL.complete}`);
      lines.push("", "## What to improve", "");
      sheet.findings.forEach((entry, index) => lines.push(`${index + 1}. **${cell(entry.rule_id)}, ${cell(entry.text)}:** ${cell(entry.remedy)}`));
      if (!sheet.findings.length) lines.push(CALL.noRemedy);
      lines.push("", "## Not assessed", "");
      for (const [heading, entries] of sheet.groups) lines.push(`- **${heading}:** ${entries.map(item).join("; ")}`);
      if (!sheet.groups.size) lines.push(CALL.allAssessed);
      lines.push("", "## Does not apply", "");
      lines.push(sheet.notApplicable.length ? sheet.notApplicable.map(item).join("; ") : CALL.allApply);
      if (sheet.withheldLine) lines.push("", sheet.withheldLine);
    }
    lines.push("", "## Questions still open", "", ...questionLines(profile, catalogue, "###"), "");
    return lines.join("\n");
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
    const table = (first, groups, narrow) => el("table", { class: narrow ? "ptable ptable--ids" : "ptable" },
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

    // Nothing is withheld for the reader: a withheld entry names the checks that still rest on it.
    const rested = restedOnWithheld(profile);
    const restedNote = (key) => (rested.has(key)
      ? el("p", { class: "pdetail prested" }, `${edit ? "Still in the file, resting on this entry" : "Checks in the file that rest on this entry"}: ${rested.get(key).join(", ")}.${edit ? " Their text can say something about it: withhold them too if it should not." : ""}`)
      : null);

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
          fact.detail ? el("p", { class: "pdetail" }, fact.detail) : null,
          restedNote(fact.id)),
        edit ? el("td", { class: "ptable__withhold" }, fact.id === ALWAYS_SHARED
          ? el("span", { class: "pdetail", id: "always-shared" }, "Always in the file")
          : tick(`w-fact-${index}`, fact.id, `the fact ${fact.id}`)) : null);
    });
    const measured = profile.facts.filter((fact) => fact.provenance === "measured" || fact.provenance === "declared").length;
    const factsWithheld = profile.facts.filter((fact) => fact.provenance === "withheld").length;

    const settings = grouped(profile.settings, (setting) => setting.scope, (setting, index) => el("tr", null,
      el("th", { scope: "row" }, ...breakable(setting.name)),
      el("td", null, el("p", { class: "pline" },
        setting.state === "value" ? el("span", { class: "pvalue" }, setting.value || "(empty)")
          : setting.state === "set" ? el("span", { class: "pdetail" }, "set (value not written)")
            : stateTag("withheld")),
        restedNote(settingKey(setting))),
      edit ? el("td", { class: "ptable__withhold" }, tick(`w-setting-${index}`, settingKey(setting), `the setting ${setting.name} of ${setting.scope}`)) : null));

    const check = profile.check;
    const outcome = [];
    if (edit) {
      outcome.push(el("label", { class: "check check--row" },
        el("input", { type: "checkbox", id: "w-check", "data-withhold": "check", checked: withheld.has("check") }),
        el("span", null, "Withhold the check outcome (the scores, the list of findings and every check)")));
    }
    if (check.withheld === true) {
      outcome.push(el("p", { class: "block__note" }, stateTag("withheld"), " The scores and the findings are not in the file; it only says that they were withheld."));
    } else {
      outcome.push(el("table", { class: "ptable" },
        el("thead", null, el("tr", null, el("th", { scope: "col" }, "Area"), el("th", { scope: "col" }, "Score"))),
        el("tbody", null, ...check.scores.map((row) => el("tr", null,
          el("th", { scope: "row" }, row.area),
          el("td", null, scoreWithheld(row) ? stateTag("withheld") : row.score === null ? "n/a" : `${numberText(row.score)}/100${row.partial ? " (partial)" : ""}`))))));
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
      const entries = checkEntries(profile);
      if (entries !== null) {
        // Every entry of check.checks with every text it holds.
        const line = (name, text) => el("p", { class: "pdetail" }, el("span", { class: "pcheck__name" }, `${name}: `), text);
        const rows = grouped(entries, (entry) => entry.area, (entry, index) => {
          const about = [];
          if (!entry.known) about.push(stateTag("unknown"), el("span", { class: "pdetail" }, NOT_KNOWN));
          else if (entry.outcome === "finding") about.push(severityTag(entry.severity));
          else if (entry.outcome === "withheld") about.push(stateTag("withheld"));
          else about.push(el("span", { class: `tag tag--${entry.outcome === "passed" ? "passed" : "reason"}` }, OUTCOME_LABELS[entry.outcome]));
          const why = entry.known && entry.outcome === "not_assessed"
            ? `${capitalize(REASON_LABELS[entry.reason])}${entry.needs.length ? `. It needs ${anyOf(entry.needs)}` : ""}.`
            : entry.known && entry.outcome === "passed" && entry.byDefault ? `${capitalize(BY_DEFAULT)}.` : "";
          return el("tr", null,
            el("th", { scope: "row", translate: "no" }, entry.rule_id),
            el("td", null,
              el("p", { class: "pline" }, entry.text ? el("span", { class: "pcheck__text" }, entry.text) : null, ...about),
              why ? el("p", { class: "pdetail" }, why) : null,
              entry.impact ? line("Impact", entry.impact) : null,
              entry.remedy ? line("Remedy", entry.remedy) : null,
              entry.restsOn.length ? line("Rests on", entry.restsOn.join(", ")) : null),
            edit ? el("td", { class: "ptable__withhold" }, tick(`w-check-${index}`, checkKey(entry), `the check ${entry.rule_id}`)) : null);
        });
        outcome.push(
          // One level under the title of this part, which is an h4 in the review and an h3 in an opened profile.
          edit ? el("h5", { class: "pchecks__title" }, `Checks (${entries.length})`) : el("h4", { class: "pchecks__title" }, `Checks (${entries.length})`),
          el("p", { class: "block__note" }, lead("What every rule stated, with every text written to the file."), " Each text is a fixed sentence of the rule, never a value of this instance.",
            edit ? " A check can be withheld on its own: it stays in the file with its rule id only, its finding leaves the list above, and the score of its area is withheld with it, whatever the check says." : ""),
          entries.length ? table("Check", rows, true) : el("p", { class: "block__note" }, "No check is listed."));
      }
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
      const kept = answers.get(question.id);
      const chosen = multi ? (Array.isArray(kept) ? kept : []) : typeof kept === "string" ? kept : "";
      const option = (value, label, number) => {
        const input = el("input", {
          type: multi ? "checkbox" : "radio", name: `q-${index}`, id: `q-${index}-${number}`, value, "data-question": question.id,
          // Nothing is selected as an answer at the start; an answer given for the run before is still selected.
          checked: multi ? chosen.includes(value) : value === chosen,
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

  /**
   * How much the run decided, from the numbers the server counted on its own profile before
   * anything was withheld. Shown on top of the review, with what each missing source would add.
   */
  function renderCoverage(coverage) {
    const known = coverage && coverage.waiting && coverage.failed;
    $("coverage-block").hidden = !known;
    if (!known) return;
    const total = whole(coverage.total);
    const decided = whole(coverage.decided);
    $("coverage-decided").textContent = decided === total ? `This run decided all ${plural(total, "check")}.` : `This run decided ${decided} of ${plural(total, "check")}.`;
    $("coverage-rest").textContent = decided === total ? "No source would add a check." : "What the others need:";
    const lines = [];
    const sources = ["url", "env", "postgres", "redis", "kubernetes", "debug_info"];
    for (const source of sources) {
      const waiting = whole(coverage.waiting[source]);
      if (waiting) lines.push(`${plural(waiting, "check")} can be decided with ${NEED_LABELS[source]}, which was not given.`);
    }
    for (const source of sources) {
      const failed = whole(coverage.failed[source]);
      if (failed) lines.push(`${checksThat(failed, "need")} ${NEED_LABELS[source]}, which was asked for and could not be read.`);
    }
    const bySource = lines.length;
    if (whole(coverage.unstated)) lines.push(`${checksThat(whole(coverage.unstated), "need")} a source that states a setting none of the sources of this run shows, or a value that could not be read.`);
    if (whole(coverage.disagree)) lines.push(`${plural(whole(coverage.disagree), "check")}: two sources of this run disagree on what the check reads.`);
    if (whole(coverage.errors)) lines.push(`${plural(whole(coverage.errors), "check")} failed to run, which is a bug in n8n-healthcheck.`);
    $("coverage-list").hidden = lines.length === 0;
    $("coverage-list").replaceChildren(...lines.map((line) => el("li", null, line)));
    $("coverage-note").hidden = bySource === 0;
    $("coverage-action").hidden = bySource === 0;
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
    // An answer kept from a run before counts only while its question is still asked.
    const given = [...asked.keys()].filter((id) => answers.has(id)).length;
    const answered = asked.size ? `Answered: ${given} of ${plural(asked.size, "question")}. ` : "";
    const checks = checkEntries(profile);
    const checksWithheld = checks ? checks.filter((entry) => entry.outcome === "withheld").length : 0;
    $("share-kept").hidden = !(runs > 1 && (withheld.size > 0 || answers.size > 0 || $("f-reference").value !== "" || $("f-reason").value !== Object.keys(PROFILE_REASONS)[0]));
    $("share-summary").textContent = `${answered}Withheld: ${facts} of ${plural(profile.facts.length, "fact")}, ${settings} of ${plural(profile.settings.length, "setting")}, check outcome ${profile.check.withheld === true ? "withheld" : "included"}${checksWithheld ? ` (${checksWithheld} of ${plural(checks.length, "check")} withheld)` : ""}.`;
  }

  /** Show the state of the reference field; return true when it can be stored. */
  function checkReference() {
    const reference = stripReference($("f-reference").value);
    const problem = referenceProblem(reference);
    $("reference-count").textContent = `${[...reference].length} of ${REFERENCE_MAX} characters.`;
    setFieldError("e-reference", problem, [$("f-reference")]);
    return !problem;
  }

  /** The name the file is saved under: the day the profile was taken. */
  function profileFileName(profile) {
    const day = /^\d{4}-\d{2}-\d{2}/.exec(String(profile.created_at));
    return `n8n-profile-${day ? day[0] : "undated"}.json`;
  }

  // Step 6: the name of the file and what it holds, as step 5 left it.
  function renderDownload() {
    $("file-name").textContent = profileFileName(currentProfile());
    $("file-holds").textContent = $("share-summary").textContent;
    $("download-status").textContent = "";
  }

  function continueFromShare() {
    if (!checkReference()) {
      $("f-reference").focus();
      return;
    }
    go(6);
  }

  function downloadProfile() {
    // Step 6 opens only for the profile of a run, which only a run with a checked debug information has.
    if (!canOpen(6)) return;
    if (!checkReference()) {
      showStep(5);
      $("f-reference").focus();
      return;
    }
    const profile = currentProfile();
    download(profileFileName(profile), "application/json", profileText(profile), "download-status");
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
    $("profile-body").replaceChildren(callSheetSection(data), ...profileSections(data, false), sheetSection(data));
    showProfile(true);
    announce("Profile opened.");
  }

  /** A button that copies `text`, with its status line and, when the browser refuses the clipboard, the same text to copy by hand. */
  function copyControls(id, text, what) {
    const status = el("p", { class: "action-status", role: "status", "aria-live": "polite" });
    const byHand = el("textarea", { class: "sheet__text", id: `${id}-text`, rows: "10", readonly: true, hidden: true, "aria-label": `The ${what} as Markdown` });
    const button = el("button", { type: "button", class: "btn", id: `${id}-copy` }, "Copy as Markdown");
    button.addEventListener("click", async () => {
      const copied = await copyText(text, button);
      byHand.hidden = copied;
      if (!copied) {
        byHand.value = text;
        byHand.focus();
        byHand.select();
      }
      status.textContent = copied
        ? `${capitalize(what)} copied to the clipboard as Markdown.`
        : "The browser did not allow copying. The sheet is in the box below: copy it from there.";
    });
    return [el("div", { class: "sheet__actions" }, button), status, byHand];
  }

  /**
   * "Call sheet" for an opened profile: what works well, what does not, what to improve and
   * what was not assessed, for a call with whoever runs the instance. Made in this browser
   * from the file alone; every string of the file is inserted as text.
   */
  function callSheetSection(profile) {
    const sheet = callSheetOf(profile, new Date());
    const catalogue = meta && meta.questions;
    const part = (title, ...children) => el("section", { class: "ppart" }, el("h4", null, title), ...children);
    const note = (...children) => el("p", { class: "block__note" }, ...children);
    const item = (entry, ...more) => el("li", null, el("code", { translate: "no" }, entry.rule_id), " ", entry.text, ...more);
    const tags = (entry) => el("span", { class: "finding__tags" }, severityTag(entry.severity), el("span", { class: "finding__id", translate: "no" }, entry.rule_id), el("span", { class: "tag tag--area" }, entry.area));
    const none = sheet.partial ? CALL.partial : CALL.complete;

    const parts = [
      part("The environment in brief", el("dl", { class: "pdetails" }, ...sheet.brief.flatMap((row) => [
        el("dt", null, row.label),
        el("dd", null, row.note ? el("span", { class: "pvalue" }, row.value) : stateTag(row.value), row.note ? el("span", { class: "pdetail" }, ` ${row.note}`) : null),
      ]))),
    ];
    if (sheet.withheld) {
      parts.push(part("Check outcome", note(stateTag("withheld"), ` ${CALL.checkWithheld}`)));
    } else if (sheet.entries === null) {
      parts.push(part(`What does not (${sheet.titles.length})`,
        sheet.titles.length
          ? el("ol", { class: "pfindings" }, ...sheet.titles.map((finding) => el("li", null, el("span", { class: "finding__title" }, finding.title), tags(finding))))
          : note(`${CALL.noFinding} ${none}`),
        note(CALL.noChecks)));
    } else {
      const passedCount = [...sheet.passed.values()].reduce((sum, entries) => sum + entries.length, 0);
      parts.push(
        part(`What works well (${passedCount})`, note(CALL.passedIntro),
          ...(passedCount ? [...sheet.passed].flatMap(([area, entries]) => [
            el("h5", { class: "sheet__area" }, capitalize(area)),
            el("ul", { class: "sheet" }, ...entries.map((entry) => item(entry, entry.byDefault ? el("p", { class: "pdetail" }, `${capitalize(BY_DEFAULT)}.`) : null))),
          ]) : [note(CALL.noPassed)])),
        part(`What does not (${sheet.findings.length})`,
          sheet.findings.length
            ? el("ol", { class: "pfindings" }, ...sheet.findings.map((entry) => el("li", null,
              el("span", { class: "finding__title" }, entry.text), tags(entry),
              el("p", { class: "pdetail" }, el("span", { class: "pcheck__name" }, "Impact: "), entry.impact),
              el("p", { class: "pdetail" }, entry.rested.length ? el("span", { class: "pcheck__name" }, "Rests on: ") : null, entry.rested.length ? entry.rested.map((row) => `${row.id} ${row.says}`).join("; ") : CALL.notInProfile))))
            : note(`${CALL.noFinding} ${none}`)),
        part(`What to improve (${sheet.findings.length})`,
          sheet.findings.length
            ? el("ol", { class: "sheet" }, ...sheet.findings.map((entry) => el("li", null,
              el("strong", null, entry.text), " ", el("code", { translate: "no" }, entry.rule_id),
              el("p", { class: "pdetail" }, entry.remedy))))
            : note(CALL.noRemedy)),
        part(`Not assessed (${[...sheet.groups.values()].reduce((sum, entries) => sum + entries.length, 0)})`,
          sheet.groups.size
            ? el("ul", { class: "sheet" }, ...[...sheet.groups].map(([heading, entries]) => el("li", null,
              el("strong", null, heading),
              el("ul", { class: "sheet sheet--inner" }, ...entries.map((entry) => item(entry))))))
            : note(CALL.allAssessed)),
        part(`Does not apply (${sheet.notApplicable.length})`,
          sheet.notApplicable.length ? el("ul", { class: "sheet" }, ...sheet.notApplicable.map((entry) => item(entry))) : note(CALL.allApply),
          sheet.withheldLine ? note(sheet.withheldLine) : null));
    }

    return el("section", { class: "block" }, el("h3", null, "Call sheet"),
      note(lead("An outline for a call with whoever runs this instance."), " It is made in this browser from the file alone and says nothing the file does not. “Copy as Markdown” copies it whole, with the run details and the questions still open."),
      sheet.age !== null ? note(el("span", { class: "tag tag--partial" }, "Old"), ` This profile was taken ${sheet.age} days ago: the environment may have changed since.`) : null,
      ...(catalogue
        ? copyControls("call", callSheetMarkdown(profile, catalogue, new Date()), "call sheet")
        : [note("The list of questions did not load from the server, so the sheet cannot be copied. Check that “n8n-healthcheck serve” is still running, reload the page and open the file again.")]),
      ...parts);
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
      ...copyControls("sheet", text, "question sheet"),
      ...SHEET.parts.map(([name, intro], index) => el("section", { class: "ppart" },
        el("h4", null, `${name} (${rows[index].length})`),
        el("p", { class: "block__note" }, intro),
        rows[index].length ? el("ul", { class: "sheet" }, ...rows[index]) : el("p", { class: "block__note sheet__none" }, SHEET.none))),
      sheet.leftOut.length ? el("p", { class: "block__note sheet__left-out" }, `${SHEET.leftOut}${sheet.leftOut.join(", ")}.`) : null);
  }

  for (const [value, label] of Object.entries(PROFILE_REASONS)) $("f-reason").append(el("option", { value }, label));
  $("f-reason").value = Object.keys(PROFILE_REASONS)[0]; // once, when the page loads: a new run does not put it back
  checkReference();
  $("f-reference").addEventListener("input", checkReference);
  $("share-form").addEventListener("submit", (event) => {
    event.preventDefault();
    continueFromShare();
  });
  $("profile-download").addEventListener("click", () => downloadProfile());
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
    if (!key || key === ALWAYS_SHARED) return;
    if (event.target.checked) withheld.add(key);
    else withheld.delete(key);
    renderShare();
  });
  $("share-body").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-bulk]");
    if (!button) return;
    const keys = button.dataset.bulk === "facts"
      ? report.profile.facts.map((fact) => fact.id).filter((id) => id !== ALWAYS_SHARED)
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
  function closeProfile() {
    $("profile-body").replaceChildren(); // nothing of the file is kept
    $("profile-meta").textContent = "";
  }
  $("profile-close").addEventListener("click", () => {
    closeProfile();
    showProfile(false);
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
  $("debug-form").addEventListener("submit", (event) => {
    event.preventDefault();
    continueFromDebug();
  });
  $("sources-form").addEventListener("submit", (event) => {
    event.preventDefault();
    continueFromSources();
  });
  for (let n = 1; n <= STEP_COUNT; n += 1) $(`go-${n}`).addEventListener("click", () => go(n));
  for (const [id, target] of [["next-1", 2], ["back-2", 1], ["back-3", 2], ["back-4", 3], ["next-4", 5], ["back-5", 4], ["back-6", 5], ["coverage-back", 3]]) {
    $(id).addEventListener("click", () => go(target));
  }
  // A text that was edited is no longer the one the server read: steps 3 to 6 close until it is checked again.
  $("f-debug").addEventListener("input", renderStepper);
  // The browser's Back and Forward move between the steps; the entries hold a step number only.
  window.history.replaceState({ step }, "");
  window.addEventListener("popstate", (event) => {
    const wanted = event.state && Number.isInteger(event.state.step) ? event.state.step : 1;
    if (!$("view-profile").hidden) closeProfile();
    showStep(wanted === step || canOpen(wanted) ? wanted : step, false);
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
  for (const input of document.querySelectorAll("#debug-form textarea, #sources-form input, #sources-form textarea")) {
    input.addEventListener("input", () => {
      // The error is the last id: a field may name its hint before it.
      const errorId = (input.getAttribute("aria-describedby") || "").split(" ").pop();
      if (errorId && !$(errorId).hidden) setFieldError(errorId, null, [input]);
      // The key's error that asks for the URL is answered in the URL field.
      if (input.id === "f-url" && $("e-apikey").textContent === KEY_NEEDS_URL) setFieldError("e-apikey", null, [$("f-apikey")]);
    });
  }

  $("act-md").addEventListener("click", () => download(`n8n-healthcheck-report-${fileStamp(report.finishedAt)}.md`, "text/markdown;charset=utf-8", report.markdown || ""));
  $("act-json").addEventListener("click", () => download(`n8n-healthcheck-snapshot-${fileStamp(report.finishedAt)}.json`, "application/json", `${JSON.stringify(report.snapshot ?? null, null, 2)}\n`));
  $("act-copy").addEventListener("click", copyMarkdown);
  $("act-print").addEventListener("click", () => window.print());

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
