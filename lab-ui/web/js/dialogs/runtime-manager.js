// A playground's runtime inventory is opened on demand from More, without occupying its main view.
import { $, icon, store, router } from "../core.js";
import { runtimePanel } from "../components/runtimes.js";

const dialog = document.createElement("dialog");
dialog.className = "runtime-dialog";
dialog.setAttribute("aria-labelledby", "runtime-manager-title");
dialog.innerHTML = `<div class="dialog-head"><h2 id="runtime-manager-title"></h2>
  <button class="icon-btn" type="button" data-close aria-label="Close" data-tip="Close">${icon("close")}</button></div>
  <div class="runtime-dialog-body"></div>
  <div class="dialog-foot"><div class="dialog-actions"><button class="btn" type="button" data-close>Close</button></div></div>`;
document.body.append(dialog);
let panel = null;
dialog.addEventListener("click", (event) => { if (event.target.closest('[data-close], a[href^="#/"]')) dialog.close(); });
dialog.addEventListener("close", () => { panel?.hide(); });
store.subscribe((state) => { if (dialog.open && state) panel?.render(state); });
router.onLeave(() => { if (dialog.open) dialog.close(); });

function showRuntimes(item) {
  panel?.hide();
  const parent = item.runtime?.parent || item.id;
  $("#runtime-manager-title", dialog).textContent = `Runtimes for ${item.runtime?.parent_name || item.name}`;
  const body = $(".runtime-dialog-body", dialog);
  body.innerHTML = "<div></div>";   // Reopening retires the previous inventory's listeners and pending reads.
  panel = runtimePanel(body.firstElementChild, parent, { heading: false, current: item.runtime?.parent ? item.id : null });
  if (!dialog.open) dialog.showModal();
  panel.show();
}

export const runtimeManagerEntry = (item) => ({ label: "Runtimes", disabled: store.offline,
  reason: store.offline ? store.wait() : "", onSelect: () => showRuntimes(item) });
