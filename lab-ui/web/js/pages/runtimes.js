// Central instance management; Containers inspects one runtime at a time.
import { $ } from "../core.js";
import { runtimePanel } from "../components/runtimes.js";

let panel;
function init(root) {
  root.innerHTML = '<div class="page-head"><div class="page-title"><h1>Sandboxes</h1></div></div><div data-runtime-panel></div>';
  panel = runtimePanel($("[data-runtime-panel]", root), null, { heading: false });
}

export const runtimesPage = { id: "runtimes", pattern: /^#\/(?:sandboxes|runtimes)$/, nav: "runtimes", title: "Sandboxes", init,
  render: (state) => panel.render(state), show: () => panel.show(), hide: () => panel.hide() };
