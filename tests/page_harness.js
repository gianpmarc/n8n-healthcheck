// Runs the web page's own script (n8n_healthcheck/web/static/app.js) under Node.js against
// a stand-in for the browser's document, so that tests/test_web_profile.py can click its
// controls and read what it shows and saves. Used by the tests only.
//
// The stand-in is deliberately small: it has the calls the page uses and no other. It
// cannot parse HTML at all, so a change that sends text through an HTML parser fails here.
//
// Input (stdin, JSON): { app: path of app.js, index: path of index.html, scenario: "async (page, input) => ...",
//                        input: any, withoutRawJSON: boolean, meta: what GET /api/meta answers (the server's own),
//                        noClipboard: boolean (the browser refuses to copy) }
// Output (stdout, JSON): what the scenario returned.
"use strict";

const fs = require("fs");
const vm = require("vm");

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const byId = new Map();
const created = []; // every element the page made, in order

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.attributes = {};
    this.children = []; // elements and strings (text nodes)
    this.parent = null;
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.className = "";
    this.style = { setProperty() {} };
    this.classList = { toggle() {} };
    this.dataset = new Proxy({}, {
      get: (_, key) => this.attributes[`data-${String(key)}`],
      set: (_, key, value) => { this.attributes[`data-${String(key)}`] = String(value); return true; },
    });
  }
  get id() { return this.attributes.id || ""; }
  set id(value) { this.setAttribute("id", value); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "id") byId.set(String(value), this);
    if (name === "hidden" || name === "disabled" || name === "checked") this[name] = true;
    if (name === "value" || name === "type") this[name] = String(value);
  }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof Element) node.parent = this;
      else if (typeof node !== "string") throw new TypeError("only elements and text can be appended");
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); this.parent = null; }
  get textContent() { return this.children.map((child) => (typeof child === "string" ? child : child.textContent)).join(""); }
  set textContent(value) { this.children = [String(value)]; }
  matches(selector) {
    const [, tag, name, attribute] = /^(\w+)?(?:\.([\w-]+))?(?:\[([\w-]+)\])?$/.exec(selector);
    return (!tag || tag === this.tagName) && (!name || this.className.split(" ").includes(name)) && (!attribute || attribute in this.attributes);
  }
  closest(selector) { for (let node = this; node; node = node.parent) if (node.matches(selector)) return node; return null; }
  contains(other) { for (let node = other; node; node = node.parent) if (node === this) return true; return false; }
  querySelectorAll() { return []; } // the page uses it for the form and the findings only, not exercised here
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  focus() { document.activeElement = this; }
  click() { return dispatch(this, "click"); }
  select() {}
}

/** Call the handlers for an event on `target` and on its ancestors, as a bubbling event does. */
function dispatch(target, type) {
  const event = { target, preventDefault() {} };
  for (let node = target; node; node = node.parent) for (const handler of node.listeners[type] || []) handler(event);
}

const document = {
  activeElement: null,
  body: new Element("body"),
  getElementById(id) {
    // An element of index.html: made on first use, since the page only reads and sets properties on it.
    if (!byId.has(id)) new Element("static").setAttribute("id", id);
    return byId.get(id);
  },
  createElement(tag) { const node = new Element(tag); created.push(node); return node; },
  createElementNS(_, tag) { return this.createElement(tag); },
  // Only the fields of the form are found, for the listeners that clear an inline error.
  querySelectorAll(selector) { return selector === "#run-form input, #run-form textarea" ? FIELDS.map((id) => this.getElementById(id)) : []; },
};
const FIELDS = ["f-debug", "f-url", "f-apikey", "f-env", "f-postgres", "f-redis", "f-kube-ns", "f-kube-ctx"];
// What index.html hides at the start is hidden here too.
for (const [, id] of fs.readFileSync(request.index, "utf8").matchAll(/<[^>]*\bid="([^"]+)"[^>]*\shidden[\s>]/g)) document.getElementById(id).hidden = true;
// The form fields are read through their fieldset (a source the server lacks is disabled).
for (const id of FIELDS) new Element("fieldset").append(document.getElementById(id));
// And each names its hint and its error, as in index.html.
for (const [, id, described] of fs.readFileSync(request.index, "utf8").matchAll(/<(?:input|textarea)\b[^>]*\bid="([^"]+)"[^>]*\baria-describedby="([^"]+)"/g)) document.getElementById(id).setAttribute("aria-describedby", described);

const saved = []; // Blobs handed to URL.createObjectURL: the downloads
const copied = []; // texts handed to the clipboard
const sent = []; // the bodies of the page's POST /api/run requests, as text
let answerRun = null; // resolves or rejects the page's pending POST /api/run
const response = (status, text) => ({ ok: status === 200, status, text: async () => text, json: async () => JSON.parse(text) });
const context = vm.createContext({
  document,
  window: { scrollTo() {}, print() {} },
  CSS: { supports: () => true },
  // Without a clipboard the page falls back to document.execCommand, which this document does not have either.
  navigator: request.noClipboard ? {} : { clipboard: { async writeText(text) { copied.push(text); } } },
  // The real URL, which the form uses to check an address, with the two calls a download makes.
  URL: Object.assign(class extends URL {}, { createObjectURL(blob) { saved.push(blob); return "blob:saved"; }, revokeObjectURL() {} }),
  fetch(url, options) {
    if (url === "/api/meta") return Promise.resolve(response(200, JSON.stringify({ version: "test", sources: [], ...request.meta })));
    if (url !== "/api/run") throw new Error(`the page asked for ${url}`);
    sent.push(options.body);
    return new Promise((resolve, reject) => { answerRun = { resolve, reject }; });
  },
  // Short waits happen; the long ones (a download's clean-up, the run's deadline) would only keep Node alive.
  setTimeout: (handler, delay) => (delay < 1000 ? setTimeout(handler, delay) : 0),
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  Blob, File, TextDecoder, TextEncoder, AbortController, Intl,
});
if (request.withoutRawJSON) vm.runInContext("delete JSON.rawJSON; delete JSON.isRawJSON;", context);
vm.runInContext(fs.readFileSync(request.app, "utf8"), context, { filename: "app.js" });

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
const walk = (node, found = []) => {
  for (const child of node.children) if (child instanceof Element) { found.push(child); walk(child, found); }
  return found;
};
const visible = (id) => !document.getElementById(id).hidden;

const page = {
  $: (id) => document.getElementById(id),
  visible,
  walk,
  settle,
  rawJSON: vm.runInContext("typeof JSON.rawJSON === 'function'", context),
  /** The view on screen: "form", "report" or "profile" (exactly one, or this throws). */
  view() {
    const shown = ["form", "report", "profile"].filter((name) => visible(`view-${name}`));
    if (shown.length !== 1) throw new Error(`views on screen: ${shown.join(", ") || "none"}`);
    return shown[0];
  },
  /** Press "Run health check"; the answer is given later with finishRun() or failRun(). */
  async startRun() {
    document.getElementById("f-env").value = "EXECUTIONS_MODE=queue";
    await this.submit();
  },
  /** Press "Run health check" with the form as it is. */
  async submit() {
    dispatch(document.getElementById("run-form"), "submit");
    await settle();
  },
  sent,
  async finishRun(text, status = 200) { answerRun.resolve(response(status, text)); await settle(); },
  async failRun() { answerRun.reject(new TypeError("network")); await settle(); },
  async run(text) { await this.startRun(); await this.finishRun(text); },
  /** The withhold checkboxes of the share step, by the id they withhold. */
  boxes() {
    const boxes = {};
    for (const node of walk(document.getElementById("share-body"))) if (node.tagName === "input") boxes[node.dataset.withhold] = node;
    return boxes;
  },
  toggle(box) { box.checked = !box.checked; dispatch(box, "change"); },
  /** The controls of "A few questions": { fact id: { value: input } }; "" is the "No answer" radio button. */
  questions() {
    const groups = {};
    for (const node of walk(document.getElementById("questions-list"))) {
      if (node.tagName === "input") (groups[node.dataset.question] = groups[node.dataset.question] || {})[node.value] = node;
    }
    return groups;
  },
  /** Select a radio button, as a browser does: the others of its group are cleared. */
  choose(radio) {
    for (const node of walk(document.getElementById("questions-list"))) if (node.attributes.name === radio.attributes.name) node.checked = false;
    radio.checked = true;
    dispatch(radio, "change");
  },
  copied,
  button(text) {
    const found = walk(document.getElementById("share-body")).filter((node) => node.tagName === "button" && node.textContent === text);
    if (found.length !== 1) throw new Error(`${found.length} buttons named ${text}`);
    return found[0];
  },
  type(id, text) { document.getElementById(id).value = text; dispatch(document.getElementById(id), "input"); },
  /** Press "Download profile"; the text of the file, or null when nothing was saved. */
  async download() {
    const before = saved.length;
    dispatch(document.getElementById("share-form"), "submit");
    await settle();
    return saved.length > before ? saved[saved.length - 1].text() : null;
  },
  /** Choose a file in "Open a profile". */
  async open(text, name = "n8n-profile.json") {
    const input = document.getElementById("f-profile");
    input.files = [new File([text], name)];
    dispatch(input, "change");
    await settle();
    const body = document.getElementById("profile-body");
    return {
      view: this.view(),
      error: visible("e-profile") ? document.getElementById("e-profile").textContent : "",
      shown: body.textContent,
      elements: walk(body).length,
    };
  },
  /** The tag names of every element the page has made so far. */
  tagsMade: () => [...new Set(created.map((node) => node.tagName))].sort(),
};

(async () => {
  await settle(); // GET /api/meta
  const result = await eval(`(${request.scenario})`)(page, request.input);
  // Leave only when the answer has been written out whole.
  process.stdout.write(JSON.stringify(result === undefined ? null : result), () => process.exit(0));
})().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
