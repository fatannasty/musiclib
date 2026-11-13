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
    const classes = new Set();
    this.classList = {
      add: (...values) => values.forEach(value => classes.add(value)),
      remove: (...values) => values.forEach(value => classes.delete(value)),
      contains: value => classes.has(value),
      toggle: (value, force = !classes.has(value)) => {
        if (force) classes.add(value); else classes.delete(value);
        return force;
      },
    };
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
    body: new Element(),
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

test("tab visibility rules do not hide album and artwork result sections", () => {
  const styles = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.match(styles, /#main\s*>\s*section\s*\{\s*display:\s*none\s*;/);
  assert.match(styles, /#main\s*>\s*section\.active\s*\{\s*display:\s*block\s*;/);
  assert.doesNotMatch(styles, /(?:^|})\s*section\s*\{\s*display:\s*none\s*;/m);
});

test("alphabet navigation is isolated from the main nav and uses one ordered column", () => {
  const styles = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.doesNotMatch(styles, /(?:^|})\s*nav(?:\s+button(?:[.:][\w-]+)?)?\s*\{/m);
  assert.match(styles, /#nav\s*\{[^}]*padding:/);
  assert.match(styles, /\.library-index\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(styles, /\.library-index\s*\{[^}]*flex-wrap:\s*nowrap/);
});

test("album detail opens as an accessible listening dialog with reduced-motion fallback", () => {
  const styles = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.match(html, /<dialog id="library-album-page" class="album-dialog"/);
  assert.match(html, /pageEl\.showModal\(\)/);
  assert.match(styles, /@keyframes album-flip-in/);
  assert.match(styles, /prefers-reduced-motion:\s*reduce[\s\S]*?album-dialog\[open\] \.album-dialog-surface \{\s*animation:\s*none/);
  assert.match(html, /--accent-blue:/);
  assert.match(html, /--accent-gold:/);
});

test("album track cards offer playback and individual edits without preselecting files", () => {
  const {context} = dashboard();
  const markup = context.albumTrackListMarkup({
    album_artist: "Band",
    tracks: [{id: 7, disc_no: 1, track_no: 2, title: "<Song>", artist: "Band",
      status: "healthy", path: "/music/song.flac"}],
  });
  assert.match(markup, /data-play-track="7"/);
  assert.match(markup, /data-edit-track="7"/);
  assert.match(markup, /data-add-playlist-track="7"/);
  assert.match(markup, /&lt;Song&gt;/);
  assert.doesNotMatch(markup, /type="checkbox"[^>]*checked/);
});

test("the player offers playlist management and queue transport", () => {
  const {run} = dashboard();
  assert.equal(run('TABS.some(([id]) => id === "playlists")'), true);
  assert.equal(run('Object.hasOwn(views, "playlists")'), true);
  assert.match(html, /id="player-prev"/);
  assert.match(html, /id="player-next"/);
  assert.match(html, /id="player-shuffle"/);
  assert.match(html, /id="player-repeat"/);
  assert.match(html, /id="playlist-picker"/);
  assert.match(html, /\/api\/playback\/formats/);
});

test("queue navigation wraps only when repeat-all is enabled", async () => {
  const {run} = dashboard();
  run('loadPlaybackTrack = id => id');
  await run('playQueue([{id: 11, title: "First"}, {id: 12, title: "Second"}])');
  assert.equal(run("playback.queueIndex"), 0);
  assert.equal(await run("advancePlayback(1)"), 12);
  assert.equal(run("playback.queueIndex"), 1);
  assert.equal(await run("advancePlayback(-1)"), 11);
  assert.equal(run("playback.queueIndex"), 0);
  await run("advancePlayback(1)");
  assert.equal(run("advancePlayback(1)"), undefined);
  run('playback.repeat = "all"');
  assert.equal(await run("advancePlayback(1)"), 11);
  assert.equal(run("playback.queueIndex"), 0);
});

test("shuffle changes upcoming order but keeps previous-track navigation", async () => {
  const {run} = dashboard();
  run('loadPlaybackTrack = id => id');
  run("Math.random = () => 0");
  await run('playQueue([{id: 21}, {id: 22}, {id: 23}], 1)');
  run("togglePlaybackShuffle()");
  assert.equal(run("playback.queue[playback.queueIndex].id"), 22);
  assert.equal(run("new Set(playback.queue.map(track => track.id)).size"), 3);
  assert.equal(await run("advancePlayback(-1)"), 21);
  assert.equal(await run("advancePlayback(1)"), 22);
  run("togglePlaybackShuffle()");
  assert.equal(run("playback.queue[playback.queueIndex].id"), 22);
  assert.equal(run("playback.queueIndex"), 1);
});

test("album discovery select-all follows individual choices and can be cleared", () => {
  const {context} = dashboard();
  const fields = [{checked: false}, {checked: false}, {checked: false}];
  const selectAll = {checked: false, indeterminate: false};
  context.bindDiscoveryFieldSelection(selectAll, fields);
  assert.equal(selectAll.checked, false);
  assert.equal(selectAll.indeterminate, false);
  selectAll.checked = true;
  selectAll.onchange();
  assert.deepEqual(fields.map(field => field.checked), [true, true, true]);
  fields[1].checked = false;
  fields[1].onchange();
  assert.equal(selectAll.checked, false);
  assert.equal(selectAll.indeterminate, true);
  selectAll.checked = false;
  selectAll.onchange();
  assert.deepEqual(fields.map(field => field.checked), [false, false, false]);
  assert.equal(selectAll.indeterminate, false);
});

test("album player stays outside the scrolling dialog surface", () => {
  const styles = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.match(html, /surface\.append\(\$\("#library-workspace"\)\);\s*pageEl\.append\(player\);/);
  assert.match(styles, /\.album-dialog \.player\s*\{\s*position:\s*fixed;/);
  assert.match(styles, /\.album-dialog\.album-dialog-playing \.album-dialog-surface\s*\{\s*padding-bottom:\s*\d+px;/);
  assert.match(styles, /\.album-dialog\.album-dialog-queue-open \.album-dialog-surface\s*\{\s*padding-bottom:\s*\d+px;/);
  assert.match(styles, /\.player > \.row:first-child > button/);
});

test("track-list comparison displays uncertainty and escapes catalog and local data", () => {
  const {context} = dashboard();
  const markup = context.tracklistAuditMarkup(
    {summary: {album_artist: "Artist", album: "<Tape>"}},
    [{id: "release", title: "<Edition>", date: "2001", country: "US", status: "",
      tracks: [{catalog: {disc: 1, position: 1, title: "<Song>", duration: 100},
        local: null, flags: ["No unique title match"]}],
      unmatched_local: [{disc_no: 1, track_no: 1, title: "Other", path: "<path>"}]}],
  );
  assert.match(markup, /&lt;Tape&gt;/);
  assert.match(markup, /&lt;Edition&gt;/);
  assert.match(markup, /&lt;Song&gt;/);
  assert.match(markup, /&lt;path&gt;/);
  assert.match(markup, /No unique title match/);
  assert.match(markup, /No files or tags were changed/);
});

test("opening saved track-list results scrolls to the report and shows loading failures", async () => {
  const {context, document, notices} = dashboard({reducedMotion: true});
  const container = document.querySelector("#activity-results");
  const panel = new Element(), close = new Element(), scrolls = [];
  container.querySelector = selector => ({
    "[data-close-results]": close, ".results-page": panel,
  })[selector];
  container.scrollIntoView = options => scrolls.push(options);
  const job = {
    id: "saved", kind: "album-tracklist-audit", status: "done",
    summary: {album_artist: "Artist", album: "Album"},
  };
  context.api = async () => ({items: [{
    id: "release", title: "Edition", date: "", country: "", status: "",
    tracks: [{catalog: {disc: 1, position: 1, title: "Song", duration: 95},
      local: {disc_no: 1, track_no: 1, title: "Song", duration: 94}, flags: []}],
    unmatched_local: [],
  }]});
  await context.showJobResults(job);
  assert.equal(scrolls.length, 1);
  assert.equal(scrolls[0].behavior, "auto");
  assert.match(panel.innerHTML, /Edition/);
  assert.match(panel.innerHTML, /Title matches/);
  context.api = async () => { throw new Error("Connection failed"); };
  await context.showJobResults(job);
  assert.match(panel.textContent, /Could not load comparison: Connection failed/);
  assert.match(notices[0][0], /Connection failed/);
});

test("workspace memory restores validated browsing fields without restoring reviews or selections", () => {
  const {context, saved, run} = dashboard();
  saved.set("musiclib.workspace.v1", JSON.stringify({
    version: 1, tab: "library",
    library: {query: "Session", genre: "Jazz", format: "mp3", status: "suspect", view: "tracks",
      batch: 25, trackOffset: 75, albumOffset: 50, initial: "B", selectedTracks: [1, 2]},
    scroll: {library: 2400}, libraryReview: {kind: "trash", jobId: "old"}, api_key: "not-persisted",
  }));
  context.loadBrowsing();
  assert.equal(run("state.tab"), "library");
  assert.equal(run("state.libraryView"), "tracks");
  assert.equal(run("state.libraryBatch"), 25);
  assert.equal(run("browsing.library.trackOffset"), 75);
  assert.equal(run("browsing.library.albumOffset"), 50);
  assert.equal(run("browsing.library.initial"), "B");
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

test("Library numbered pages are bounded and mark the current page", () => {
  const {context} = dashboard();
  const markup = context.libraryPagerMarkup(125, 500, 25);
  assert.match(markup, /126–150 of 500/);
  assert.match(markup, /data-library-page="6"[^>]*aria-current="page"/);
  assert.match(markup, /data-library-page="1"/);
  assert.match(markup, /data-library-page="20"/);
  assert.doesNotMatch(markup, /data-library-page="15"/);
  assert.match(context.libraryPagerMarkup(0, 500, 25), /data-library-page="5"/);
  assert.equal(context.libraryPagerMarkup(0, 0, 25), "");
});

test("legacy page sizes are reset safely and saved offsets align to the selected size", () => {
  const {context} = dashboard();
  const saved = context.validateBrowsing({version: 1, library: {batch: 150, trackOffset: 175, albumOffset: 175, initial: "AA"}});
  assert.equal(saved.library.batch, 50);
  assert.equal(saved.library.trackOffset, 150);
  assert.equal(saved.library.albumOffset, 150);
  assert.equal(saved.library.initial, "");
});

test("closing a review returns to the edited track without scrolling the wrong surface", () => {
  const {context, scrolls} = dashboard();
  const focus = [];
  const track = {isConnected: true, focus: options => focus.push(options)};
  const surface = {scrollTop: 0};
  const dialog = {open: true, querySelector: () => surface};
  context.restoreReviewPosition({element: track, scrollTop: 640, scrollY: 2100}, dialog);
  assert.equal(surface.scrollTop, 640);
  assert.equal(scrolls.length, 0);
  assert.equal(focus[0].preventScroll, true);
  dialog.open = false;
  track.isConnected = false;
  context.restoreReviewPosition({element: track, scrollTop: null, scrollY: 2100}, dialog);
  assert.equal(scrolls[0].top, 2100);
  assert.equal(focus.length, 1);
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

test("settings saves audio keys and explicit removal without exposing the saved key", async () => {
  const {context, document, run} = dashboard(), panel = new Element(), requests = [];
  const settings = {
    library: "/music", sabnzbd: {}, brainz: {}, catalog: {album_source: "all"},
    acoustid: {api_key: "", api_key_set: true}, organize: {}, doctor: {},
    duplicates: {strategy: "tags", schedule_hours: 0}, artwork: {}, indexers: [],
  };
  context.api = async (url, options) => {
    if (!options) return settings;
    requests.push({url, body: JSON.parse(options.body)});
    return {saved: true};
  };
  context.render = () => {};
  await run("views.settings")(panel);
  assert.match(panel.innerHTML, /type="password" id="set-acoustid-api_key"/);
  assert.match(panel.innerHTML, /saved — leave blank to keep/);
  document.querySelector("#set-acoustid-api_key").value = "replacement-key";
  document.querySelector("#set-acoustid-clear").checked = false;
  await document.querySelector("#set-save").onclick();
  assert.equal(requests[0].url, "/api/settings");
  assert.deepEqual(requests[0].body.acoustid, {
    api_key: "replacement-key", clear_api_key: false,
  });
  document.querySelector("#set-acoustid-api_key").value = "";
  document.querySelector("#set-acoustid-clear").checked = true;
  await document.querySelector("#set-save").onclick();
  assert.deepEqual(requests[1].body.acoustid, {api_key: "", clear_api_key: true});
});

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
  run('state.jobs = [{status:"running", read_only:true, lane:"artwork", kind:"artwork-discovery"}]');
  context.applyJobLocks(root);
  assert.equal(search.hasAttribute("aria-disabled"), false);
  run('state.jobs = [{status:"running", read_only:true, lane:"default", kind:"album-tracklist-audit"}]');
  context.applyJobLocks(root);
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

test("latest results stay available during other jobs without replacing the button on polling", async () => {
  const {context, document} = dashboard();
  const summary = document.querySelector("#activity-summary");
  const button = new Element();
  summary.querySelector = () => button;
  document.querySelector("#activity-live-details").parentElement = {open: false};
  const job = {id: "saved", kind: "scan", status: "done", summary: {scanned: 2}};
  let jobs = [job];
  context.api = async () => jobs;
  context.renderCurrentJobs = () => {};
  context.applyJobLocks = () => {};
  let opened;
  context.showJobResults = item => { opened = item; };
  await context.refreshActivity();
  assert.match(summary.innerHTML, /View results/);
  const writes = summary.writes, handler = button.onclick;
  jobs = [{id: "running", kind: "scan", status: "running"}, job];
  await context.refreshActivity();
  assert.equal(summary.writes, writes);
  assert.equal(button.onclick, handler);
  button.onclick();
  assert.equal(opened.id, "saved");
  jobs = [{id: "failed", kind: "scan", status: "error", error: "Connection failed"}];
  await context.refreshActivity();
  assert.match(summary.innerHTML, /Connection failed/);
  assert.doesNotMatch(summary.innerHTML, /View results/);
  assert.match(document.querySelector("#activity-notice").textContent, /Connection failed/);
  jobs = [];
  await context.refreshActivity();
  assert.equal(summary.innerHTML, "");
  const emptyWrites = summary.writes;
  await context.refreshActivity();
  assert.equal(summary.writes, emptyWrites);
});

test("library filter controls restore the count, apply, and clear without clearing search text", () => {
  const {context, document} = dashboard();
  const node = selector => document.querySelector(selector);
  node("#library-query").value = "Artist";
  node("#library-genre").value = "Jazz";
  node("#library-format").value = "";
  node("#library-status").value = "";
  let searches = 0, focused = false;
  node("#library-search").click = () => { searches++; };
  node("#library-filters summary").focus = () => { focused = true; };
  context.configureLibraryFilters();
  assert.equal(node("#library-filter-count").textContent, " · 1 selected");
  assert.equal(node("#library-filter-clear").disabled, false);
  node("#library-status").value = "suspect";
  node("#library-status").onchange();
  assert.equal(node("#library-filter-count").textContent, " · 2 selected");
  node("#library-filters").open = true;
  node("#library-filter-apply").onclick();
  assert.equal(searches, 1);
  assert.equal(node("#library-filters").open, false);
  assert.equal(focused, true);
  node("#library-filter-clear").onclick();
  assert.equal(searches, 2);
  assert.equal(node("#library-query").value, "Artist");
  assert.equal(node("#library-genre").value, "");
  assert.equal(node("#library-status").value, "");
  assert.equal(node("#library-filter-count").textContent, "");
  assert.equal(node("#library-filter-clear").disabled, true);
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
  assert.match(markup, /data-keep="1:10" checked/);
  assert.match(markup, /data-keep="1:11"/);
  assert.match(markup, /data-approve="1"/);
  assert.match(markup, /Keep this file/);
  assert.doesNotMatch(markup, /Trash approved copies|<copy>|data-approve="1" checked/);
});

test("duplicate review keeps selections and snapshots behind confirmation", async () => {
  const {context, document, run} = dashboard();
  const node = selector => document.querySelector(selector);
  const approve = new Element({approve: "1"}), keep = new Element({keep: "1:11"});
  const el = new Element();
  el.querySelectorAll = selector => ({
    "[data-approve]": [approve], "[data-keep]": [keep],
  })[selector] || [];
  const group = {group_id: 1, keep_id: 10, members: [
    {id: 10, path: "/music/one", file_hash: "one"},
    {id: 11, path: "/music/two", file_hash: "two"},
  ]};
  context.api = async () => ({hours: 0});
  context.pagedTable = async (_container, _load, _headers, _row, onPage) => onPage([group]);
  await run("views.dupes")(el);
  assert.equal(node("#btn-apply").hidden, true);
  keep.onchange();
  approve.checked = true;
  approve.onchange();
  assert.equal(node("#btn-apply").hidden, false);
  assert.match(node("#dupe-selected").textContent, /1 keeper stays; 1 extra copy/);
  let posted;
  context.post = async (url, body) => { posted = {url, body}; return {job_id: "job"}; };
  context.refreshActivity = async () => {};
  context.pollJob = async () => ({summary: {trashed: 1}});
  context.loadStats = async () => {};
  context.confirm = () => false;
  await node("#btn-apply").onclick();
  assert.equal(posted, undefined);
  context.confirm = () => true;
  await node("#btn-apply").onclick();
  assert.equal(posted.url, "/api/dupes/apply-job");
  assert.equal(posted.body.keep_ids[1], 11);
  assert.equal(posted.body.snapshots[10].file_hash, "one");
  assert.equal(posted.body.snapshots[11].path, "/music/two");
  assert.equal(posted.body.hard_delete, false);
  assert.equal(node("#btn-apply").hidden, true);
});

test("duplicate review offers a clear next step when there are no candidates", async () => {
  const {context, document, run} = dashboard();
  const node = selector => document.querySelector(selector);
  const el = new Element();
  let groups = [];
  context.api = async () => ({hours: 0});
  context.pagedTable = async (_container, _load, _headers, _row, onPage) => onPage(groups);
  context.runTask = async () => {};
  await run("views.dupes")(el);
  assert.equal(node("#dupe-empty").hidden, false);
  assert.equal(node("#dupe-selection").hidden, true);
  groups = [{group_id: 1, keep_id: 1, members: [{id: 1}, {id: 2}]}];
  await node("#btn-find").onclick();
  assert.equal(node("#dupe-empty").hidden, true);
  assert.equal(node("#dupe-selection").hidden, false);
  assert.equal(node("#btn-apply").hidden, true);
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

test("bad-file review separates checking, filtered selection, and recoverable trash", async () => {
  const {context, document, run} = dashboard();
  const node = selector => document.querySelector(selector);
  const issue = {track_id: 7, path: "/music/broken.mp3", severity: "bad",
    kind: "decode_error", detail: "Decode failed"};
  const checkbox = new Element({trashTrack: "7"});
  const el = new Element();
  el.querySelectorAll = selector => selector === "[data-trash-track]" ? [checkbox] : [];
  let trashed;
  context.api = async () => ({items: [issue], total: 1});
  context.pagedTable = async (_container, load, _headers, _row, onPage) =>
    onPage((await load(0)).items);
  context.post = async (url, body) => { trashed = {url, body}; return {job_id: "trash-job"}; };
  context.refreshActivity = async () => {};
  context.pollJob = async () => ({summary: {trashed: 1, failed: 0}});
  context.loadStats = async () => {};
  await run("views.doctor")(el);
  assert.equal(node("#doctor-bulk-trash").hidden, false);
  assert.equal(node("#issue-selection").hidden, false);
  assert.equal(node("#btn-trash-selected").hidden, true);
  assert.match(html, /id="btn-remove" data-operation>Trash all currently broken files/);
  assert.equal(node("#issue-filter-count").textContent, "");
  node("#issue-select-page").onclick();
  assert.equal(node("#issue-selected").textContent, "1 file selected");
  assert.equal(node("#btn-trash-selected").hidden, false);
  assert.equal(node("#btn-trash-selected").textContent, "Trash 1 selected file");
  node("#issue-severity").value = "bad";
  node("#issue-kind").value = "decode_error";
  node("#issue-kind").onchange();
  assert.equal(node("#issue-selected").textContent,
    "No files selected. Select this page or choose all matching files.");
  assert.equal(node("#issue-selection").hidden, false);
  assert.equal(node("#btn-trash-selected").hidden, true);
  assert.equal(node("#issue-filter-count").textContent, " · 2 selected");
  context.confirm = () => false;
  await node("#btn-remove").onclick();
  assert.equal(trashed, undefined);
  node("#issue-select-page").onclick();
  context.confirm = () => true;
  await node("#btn-trash-selected").onclick();
  assert.equal(trashed.url, "/api/issues/trash");
  assert.equal(JSON.stringify(trashed.body.track_ids), "[7]");
});

test("bad-file review hides cleanup actions when no findings exist", async () => {
  const {context, document, run} = dashboard();
  const node = selector => document.querySelector(selector);
  const el = new Element();
  el.querySelectorAll = () => [];
  context.api = async () => ({items: [], total: 0});
  context.pagedTable = async (_container, load, _headers, _row, onPage) =>
    onPage((await load(0)).items);
  await run("views.doctor")(el);
  assert.equal(node("#doctor-bulk-trash").hidden, true);
  assert.equal(node("#issue-selection").hidden, true);
  assert.equal(node("#issue-select-page").disabled, true);
  assert.equal(node("#btn-trash-selected").hidden, true);
});

test("Recovery selection reveals preview only for recoverable entries", async () => {
  const {context, document, run} = dashboard();
  const node = selector => document.querySelector(selector);
  const entry = {id: 42, path: "/music/song.flac", moved_to: "/trash/song.flac",
    restored_at: null, deleted_at: "2026-01-01", reason: "Duplicate"};
  const checkbox = new Element({selectRestore: "42"});
  const restoreButton = new Element();
  const el = new Element();
  el.querySelector = selector => selector === '[data-restore="42"]' ? restoreButton :
    document.querySelector(selector);
  el.querySelectorAll = selector => selector === "[data-select-restore]" ? [checkbox] : [];
  let posted;
  context.api = async () => ({items: [entry], total: 1});
  context.pagedTable = async (_container, load, _headers, _row, onPage) =>
    onPage((await load(0)).items);
  context.post = async (url, body) => { posted = {url, body}; return {job_id: "preview"}; };
  context.refreshActivity = async () => {};
  context.pollJob = async () => { el.isConnected = false; return {id: "preview", status: "done"}; };
  context.loadStats = async () => {};
  await run("views.recovery")(el);
  assert.equal(node("#restore-selection").hidden, false);
  assert.equal(node("#restore-preview-selected").hidden, true);
  assert.equal(node("#restore-select-page").disabled, false);
  assert.match(html, /Previously restored entries<\/summary>/);
  assert.match(html, /Restore to the exact original paths shown/);
  node("#restore-select-page").onclick();
  assert.equal(node("#restore-count").textContent, "1 file selected for preview");
  assert.equal(node("#restore-preview-selected").hidden, false);
  assert.equal(node("#restore-preview-selected").textContent, "Preview 1 selected restore");
  await node("#restore-preview-selected").onclick();
  assert.equal(posted.url, "/api/trash/restore-preview");
  assert.equal(JSON.stringify(posted.body.deletion_ids), "[42]");
});

test("Recovery hides selection controls when the page contains no recoverable files", async () => {
  const {context, document, run} = dashboard();
  const el = new Element();
  el.querySelector = selector => document.querySelector(selector);
  el.querySelectorAll = () => [];
  context.api = async () => ({items: [], total: 0});
  context.pagedTable = async (_container, load, _headers, _row, onPage) =>
    onPage((await load(0)).items);
  await run("views.recovery")(el);
  assert.equal(document.querySelector("#restore-selection").hidden, true);
  assert.equal(document.querySelector("#restore-preview-selected").hidden, true);
  assert.equal(document.querySelector("#restore-select-page").disabled, true);
});

test("Organize separates preview from apply and shows destination reasons", async () => {
  const {context, document, run} = dashboard();
  const node = selector => document.querySelector(selector);
  const el = new Element();
  const section = new Element({section: "moves"});
  section.click = () => {};
  el.querySelector = selector => selector === "[data-section]" ? section :
    document.querySelector(selector);
  el.querySelectorAll = selector => selector === "[data-section]" ? [section] : [];
  let applyCalls = 0, confirmation = false, rendered;
  const plan = {summary: {applied: false, to_move: 2, moved: 0,
    already_ok: 3, skipped: 1, errors: 0}};
  context.runTask = async (_kind, body) => {
    if (body.apply) {
      applyCalls++;
      return {summary: {applied: true, to_move: 2, moved: 2,
        already_ok: 3, skipped: 1, errors: 0}};
    }
    return plan;
  };
  context.pagedTable = async (_container, _load, headers, row) => {
    rendered = {headers, row: row({source: "/from/song.flac", target: "/to/song.flac",
      status: "blocked", reason: "<destination occupied>"})};
  };
  context.confirm = () => confirmation;
  context.loadStats = async () => {};
  await run("views.organize")(el);
  assert.equal(node("#btn-apply").hidden, true);
  await node("#btn-plan").onclick();
  assert.equal(node("#btn-apply").hidden, false);
  assert.equal(node("#btn-apply").textContent, "Apply organization");
  assert.match(node("#ren-summary").textContent, /2 destinations to review/);
  assert.match(node("#ren-summary").textContent, /3 already organized/);
  assert.match(html, /Applying creates a fresh plan/);
  section.onclick();
  assert.equal(JSON.stringify(rendered.headers),
    JSON.stringify(["Current location", "New location", "Review status"]));
  assert.match(rendered.row, /&lt;destination occupied&gt;/);
  await node("#btn-apply").onclick();
  assert.equal(applyCalls, 0);
  confirmation = true;
  await node("#btn-apply").onclick();
  assert.equal(applyCalls, 1);
  assert.equal(node("#btn-apply").hidden, true);
  assert.match(node("#ren-summary").textContent, /2 files moved/);
});

test("Overview workflow items open the relevant pages", async () => {
  const {context, run} = dashboard();
  const el = new Element();
  context.loadStats = async () => ({
    tracks: 1, issues: 0, dupe_groups: 0, albums_missing_art: 0,
    albums_missing_tracks: 0, albums: 1, bytes: 1, duration_seconds: 1,
  });
  context.pagedTable = async () => {};
  await run("views.overview")(el);
  for (const tab of ["doctor", "dupes", "organize", "art", "missing", "usenet"]) {
    assert.match(el.innerHTML, new RegExp(`class="step workflow-step" data-go="${tab}"`));
  }
});

test("Cover Art does not offer an apply action when no albums need artwork", async () => {
  const {context, document, run} = dashboard();
  const el = new Element();
  run(`state.jobs = [{kind: "art", id: "empty", status: "done",
    summary: {applied: false, albums: []}}]`);
  context.api = async () => ({hours: 0});
  context.pagedTable = async () => {};
  document.querySelector("#btn-dl").style = {};
  await run("views.art")(el);
  assert.equal(document.querySelector("#btn-dl").style.display, "none");
  assert.match(document.querySelector("#art-status").textContent, /No albums without embedded artwork/);
});

test("Missing Music turns an album gap into an indexer search", async () => {
  const {context, document, run} = dashboard();
  const el = new Element();
  const search = new Element({q: "Artist & Band - Album"});
  let clicked = false;
  document.querySelector("#btn-usearch").click = () => { clicked = true; };
  el.querySelectorAll = selector => selector === "[data-q]" ? [search] : [];
  context.post = async path => path === "/api/brainz/refresh" ? {job_id: "brainz"} : {};
  context.refreshActivity = async () => {};
  context.pollJob = async () => {};
  context.api = async path => path === "/api/missing"
    ? [{artist: "Artist & Band", album: "Album", missing: ["Song 1", "Song 2"]}]
    : [];
  context.loadStats = async () => {};
  context.render = async () => run('state.currentTab = "usenet"');
  await run("views.missing")(el);
  await document.querySelector("#btn-refresh").onclick();
  assert.match(document.querySelector("#miss-out").innerHTML, /2 missing tracks/);
  assert.match(document.querySelector("#miss-out").innerHTML, /Show missing tracks/);
  assert.match(document.querySelector("#miss-out").innerHTML, /Search indexers for this album/);
  await search.onclick();
  assert.equal(document.querySelector("#usenet-q").value, "Artist & Band - Album");
  assert.equal(document.querySelector("#btn-usearch").disabled, false);
  assert.equal(clicked, true);
});

test("Usenet search reports selectable downloads and confirms the queue action", async () => {
  const {context, document, run} = dashboard();
  const el = new Element();
  const send = new Element({url: "https://indexer.example/result.nzb", name: "Artist - Album"});
  el.querySelectorAll = selector => selector === "[data-url]" ? [send] : [];
  context.api = async () => [];
  context.post = async path => path === "/api/search"
    ? [{score: 90, size: 1024, title: "Artist - Album", indexer: "Example", url: send.dataset.url}]
    : {nzo_id: "queue-1"};
  await run("views.usenet")(el);
  const query = document.querySelector("#usenet-q");
  const search = document.querySelector("#btn-usearch");
  assert.equal(search.disabled, true);
  query.value = "Artist - Album";
  query.oninput();
  assert.equal(search.disabled, false);
  await search.onclick();
  assert.match(document.querySelector("#usenet-status").textContent, /Choose one to send to SABnzbd/);
  assert.match(document.querySelector("#usearch-out").innerHTML, /Send to SABnzbd/);
  await send.onclick();
  assert.match(document.querySelector("#usenet-status").textContent, /sent to SABnzbd/);
});
