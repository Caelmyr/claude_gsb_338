/* Scene config: list, create, edit scene definitions. */

let CATALOG = null;
let cur = null;   // currently edited scene dict (null = new)
let itvs = [];    // [{type, at_step, params}]

function domain() { return el("fDomain").value; }
function model() { return el("fModel").value; }

function paramInput(p, value) {
  const v = value != null ? value : p.default;
  if (p.type === "bool") {
    return `<div class="field"><label>${esc(p.label)}</label>
      <label class="check"><input type="checkbox" data-key="${esc(p.key)}" data-type="bool" ${v ? "checked" : ""}> 启用</label></div>`;
  }
  if (p.type === "choice") {
    return `<div class="field"><label>${esc(p.label)}</label>
      <select data-key="${esc(p.key)}" data-type="choice">${p.options.map((o) => `<option ${o == v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
  }
  const step = p.step || (p.type === "int" ? 1 : 0.05);
  return `<div class="field"><label>${esc(p.label)}</label>
    <div class="range-row">
      <input type="range" data-key="${esc(p.key)}" data-type="${p.type}" min="${p.min}" max="${p.max}" step="${step}" value="${v}">
      <span class="range-val">${fmt(v)}</span>
    </div></div>`;
}

function renderModels() {
  const dom = CATALOG[domain()];
  el("fModel").innerHTML = Object.entries(dom.models)
    .map(([k, m]) => `<option value="${k}">${esc(m.label)}</option>`).join("");
}

function renderParams() {
  const dom = CATALOG[domain()];
  const params = dom.models[model()].params;
  const cfg = (cur && cur.config) || {};
  el("fParams").innerHTML = params.map((p) => paramInput(p, cfg[p.key])).join("");
  el("fParams").querySelectorAll('input[type="range"]').forEach((r) => {
    const lab = r.parentElement.querySelector(".range-val");
    r.oninput = () => {
      lab.textContent = fmt(parseFloat(r.value));
      if (typeof schedulePreviewRefresh === "function") schedulePreviewRefresh();
    };
  });
  el("fParams").querySelectorAll("select[data-key],input[data-type='bool']").forEach((inp) => {
    inp.onchange = () => { if (typeof schedulePreviewRefresh === "function") schedulePreviewRefresh(); };
  });
}

function itvParamInputs(type, params) {
  const itv = CATALOG[domain()].interventions.find((i) => i.type === type);
  if (!itv || !itv.params.length) return '<span class="muted small">无参数</span>';
  return itv.params.map((p) => paramInput(p, (params || {})[p.key])).join("");
}

function renderInterventions() {
  const dom = CATALOG[domain()];
  const cont = el("fInterventions");
  cont.innerHTML = itvs.map((itv, i) => `
    <div class="card itv-entry" style="padding:12px" data-i="${i}">
      <div class="row between">
        <select class="itv-type" style="max-width:280px">${dom.interventions.map((opt) =>
          `<option value="${opt.type}" ${opt.type === itv.type ? "selected" : ""}>${esc(opt.label)}</option>`).join("")}</select>
        <div class="row gap-6">
          <label class="muted small">触发步</label>
          <input class="itv-step" type="number" min="0" value="${itv.at_step}" style="width:90px">
          <button class="btn danger small itv-del">删除</button>
        </div>
      </div>
      <div class="grid grid-2" style="margin-top:8px">${itvParamInputs(itv.type, itv.params)}</div>
    </div>`).join("") || '<p class="muted small">尚未安排干预。</p>';

  cont.querySelectorAll(".itv-type").forEach((s) => {
    s.onchange = () => {
      const i = +s.closest(".itv-entry").dataset.i;
      itvs[i] = { type: s.value, at_step: itvs[i].at_step, params: {} };
      renderInterventions();
    };
  });
  cont.querySelectorAll(".itv-step").forEach((s) => {
    s.oninput = () => { itvs[+s.closest(".itv-entry").dataset.i].at_step = parseInt(s.value || 0, 10); };
  });
  cont.querySelectorAll(".itv-del").forEach((b) => {
    b.onclick = () => { itvs.splice(+b.closest(".itv-entry").dataset.i, 1); renderInterventions(); };
  });
  cont.querySelectorAll('input[type="range"]').forEach((r) => {
    const lab = r.parentElement.querySelector(".range-val");
    r.oninput = () => { lab.textContent = fmt(parseFloat(r.value)); };
  });
}

function readInput(inp) {
  const t = inp.dataset.type;
  if (t === "bool") return inp.checked;
  if (t === "choice") return inp.value;
  if (t === "int") return parseInt(inp.value, 10);
  return parseFloat(inp.value);
}

function collectParams(root) {
  const out = {};
  root.querySelectorAll("[data-key]").forEach((inp) => { out[inp.dataset.key] = readInput(inp); });
  return out;
}

function collectInterventions() {
  const out = [];
  document.querySelectorAll(".itv-entry").forEach((e) => {
    out.push({
      type: e.querySelector(".itv-type").value,
      at_step: parseInt(e.querySelector(".itv-step").value || 0, 10),
      params: collectParams(e),
    });
  });
  return out;
}

async function reloadScenes() {
  const { scenes } = await get("/api/scenes");
  const list = el("sceneList");
  list.innerHTML = scenes.map((s) => `
    <div class="list-item" data-id="${esc(s.id)}">
      <div style="min-width:0">
        <div class="t">${esc(s.name)}</div>
        <div class="s">${DOMAIN_LABEL[s.domain] || s.domain} · ${MODEL_LABEL[s.model] || s.model}</div>
      </div>
      <button class="btn danger small del" data-id="${esc(s.id)}">删除</button>
    </div>`).join("") || '<p class="muted small">暂无场景。</p>';

  list.querySelectorAll(".list-item").forEach((li) => {
    li.onclick = (ev) => { if (ev.target.closest(".del")) return; editScene(li.dataset.id); };
  });
  list.querySelectorAll(".del").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("确认删除该场景？")) return;
      await del(`/api/scenes/${b.dataset.id}`);
      if (cur && cur.id === b.dataset.id) newScene();
      reloadScenes();
    };
  });
}

function editScene(id) {
  get(`/api/scenes/${id}`).then(async (s) => {
    cur = s;
    itvs = (s.interventions || []).map((i) => ({ type: i.type, at_step: i.at_step || 0, params: i.params || {} }));
    el("formTitle").textContent = "编辑场景 · " + s.name;
    el("fName").value = s.name;
    el("fDomain").value = s.domain;
    renderModels();
    el("fModel").value = s.model;
    el("fDesc").value = s.description;
    renderParams();
    renderInterventions();
    await hydrateInitialState(s);
    document.querySelectorAll(".list-item").forEach((li) =>
      li.classList.toggle("selected", li.dataset.id === id));
  });
}

/* Restore the deterministic initial-state section from a saved scene: fill the
 * text box with its JSON and rebuild the same preview the run will produce. */
async function hydrateInitialState(s) {
  const eEls = (typeof initEls === "function") ? initEls() : null;
  resetInitialState();
  await refreshInitialHint(s.domain, s.model);
  if (!eEls) return;
  if (s.initial_state && s.initial_state.length) {
    eEls.text.value = JSON.stringify({ individuals: s.initial_state }, null, 2);
    eEls.fmt.value = "json";
    const ok = await importLayout(s.domain, s.model, collectParams(el("fParams")));
    if (!ok) eEls.summary.textContent = "已保存的初始态在当前参数下未通过校验，请修正后重新导入。";
  } else {
    eEls.text.value = "";
    eEls.file.value = "";
  }
}

async function newScene() {
  cur = null;
  itvs = [];
  el("formTitle").textContent = "新建场景";
  el("fName").value = "";
  el("fDesc").value = "";
  el("fDomain").value = "traffic";
  renderModels();
  renderParams();
  renderInterventions();
  if (typeof resetInitialState === "function") resetInitialState();
  if (typeof refreshInitialHint === "function") refreshInitialHint(domain(), model());
  document.querySelectorAll(".list-item").forEach((li) => li.classList.remove("selected"));
}

function currentSceneObject() {
  return {
    id: cur ? cur.id : "",
    name: el("fName").value || "未命名场景",
    domain: domain(),
    model: model(),
    description: el("fDesc").value,
    config: collectParams(el("fParams")),
    interventions: collectInterventions(),
    initial_state: currentLayout(),
  };
}

async function saveScene() {
  const obj = currentSceneObject();
  try {
    const saved = cur ? await put(`/api/scenes/${cur.id}`, obj) : await post("/api/scenes", obj);
    cur = saved;
    el("formTitle").textContent = "编辑场景 · " + saved.name;
    el("formStatus").textContent = "已保存 ✓";
    setTimeout(() => el("formStatus").textContent = "", 2000);
    await reloadScenes();
    return saved;
  } catch (e) {
    if (typeof showInitialStateSaveErrors === "function"
        && showInitialStateSaveErrors(e)) return;
    alert("保存失败：" + e.message);
  }
}

async function init() {
  const { domains, order } = await get("/api/catalog");
  CATALOG = domains;
  el("fDomain").innerHTML = order.map((d) => `<option value="${d}">${esc(domains[d].label)}</option>`).join("");
  el("fDomain").onchange = () => {
    renderModels(); renderParams(); renderInterventions();
    if (typeof resetInitialState === "function") resetInitialState();
    if (typeof refreshInitialHint === "function") refreshInitialHint(domain(), model());
  };
  el("fModel").onchange = () => {
    renderParams();
    if (typeof resetInitialState === "function") resetInitialState();
    if (typeof refreshInitialHint === "function") refreshInitialHint(domain(), model());
  };
  el("newScene").onclick = newScene;
  el("addItv").onclick = () => { itvs.push({ type: CATALOG[domain()].interventions[0].type, at_step: 0, params: {} }); renderInterventions(); };
  el("saveScene").onclick = saveScene;
  el("runScene").onclick = async () => {
    const saved = await saveScene();
    if (!saved) return;
    try {
      const run = await post("/api/runs", { scene_id: saved.id });
      window.location.href = `/visualize.html?run=${run.id}`;
    } catch (e) { alert("创建运行失败：" + e.message); }
  };

  // Deterministic initial-state import section.
  if (typeof registerInitialStateContext === "function") {
    registerInitialStateContext(() => collectParams(el("fParams")),
                                () => ({ domain: domain(), model: model() }));
    bindInitialState();
    refreshInitialHint(domain(), model());
  }

  await reloadScenes();
  newScene();
  const qs = new URLSearchParams(window.location.search).get("scene");
  if (qs) editScene(qs);
}

init().catch((e) => showNotice(el("formStatus"), "初始化失败：" + e.message, "error"));
