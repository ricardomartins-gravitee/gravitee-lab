// The Containers page's runtime inventory: stopped runtimes stay visible, and cleanup previews only their own resources.
import { $, esc, icon, size, api, store, router, setHTML, loading } from "../core.js";
import { effectsDialog, confirmDialog, openMenu, toast, statusHtml, itemLink, paintActions, actionButton, holdOff } from "./ui.js";
import { createRuntime, independentAction } from "../dialogs/runtimes.js";
import { openSettings } from "../dialogs/editor.js";
import { runJob } from "../activity/jobs.js";

export function runtimePanel(root, playground = null, { heading = true, current = null } = {}) {
  let model = null, visible = false, reading = false, pending = false, serial = 0, signature = "";
  root.innerHTML = `<div class="card runtime-overview"><div class="card-head">${heading ? "<h2>Runtimes</h2>" : ""}
    <div class="page-actions">${playground ? '<button class="btn" data-runtime-create>Create runtime</button>' : ""}<button class="btn" data-runtime-refresh>Refresh storage</button></div></div>
    <div class="runtime-summary"></div><div class="table-wrap"><table class="table"><thead><tr><th>Runtime</th><th>State</th><th>CPU</th><th>Memory</th><th>Storage</th><th>Actions</th></tr></thead>
    <tbody><tr><td colspan="6" class="empty">${loading("Reading runtimes")}</td></tr></tbody></table></div></div>`;
  const source = (row) => row.id ? store.item("playground", row.playground || row.id) : store.state?.platform;
  const wait = () => store.wait(true) || (reading ? "Reading runtimes" : "");
  const cannot = (row) => row.problem || (!row.source ? "Catalog source unavailable" : "");

  function render() {
    if (!model) return;
    const summary = [...(playground ? [] : ["Playgrounds use the shared platform unless a named runtime is opened"]),
      ...(model.shared_storage || []).map((part) => `${part.kind}: ${size(part.bytes)} (machine-wide)`),
      ...(model.problems || []).map((p) => p.message)];
    setHTML($(".runtime-summary", root), summary.length ? `<p class="sub">${summary.map(esc).join("; ")}</p>` : "");
    setHTML($("tbody", root), (model.runtimes || []).map((row, index) => {
      if (playground && row.playground !== playground) return "";
      const total = row.volume_bytes === null ? null : row.volume_bytes + (row.local_bytes || 0);
      const button = row.id ? actionButton({ key: `playground:${row.id}`, verb: row.running || row.active ? "stop" : "start",
        label: row.running || row.active ? "Stop" : "Start", cls: "btn", attrs: `data-runtime-act="${row.running || row.active ? "stop" : "start"}" data-busy` }) : "";
      return `<tr data-runtime-row="${index}"><td>${row.id && row.source ? itemLink({ kind: "playground", id: row.id, name: row.name }) : esc(row.name)}
        <span class="sub">${row.id ? "Independent" + (row.playground_name ? `, ${row.playground_name}` : "") + (!row.source ? ", Source unavailable" : "") : "Shared"}</span></td>
        <td>${statusHtml(row.problem ? "issue" : row.running ? "running" : "stopped", row.problem ? "Needs attention" : row.state)}</td>
        <td>${row.cpu_percent === null ? "—" : `${row.cpu_percent}%`}</td><td>${size(row.memory_bytes, true)}</td>
        <td><button class="btn" data-runtime-details aria-label="Storage of ${esc(row.name)}" data-tip="Storage details">${size(total)}</button></td>
        <td><span class="row-icons">${button}<button class="icon-btn" data-runtime-menu aria-haspopup="true" aria-label="Manage ${esc(row.name)}" data-tip="More">${icon("more")}</button></span></td></tr>`;
    }).join("") || '<tr><td colspan="6" class="empty">No independent runtimes yet</td></tr>');
    for (const el of root.querySelectorAll("[data-runtime-act]")) {
      const row = model.runtimes[Number(el.closest("[data-runtime-row]").dataset.runtimeRow)];
      const reason = wait() || (el.dataset.runtimeAct === "start" ? cannot(row) : row.problem);
      holdOff(el, !!reason, reason || "");
    }
    for (const button of root.querySelectorAll("[data-runtime-menu], [data-runtime-create]")) holdOff(button, !!wait(), wait());
    paintActions(root);
  }

  async function read(fresh = false) {
    if (!visible) return;
    if (reading) { pending = true; return; }
    const current = ++serial;
    reading = true;
    render();
    try {
      const query = new URLSearchParams();
      if (playground) query.set("playground", playground);
      if (fresh) query.set("refresh", "1");
      const path = "/api/runtimes";
      const result = await api(query.size ? path + "?" + query : path);
      if (current === serial && visible) { model = result; render(); }
    } catch (error) {
      if (current === serial && visible) setHTML($(".runtime-summary", root), `<p class="sub">${esc(error.message)}</p>`);
    } finally {
      reading = false;
      if (current === serial && visible) render();
      if (pending && visible) { pending = false; read(); }
    }
  }

  async function act(row, operation) {
    if (wait()) return;
    if (operation === "start" || operation === "rebuild") return independentAction(store.item("playground", row.id) || { id: row.id, name: row.name }, operation);
    runJob(() => api(`/api/runtimes/${row.id}/${operation}`, {}), { key: `playground:${row.id}`, verb: operation === "rebuild" ? "start" : operation });
  }

  async function cleanup(row, operation) {
    try {
      const path = `/api/runtimes/${row.id}/cleanup`;
      const plan = await api(path, { operation, check: true });
      const title = operation === "containers" ? "Remove containers" : operation === "delete" ? "Delete runtime" : "Delete runtime data";
      const rows = Object.entries(plan.removes).filter(([, values]) => values.length).map(([kind, values]) => ({ label: `Removes ${kind}`, items: values }));
      rows.push({ label: "Keeps", items: plan.keeps, note: true });
      if (!(await effectsDialog({ title: `${title}: ${row.name}`, options: [{ value: true, rows }], action: () => title, danger: () => operation !== "containers" }))) return;
      if (operation !== "containers" && !(await confirmDialog({ title: `${title}: ${row.name}`,
        text: "Deletes only this runtime’s resources listed in the preview. The source playground, shared platform and other runtimes stay.",
        action: title, word: "delete", danger: true }))) return;
      runJob(() => api(path, { operation, fingerprint: plan.fingerprint }), { key: `playground:${row.id}`, verb: "stop" });
    } catch (error) { toast(error.message, true); }
  }

  function details(row) {
    const rows = [{ label: "Containers", items: row.containers.map((c) => `${c.name}: ${c.state}`) },
      { label: "Ports", items: row.ports || [] },
      { label: "Volumes", items: row.volumes.map((v) => `${v.name}: ${size(v.bytes)}`) },
      { label: "Runtime files", items: row.folder ? [`${row.folder}: ${size(row.local_bytes)}`] : [] },
      { label: "Captured versions", items: Object.entries(row.versions || {}).map(([key, value]) => `${key}: ${value}`) },
      { label: "Captured images", items: (row.images || []).map((image) => `${image.service}: ${image.reference} (${image.id})`) },
      { label: "Prepared", items: row.prepared_at ? [new Date(row.prepared_at).toLocaleString()] : [] },
      { label: "Problem", items: row.problem ? [row.problem] : [] }];
    effectsDialog({ title: row.name, options: [{ value: true, rows: rows.map((row) => ({ ...row, note: true })) }], action: () => "Close" });
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.getAttribute("aria-disabled") === "true") return;
    if (button.hasAttribute("data-runtime-refresh")) return read(true);
    if (button.hasAttribute("data-runtime-create")) return createRuntime(store.item("playground", playground), current);
    const row = model?.runtimes[Number(button.closest("[data-runtime-row]")?.dataset.runtimeRow)];
    if (!row) return;
    if (button.hasAttribute("data-runtime-details")) return details(row);
    if (button.hasAttribute("data-runtime-act")) return act(row, button.dataset.runtimeAct);
    if (button.hasAttribute("data-runtime-menu")) {
      const item = source(row), blocked = wait();
      const choices = row.id ? [{ label: "Source playground", onSelect: () => router.go(`#/playgrounds/${row.playground || row.id}`),
        disabled: !item, reason: item ? "" : "Catalog source unavailable" }] : [
        { label: "Platform settings", disabled: !!blocked || !item, reason: blocked || "", onSelect: () => openSettings(item) }];
      if (row.id) choices.push(
        { label: "Restart", disabled: !!blocked || !row.running || !!cannot(row), reason: blocked || cannot(row) || "Start this runtime first", onSelect: () => act(row, "restart") },
        { label: "Rebuild from current settings", disabled: !!blocked || !row.prepared || !!cannot(row), reason: blocked || cannot(row) || "Start this runtime first", onSelect: () => act(row, "rebuild") },
        ...["containers", "data", "delete"].map((operation) => ({ label: operation === "containers" ? "Remove containers" : operation === "delete" ? "Delete runtime" : "Delete runtime data", danger: operation !== "containers",
          disabled: !!blocked || !(row.cleanup[operation] || { enabled: false }).enabled, reason: blocked || row.cleanup[operation]?.reason,
          onSelect: () => cleanup(row, operation) })));
      openMenu(button, choices);
    }
  });

  const stamp = (state) => JSON.stringify([state?.job?.id, state?.job?.done, state?.platform?.state?.key,
    (state?.playgrounds || []).map((p) => [p.id, p.runtime?.mode, p.on, p.state?.key])]);
  return {
    show() { visible = true; signature = stamp(store.state); read(); },
    hide() { visible = false; serial += 1; },
    render(state) {
      const next = stamp(state);
      if (visible && next !== signature && (!state.job || state.job.done)) { signature = next; read(); }
      render();
    },
  };
}
