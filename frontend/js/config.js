/* Scene config: list, create, edit scene definitions. */

let CATALOG = null;
let cur = null;   // currently edited scene dict (null = new)
let itvs = [];    // [{type, at_step, params}]
let initState = null;        // validated, normalized individuals for config.initial_state
let initStateDirty = false;  // textarea edited since last successful validation

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
    r.oninput = () => { lab.textContent = fmt(parseFloat(r.value)); markInitStale(); };
  });
  el("fParams").querySelectorAll("select, input[type=checkbox]").forEach((inp) => {
    inp.onchange = () => markInitStale();
  });
}

/* The preview is rendered from the current params; once params change the
 * stored import may no longer match (bounds/capacity), so ask the user to
 * re-validate.  Saving re-validates on the backend regardless. */
function markInitStale() {
  if (initState) el("isHint").textContent = "参数已修改，请重新校验初始态";
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

/* ------------------------------------------------------------------ */
/* Initial-state import                                                */
/* ------------------------------------------------------------------ */
function isShowErrors(errors, warnings) {
  const box = el("isErrors");
  let html = "";
  if (errors && errors.length) {
    html += `<div class="notice error"><b>校验未通过（${errors.length} 处）：</b><ul style="margin:6px 0 0; padding-left:18px">` +
      errors.map((e) => `<li>${esc(e.message)}</li>`).join("") + "</ul></div>";
  }
  if (warnings && warnings.length) {
    html += `<div class="notice warn" style="margin-top:8px"><b>提示：</b><ul style="margin:6px 0 0; padding-left:18px">` +
      warnings.map((w) => `<li>${esc(w)}</li>`).join("") + "</ul></div>";
  }
  box.innerHTML = html;
}

function isSummaryText(summary) {
  const parts = [`共 ${summary.total} 条个体`];
  const states = Object.entries(summary.by_state || {}).map(([k, v]) => `${k}=${v}`).join("，");
  const types = Object.entries(summary.by_type || {}).map(([k, v]) => `${k}=${v}`).join("，");
  if (states) parts.push(`状态：${states}`);
  if (types) parts.push(`类型：${types}`);
  return parts.join(" · ");
}

function isShowPreview(resp) {
  el("isPreviewBox").style.display = "block";
  renderSnapshot(el("isPreview"), resp.snapshot, domain(), model());
  renderLegend(el("isLegend"), resp.snapshot.palette || {});
  el("isSummary").textContent = isSummaryText(resp.summary) + " · 预览与运行第 0 步完全一致";
}

function isHidePreview() {
  el("isPreviewBox").style.display = "none";
}

/* Validate the textarea content (or the stored import when the textarea is
 * empty) against the current domain/model/params.  Returns true when valid. */
async function validateImport(quiet = false) {
  let text = el("isText").value.trim();
  if (!text && initState) text = JSON.stringify(initState);
  if (!text) {
    if (!quiet) isShowErrors([{ message: "请先粘贴或选择要导入的 JSON / CSV 内容" }], []);
    return false;
  }
  const body = {
    domain: domain(), model: model(),
    config: collectParams(el("fParams")),
    format: el("isFormat").value,
    text,
  };
  let resp;
  try {
    resp = await post("/api/initial-state/validate", body);
  } catch (e) {
    isShowErrors([{ message: e.message }], []);
    return false;
  }
  isShowErrors(resp.errors, resp.warnings);
  if (resp.ok) {
    initState = resp.individuals;
    initStateDirty = false;
    isShowPreview(resp);
    el("isHint").textContent = "校验通过 ✓ 保存场景后生效";
  } else {
    // Keep any previous import: silently dropping it on save would lose user
    // data — the backend re-validates on save and reports exact errors.
    isHidePreview();
    el("isHint").textContent = "";
  }
  return resp.ok;
}

function clearImport() {
  initState = null;
  initStateDirty = false;
  el("isText").value = "";
  el("isErrors").innerHTML = "";
  el("isHint").textContent = "已清除，将使用随机初始化";
  isHidePreview();
}

async function downloadTemplate() {
  const fmt = el("isFormat").value === "csv" ? "csv" : "json";
  const { text } = await get(`/api/initial-state/template?domain=${domain()}&model=${model()}&format=${fmt}`);
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `initial_state_${domain()}_${model()}.${fmt}`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function toggleFields() {
  const box = el("isFieldsBox");
  if (box.style.display !== "none") { box.style.display = "none"; return; }
  const { fields } = await get(`/api/initial-state/template?domain=${domain()}&model=${model()}&format=json`);
  box.innerHTML = `<div class="notice"><b>字段说明（${DOMAIN_LABEL[domain()]} · ${MODEL_LABEL[model()]}）：</b>
    <table style="margin-top:6px"><thead><tr><th>字段</th><th>含义</th><th>类型</th><th>约束</th></tr></thead>
    <tbody>${fields.map((f) => `<tr><td class="mono">${esc(f.name)}</td><td>${esc(f.label)}</td><td>${esc(f.kind)}</td><td>${esc(f.desc)}</td></tr>`).join("")}</tbody></table></div>`;
  box.style.display = "block";
}

/* Load the stored import of an existing scene into the import panel and
 * re-validate it against the current params so the preview (and any drift
 * caused by later param edits) is shown immediately. */
function loadStoredImport() {
  el("isText").value = "";
  el("isErrors").innerHTML = "";
  el("isHint").textContent = "";
  initStateDirty = false;
  if (initState) {
    el("isHint").textContent = `已导入 ${initState.length} 条个体，正在按当前参数复核…`;
    validateImport(true).then((ok) => {
      if (ok) el("isHint").textContent = `已导入 ${initState.length} 条个体（复核通过 ✓）`;
    });
  } else {
    isHidePreview();
  }
}

function resetImportPanel() {
  initState = null;
  initStateDirty = false;
  el("isText").value = "";
  el("isErrors").innerHTML = "";
  el("isHint").textContent = "";
  el("isFieldsBox").style.display = "none";
  isHidePreview();
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
  get(`/api/scenes/${id}`).then((s) => {
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
    initState = (s.config && s.config.initial_state) || null;
    loadStoredImport();
    document.querySelectorAll(".list-item").forEach((li) =>
      li.classList.toggle("selected", li.dataset.id === id));
  });
}

function newScene() {
  cur = null;
  itvs = [];
  el("formTitle").textContent = "新建场景";
  el("fName").value = "";
  el("fDesc").value = "";
  el("fDomain").value = "traffic";
  renderModels();
  renderParams();
  renderInterventions();
  resetImportPanel();
  document.querySelectorAll(".list-item").forEach((li) => li.classList.remove("selected"));
}

function currentSceneObject() {
  const config = collectParams(el("fParams"));
  if (initState) config.initial_state = initState;
  return {
    id: cur ? cur.id : "",
    name: el("fName").value || "未命名场景",
    domain: domain(),
    model: model(),
    description: el("fDesc").value,
    config,
    interventions: collectInterventions(),
  };
}

async function saveScene() {
  // Unvalidated textarea content must pass validation before it can be saved.
  if (el("isText").value.trim() && initStateDirty) {
    const ok = await validateImport();
    if (!ok) {
      alert("初始态导入校验未通过，请根据错误列表修正后再保存。");
      return;
    }
  }
  const obj = currentSceneObject();
  try {
    const saved = cur ? await put(`/api/scenes/${cur.id}`, obj) : await post("/api/scenes", obj);
    cur = saved;
    el("formTitle").textContent = "编辑场景 · " + saved.name;
    el("formStatus").textContent = "已保存 ✓";
    setTimeout(() => el("formStatus").textContent = "", 2000);
    await reloadScenes();
    return saved;
  } catch (e) { alert("保存失败：" + e.message); }
}

async function init() {
  const { domains, order } = await get("/api/catalog");
  CATALOG = domains;
  el("fDomain").innerHTML = order.map((d) => `<option value="${d}">${esc(domains[d].label)}</option>`).join("");
  el("fDomain").onchange = () => { renderModels(); renderParams(); renderInterventions(); resetImportPanel(); };
  el("fModel").onchange = () => { renderParams(); resetImportPanel(); };
  el("newScene").onclick = newScene;
  el("addItv").onclick = () => { itvs.push({ type: CATALOG[domain()].interventions[0].type, at_step: 0, params: {} }); renderInterventions(); };
  el("saveScene").onclick = saveScene;
  el("isValidate").onclick = () => validateImport();
  el("isClear").onclick = clearImport;
  el("isTemplate").onclick = downloadTemplate;
  el("isFields").onclick = toggleFields;
  el("isText").oninput = () => { initStateDirty = true; };
  el("isFile").onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    el("isFormat").value = file.name.toLowerCase().endsWith(".csv") ? "csv" : "json";
    const reader = new FileReader();
    reader.onload = () => { el("isText").value = reader.result; initStateDirty = true; };
    reader.readAsText(file);
  };
  el("runScene").onclick = async () => {
    const saved = await saveScene();
    if (!saved) return;
    try {
      const run = await post("/api/runs", { scene_id: saved.id });
      window.location.href = `/visualize.html?run=${run.id}`;
    } catch (e) { alert("创建运行失败：" + e.message); }
  };
  await reloadScenes();
  newScene();
  const qs = new URLSearchParams(window.location.search).get("scene");
  if (qs) editScene(qs);
}

init().catch((e) => showNotice(el("formStatus"), "初始化失败：" + e.message, "error"));
