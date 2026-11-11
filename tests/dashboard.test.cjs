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
  querySelectorAll() { return []; }
}

function dashboard({storage, reducedMotion = false} = {}) {
  const nodes = new Map();
  const saved = new Map(), warnings = [], notices = [], scrolls = [], events = new Map();
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
      addEventListener: (name, handler) => events.set(name, handler),
      scrollY: 0, innerHeight: 800, history: {},
      scrollTo: options => scrolls.push(options),
      matchMedia: () => ({matches: reducedMotion}),
      localStorage: storage || {
        getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value),
        removeItem: key => saved.delete(key),
      },
    },
    console: {...console, warn: (...args) => warnings.push(args)}, setTimeout, clearTimeout,
    requestAnimationFrame: callback => callback(),
    confirm: () => true,
  });
  vm.runInContext(appearance, context);
  vm.runInContext(source, context);
  context.toast = (...args) => notices.push(args);
  return {context, nodes, document, saved, warnings, notices, scrolls, events,
    run: script => vm.runInContext(script, context)};
}

test("workspace memory restores validated browsing fields without restoring reviews or selections", () => {
  const {context, saved, run} = dashboard();
  saved.set("musiclib.workspace.v1", JSON.stringify({
    version: 1, tab: "library",
    library: {query: "Session", genre: "Jazz", format: "mp3", status: "suspect", view: "tracks",
      batch: 150, trackOffset: 100, selectedTracks: [1, 2]},
    scroll: {library: 2400}, libraryReview: {kind: "trash", jobId: "old"}, api_key: "not-persisted",
  }));
  context.loadBrowsing();
  assert.equal(run("state.tab"), "library");
  assert.equal(run("state.libraryView"), "tracks");
  assert.equal(run("state.libraryBatch"), 150);
  assert.equal(run("browsing.library.trackOffset"), 100);
  assert.equal(run("browsing.scroll.library"), 2400);
  assert.equal(run("state.libraryReview"), undefined);
  context.saveBrowsing();
  const memory = JSON.parse(saved.get("musiclib.workspace.v1"));
  assert.equal(memory.library.query, "Session");
  assert.equal(memory.library.genre, "Jazz");
  assert.equal(memory.library.format, "mp3");
  assert.equal(memory.library.status, "suspect");
  assert.doesNotMatch(JSON.stringify(memory), /selectedTracks|libraryReview|api_key/);
});

test("invalid workspace values cannot select unknown tabs, run actions, or request unbounded restoration", () => {
  const {context} = dashboard();
  const value = context.validateBrowsing({
    version: 1, tab: "__proto__",
    library: {query: 45, genre: "x".repeat(257), status: "deleted", batch: 50000, view: "trash",
      trackOffset: -50},
    scroll: {library: Infinity, settings: "1000", overview: -1},
  });
  assert.equal(value.tab, "overview");
  assert.equal(value.library.query, "");
  assert.equal(value.library.genre, "");
  assert.equal(value.library.status, "");
  assert.equal(value.library.batch, 50);
  assert.equal(value.library.view, "albums");
  assert.equal(value.library.trackOffset, 0);
  assert.equal(value.scroll.library, 0);
  assert.equal(value.scroll.settings, 0);
});

test("unreadable saved workspace reports a reset without breaking startup preferences", () => {
  for (const raw of ["{not json", '{"version":99}', "null"]) {
    const {context, saved, run, notices, warnings} = dashboard();
    saved.set("musiclib.workspace.v1", raw);
    assert.doesNotThrow(() => context.loadBrowsing());
    assert.equal(run("state.tab"), "overview");
    assert.equal(warnings.length, 1);
    assert.match(notices[0][0], /could not be restored/);
    context.saveBrowsing();
    assert.equal(JSON.parse(saved.get("musiclib.workspace.v1")).version, 1);
  }
});

test("unavailable workspace storage reports once and preserves browsing state for this visit", () => {
  for (const failure of ["read", "write"]) {
    let writes = 0;
    const {context, notices, run} = dashboard({storage: {
      getItem(key) {
        if (key === "musiclib.workspace.v1" && failure === "read") throw Error("Storage blocked");
      },
      setItem() { writes++; throw Error("Storage full"); },
    }});
    context.loadBrowsing();
    run('browsing.library.query = "Jazz"; browsing.tab = "library"');
    context.saveBrowsing(); context.saveBrowsing();
    assert.equal(run("browsing.library.query"), "Jazz");
    assert.equal(notices.length, 1);
    assert.match(notices[0][0], /only for this visit/);
    assert.equal(writes, failure === "read" ? 0 : 1);
  }
});

test("clearing browsing memory leaves the theme and unrelated browser data alone", () => {
  const {context, saved, run} = dashboard();
  saved.set("musiclib.theme", "dark");
  saved.set("unrelated", "keep");
  saved.set("musiclib.workspace.v1", '{"version":1,"tab":"library"}');
  context.loadBrowsing();
  context.forgetBrowsing();
  assert.equal(saved.has("musiclib.workspace.v1"), false);
  assert.equal(saved.get("musiclib.theme"), "dark");
  assert.equal(saved.get("unrelated"), "keep");
  assert.equal(run("browsing.library.query"), "");
  assert.equal(run("state.libraryBatch"), 50);
});

test("scroll capture uses the displayed tab and never replaces browsing position with a review or album detail", () => {
  const {context, run} = dashboard();
  run('state.currentTab="library"; state.tab="settings"; window.scrollY=1600');
  context.captureBrowsingPosition();
  assert.equal(run("browsing.scroll.library"), 1600);
  for (const reason of [
    "state.libraryDetail=true", "state.libraryDetail=false; state.libraryWorkspace={active:true}",
    "state.libraryWorkspace=null; state.rendering=true", "state.rendering=false; state.restoringScroll=true",
  ]) {
    run(reason + "; window.scrollY=9999");
    context.captureBrowsingPosition();
    assert.equal(run("browsing.scroll.library"), 1600);
  }
});

test("scroll restoration waits for Library loading and yields to newer navigation or user input", async () => {
  for (const interrupt of ["none", "user", "navigation"]) {
    const {context, run, scrolls} = dashboard();
    let finish;
    context.loadMore = () => new Promise(resolve => { finish = resolve; });
    run('state.currentTab="library"; state.renderGeneration=1; browsing.scroll.library=2300; state.loadBrowsingScroll=loadMore');
    const restored = context.restoreBrowsingPosition(1);
    assert.equal(scrolls.length, 0);
    if (interrupt === "user") run("state.cancelScrollRestore=true");
    if (interrupt === "navigation") run("state.renderGeneration=2; state.restoringScroll=false");
    finish();
    await restored;
    assert.equal(scrolls.length, interrupt === "none" ? 1 : 0);
    if (scrolls.length) {
      assert.equal(scrolls[0].top, 2300);
      assert.equal(scrolls[0].behavior, "instant");
    }
    assert.equal(run("state.restoringScroll"), false);
  }
});

test("restored track pages clamp to the last available page when the collection shrinks", async () => {
  const {context} = dashboard(), container = new Element();
  const prev = new Element(), next = new Element(), requests = [], offsets = [];
  container.querySelector = selector => selector === ".prev" ? prev : next;
  await context.pagedTable(container, async offset => {
    requests.push(offset);
    return {total: 101, items: offset < 101 ? [{id: 101}] : []};
  }, ["Track"], row => `<tr><td>${row.id}</td></tr>`, () => {}, {
    offset: 250, onOffset: offset => offsets.push(offset),
  });
  assert.deepEqual(requests, [250, 100]);
  assert.deepEqual(offsets, [100]);
  assert.match(container.innerHTML, /101–101 of 101/);
});

test("stale track search responses cannot overwrite a newer search or its remembered page", async () => {
  const {context} = dashboard(), container = new Element();
  let finish, current = true, remembered = false;
  const pending = context.pagedTable(container, () => new Promise(resolve => { finish = resolve; }),
    ["Track"], () => "<tr><td>Old search</td></tr>", () => {}, {
      current: () => current, onOffset: () => { remembered = true; },
    });
  current = false;
  container.innerHTML = "New search";
  finish({total: 1, items: [{id: 1}]});
  await pending;
  assert.equal(container.innerHTML, "New search");
  assert.equal(remembered, false);
});

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

test("declining navigation preserves both the review draft and saved Library location", async () => {
  const {context, run, saved} = dashboard();
  const {root, slots} = reviewRoot();
  const workspace = context.createReviewWorkspace(root);
  const token = workspace.begin("manual", "Edit tags");
  slots[0].children.push("unsaved album name");
  context.testWorkspace = workspace;
  run('state.currentTab="library"; state.libraryWorkspace=testWorkspace; browsing.tab="library"; browsing.scroll.library=1600');
  context.saveBrowsing();
  const before = saved.get("musiclib.workspace.v1");
  context.confirm = () => false;
  run('state.tab="settings"; window.scrollY=2500');
  await context.render();
  assert.equal(run("state.tab"), "library");
  assert.equal(workspace.owns(token), true);
  assert.deepEqual(slots[0].children, ["unsaved album name"]);
  assert.equal(saved.get("musiclib.workspace.v1"), before);
});

test("rapid navigation saves only accepted tabs and ignores an older pending render", async () => {
  const {context, run, saved} = dashboard(), pending = [], rendered = [];
  context.loadStats = () => new Promise(resolve => pending.push(resolve));
  context.recordRender = name => rendered.push(name);
  run('views.overview=async()=>recordRender("overview"); views.settings=async()=>recordRender("settings")');
  const first = context.render();
  run('state.tab="settings"');
  const second = context.render();
  pending[1]();
  await second;
  pending[0]();
  await first;
  assert.deepEqual(rendered, ["settings"]);
  assert.equal(JSON.parse(saved.get("musiclib.workspace.v1")).tab, "settings");
  assert.equal(run("state.currentTab"), "settings");
  assert.equal(run("state.rendering"), false);
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

test("duplicate review explicitly separates keeper and other copies with escaped evidence", () => {
  const {context} = dashboard();
  const markup = context.duplicateGroupMarkup({
    group_id: 1, keep_id: 10, members: [
      {id: 10, path: "/music/keeper.flac", artist: "Artist", title: "Song", duration: 180,
        format: "flac", evidence: ["Recommended keeper"]},
      {id: 11, path: "/music/<copy>.mp3", title: "Song", duration: 181, album: "Compilation",
        format: "mp3", evidence: ["Same title <not proof>"]},
    ],
  }, false, "test");
  assert.match(markup, /KEEP/);
  assert.match(markup, /Move to trash/);
  assert.match(markup, /Include this group for cleanup/);
  assert.match(markup, /Compilation/);
  assert.match(markup, /Same title &lt;not proof&gt;/);
  assert.doesNotMatch(markup, /Trash approved copies|<copy>|data-approve="1" checked/);
});

test("file outcome summaries distinguish unfinished files from completed changes", () => {
  const {context, run} = dashboard();
  assert.equal(run("outcomeSummary({outcomes: {completed: 2, skipped: 1, failed: 1, stopped: 3}})"),
    "2 completed · 1 skipped · 1 failed · 3 stopped");
  const row = context.resultRow({status: "stopped", path: "/music/untouched.mp3",
    reason: "Check current state before retrying."});
  assert.match(row, /Stopped/);
  assert.match(row, /Check current state before retrying/);
});
