// Named independent runtimes are created inside a playground. Normal playgrounds always use the shared platform.
import { $, api, store, router } from "../core.js";
import { effectsDialog, planRows, toast } from "../components/ui.js";
import { runJob } from "../activity/jobs.js";
import { field, formDialog } from "./form.js";

let creating = null;
const openCreate = formDialog("runtime-create", field("runtime-create-name", "Name",
  '<input id="runtime-create-name" maxlength="60" required>'),
  { key: "new:runtime", verb: "create", label: "Create runtime" },
  () => api(`/api/playgrounds/${creating.id}/runtimes`, { name: $("#runtime-create-name").value.trim() }),
  async (created) => { await store.refresh(); router.go(`#/playgrounds/${created.id}`); });

export function createRuntime(item) {
  creating = item;
  openCreate(`Create runtime for ${item.name}`, () => { $("#runtime-create-name").value = ""; });
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
