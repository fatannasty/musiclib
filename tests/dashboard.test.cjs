const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

const html = readFileSync(join(__dirname, "../src/musiclib/server/static/index.html"), "utf8");
const appearance = html.match(/<script id="appearance-init">([\s\S]*?)<\/script>/)[1];
const source = html
  .match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nstartDashboard\(\);\s*$/, "");

class Element {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.attributes = new Map();
    this.textContent = "";
    this.disabled = false;
    this.isConnected = true;
    this.children = [];
    this.writes = 0;
  }
  set innerHTML(value) { this.html = value; this.writes++; }
  get innerHTML() { return this.html || ""; }
  setAttribute(key, value) { this.attributes.set(key, value); }
  hasAttribute(key) { return this.attributes.has(key); }
  removeAttribute(key) { this.attributes.delete(key); }
  replaceChildren() { this.children = []; }
}

function dashboard({storage, reducedMotion = false} = {}) {
  const nodes = new Map();
  const saved = new Map(), warnings = [], notices = [];
  const document = {
    documentElement: new Element(),
    addEventListener() {},
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new Element());
      return nodes.get(selector);
    },
    querySelectorAll() { return []; },
  };
  const context = vm.createContext({
    document, window: {
      addEventListener() {},
      matchMedia: () => ({matches: reducedMotion}),
      localStorage: storage || {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value)},
    },
    console: {...console, warn: (...args) => warnings.push(args)}, setTimeout, clearTimeout,
    confirm: () => true,
  });
  vm.runInContext(appearance, context);
  vm.runInContext(source, context);
  context.toast = (...args) => notices.push(args);
  return {context, nodes, document, saved, warnings, notices, run: script => vm.runInContext(script, context)};
}

test("appearance starts light, saves toggles, and restores dark mode before dashboard rendering", () => {
  const {context, document, saved} = dashboard();
  context.updateThemeToggle();
  const button = document.querySelector("#theme-toggle");
  assert.equal(button.attributes.get("aria-pressed"), "false");
  context.toggleTheme();
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(button.attributes.get("aria-pressed"), "true");
  assert.equal(saved.get("musiclib.theme"), "dark");
  const restored = dashboard({storage: {getItem: key => saved.get(key)}});
  assert.equal(restored.document.documentElement.dataset.theme, "dark");
  context.toggleTheme();
  assert.equal(button.attributes.get("aria-pressed"), "false");
  assert.equal(saved.get("musiclib.theme"), "light");
});

test("blocked browser storage leaves theme controls usable and reports the unsaved preference", () => {
  const storage = {
    getItem() { throw new Error("Storage blocked"); },
    setItem() { throw new Error("Storage blocked"); },
  };
  const {context, document, warnings, notices} = dashboard({storage});
  assert.equal(warnings.length, 1);
  assert.doesNotThrow(() => context.toggleTheme());
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(warnings.length, 2);
  assert.match(notices[0][0], /could not save/);
  assert.equal(notices[0][1], true);
});

test("review scrolling is immediate when reduced motion is requested", () => {
  for (const reducedMotion of [true, false]) {
    const {context} = dashboard({reducedMotion});
    let behavior;
    context.revealReview({scrollIntoView: options => { behavior = options.behavior; }});
    assert.equal(behavior, reducedMotion ? "auto" : "smooth");
  }
});

function reviewRoot() {
  const root = new Element();
  const close = new Element(), title = new Element(), status = new Element(), content = new Element();
  const slots = [new Element(), new Element()];
  const steps = ["select", "preview", "confirm", "results"]
    .map(workspaceStep => new Element({workspaceStep}));
  root.querySelector = selector => ({
    "[data-workspace-close]": close, "[data-workspace-title]": title,
    "[data-workspace-status]": status,
    "[data-workspace-content]": content,
  })[selector];
  root.querySelectorAll = selector => selector === "[data-review-slot]" ? slots : steps;
  return {root, close, title, status, slots, steps};
}

test("declining replacement preserves the existing review and its choices", () => {
  const {context} = dashboard();
  const {root, slots, title} = reviewRoot();
  const workspace = context.createReviewWorkspace(root);
  const first = workspace.begin("manual", "Edit tags");
  slots[0].children.push("unsaved tag");
  context.confirm = () => false;
  assert.equal(workspace.begin("rename", "Organize"), null);
  assert.equal(workspace.owns(first), true);
  assert.deepEqual(slots[0].children, ["unsaved tag"]);
  assert.equal(title.textContent, "Edit tags");
  assert.equal(workspace.leave(), false);
});

test("replacement clears old panels and invalidates late asynchronous results", () => {
  const {context} = dashboard();
  const {root, slots, status} = reviewRoot();
  const workspace = context.createReviewWorkspace(root);
  const first = workspace.begin("artwork", "Artwork");
  slots[1].children.push("old preview");
  const next = workspace.begin("rename", "Organize");
  assert.equal(workspace.owns(first), false);
  assert.equal(workspace.owns(next), true);
  assert.equal(slots[1].children.length, 0);
  workspace.fail(first, "late error from old request");
  assert.equal(status.textContent, "");
  workspace.fail(next, "current error");
  assert.equal(status.textContent, "current error");
});

test("a running review cannot be replaced; completed results close without a discard prompt", () => {
  const {context} = dashboard();
  const {root, steps} = reviewRoot();
  const workspace = context.createReviewWorkspace(root);
  const token = workspace.begin("manual", "Edit tags");
  workspace.busy(token, true);
  assert.equal(workspace.begin("rename", "Organize"), null);
  assert.equal(workspace.owns(token), true);
  workspace.busy(token, false);
  workspace.stage(token, "results");
  assert.equal(steps[3].attributes.get("aria-current"), "step");
  context.confirm = () => { throw Error("Completed results need no discard prompt"); };
  assert.equal(workspace.leave(), true);
  assert.equal(workspace.active, false);
  assert.equal(root.hidden, true);
});

test("job locks respect search/write lanes without overriding local disabled state", () => {
  const {context, run} = dashboard();
  const write = new Element(), search = new Element(), locallyBusy = new Element();
  write.setAttribute("data-operation", "");
  locallyBusy.setAttribute("data-operation", "");
  locallyBusy.disabled = true;
  search.setAttribute("data-lookup-operation", "");
  const root = {querySelectorAll: () => [write, search, locallyBusy]};
  run('state.jobs = [{status:"running", read_only:false}]');
  context.applyJobLocks(root);
  assert.equal(write.attributes.get("aria-disabled"), "true");
  assert.equal(search.hasAttribute("aria-disabled"), false);
  run('state.jobs = [{status:"stopping", read_only:true}]');
  context.applyJobLocks(root);
  assert.equal(write.hasAttribute("aria-disabled"), false);
  assert.equal(search.attributes.get("aria-disabled"), "true");
  run("state.jobs = []");
  context.applyJobLocks(root);
  assert.equal(search.hasAttribute("aria-disabled"), false);
  assert.equal(locallyBusy.disabled, true);
});

test("progress updates preserve the Stop control and its focus target", () => {
  const {context, document} = dashboard();
  const container = document.querySelector("#activity-current");
  const row = new Element(), stop = new Element(), phase = new Element();
  const progress = new Element(), count = new Element();
  row.querySelector = selector => ({
    "[data-stop-job]": stop, "[data-job-phase]": phase,
    progress, "[data-job-count]": count,
  })[selector];
  stop.dataset.stopJob = "example";
  container.querySelectorAll = () => [stop];
  container.querySelector = () => row;
  document.querySelector("#activity-live-details").parentElement = {open: false};
  const job = {
    id: "example", kind: "scan", phase: "Scanning", status: "running",
    started: Date.now() / 1000, total: 10, completed: 1,
  };
  context.renderCurrentJobs([job]);
  const originalHandler = stop.onclick, writes = container.writes;
  context.renderCurrentJobs([{...job, completed: 2, status: "stopping"}]);
  assert.equal(container.writes, writes);
  assert.equal(stop.onclick, originalHandler);
  assert.equal(stop.disabled, true);
  assert.equal(progress.value, 2);
  assert.equal(count.textContent, "2 / 10 processed");
});

test("human-readable results escape filenames and keep raw data collapsed", () => {
  const {context} = dashboard();
  const row = context.resultRow({path: "<script>unsafe</script>", error: "Permission denied"});
  assert.match(row, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.match(row, /Permission denied/);
  assert.match(row, /<details><summary>Technical details/);
  assert.doesNotMatch(row, /<script>|<details open/);
  assert.doesNotThrow(() => context.resultRow({fields: {album: "Album"}}));
});
