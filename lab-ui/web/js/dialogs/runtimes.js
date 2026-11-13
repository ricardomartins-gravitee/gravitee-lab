// Named independent runtimes are created inside a playground. Normal playgrounds always use the shared platform.
import { $, esc, api, store, router } from "../core.js";
import { effectsDialog, planRows, toast } from "../components/ui.js";
import { runJob } from "../activity/jobs.js";
import { field, formDialog, note } from "./form.js";

let creating = null, choices = [];
const FRESH = "__fresh__";   // Catalog ids cannot contain underscores, so this cannot name a clone source.
const openCreate = formDialog("runtime-create", field("runtime-create-name", "Name",
  '<div class="search"><input id="runtime-create-name" maxlength="60" required></div>')
  + field("runtime-create-source", "Start from", '<select id="runtime-create-source" required></select>')
  + note("runtime-create-note"),
  { key: "new:runtime", verb: "create", label: "Create runtime" },
  () => {
    const source = $("#runtime-create-source").value;
    return api(`/api/playgrounds/${creating.id}/runtimes`, { name: $("#runtime-create-name").value.trim(),
      mode: source === FRESH ? "fresh" : "clone", ...(source !== FRESH ? { source } : {}) });
  },
  async (created) => {
    if (created.job) await runJob(() => Promise.resolve(created.job), { key: `playground:${created.id}`, verb: "create" });
    await store.refresh(); router.go(`#/playgrounds/${created.id}`);
  }, () => choices.find((row) => row.id === $("#runtime-create-source").value)?.reason || "");

function showCopyNote() {
  const source = choices.find((row) => row.id === $("#runtime-create-source").value), note = $("#runtime-create-note");
  note.hidden = !source;
  note.textContent = !source ? "" : source.private ? "Briefly pauses the source containers to copy their data. The copy stays stopped."
    : "Copies all Shared data, including other playgrounds. Briefly pauses Shared containers. The copy stays stopped.";
}
$("#runtime-create-source").addEventListener("input", showCopyNote);

export async function createRuntime(item, current = null) {
  try {
    const options = await api(`/api/playgrounds/${item.id}/runtimes`);
    creating = item;
    choices = options.sources || [];
    openCreate(`Create runtime for ${item.name}`, () => {
      $("#runtime-create-name").value = "";
      const select = $("#runtime-create-source");
      select.innerHTML = `<option value="${FRESH}">Fresh from playground</option>` + choices.map((row) =>
        `<option value="${esc(row.id)}">Clone ${esc(row.name)}${row.reason ? ` — ${esc(row.reason)}` : ""}</option>`).join("");
      select.value = current && choices.some((row) => row.id === current && !row.reason) ? current : FRESH;
      showCopyNote();
    });
  } catch (error) { toast(error.message, true); }
}

export async function independentAction(item, verb) {
  try {
    let rows;
    if (verb === "start" || verb === "rebuild") {
      const path = `/api/playgrounds/${item.id}/check`;
      const checks = await api(verb === "rebuild" ? `${path}?rebuild=1` : path);
      const blocked = checks.checks.find((row) => row.state === "warn");
      if (blocked) throw new Error(blocked.detail);
      rows = planRows(checks.dialog || {});
      rows.push({ label: "Keeps", items: ["All its data", "Other playgrounds and the shared platform"], note: true });
    } else {
      rows = [{ label: "Stops", items: ["Only this playground's platform and apps"] },
        { label: "Keeps", items: ["All its data", "Other playgrounds and the shared platform"], note: true }];
    }
    const confirmed = await effectsDialog({ title: `${verb === "rebuild" ? "Rebuild" : verb === "start" ? "Start" : "Stop"} ${item.name}`,
      options: [{ value: true, rows }], action: () => verb === "rebuild" ? "Rebuild runtime" : verb === "start" ? "Start" : "Stop" });
    if (!confirmed) return;
    const route = verb === "rebuild" ? "runtime/rebuild" : verb;
    runJob(() => api(`/api/playgrounds/${item.id}/${route}`, {}), { key: `playground:${item.id}`, verb: verb === "rebuild" ? "start" : verb });
  } catch (error) { toast(error.message, true); }
}

export const rebuildRuntimeEntry = (item) => ({ label: "Rebuild runtime from current settings",
  disabled: !!store.wait(true) || !item.runtime?.prepared, reason: store.wait(true) || (item.runtime?.prepared ? "" : "Start this playground first"),
  onSelect: () => independentAction(item, "rebuild") });
