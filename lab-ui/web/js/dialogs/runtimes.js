// Runtime defaults and per-playground choices are machine-local; switching never migrates or deletes data.
import { api, store } from "../core.js";
import { effectsDialog, planRows, toast } from "../components/ui.js";
import { runJob } from "../activity/jobs.js";

export async function runtimeSettings(item = null) {
  try {
    const settings = await api("/api/playground-runtimes");
    const current = item?.runtime?.mode || settings.default;
    const modes = [current, ...["independent", "shared"].filter((mode) => mode !== current)];
    const selected = await effectsDialog({ title: item ? `Runtime for ${item.name}` : "Default playground runtime",
      steps: [{ label: "Keeps", items: ["Existing data and catalog links"], note: true }],
      options: modes.map((mode) => ({ value: mode, label: mode === "independent" ? "Independent" : "Shared",
        rows: [{ label: mode === "independent" ? "Owns" : "Uses", items: [mode === "independent"
          ? "Its own platform, storage and ports" : "The Lab's shared platform"] }],
        sub: !item ? "Applies to newly assigned playgrounds" : mode !== current ? "Existing data stays in its current runtime" : "Current choice" })),
      action: () => "Save" });
    if (selected === null) return;
    await api("/api/playground-runtimes", item ? { id: item.id, mode: selected } : { default: selected });
    await store.refresh();
  } catch (error) { toast(error.message, true); }
}

export const runtimeEntry = (item) => {
  const reason = store.offline ? store.wait() : item.on ? "Stop this playground before changing its runtime" : "";
  return { label: "Runtime", disabled: !!reason, reason, onSelect: () => runtimeSettings(item) };
};

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
