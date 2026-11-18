// The Stack page (web/js/pages/stack.js), run for real over a small DOM: what it draws for a service that needs a license while there is
// none. The server says so (`needs_license`, a note that opens the License form, `on` false): the page lists it all the same, with the
// note, and offers it no Start, on its row or on its group's. And what its Reset says before it runs: what it turns off, and what stays
// in a platform that playgrounds deployed to.
import assert from "node:assert/strict";
import { mock, test } from "node:test";

import { Ev, installDom, isOff, offWhy, text } from "./dom-stub.mjs";

mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
const OPEN = JSON.stringify({ "group:alert-engine": true, "group:api-management": true });   // the groups the page remembers open
const requests = [];
const page = installDom({ stored: { expanded: OPEN }, fetch: async (path, options = {}) => {
  requests.push({ path, body: options.body ? JSON.parse(options.body) : null });
  return { ok: true, status: 200, json: async () => ({ id: "abcdef12", key: "service:runtime-a--api", verb: "stop", title: "Stop", done: true, ok: true, phases: [], progress: null }) };
} });
globalThis.innerHeight = 800;   // the menu opens under its button, or above it when the window ends first
const { store } = await import("../../../web/js/core.js");
const { stackPage } = await import("../../../web/js/pages/stack.js");
const { paintBusy } = await import("../../../web/js/components/ui.js");
const registry = { platforms: [] };   // the platforms registered, which a test sets
Object.defineProperty(store, "platforms", { get: () => registry.platforms, configurable: true });

const RUNNING = { key: "running", label: "Running", running: true };
const ABSENT = { key: "absent", label: "Not created", running: false };
const WAITING = { note: "Needs a license", note_tone: "warn", note_title: "Not started without one", note_target: null, note_file: ".env" };
const NONE = { note: "", note_tone: "", note_title: "", note_target: null, note_file: null };

function service(label, state, more = {}) {
  return { label, description: "", container: `gio-${label}`, image: `graviteeio/${label}:1`, tag: "1", registry: "graviteeio", built: false,
    role: null, needs_license: false, name: label, ports: [], state, uptime: state.running ? "5 min" : "", on: true, running_tag: state.running ? "1" : null,
    behind: false, brings: [], recreates: [], ...NONE, ...more };
}
const group = (id, name, services, state, more = {}) => ({ id, name, services, state, progress: null, version: "1", registry: null, registry_label: null,
  product: null, file: "docker-compose.yml", env_file: ".env", settings: [], brings: [], recreates: [], ...NONE, ...more });

// The platform with everything running but the Alert Engine, which needs a license that is not there, or is
function lab({ licensed }) {
  const engine = licensed ? service("alert-engine", ABSENT, { needs_license: true })
    : service("alert-engine", ABSENT, { needs_license: true, on: false, ...WAITING });
  const groups = [
    group("api-management", "API Management", ["gateway"], { key: "running", label: "Running" }),
    group("alert-engine", "Alert Engine", ["alert-engine"], licensed ? { key: "stopped", label: "Stopped" } : { key: "off", label: "" },
      licensed ? {} : WAITING),
  ];
  return {
    blind: false, wait: "", docker: { error: null }, license: null, products: [], categories: [], playgrounds: [], addons: [], setups: [], links: [], problems: [],
    services: { gateway: service("gateway", RUNNING), "alert-engine": engine },
    platform: { name: "Gravitee platform", state: { key: "running", label: "Running" }, progress: null, groups, file: "docker-compose.yml", env_file: ".env",
      settings: [], start: { needed: licensed, starts: [], restarts: [], enabled: true, reason: null, dialog: null },
      apply: { needed: false }, restart: { needed: true, enabled: true, reason: null }, update: { needed: true, enabled: true, reason: null, dialog: null } },
  };
}

const section = page.doc.createElement("section");
page.doc.body.append(section);
stackPage.init(section);

function draw(state) {
  store.state = state;
  stackPage.render(state);
  const row = section.querySelector('.group[data-key="group:alert-engine"] > .group-row');
  const container = section.querySelector('.container[data-service="alert-engine"]');
  return { row, container };
}

test("a service that needs a license says so on its group and on its line, and is not offered a Start", () => {
  const { row, container } = draw(lab({ licensed: false }));
  assert.ok(row, "its group is listed though nothing of it is on or has a container");
  assert.equal(text(row.querySelector(".g-name")), "Alert Engine");
  assert.equal(text(row.querySelector(".s-name .pill.note")), "Needs a license", "a badge after the name");
  assert.equal(row.querySelectorAll('[data-act="start"]').length, 0, "its group has no Start");
  assert.ok(container, "its line is listed");
  assert.equal(text(container.querySelector(".c-name")), "alert-engine");
  assert.equal(text(container.querySelector(".s-name .pill.note")), "Needs a license");
  assert.equal(container.querySelectorAll('[data-act="start"]').length, 0, "nor has its line");
  assert.equal(text(container.querySelector(".s-state")), "Not created", "its state in the State column");
  assert.equal(text(container.querySelector(".s-uptime")), "", "no uptime: it does not run");
});

test("its note is the one every license note is: a warn badge that leads to the License form, its reason the tooltip", () => {
  const { row, container } = draw(lab({ licensed: false }));
  for (const note of [row.querySelector(".pill.note"), container.querySelector(".pill.note")]) {
    assert.ok(note.classList.contains("warn"), "the warn tone, not the err one of an issue");
    assert.equal(note.tag, "button");
    assert.equal(note.getAttribute("data-license"), "form");
    assert.equal(note.getAttribute("data-tip"), "Not started without one");
  }
  assert.ok(!row.querySelector(".s-state .status"), "no state is said, being off");
  assert.ok(!row.querySelector(".dot-status.issue") && !container.querySelector(".dot-status.issue"), "nothing red");
});

test("with a license it is a service as any other: no note, its state a dot and a word, a Start for what is on and does not run", () => {
  const { row, container } = draw(lab({ licensed: true }));
  assert.equal(text(row.querySelector(".s-name")), "Alert Engine");
  assert.equal(text(row.querySelector(".s-state")), "Stopped");
  assert.ok(row.querySelector(".s-state .status.muted .dot-status.stopped"), "the state component, muted while stopped");
  assert.equal(row.querySelectorAll('[data-act="start"]').length, 1);
  assert.equal(container.querySelectorAll('[data-act="start"]').length, 1);
  assert.equal(container.querySelectorAll(".pill.note").length, 0);
});

test("a group has run whole as long as its youngest container, one in its first minute too", () => {
  const state = lab({ licensed: true });
  state.services.api = service("api", RUNNING, { uptime: "< 1 min" });
  state.platform.groups[0].services = ["gateway", "api"];
  draw(state);
  const row = section.querySelector('.group[data-key="group:api-management"] > .group-row');
  assert.equal(text(row.querySelector(".s-uptime")), "< 1 min");
  assert.equal(text(section.querySelector('.container[data-service="api"] .s-uptime')), "< 1 min");
});

test("a service of no license that is not on and has no container is still left out", () => {
  const state = lab({ licensed: true });
  state.services["alert-engine"].needs_license = false;
  state.services["alert-engine"].on = false;
  const { row, container } = draw(state);
  assert.equal(row, null);
  assert.equal(container, null);
});

test("an entry of the on file that Compose cannot read holds what reads the files, not what Compose runs by the project's name", () => {
  // Stop, a group's Stop and Restart go by the project's name, which reads no file: the lab that runs can still be stopped
  const line = "addons/local/mine/compose.yml does not exist";
  draw({ ...lab({ licensed: true }), unread: line });
  paintBusy(section);
  const head = (verb) => section.querySelector(`[data-stack="${verb}"]`);
  const api = section.querySelector('.group[data-key="group:api-management"] > .group-row');
  for (const button of [head("stop"), api.querySelector('[data-act="stop"]'), api.querySelector('[data-act="restart"]')]) {
    assert.ok(button && !isOff(button), button?.getAttribute("data-act") || button?.getAttribute("data-stack"));
  }
  const engine = section.querySelector('.group[data-key="group:alert-engine"] > .group-row');
  for (const button of [head("start"), head("restart"), head("update"), engine.querySelector('[data-act="start"]')]) {
    assert.ok(isOff(button), button.getAttribute("data-act") || button.getAttribute("data-stack"));
    assert.equal(offWhy(button), line);
  }
  draw(lab({ licensed: true }));
  paintBusy(section);
  assert.ok(!isOff(head("start")), "the entry is fixed: Start no longer waits");
});

// --- The bands --------------------------------------------------------------------------------

// A platform with an add-on of its own, another maker's, and a playground that run
function withItems() {
  const state = lab({ licensed: true });
  state.services.agent = service("agent", RUNNING);
  state.services.kazoo = service("kazoo", RUNNING);
  state.services.app = service("app", RUNNING);
  state.addons = [{ ...group("kazoo", "Kazoo", ["kazoo"], RUNNING), kind: "addon", category: "Event", on: true, up: true },
    { ...group("sorrel", "Sorrel", ["agent"], RUNNING), kind: "addon", category: "Gravitee platform", on: true, up: true }];
  state.playgrounds = [{ ...group("shop", "Shop", ["app"], RUNNING), kind: "playground", on: true, up: true, apps: ["app"], deployed: [], deploys_to: [] }];
  return state;
}
const keys = () => section.querySelectorAll(".groups > li").map((li) => li.getAttribute("data-key"));

test("the platform's groups come under its band, its own add-ons inside it under a band of their own, then the other add-ons and the playground", () => {
  draw(withItems());
  assert.deepEqual(keys(), ["core", "group:api-management", "group:alert-engine", "section:Add-ons", "addon:sorrel", "section:Other add-ons", "addon:kazoo",
    "section:Playground", "playground:shop"]);
});

test("a band folds what is under it: the platform's its groups and the add-ons, the add-ons' theirs; the playground stays", () => {
  const state = withItems();
  draw(state);
  const click = (key) => {
    section.querySelector(`.groups > li[data-key="${key}"] [data-fold]`).click();
    draw(state);   // the page draws again when the state is told of the change
  };
  click("section:Add-ons");
  assert.deepEqual(keys(), ["core", "group:api-management", "group:alert-engine", "section:Add-ons", "section:Other add-ons", "addon:kazoo", "section:Playground", "playground:shop"]);
  click("core");
  assert.deepEqual(keys(), ["core", "section:Other add-ons", "addon:kazoo", "section:Playground", "playground:shop"], "what is not the platform's stays");
  click("core");
  click("section:Add-ons");
  assert.equal(keys().length, 9, "open again");
});

test("a band stays open while one of its groups is in an issue, and has no toggle then", () => {
  const state = withItems();
  draw(state);
  section.querySelector('.groups > li[data-key="core"] [data-fold]').click();
  draw(state);
  assert.equal(keys().length, 5, "folded");
  state.platform.groups[0].state = { key: "issue", label: "Gateway exited (1)", running: true };
  draw(state);
  assert.ok(keys().includes("group:api-management"), "what is wrong is not hidden");
  assert.equal(section.querySelector('.groups > li[data-key="core"] [data-fold]'), null);
  state.platform.groups[0].state = RUNNING;
  draw(state);
  assert.equal(keys().length, 5, "the folded choice was kept");
  section.querySelector('.groups > li[data-key="core"] [data-fold]').click();
  draw(state);
  assert.equal(keys().length, 9);
});

// --- Reset --------------------------------------------------------------------------------------

const ACCOUNT = { id: "acme", name: "My Account", kind: "cloud", url: "https://eu.cloud.gravitee.io" };
const playground = (name, deployed = []) => ({ kind: "playground", id: name.toLowerCase().replace(" ", "-"), name, deployed, deploys_to: ["cloud"] });

// What a menu entry of the head says before it runs: its dialog, left open
function says(entry, { off = null, playgrounds = [], platforms = [] } = {}) {
  const state = lab({ licensed: true });
  registry.platforms = platforms;
  store.state = { ...state, playgrounds, platform: { ...state.platform, reset_turns_off: off } };
  stackPage.render(store.state);
  section.querySelector("[data-more]").click();
  const item = page.ids.menu.querySelectorAll(".menu-item").find((el) => text(el).startsWith(entry));
  page.doc.dispatchEvent(new Ev("click", { target: item }));
  return { title: text(page.ids["confirm-title"]), text: text(page.ids["confirm-text"]), action: text(page.ids["confirm-ok"]),
    word: page.ids["confirm-input"].placeholder };
}

const RESET = { title: "Reset", action: "Reset", word: "Type reset to confirm" };

test("Reset says what it turns off, and what it deletes", () => {
  assert.deepEqual(says("Reset"), { ...RESET, text: "Deletes every container and volume" });
  assert.deepEqual(says("Reset", { off: "Acme Bakery" }), { ...RESET, text: "Deletes every container and volume, and turns off Acme Bakery" });
});

test("it says what stays in a platform that a playground deployed to, and keeps its typed word", () => {
  const hotels = playground("Acme Hostels", [{ place: "acme/development", environment: "Development", created: [{ what: "APIs", count: 3 }] }]);
  const said = says("Reset", { off: "Acme Hostels", playgrounds: [hotels], platforms: [ACCOUNT] });
  assert.deepEqual(said, { ...RESET, text: "Deletes every container and volume, and turns off Acme Hostels. Stays: Acme Hostels in Development (My Account)" });
  assert.ok(page.ids["confirm-ok"].disabled, "it waits for the word");
  assert.equal(says("Reset", { playgrounds: [hotels], platforms: [ACCOUNT] }).text, "Deletes every container and volume. Stays: Acme Hostels in Development (My Account)");
});

test("each place that holds a record stays, whichever playground made it", () => {
  const record = (place, environment) => ({ place, environment, created: [{ what: "APIs", count: 1 }] });
  const playgrounds = [playground("Acme Hostels", [record("acme/development", "Development"), record("client/test", "Test")]),
    playground("Acme Bakery"), playground("Welcome", [record("acme/test", "Test")])];
  const platforms = [ACCOUNT, { ...ACCOUNT, id: "client", name: "Client lab" }];
  assert.equal(says("Reset", { playgrounds, platforms }).text, "Deletes every container and volume. Stays: "
    + "Acme Hostels in Development (My Account), Acme Hostels in Test (Client lab), Welcome in Test (My Account)");
});

test("a record of a platform that is no longer registered still stays, by the id of its platform", () => {
  const hotels = playground("Acme Hostels", [{ place: "gone/development-1b4548fc", environment: "Development", created: [{ what: "APIs", count: 3 }] }]);
  assert.equal(says("Reset", { playgrounds: [hotels], platforms: [ACCOUNT] }).text, "Deletes every container and volume. Stays: Acme Hostels in Development (gone)");
});

test("a platform where nothing was deployed, and the other action that removes, say nothing of it", () => {
  assert.equal(says("Reset", { off: "Acme Bakery", playgrounds: [playground("Acme Hostels")], platforms: [ACCOUNT] }).text,
    "Deletes every container and volume, and turns off Acme Bakery");
  const record = { place: "acme/development", environment: "Development", created: [] };
  assert.deepEqual(says("Remove containers", { playgrounds: [playground("Acme Hostels", [record])], platforms: [ACCOUNT] }),
    { title: "Remove containers", text: "Data is kept", action: "Remove", word: "" });
});


test("shared Reset names its own scope and explicitly keeps independent runtime data", () => {
  const independent = { ...playground("Private A"), runtime: { mode: "independent", prepared: true } };
  const text = says("Reset", { playgrounds: [independent] }).text;
  assert.match(text, /shared platform/);
  assert.match(text, /Stays: Private A \(independent\)/);
});

test("Containers has a runtime selector instead of a runtime inventory", () => {
  draw(lab({ licensed: true }));
  assert.equal(section.querySelector("[data-runtime-panel]"), null);
  assert.equal(text(section.querySelector("#container-runtime-value")), "Shared platform");
  assert.ok(section.querySelector('a[href="#/runtimes"]'));
});

function scopedLab() {
  const state = lab({ licensed: true });
  state.runtime_scopes = {};
  for (const [id, port] of [["runtime-a", "31000"], ["runtime-b", "32000"]]) {
    const api = `${id}--api`, seed = `${id}--seed`;
    state.services[api] = service("Private API", RUNNING, { container: `${id}-api-1`, ports: [port], tag: "2.3", running_tag: null,
      runtime: { id, service: "api" } });
    state.services[seed] = service("Seed", { key: "done", label: "Done", running: false }, { role: "seed", runtime: { id, service: "seed" } });
    state.runtime_scopes[id] = { id, name: id === "runtime-a" ? "Demo" : "Other", playground: "local/source", playground_name: "Source", source: true,
      prepared: true, initialized: true, running: true, active: true, services: [api, seed], groups: [
        group(`${id}--product`, "Product", [api, seed], RUNNING, { runtime: id })] };
  }
  state.links = [{ label: "Demo console", url: "http://localhost:31000", port: 31000, section: "Demo", service: "runtime-a--api", up: true },
    { label: "Other console", url: "http://localhost:32000", port: 32000, section: "Other", service: "runtime-b--api", up: true }];
  return state;
}

test("one Containers table displays only the selected runtime, with its ports, console and captured version", () => {
  const state = scopedLab();
  stackPage.show(["runtime-a"]);
  draw(state);
  assert.equal(store.unit('group:runtime-a--product'), state.runtime_scopes['runtime-a'].groups[0]);
  section.querySelector('[data-toggle]').click();
  stackPage.render(state);
  assert.match(text(section.querySelector('.groups')), /Private API/);
  assert.match(text(section.querySelector('.groups')), /31000/);
  assert.match(text(section.querySelector('.groups')), /2\.3/);
  assert.doesNotMatch(text(section.querySelector('.groups')), /32000|gateway|alert-engine/);
  assert.match(text(section.querySelector('.links')), /Demo console/);
  assert.doesNotMatch(text(section.querySelector('.links')), /Other console/);
  assert.equal(section.querySelector('.page-head > .page-actions').hidden, true);
  assert.equal(section.querySelector('.groups [data-settings]'), null);
  const seed = section.querySelector('[data-service="runtime-a--seed"]');
  assert.equal(seed.querySelector('[data-act="start"]'), null);
  assert.equal(seed.querySelector('[data-act="restart"]'), null);
  stackPage.show([]);
  draw(state);
  assert.equal(text(section.querySelector('#container-runtime-value')), 'Shared platform');
  assert.equal(section.querySelector('[data-service="runtime-a--api"]'), null);
  assert.equal(section.querySelector('.page-head > .page-actions').hidden, false);
});

test("service and group controls retain the selected runtime's keys and never call Shared", async () => {
  const refresh = store.refresh;
  store.refresh = async () => {};
  const state = scopedLab();
  const flush = async () => { for (let i = 0; i < 15; i += 1) await Promise.resolve(); };
  try {
    for (const action of ['stop', 'restart', 'start']) {
      state.services['runtime-a--api'].state = action === 'start' ? { key: 'stopped', label: 'Stopped', running: false } : RUNNING;
      stackPage.show(['runtime-a']);
      draw(state);
      const group = section.querySelector('.group-row');
      if (group.querySelector('[data-toggle]').getAttribute('aria-expanded') !== 'true') group.querySelector('[data-toggle]').click();
      stackPage.render(state);
      requests.length = 0;
      section.querySelector(`[data-service="runtime-a--api"] [data-act="${action}"]`).click();
      await flush();
      assert.deepEqual(requests.filter(r => r.body).map(r => r.path), [`/api/services/runtime-a--api/${action}`]);
    }
    state.services['runtime-a--api'].state = RUNNING;
    draw(state);
    requests.length = 0;
    section.querySelector('.group-row [data-act="stop"]').click();
    await flush();
    assert.deepEqual(requests.filter(r => r.body).map(r => r.path), ['/api/runtimes/runtime-a/groups/runtime-a--product/stop']);
  } finally { store.refresh = refresh; stackPage.show([]); draw(lab({ licensed: true })); }
});

test("Docker offline keeps a selected runtime's captured ports and never falls back to Shared", () => {
  const state = scopedLab();
  state.docker.error = 'Docker is not reachable';
  state.services['runtime-a--api'].state = { key: 'unknown', label: 'Unavailable', running: false };
  stackPage.show(['runtime-a']);
  draw(state);
  assert.equal(text(section.querySelector('#container-runtime-value')), 'Demo');
  assert.match(text(section.querySelector('.groups')), /31000/);
  assert.equal(section.querySelector('[data-service="gateway"]'), null);
  stackPage.show(['runtime-missing']);
  draw(state);
  assert.equal(section.querySelector('.stack-layout').hidden, true);
  assert.equal(section.querySelector('.page-head > .page-actions').hidden, true);
  stackPage.show([]);
  draw(lab({ licensed: true }));
});

test("container Logs selects only its private service and navigation sends no mutation", async () => {
  const streams = [];
  globalThis.EventSource = class { constructor(url) { streams.push(url); } close() {} addEventListener() {} };
  const { logsPage } = await import('../../../web/js/pages/logs.js');
  const state = scopedLab();
  stackPage.show(['runtime-a']);
  draw(state);
  const logRoot = page.doc.createElement('section');
  page.doc.body.append(logRoot);
  logsPage.init(logRoot);
  const toggle = section.querySelector('.group-row [data-toggle]');
  if (toggle.getAttribute('aria-expanded') !== 'true') toggle.click();
  stackPage.render(state);
  requests.length = 0;
  section.querySelector('[data-service="runtime-a--api"] [data-act="logs"]').click();
  assert.equal(location.hash, '#/logs');
  assert.match(streams.at(-1), /services=runtime-a--api&/);
  assert.doesNotMatch(streams.at(-1), /runtime-b--api|services=gateway&/);
  assert.deepEqual(requests.filter(r => r.body), []);
  logsPage.hide();
  logRoot.remove();
  stackPage.show([]);
  draw(lab({ licensed: true }));
});
