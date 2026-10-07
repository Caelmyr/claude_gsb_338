/* Deterministic initial-state import for the scene config page.
 *
 * Lifecycle (kept in sync with config.js):
 *   - importLayout(text, fmt)  -> POST /parse: validates every row, stores the
 *     canonical layout + config sync, renders the exact step-0 preview via the
 *     same engine the run will use.
 *   - currentLayout()          -> the validated layout or null.
 *   - resetInitialState()      -> drop the layout when domain/model changes.
 *   - bindInitialState(getCfg, onSynced) wires textarea, file picker, sample
 *     template download, validation and slider-driven preview refreshes.
 *
 * The preview is rebuilt by the backend (not drawn from the pasted rows), so
 * what the user sees is literally the run's step-0 snapshot.
 */

let initLayout = null;        // canonical rows from the last successful import
let initWarnings = [];
let initCounts = null;
let _getConfig = null;        // registered by config.js

function currentLayout() { return initLayout; }

function registerInitialStateContext(getConfig, getDM) {
  _getConfig = getConfig;
  domainModelForInitialState = getDM;
}

function initEls() {
  return {
    fmt: el("isFormat"), file: el("isFile"), text: el("isText"),
    sample: el("isLoadSample"), download: el("isDownload"),
    clear: el("isClear"), validate: el("isValidate"),
    errs: el("isErrors"), warns: el("isWarnings"), preview: el("isPreview"),
    canvas: el("isCanvas"), legend: el("isLegend"), counts: el("isCounts"),
    status: el("isStatus"), summary: el("isSummary"), hint: el("isHint"),
  };
}

function resetInitialState() {
  initLayout = null;
  initWarnings = [];
  initCounts = null;
  const e = initEls();
  e.preview.style.display = "none";
  e.errs.style.display = "none";
  e.warns.style.display = "none";
  e.status.style.display = "none";
  e.summary.textContent = "";
  e.counts.textContent = "";
}

/* Surface scene-save validation failures that originate in the initial layout
 * inside the import section (returns true when it handled the error). */
function showInitialStateSaveErrors(err) {
  const details = err && err.details;
  if (!Array.isArray(details) || !details.some((d) => String(d).includes("初始态"))) {
    return false;
  }
  const layoutErrors = details.filter((d) => String(d).includes("初始态"))
    .map((d) => String(d).replace(/^初始态：/, "• "));
  showErrors(layoutErrors);
  initLayout = null;
  el("isPreview").style.display = "none";
  el("isStatus").style.display = "none";
  el("isSummary").textContent = "保存被拒绝：初始态未通过后端校验，请按下列问题修正后重新导入。";
  el("isPreview").scrollIntoView({ behavior: "smooth", block: "center" });
  return true;
}

function showErrors(es) {
  const e = initEls();
  if (es && es.length) {
    e.errs.textContent = `发现 ${es.length} 个问题，未生成初始态：\n` + es.map((m) => "• " + m).join("\n");
    e.errs.style.display = "block";
  } else {
    e.errs.style.display = "none";
    e.errs.textContent = "";
  }
}

function showWarnings(ws) {
  const e = initEls();
  if (ws && ws.length) {
    e.warns.textContent = ws.map((m) => "• " + m).join("\n");
    e.warns.style.display = "block";
  } else {
    e.warns.style.display = "none";
    e.warns.textContent = "";
  }
}

function countLabel(c) {
  if (!c) return "";
  const order = ["vehicles", "boids", "predators", "rabbits", "foxes",
                 "susceptible", "infected", "recovered"];
  const labels = { vehicles: "车辆", boids: "鸟", predators: "捕食者",
                   rabbits: "兔子", foxes: "狐狸",
                   susceptible: "易感 S", infected: "感染 I", recovered: "康复 R" };
  const parts = [`共 ${c.total} 个个体`];
  for (const k of order) {
    if (k in c && k !== "total") parts.push(`${labels[k]} ${c[k]}`);
  }
  if ("total_population" in c) parts.push(`栅格人口 ${c.total_population}`);
  return parts.join("　·　");
}

async function refreshInitialHint(domain, model) {
  const e = initEls();
  e.download.href = `/api/initial-state/template?domain=${domain}&model=${model}&format=${e.fmt.value}`;
  try {
    const meta = await get(`/api/initial-state/fields?domain=${domain}&model=${model}`);
    const cols = meta.fields.map((f) => `${f.key}${f.required ? "*" : ""}`).join(", ");
    e.hint.textContent = `${meta.hint}　字段（*为必填）：${cols}`;
  } catch (err) { /* ignore hint failure */ }
}

async function buildPreview(domain, model, config) {
  const e = initEls();
  const body = { domain, model, config, layout: initLayout };
  const res = await post("/api/initial-state/preview", body);
  renderSnapshot(e.canvas, res.snapshot, domain, model);
  renderLegend(e.legend, res.snapshot.palette || {});
  initCounts = res.counts;
  e.counts.textContent = countLabel(res.counts) +
    `　·　初始统计: ` + Object.entries(res.snapshot.stats || {})
      .map(([k, v]) => `${k}=${fmt(v)}`).join(", ");
  e.preview.style.display = "block";
}

async function importLayout(domain, model, config) {
  const e = initEls();
  const text = e.text.value;
  showErrors([]);
  e.status.style.display = "none";
  let parsed;
  try {
    parsed = await post("/api/initial-state/parse",
                        { domain, model, config, format: e.fmt.value, text });
  } catch (err) {
    showErrors([err.message]);
    return false;
  }
  if (!parsed.ok) {
    showErrors(parsed.errors || ["校验失败"]);
    initLayout = null;
    e.preview.style.display = "none";
    e.summary.textContent = "";
    return false;
  }
  initLayout = parsed.layout;
  initWarnings = parsed.warnings || [];
  initCounts = parsed.counts;
  // Sync count/density params to the form so saved config matches the layout.
  syncParamsFromLayout(parsed.config_updates);
  const syncedConfig = _getConfig
    ? { ..._getConfig(), ...(parsed.config_updates || {}) }
    : { ...config, ...(parsed.config_updates || {}) };
  try {
    await buildPreview(domain, model, syncedConfig);
  } catch (err) {
    // Preview re-validates server-side; surface its structured details.
    showErrors(["预览生成失败：" + err.message]);
    initLayout = null;
    e.preview.style.display = "none";
    return false;
  }
  showWarnings(initWarnings);
  e.summary.textContent = `✓ 已校验 ${initLayout.length} 条，初始态已确定`;
  e.status.textContent = "确定初始态";
  e.status.className = "badge finished";
  e.status.style.display = "";
  return true;
}

let _syncing = false;  // guard: programmatic param sync must not re-trigger preview

/* Update the parameter form's inputs for keys the layout overrides. */
function syncParamsFromLayout(updates) {
  _syncing = true;
  try {
    document.querySelectorAll("#fParams [data-key]").forEach((inp) => {
      const k = inp.dataset.key;
      if (!(k in updates)) return;
      const v = updates[k];
      if (inp.dataset.type === "bool") inp.checked = !!v;
      else if (inp.dataset.type === "choice") inp.value = v;
      else {
        inp.value = v;
        const lab = inp.parentElement && inp.parentElement.querySelector(".range-val");
        if (lab) lab.textContent = fmt(v);
      }
    });
  } finally {
    _syncing = false;
  }
}

async function loadSampleIntoBox(domain, model) {
  const e = initEls();
  const text = await fetch(
    `/api/initial-state/template?domain=${domain}&model=${model}&format=${e.fmt.value}`)
    .then((r) => r.text());
  e.text.value = text;
}

/* Wire all controls. Context is registered separately so validation can read
 * the latest form config even after slider edits. */
function bindInitialState() {
  const e = initEls();

  e.validate.onclick = async () => {
    const { domain, model } = domainModelForInitialState();
    await importLayout(domain, model, _getConfig ? _getConfig() : {});
  };

  e.file.onchange = () => {
    const f = e.file.files[0];
    if (!f) return;
    if (/\.json$/i.test(f.name)) e.fmt.value = "json";
    else if (/\.(csv|txt)$/i.test(f.name)) e.fmt.value = "csv";
    const reader = new FileReader();
    reader.onload = () => { e.text.value = String(reader.result || ""); };
    reader.readAsText(f, "utf-8");
  };

  e.sample.onclick = () => {
    const { domain, model } = domainModelForInitialState();
    loadSampleIntoBox(domain, model).catch((err) => showErrors([err.message]));
  };

  e.clear.onclick = () => {
    resetInitialState();
    e.text.value = "";
    e.file.value = "";
    e.summary.textContent = "已清除，初始个体将由引擎按参数随机生成（依赖随机种子）。";
  };

  e.fmt.onchange = () => {
    const { domain, model } = domainModelForInitialState();
    refreshInitialHint(domain, model);
  };
}

/* Provided by config.js via registerInitialStateContext. */
let domainModelForInitialState = () => ({ domain: "traffic", model: "ca" });

/* Re-render the preview after a config change (bounds may have changed).
 * Only fires while a validated layout exists. */
const schedulePreviewRefresh = debounce(async () => {
  if (_syncing || !initLayout || !_getConfig) return;
  const { domain, model } = domainModelForInitialState();
  // Bounds changes require re-validation (a moved wall may invalidate rows).
  try {
    const parsed = await post("/api/initial-state/parse",
      { domain, model, config: _getConfig(), format: "json",
        text: JSON.stringify({ individuals: initLayout }) });
    if (!parsed.ok) {
      showErrors(parsed.errors || ["参数变更后初始态不再合法"]);
      return;
    }
    showErrors([]);
    initLayout = parsed.layout;
    const synced = { ..._getConfig(), ...(parsed.config_updates || {}) };
    await buildPreview(domain, model, synced);
    showWarnings(parsed.warnings || initWarnings || []);
  } catch (err) { /* keep last preview */ }
}, 300);
