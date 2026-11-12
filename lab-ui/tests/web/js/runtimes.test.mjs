// Central runtime inventory, stopped/orphaned rows and exact two-stage data cleanup confirmation.
import assert from "node:assert/strict";
import { mock, test } from "node:test";
import { Ev, installDom, reply, text, isOff, offWhy } from "./dom-stub.mjs";

mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
const calls = [];
let payload, holdRead = null;
const job = { id: "abcdef12", for: "playground:local/a", verb: "stop", title: "Cleanup", done: false, ok: null, phases: [], progress: null };
const page = installDom({ fetch: async (path, options = {}) => {
  if (!options.body && holdRead && path.startsWith("/api/runtimes")) await holdRead;
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ path, body });
  const value = path.startsWith("/api/runtimes/") && body?.check ? { fingerprint: "reviewed-resources", removes: {
    containers: ["own-app"], networks: ["own-net"], volumes: body.operation === "data" ? ["own-volume"] : [],
    folders: body.operation === "data" ? [".lab/runtimes/own"] : [] }, keeps: ["Other runtimes", "Catalog source and links"] }
    : path === "/api/state" ? store.state : body ? job : payload;
  return reply(value);
} });
globalThis.innerHeight = 900;
globalThis.EventSource = class { close() {} addEventListener() {} };
const { store } = await import("../../../web/js/core.js");
const { runtimePanel } = await import("../../../web/js/components/runtimes.js");
const flush = async () => { for (let i = 0; i < 14; i += 1) await Promise.resolve(); };
const base = { blind: false, wait: "", docker: { error: null }, job: null, services: {}, links: [], addons: [], setups: [],
  playgrounds: [{ kind: "playground", id: "local/a", name: "Private A", on: false, runtime: { mode: "independent" }, settings: [] }],
  platform: { name: "Platform", settings: [] } };
const privateRow = { id: "local/a", name: "Private A", mode: "independent", selected: true, prepared: true, source: true,
  containers: [], volumes: [{ name: "own-volume", bytes: 100 }], local_bytes: 100, volume_bytes: 100,
  ports: ["31000"], problem: null, state: "Stopped", running: 0, active: false, memory_bytes: 0, cpu_percent: 0,
  cleanup: { containers: { enabled: true }, data: { enabled: true } } };
let root, panel;
async function show(rows = [privateRow]) {
  store.state = structuredClone(base);
  store.error = null;
  store.offline = false;
  calls.length = 0;
  payload = { default: "independent", runtimes: structuredClone(rows), shared_storage: [{ kind: "Images", bytes: 9000 }], problems: [] };
  root = page.doc.createElement("section");
  page.doc.body.append(root);
  panel = runtimePanel(root);
  panel.show();
  await flush();
}
async function menu(label) {
  root.querySelector("[data-runtime-menu]").click();
  await flush();
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
  assert.match(text(root), /Stopped/);
  assert.match(text(root), /Source unavailable/);
  assert.match(text(root), /Images: 9 kB \(machine-wide\)/);
  const start = root.querySelector("[data-runtime-act]");
  assert.ok(isOff(start));
  assert.equal(offWhy(start), "Catalog source unavailable");
  close();
});

test("container-only cleanup keeps data and sends exactly the reviewed fingerprint", async () => {
  await show();
  await menu("Remove containers");
  assert.deepEqual(posts(), []);
  assert.match(text(page.ids["choice-list"]), /own-app/);
  assert.match(text(page.ids["choice-list"]), /Other runtimes/);
  await choice(true);
  mock.timers.tick(600);
  await flush();
  assert.deepEqual(posts().map(({ path, body }) => [path, body]), [["/api/runtimes/local/a/cleanup", { operation: "containers", fingerprint: "reviewed-resources" }]]);
  close();
});

test("data deletion waits for both the exact scope and typed confirmation; cancellation removes nothing", async () => {
  await show();
  await menu("Delete runtime data");
  assert.match(text(page.ids["choice-list"]), /own-volume/);
  assert.match(text(page.ids["choice-list"]), /\.lab\/runtimes\/own/);
  assert.deepEqual(posts(), []);
  await choice(true);
  assert.equal(page.ids["confirm-input"].placeholder, "Type delete to confirm");
  assert.deepEqual(posts(), []);
  page.ids.confirm.returnValue = "";
  page.ids.confirm.onclose();
  await flush();
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
  assert.ok(isOff(root.querySelector("[data-runtime-menu]")));
  release();
  holdRead = null;
  await flush();
  assert.ok(!isOff(root.querySelector("[data-runtime-act]")));
  assert.ok(!isOff(root.querySelector("[data-runtime-menu]")));
  close();
});
