// Named runtime management and storage, shared by its page and a playground's More dialog.
import { $, esc, icon, size, api, store, router, setHTML, loading } from "../core.js";
import { effectsDialog, confirmDialog, openMenu, toast, statusHtml, paintActions, actionButton, holdOff } from "./ui.js";
import { createRuntime, independentAction, runtimeActionEntries } from "../dialogs/runtimes.js";
import { runJob } from "../activity/jobs.js";

const CLEANUP = {
  containers: { label: "Delete containers, keep data", hint: "Keeps sandbox; Start recreates containers" },
  data: { label: "Delete data, keep sandbox", hint: "Deletes all data; next Start initializes fresh data" },
  delete: { label: "Delete sandbox and data", hint: "Permanently deletes this sandbox and all its data" },
};

export function runtimePanel(root, playground = null, { heading = true, current = null } = {}) {
  let model = null, visible = false, reading = false, pending = false, serial = 0, signature = "", failure = "";
  let measured = null, measuring = false, storageFailure = "";
  root.innerHTML = `<div class="card runtime-overview"><div class="card-head">${heading ? "<h2>Sandboxes</h2>" : ""}
    <div class="page-actions">${playground ? '<button class="btn" data-runtime-create>Create sandbox</button>' : ""}<button class="btn" data-runtime-refresh>Refresh storage</button></div></div>
    <div class="runtime-summary" role="status"></div><div class="table-wrap"><table class="table"><thead><tr><th>Sandbox</th><th>State</th><th>Storage</th><th>Actions</th></tr></thead>
    <tbody><tr><td colspan="4" class="empty">${loading("Reading sandboxes")}</td></tr></tbody></table></div></div>`;
  const source = (row) => store.item("playground", row.playground || row.id);
  const wait = () => store.wait(true) || (reading ? "Reading sandboxes" : failure ? "Refresh sandbox information first" : "");
  const cannot = (row) => row.problem || (!row.source ? "Catalog source unavailable" : "");
  const deleting = (row) => {
    const key = `playground:${row.id}`, job = store.state?.job;
    return store.pending.get(key) === "delete" || (job?.for === key && job.verb === "delete" && !job.done);
  };

  function withStorage(row) {
    const sizes = measured?.runtimes?.find((entry) => entry.id === row.id);
    if (!sizes) return row;
    const { local_bytes, volume_bytes, cpu_percent, memory_bytes } = sizes;
    const volumes = row.volumes.map((volume) => ({ ...volume,
      bytes: sizes.volumes.find((found) => found.name === volume.name)?.bytes ?? null }));
    return { ...row, local_bytes, volume_bytes, cpu_percent, memory_bytes, volumes, problem: row.problem || sizes.problem };
  }

  function render() {
    const summary = [...(measured?.shared_storage || []).map((part) => `${part.kind}: ${size(part.bytes)} (machine-wide)`),
      ...new Set([...(model?.problems || []), ...(measured?.problems || [])].map((p) => p.message)),
      ...(failure ? [`Sandbox information could not be refreshed. ${failure}`] : []),
      ...(storageFailure ? [`Storage could not be refreshed. ${storageFailure}`] : [])];
    const progress = !model ? "" : reading ? loading("Refreshing sandboxes") : measuring ? loading("Reading storage") : "";
    setHTML($(".runtime-summary", root), progress + (summary.length ? `<p class="sub">${summary.map(esc).join("; ")}</p>` : ""));
    const refresh = $("[data-runtime-refresh]", root);
    const refreshWait = store.wait(true) || (reading ? "Reading sandboxes" : measuring ? "Reading storage"
      : model?.available === false && !failure ? "Docker is not reachable" : "");
    holdOff(refresh, !!refreshWait, refreshWait);
    if (!model) {
      setHTML($("tbody", root), `<tr><td colspan="4" class="empty">${failure ? "Sandbox information unavailable" : loading("Reading sandboxes")}</td></tr>`);
      return;
    }
    let parent = null;
    setHTML($("tbody", root), (model.runtimes || []).map((saved, index) => {
      const row = withStorage(saved);
      if (!row.id || playground && row.playground !== playground) return "";
      const group = !playground && parent !== row.playground;
      parent = row.playground;
      const total = row.volume_bytes === null ? null : row.volume_bytes + (row.local_bytes || 0);
      const status = deleting(row) ? statusHtml("stopping", "Deleting")
        : statusHtml(row.problem ? "issue" : row.running ? "running" : "stopped", row.problem ? "Needs attention" : row.state);
      const button = actionButton({ key: `playground:${row.id}`, verb: row.running || row.active ? "stop" : "start",
        label: row.running || row.active ? "Stop" : "Start", cls: "btn", attrs: `data-runtime-act="${row.running || row.active ? "stop" : "start"}" data-busy` });
      const open = row.source ? `<a class="btn" href="#/playgrounds/${esc(row.id)}" aria-label="Open sandbox ${esc(row.name)}" data-tip="Guide and consoles">Open sandbox</a>` : "";
      return `${group ? `<tr><td colspan="4"><div class="card-head"><h3>${esc(row.playground_name || row.playground || row.name)}</h3>${source(row) ? `<button class="btn" data-runtime-create-for="${esc(row.playground || row.id)}">Create sandbox</button>` : ""}</div></td></tr>` : ""}
        <tr data-runtime-row="${index}"><td><div class="runtime-name">${esc(row.name)}${!row.source ? '<span class="sub">Source unavailable</span>' : ""}</div></td>
        <td>${status}</td>
        <td><button class="btn" data-runtime-details aria-label="Storage of ${esc(row.name)}" data-tip="Storage details">${size(total)}</button></td>
        <td><div class="page-actions"><a class="btn" href="#/containers/${encodeURIComponent(row.id)}" aria-label="Containers of ${esc(row.name)}">Containers</a>${open}${button}<span class="row-icons"><button class="icon-btn" data-runtime-menu aria-haspopup="true" aria-label="Manage ${esc(row.name)}" data-tip="More">${icon("more")}</button></span></div></td></tr>`;
    }).join("") || `<tr><td colspan="4" class="empty">No sandboxes yet${playground ? "" : ' <a href="#/playgrounds">Choose a playground</a>'}</td></tr>`);
    for (const el of root.querySelectorAll("[data-runtime-act], [data-runtime-menu]")) {
      const row = withStorage(model.runtimes[Number(el.closest("[data-runtime-row]").dataset.runtimeRow)]);
      const reason = deleting(row) ? "Cleanup in progress" : el.hasAttribute("data-runtime-act")
        ? wait() || (el.dataset.runtimeAct === "start" ? cannot(row) : row.problem) : "";
      holdOff(el, !!reason, reason || "");
    }
    for (const button of root.querySelectorAll("[data-runtime-create], [data-runtime-create-for]")) holdOff(button, !!wait(), wait());
    paintActions(root);
  }

  function inventoryPath(option = "") {
    const query = new URLSearchParams();
    if (playground) query.set("playground", playground);
    if (option) query.set(option, "1");
    return query.size ? "/api/runtimes?" + query : "/api/runtimes";
  }

  async function readStorage(fresh = false) {
    if (!visible || measuring || !model?.available) return;
    const listed = model;
    measuring = true;
    storageFailure = "";
    render();
    try {
      const result = await api(inventoryPath(fresh ? "refresh" : "storage"));
      if (visible && model === listed) measured = result;
    } catch (error) {
      if (visible && model === listed) storageFailure = error.message;
    } finally {
      measuring = false;
      if (visible) {
        render();
        if (model !== listed && !reading && !failure) readStorage();
      }
    }
  }

  async function read() {
    if (!visible) return;
    if (reading) { pending = true; render(); return; }
    const current = ++serial;
    reading = true;
    failure = "";
    render();
    try {
      const result = await api(inventoryPath());
      if (current === serial && visible) {
        model = result;
        measured = null;
        storageFailure = "";
      }
    } catch (error) {
      if (current === serial && visible) failure = error.message;
    } finally {
      reading = false;
      if (pending && visible) { pending = false; read(); }
      else if (current === serial && visible) {
        render();
        if (!failure) readStorage();
      }
    }
  }

  async function act(row, operation) {
    if (wait()) return;
    if (operation === "start") return independentAction(store.item("playground", row.id) || { id: row.id, name: row.name }, operation);
    runJob(() => api(`/api/runtimes/${row.id}/${operation}`, {}), { key: `playground:${row.id}`, verb: operation });
  }

  async function cleanup(row, operation) {
    try {
      const path = `/api/runtimes/${row.id}/cleanup`;
      const plan = await api(path, { operation, check: true });
      const { label: title, hint } = CLEANUP[operation];
      const rows = Object.entries(plan.removes).filter(([, values]) => values.length).map(([kind, values]) => ({ label: `Deletes ${kind === "runtime" ? "sandbox" : kind}`, items: values }));
      rows.push({ label: "Keeps", items: plan.keeps, note: true });
      if (!(await effectsDialog({ title: `${title}: ${row.name}`, options: [{ value: true, rows, sub: hint }], action: () => title, danger: () => operation !== "containers" }))) return;
      if (operation !== "containers" && !(await confirmDialog({ title: `${title}: ${row.name}`,
        text: `${hint}. The source playground, shared platform and other sandboxes stay.`,
        action: title, word: "delete", danger: true }))) return;
      runJob(async () => {
        const job = await api(path, { operation, fingerprint: plan.fingerprint });
        toast(`Cleanup started for ${row.name}`);
        return job;
      }, { key: `playground:${row.id}`, verb: "delete", quiet: true });
      store.notify();
    } catch (error) { toast(error.message, true); }
  }

  function navigate(id) {
    root.closest("dialog[open]")?.close();
    router.go(`#/playgrounds/${id}`);
  }

  function details(row) {
    const rows = [{ label: "Containers", items: row.containers.map((c) => `${c.name}: ${c.state}`) },
      { label: "CPU", items: row.cpu_percent == null ? [] : [`${row.cpu_percent}%`] },
      { label: "Memory", items: row.memory_bytes == null ? [] : [size(row.memory_bytes)] },
      { label: "Ports", items: row.ports || [] },
      { label: "Volumes", items: row.volumes.map((v) => `${v.name}: ${size(v.bytes)}`) },
      { label: "Sandbox files", items: row.folder ? [`${row.folder}: ${size(row.local_bytes)}`] : [] },
      { label: "Captured versions", items: Object.entries(row.versions || {}).map(([key, value]) => `${key}: ${value}`) },
      { label: "Captured images", items: (row.images || []).map((image) => `${image.service}: ${image.reference} (${image.id})`) },
      { label: "Prepared", items: row.prepared_at ? [new Date(row.prepared_at).toLocaleString()] : [] },
      { label: "Problem", items: row.problem ? [row.problem] : [] }];
    effectsDialog({ title: row.name, options: [{ value: true, rows: rows.map((row) => ({ ...row, note: true })) }], action: () => "Close" });
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.getAttribute("aria-disabled") === "true") return;
    if (button.hasAttribute("data-runtime-refresh")) return failure ? read() : readStorage(true);
    if (button.hasAttribute("data-runtime-create")) return createRuntime(store.item("playground", playground), current);
    if (button.hasAttribute("data-runtime-create-for")) return createRuntime(store.item("playground", button.dataset.runtimeCreateFor));
    const saved = model?.runtimes[Number(button.closest("[data-runtime-row]")?.dataset.runtimeRow)];
    if (!saved) return;
    const row = withStorage(saved);
    if (button.hasAttribute("data-runtime-details")) return details(row);
    if (button.hasAttribute("data-runtime-act")) return act(row, button.dataset.runtimeAct);
    if (button.hasAttribute("data-runtime-menu")) {
      const item = source(row), blocked = wait();
      const choices = [
        { label: "Open sandbox", onSelect: () => navigate(row.id), disabled: !item, reason: item ? "" : "Catalog source unavailable" },
        { label: "Source playground", onSelect: () => navigate(row.playground || row.id), disabled: !item, reason: item ? "" : "Catalog source unavailable" },
        { label: "Clone sandbox", disabled: !!blocked || !item || !row.running || !!cannot(row), reason: blocked || cannot(row) || "Start this sandbox first",
          onSelect: () => createRuntime(item, row.id) },
        { label: "Restart", disabled: !!blocked || !row.running || !!cannot(row), reason: blocked || cannot(row) || "Start this sandbox first", onSelect: () => act(row, "restart") },
        ...runtimeActionEntries(store.item("playground", row.id) || { id: row.id, name: row.name,
          runtime: { prepared: row.prepared, initialized: row.initialized, running: !!row.running } }).map((entry) => ({ ...entry,
          disabled: !!blocked || !!cannot(row) || entry.disabled, reason: blocked || cannot(row) || entry.reason })),
        ...["containers", "data", "delete"].map((operation) => ({ ...CLEANUP[operation], danger: operation !== "containers",
          disabled: !!blocked || !!row.problem || !(row.cleanup[operation] || { enabled: false }).enabled,
          reason: blocked || row.problem || row.cleanup[operation]?.reason,
          onSelect: () => cleanup(row, operation) }))];
      openMenu(button, choices);
    }
  });

  const stamp = (state) => JSON.stringify([state?.docker?.error, state?.job?.id, state?.job?.done,
    (state?.playgrounds || []).map((p) => [p.id, p.runtime?.mode, p.on, p.state?.key])]);
  return {
    show() { visible = true; signature = stamp(store.state); read(); },
    hide() { visible = false; serial += 1; },
    render(state) {
      const next = stamp(state);
      if (visible && next !== signature && (!state?.job || state.job.done)) { signature = next; read(); }
      render();
    },
  };
}
