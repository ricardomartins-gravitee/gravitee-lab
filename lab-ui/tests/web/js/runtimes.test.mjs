// Central runtime inventory, stopped/orphaned rows and exact two-stage data cleanup confirmation.
import assert from "node:assert/strict";
import { mock, test } from "node:test";
import { Ev, installDom, reply, text, isOff, offWhy } from "./dom-stub.mjs";

mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
const calls = [];
let payload, holdRead = null, readFailure = false;
const job = { id: "abcdef12", for: "playground:local/a", verb: "stop", title: "Cleanup", done: false, ok: null, phases: [], progress: null };
const page = installDom({ fetch: async (path, options = {}) => {
  if (readFailure && !options.body && path.startsWith('/api/runtimes')) throw new Error('Offline');
  if (!options.body && holdRead && path.startsWith("/api/runtimes")) await holdRead;
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ path, body });
  const value = /\/api\/(?:playgrounds|runtimes)\/.+\/check/.test(path) ? { checks: [], dialog: { starts: [] } }
    : path.startsWith("/api/runtimes/") && body?.check ? { fingerprint: "reviewed-resources", removes: {
    containers: ["own-app"], networks: ["own-net"], volumes: body.operation === "data" ? ["own-volume"] : [],
    folders: body.operation === "data" ? [".lab/runtimes/own"] : [],
    runtime: body.operation === "delete" ? ["Private A"] : [] }, keeps: ["Other sandboxes", "Catalog source and links"] }
    : path === "/api/state" ? store.state : body ? job : payload;
  return reply(value);
} });
globalThis.innerHeight = 900;
const logStreams = [];
globalThis.EventSource = class { constructor(url) { logStreams.push(url); } close() {} addEventListener() {} };
const { store, remember } = await import("../../../web/js/core.js");
const { runtimePanel } = await import("../../../web/js/components/runtimes.js");
const flush = async () => { for (let i = 0; i < 14; i += 1) await Promise.resolve(); };
const base = { blind: false, wait: "", docker: { error: null }, job: null, services: {}, links: [], addons: [], setups: [],
  playgrounds: [{ kind: "playground", id: "local/a", name: "Private A", on: false, runtime: { mode: "independent", prepared: true, running: false }, settings: [] }],
  platform: { name: "Platform", settings: [], start: { needed: false }, groups: [] } };
const privateRow = { id: "local/a", name: "Private A", mode: "independent", selected: true, prepared: true, source: true,
  containers: [], volumes: [{ name: "own-volume", bytes: 100 }], local_bytes: 100, volume_bytes: 100,
  ports: ["31000"], problem: null, state: "Stopped", running: 0, active: false, memory_bytes: 0, cpu_percent: 0,
  cleanup: { containers: { enabled: true }, data: { enabled: true }, delete: { enabled: true } } };
let root, panel;
async function show(rows = [privateRow], playground = null) {
  remember("runtime-expanded", "{}");
  store.state = structuredClone(base);
  store.error = null;
  store.offline = false;
  calls.length = 0;
  payload = { default: "independent", runtimes: structuredClone(rows), shared_storage: [{ kind: "Images", bytes: 9000 }], problems: [] };
  root = page.doc.createElement("section");
  page.doc.body.append(root);
  panel = runtimePanel(root, playground);
  panel.show();
  await flush();
}
async function menu(label) {
  root.querySelector("[data-runtime-menu]").click();
  await flush();
  assert.doesNotMatch(text(page.ids.menu), /Rebuild/, "runtime controls reuse the existing Apply and Update concepts");
  const entry = page.ids.menu.querySelectorAll(".menu-item").find((el) => text(el).startsWith(label));
  assert.ok(entry, label);
  page.doc.dispatchEvent(new Ev("click", { target: entry }));
  await flush();
}
async function choice(yes) {
  page.ids.choice.returnValue = yes ? "ok" : "";
  page.ids.choice.onclose();
  await flush();
}
const posts = () => calls.filter((call) => call.body && !call.body.check);
const close = () => { panel.hide(); root.remove(); };

test("stopped and missing-source runtimes stay visible, with shared images counted separately", async () => {
  await show([{ ...privateRow, source: false }]);
  assert.match(text(root), /Private A/);
  assert.match(text(root), /Sandboxes/);
  assert.match(text(root), /Stopped/);
  assert.match(text(root), /Source unavailable/);
  assert.equal(root.querySelectorAll('a.btn').find((a) => a.getAttribute('aria-label') === 'Open sandbox Private A'), undefined);
  assert.ok(root.querySelectorAll('a.btn').find((a) => a.getAttribute('aria-label') === 'Containers of Private A'));
  assert.match(text(root), /Images: 9 kB \(machine-wide\)/);
  const start = root.querySelector("[data-runtime-act]");
  assert.ok(isOff(start));
  assert.equal(offWhy(start), "Catalog source unavailable");
  close();
});

test("Source playground navigates to the parent for named and legacy runtimes", async () => {
  for (const row of [{ ...privateRow, id: "runtime-demo", playground: "local/a" }, privateRow]) {
    await show([row]);
    location.hash = "#/containers";
    await menu("Source playground");
    assert.equal(location.hash, "#/playgrounds/local/a");
    assert.deepEqual(posts(), [], "navigation never starts or changes a runtime");
    close();
  }
});

test("Open sandbox targets the selected child's guide, while Source playground targets Shared", async () => {
  await show([{ ...privateRow, id: "runtime-demo", playground: "local/a", name: "Demo" }]);
  const open = root.querySelectorAll("a.btn").find((el) => el.getAttribute("aria-label") === "Open sandbox Demo");
  assert.equal(open.getAttribute("href"), "#/playgrounds/runtime-demo");
  location.hash = "#/containers";
  await menu("Open sandbox");
  assert.equal(location.hash, "#/playgrounds/runtime-demo");
  await menu("Source playground");
  assert.equal(location.hash, "#/playgrounds/local/a");
  assert.deepEqual(posts(), []);
  close();
});

test("runtime management links to scoped Containers without duplicating service controls", async () => {
  await show([{ ...privateRow, id: "runtime-demo", playground: "local/a", name: "Demo", services: [{ id: "api", ports: ["31000"] }] }]);
  const link = root.querySelectorAll('a.btn').find((a) => a.getAttribute('aria-label') === 'Containers of Demo');
  assert.equal(link.getAttribute("href"), "#/containers/runtime-demo");
  assert.equal(root.querySelector("[data-runtime-toggle]"), null);
  assert.equal(root.querySelector("[data-runtime-service-act]"), null);
  assert.deepEqual(posts(), []);
  close();
});

test("a failed inventory read settles instead of retaining Reading sandboxes", async () => {
  await show();
  close();
  readFailure = true;
  root = page.doc.createElement('section');
  page.doc.body.append(root);
  panel = runtimePanel(root);
  panel.show();
  await flush();
  assert.match(text(root), /Sandbox information unavailable/);
  assert.doesNotMatch(text(root), /Reading sandboxes/);
  readFailure = false;
  close();
});

test("offline management retains saved runtime names and leaves container navigation available", async () => {
  await show([{ ...privateRow, state: 'Unavailable', volume_bytes: null }]);
  store.state.docker.error = 'Docker is not reachable';
  store.state.wait = 'Docker is not reachable';
  panel.render(store.state);
  await flush();
  assert.match(text(root), /Private A/);
  assert.match(text(root), /Unavailable/);
  assert.equal(root.querySelectorAll('a.btn').find(a => a.getAttribute('aria-label') === 'Containers of Private A').getAttribute('href'), '#/containers/local%2Fa');
  assert.ok(isOff(root.querySelector('[data-runtime-act]')));
  close();
});

test("storage details show captured image references and exact identities", async () => {
  await show([{ ...privateRow, images: [{ service: "db", reference: "store:1", id: "sha256:captured" }] }]);
  root.querySelector("[data-runtime-details]").click();
  await flush();
  assert.match(text(page.ids["choice-list"]), /db: store:1 \(sha256:captured\)/);
  await choice(false);
  close();
});

test("Apply opens its scoped confirmation before sending a change", async () => {
  await show();
  await menu("Apply");
  assert.equal(calls.at(-1).path, "/api/runtimes/local/a/check?action=apply");
  assert.match(text(page.ids["choice-title"]), /Apply Private A/);
  assert.match(text(page.ids["choice-list"]), /All its data/);
  assert.deepEqual(posts(), []);
  await choice(false);
  close();
});

test("container-only cleanup keeps data and sends exactly the reviewed fingerprint", async () => {
  await show();
  await menu("Delete containers, keep data");
  assert.deepEqual(posts(), []);
  assert.match(text(page.ids["choice-list"]), /own-app/);
  assert.match(text(page.ids["choice-list"]), /Other sandboxes/);
  assert.match(text(page.ids["choice-title"]), /Delete containers, keep data/);
  assert.match(text(page.ids["choice-list"]), /Start recreates containers/);
  assert.match(text(page.ids["choice-list"]), /Deletes containers/);
  await choice(true);
  mock.timers.tick(600);
  await flush();
  assert.deepEqual(posts().map(({ path, body }) => [path, body]), [["/api/runtimes/local/a/cleanup", { operation: "containers", fingerprint: "reviewed-resources" }]]);
  close();
});

test("data deletion waits for both the exact scope and typed confirmation; cancellation removes nothing", async () => {
  await show();
  await menu("Delete data, keep sandbox");
  assert.match(text(page.ids["choice-list"]), /own-volume/);
  assert.match(text(page.ids["choice-list"]), /\.lab\/runtimes\/own/);
  assert.deepEqual(posts(), []);
  await choice(true);
  assert.equal(page.ids["confirm-input"].placeholder, "Type delete to confirm");
  assert.match(text(page.ids["confirm-title"]), /Delete data, keep sandbox/);
  assert.match(text(page.ids["confirm-text"]), /next Start initializes fresh data/);
  assert.deepEqual(posts(), []);
  page.ids.confirm.returnValue = "";
  page.ids.confirm.onclose();
  await flush();
  assert.deepEqual(posts(), []);
  await menu("Delete sandbox and data");
  assert.match(text(page.ids["choice-list"]), /Deletes sandbox/);
  assert.doesNotMatch(text(page.ids["choice-list"]), /Deletes runtime/);
  await choice(false);
  assert.deepEqual(posts(), []);
  close();
});

test("a completed cleanup refreshes the inventory, and Refresh storage requests fresh sizes", async () => {
  await show();
  panel.render(store.state);
  await flush();
  const before = calls.length;
  store.state.job = { ...job, done: true, ok: true };
  panel.render(store.state);
  await flush();
  assert.ok(calls.length > before);
  root.querySelector("[data-runtime-refresh]").click();
  await flush();
  assert.equal(calls.at(-1).path, "/api/runtimes?refresh=1");
  close();
});


test("Containers can open before the initial Lab state arrives", async () => {
  await show();
  panel.hide();
  store.state = null;
  assert.doesNotThrow(() => panel.show());
  await flush();
  assert.match(text(root), /Private A/);
  store.state = structuredClone(base);
  panel.render(store.state);
  await flush();
  close();
});


test("management controls wait for an inventory refresh and become usable again", async () => {
  await show();
  let release;
  holdRead = new Promise((resolve) => { release = resolve; });
  root.querySelector("[data-runtime-refresh]").click();
  assert.ok(isOff(root.querySelector("[data-runtime-act]")));
  assert.ok(!isOff(root.querySelector("[data-runtime-menu]")), "navigation remains available during storage reads");
  release();
  holdRead = null;
  await flush();
  assert.ok(!isOff(root.querySelector("[data-runtime-act]")));
  assert.ok(!isOff(root.querySelector("[data-runtime-menu]")));
  close();
});


test("a playground lists only its named children and offers Create sandbox rather than mode settings", async () => {
  const { runtimesPage } = await import("../../../web/js/pages/runtimes.js");
  assert.equal(runtimesPage.title, "Sandboxes");
  assert.ok(runtimesPage.pattern.test("#/sandboxes"));
  assert.ok(runtimesPage.pattern.test("#/runtimes"), "existing bookmarks still open sandbox management");
  await show([{ ...privateRow, id: "runtime-demo", playground: "local/a", name: "Demo" },
    { ...privateRow, id: "runtime-other", playground: "local/b", name: "Not here" }], "local/a");
  assert.match(text(root), /Demo/);
  assert.doesNotMatch(text(root), /Not here|Runtime settings/);
  assert.ok(root.querySelector("[data-runtime-create]"));
  assert.equal(calls[0].path, "/api/runtimes?playground=local%2Fa");
  close();
});


test("the More popup follows jobs only while open and reopening targets the chosen source", async () => {
  const { runtimeManagerEntry } = await import("../../../web/js/dialogs/runtime-manager.js");
  await show([{ ...privateRow, playground: "local/a" }]);
  close();
  calls.length = 0;
  runtimeManagerEntry(store.item("playground", "local/a")).onSelect();
  await flush();
  const dialog = page.doc.querySelector(".runtime-dialog");
  assert.ok(dialog.open);
  assert.equal(runtimeManagerEntry(store.item("playground", "local/a")).label, "Sandboxes");
  assert.match(text(dialog.querySelector("h2")), /Private A/);
  assert.equal(calls[0].path, "/api/runtimes?playground=local%2Fa");
  assert.equal(posts().length, 0, "opening the inventory changes no runtime");
  const before = calls.length;
  store.state.job = { ...job, done: true };
  store.notify();
  await flush();
  assert.ok(calls.length > before, "a completed job updates the open inventory");
  dialog.querySelector("[data-close]").click();
  const after = calls.length;
  store.state.job = { ...job, id: "another", done: true };
  store.notify();
  await flush();
  assert.equal(calls.length, after, "closed popups do not refresh storage");
  store.state.playgrounds.push({ id: "local/b", name: "Source B", runtime: { mode: "shared" } });
  payload.runtimes = [{ ...privateRow, name: "Only B", playground: "local/b" }];
  runtimeManagerEntry(store.item("playground", "local/b")).onSelect();
  await flush();
  assert.match(text(dialog), /Source B/);
  assert.match(text(dialog), /Only B/);
  assert.equal(calls.at(-1).path, "/api/runtimes?playground=local%2Fb");
  dialog.querySelector("[data-runtime-menu]").click();
  const escape = new Ev("keydown", { key: "Escape", cancelable: true });
  page.doc.dispatchEvent(escape);
  assert.ok(escape.defaultPrevented, "Escape does not also trigger the browser's modal cancellation");
  assert.ok(dialog.open);
  assert.ok(page.ids.menu.hidden);
  location.hash = "#/playgrounds/local/b";
  dialog.querySelector("[data-runtime-menu]").click();
  const source = page.ids.menu.querySelectorAll(".menu-item").find((el) => text(el) === "Source playground");
  page.doc.dispatchEvent(new Ev("click", { target: source }));
  await flush();
  assert.ok(!dialog.open, "the inventory closes even when the source page is already open");
  assert.equal(location.hash, "#/playgrounds/local/b");
});

test("Restart targets only the selected running runtime", async () => {
  await show([{ ...privateRow, running: 1 }]);
  await menu("Restart");
  mock.timers.tick(600);
  await flush();
  assert.deepEqual(posts().map(({ path, body }) => [path, body]), [["/api/runtimes/local/a/restart", {}]]);
  close();
});
