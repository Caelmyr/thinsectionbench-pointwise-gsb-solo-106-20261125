import {Scope, pixelValue} from "./scope.js";
import {analysisPanel, drawAnalysis, analysisTitle} from "./reports.js";

const $ = selector => document.querySelector(selector);
const one = (selector, root = document) => root.querySelector(selector);
const many = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));
const fmt = (value, digits = 1) => value == null ? "—" : new Intl.NumberFormat("zh-CN", {maximumFractionDigits: digits}).format(value);
const work = {
  document: null, tab: "survey", tool: null, mutate: false, timer: null,
  source: "primary", currentReport: null, checkpoints: [], sourceKey: 0, selectedPoint: null,
  overlays: {field: true, defects: true, grid: true, points: false, measure: false},
  scaleMode: null, draftRulers: [], fitReport: null
};

async function api(path, {method = "GET", data} = {}) {
  const options = {method, headers: {"Accept": "application/json"}};
  if (data instanceof FormData) {
    options.body = data;
  } else if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `工作台请求失败 (${response.status})`);
  return body;
}

function tell(message, error = false) {
  const node = $("#message");
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("error", error);
  clearTimeout(work.timer);
  if (message) work.timer = setTimeout(() => node.hidden = true, error ? 7000 : 3600);
}

function present(error) {
  tell(error.message || String(error), true);
}

function resource(path = "") {
  return `/api/studies/${work.document.id}${path}`;
}

function withRevision(path) {
  const joiner = path.includes("?") ? "&" : "?";
  return `${path}${joiner}revision=${work.document.revision}`;
}

function button(label, action, classes = "", identifier = "") {
  const attr = identifier ? ` data-id="${esc(identifier)}"` : "";
  return `<button type="button" class="${classes}" data-action="${action}"${attr}>${label}</button>`;
}

function field(label, name, value, step = "any", extras = "") {
  return `<label>${label}<input name="${name}" type="number" step="${step}" value="${esc(value)}" ${extras} required></label>`;
}

function options(values, value) {
  return Object.entries(values).map(([key, label]) =>
    `<option value="${esc(key)}"${key === value ? " selected" : ""}>${esc(label)}</option>`).join("");
}

function list(items, makeRow, fallback) {
  if (!items.length) return `<p class="muted">${fallback}</p>`;
  return `<div class="list">${items.map(makeRow).join("")}</div>`;
}

function card(title, content, classes = "") {
  return `<section class="card ${classes}"><h3>${title}</h3>${content}</section>`;
}

function submit(label) {
  return `<button class="primary" type="submit">${label}</button>`;
}

const hints = {
  field: "在原图拖出新视域，随后可以输入角度。",
  defect: "在原图拖出一个废区。",
  point: "在原图点击一个位置，以当前分类记录人工点。",
  transect: "在原图点击测线的起点和终点。",
  scale: "输入标尺实长，再在原图依次点击两端。",
  ruler: "每段标尺在原图依次点击起点和终点，再在面板填写实长。",
  grain: "在原图依次点击矿粒轮廓顶点，按 Enter 或使用完成按钮。",
  pixel: "在原图点击要核对的像素。"
};

const scope = new Scope($("#micrograph"), {
  tip(text) { $("#tool-hint").textContent = text; },
  cancel() { selectTool(null); },
  position(xy) { $("#cursor-pos").textContent = xy ? `X ${fmt(xy.x)}  Y ${fmt(xy.y)}` : "原图坐标"; },
  zoom(value) { $("#zoom-label").textContent = `${value}%`; },
  image(ready) {
    const message = $("#scope-message");
    message.textContent = ready ? "" : "无法显示当前图层，请检查视域和对照照片";
    message.hidden = ready;
  },
  action(kind, value) { scopeAction(kind, value).catch(present); }
});

function selectTool(tool) {
  work.tool = work.tool === tool ? null : tool;
  scope.setTool(work.tool);
  $("#tool-hint").textContent = hints[work.tool] || "滚轮缩放 · 空格拖移";
  many("button[data-tool]").forEach(node => node.setAttribute("aria-pressed", node.dataset.tool === work.tool));
}

function tool(label, kind) {
  return `<button type="button" data-tool="${kind}" aria-pressed="${work.tool === kind}">${label}</button>`;
}

function applySource() {
  if (!work.document) return;
  const current = $("#source-mode");
  const reference = work.document.assets.some(item => item.kind === "crosscheck");
  for (const option of current.options) option.disabled = option.value !== "primary" && !reference;
  if (!reference) work.source = "primary";
  current.value = work.source;
  $("#mix-control").hidden = work.source !== "mix";
  const mix = +$("#mix-amount").value;
  $("#mix-label").textContent = `${mix}%`;
  scope.setImage(withRevision(`${work.document.preview_url}?mode=${work.source}&mix=${mix / 100}`));
}

function showDocument(doc, replaceTab = false) {
  if (!work.document || work.document.id !== doc.id) {
    work.tab = "survey";
    work.source = "primary";
    work.checkpoints = [];
    work.selectedPoint = null;
    work.overlays = {field: true, defects: true, grid: true, points: false, measure: false};
    work.scaleMode = null;
    work.draftRulers = [];
    work.fitReport = null;
    selectTool(null);
  }
  $("#library").hidden = true;
  $("#workspace").hidden = false;
  $("#main-nav").hidden = false;
  $("#study-title").textContent = doc.title;
  $("#study-tag").textContent = doc.synthetic ? "合成练习" : doc.specimen || "岩片工作";
  $("#dimensions").textContent = `${doc.width} × ${doc.height} px`;
  $("#revision").textContent = `r${doc.revision}`;
  $("#top-status").innerHTML = "<i></i>工作已保存";
  work.document = doc;
  work.currentReport = null;
  $("#stage-output").hidden = true;
  scope.beginDocument({...doc, draftRulers: work.draftRulers});
  applySource();
  renderPanel();
  if (replaceTab) history.replaceState({}, "", `#study=${doc.id}`);
}

function tab(name) {
  if (!work.document) return;
  work.tab = name;
  work.currentReport = null;
  $("#stage-output").hidden = true;
  selectTool(null);
  renderPanel();
  if (name === "files") refreshCheckpoints().catch(present);
}

function renderPanel() {
  const doc = work.document;
  if (!doc) return;
  for (const item of many("#main-nav [data-tab]")) {
    if (item.dataset.tab === work.tab) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  const labels = {survey: "视域与载物台", count: "人工点数", measure: "标尺与矿粒", analysis: "图像分析", files: "工作与导出"};
  const content = {
    survey: surveyPanel, count: countPanel, measure: measurePanel, analysis: analysisPanel, files: filesPanel
  }[work.tab](doc, panelTools);
  $("#panel").innerHTML = `<div class="panel-header"><h2>${labels[work.tab]}</h2></div><div class="panel-content">${content}</div>`;
  scope.setSettings({
    field: work.overlays.field, defects: work.overlays.defects,
    grid: (work.tab === "survey" || work.tab === "count") && work.overlays.grid,
    points: work.tab === "count" || work.overlays.points,
    measure: work.tab === "measure" || work.overlays.measure
  });
  if (scope.doc) scope.doc.draftRulers = work.draftRulers;
  scope.paint();
}

function choice(label, name, values, current) {
  return `<label>${label}<select name="${name}">${options(values, current)}</select></label>`;
}

function coordinates(shape) {
  return `<div class="inline-group">
    ${field("起点 X", "x", pixelValue(shape.x))}
    ${field("起点 Y", "y", pixelValue(shape.y))}
    ${field("U 轴长度", "width", pixelValue(shape.width), "any", 'min="4"')}
    ${field("V 轴长度", "height", pixelValue(shape.height), "any", 'min="4"')}
  </div>${field("U 轴顺时针角度 °", "angle", shape.angle, "any", 'min="-180" max="180"')}`;
}

function overlayChecks(keys) {
  const labels = {field:"视域", defects:"废区", grid:"引导点", points:"人工点", measure:"量测"};
  return `<div class="actions">${keys.map(key => `<label><span><input type="checkbox" data-overlay="${key}" ${work.overlays[key] ? "checked" : ""}> ${labels[key]}</span></label>`).join("")}</div>`;
}

function surveyPanel(doc) {
  const state = doc.state;
  const fieldCard = card("观察视域", `<form class="card inner-form" data-form="field">
    ${coordinates(state.field)}
    <div class="actions">${submit("保存视域")}${tool("框出视域", "field")}${button("查看裁正图", "crop")}</div>
  </form>${overlayChecks(["field", "defects", "grid"])}`);
  const phase = {"early":"1/4 步", "late":"3/4 步"};
  const gridCard = card("载物台引导", `<form class="card inner-form" data-form="grid">
    <div class="inline-group three">
      ${field("步距 px", "step", state.grid.step, "any", 'min="4" max="2000"')}
      ${choice("U 轴停位", "phase_u", phase, state.grid.phase_u)}
      ${choice("V 轴停位", "phase_v", phase, state.grid.phase_v)}
    </div>
    <div class="actions">${submit("更新引导")}<span class="muted">${esc(doc.guide.message || `${doc.guide.count} 个引导点`)}</span></div>
  </form>`);
  const defectList = list(state.defects, item => `<div class="item">
    <span>${esc(item.name || doc.defect_types[item.kind])}<small>${fmt(item.shape.width)} × ${fmt(item.shape.height)} px · ${fmt(item.shape.angle)}°</small></span>
    <span class="actions">${button("编辑","edit-defect","low",item.id)}${button("移除","delete-defect","low",item.id)}</span>
  </div>`, "暂无废区");
  const wasteCard = card("装片废区", `<form class="card inner-form" data-form="defect" id="defect-form">
    <div class="inline-group">${choice("废区类别","kind",doc.defect_types,"bubble")}<label>说明<input name="name" placeholder="例如 封固气泡" maxlength="80"></label></div>
    <details id="defect-geometry"><summary>输入废区坐标</summary>${coordinates({x:20,y:20,width:24,height:24,angle:0})}</details>
    <div class="actions">${tool("框出废区","defect")}${submit("保存废区")}${button("面积摘要","report","","survey")}</div>
  </form>${defectList}`);
  const toneCard = card("图像显示", `<form class="card inner-form" data-form="tone">
    ${choice("观察模式","tone",{original:"原始颜色",levels:"视域色阶",mono:"灰阶",detail:"局部细节",heat:"亮度假色"},state.view.tone)}
    <div class="inline-group">
      ${field("暗端 %", "low", state.view.low, "1", 'min="0" max="49"')}
      ${field("亮端 %", "high", state.view.high, "1", 'min="51" max="100"')}
    </div>
    <div class="actions">${submit("应用显示")}${button("采样像素","pixel")}</div>
  </form>`);
  return [fieldCard,gridCard,wasteCard,toneCard].join("");
}

function countPanel(doc) {
  const values = Object.fromEntries(Object.entries(doc.palette).map(([key, entry]) => [key, entry.label]));
  const pointForm = card("添加或编辑点", `<form class="card inner-form" data-form="point" id="point-form">
    ${choice("判读类别","mineral",values,"quartz")}
    <label>判读说明<input name="note" maxlength="160"></label>
    <div class="inline-group">
      ${field("原图 X","x",Math.floor(doc.width / 2))}
      ${field("原图 Y","y",Math.floor(doc.height / 2))}
    </div>
    <div class="actions">${tool("在图上记点","point")}${submit("保存点")}${button("清空当前编辑","clear-point","low")}</div>
  </form>`);
  const stats = doc.tally.minerals.map(item => `<tr><td><i class="swatch" style="background:${esc(item.color)}"></i>${esc(item.label)}</td><td>${item.count}</td><td>${item.percent == null ? "—" : `${fmt(item.percent)}%`}</td></tr>`).join("");
  const tally = card("点数统计", `<p>手点共 <strong>${doc.tally.count}</strong> 个 · 可判 ${doc.tally.classified} · 无法判读 ${doc.tally.undetermined}</p>
    <table class="data-table"><thead><tr><th>类别</th><th>点数</th><th>占已记点</th></tr></thead><tbody>${stats}</tbody></table>`);
  const marks = list(doc.state.points, item => `<div class="item">
    <span><i class="swatch" style="background:${esc(doc.palette[item.mineral].color)}"></i>${esc(doc.palette[item.mineral].label)}<small>X ${fmt(item.x)} · Y ${fmt(item.y)} ${item.note ? `· ${esc(item.note)}` : ""}</small></span>
    <span class="actions">${button("编辑","edit-point","low",item.id)}${button("移除","delete-point","low",item.id)}</span>
  </div>`, "暂无手点记录");
  return pointForm + tally + card("记录", marks);
}

function lineFields(line) {
  return `<div class="inline-group">
    ${field("起点 X","x0",pixelValue(line.start.x))}
    ${field("起点 Y","y0",pixelValue(line.start.y))}
    ${field("终点 X","x1",pixelValue(line.end.x))}
    ${field("终点 Y","y1",pixelValue(line.end.y))}
  </div>`;
}

function scaleModeFor(doc) {
  if (work.scaleMode) return work.scaleMode;
  return doc.state.scale?.mode === "anisotropic" ? "multi" : "single";
}

function singleScaleCard(doc) {
  const saved = doc.state.scale;
  const line = saved && saved.mode !== "anisotropic" ? saved.line : doc.state.transect;
  return card("显微标尺（单标尺）", `<form class="card inner-form" data-form="scale" id="scale-form">
    ${field("实长 µm", "length_um", saved?.mode !== "anisotropic" ? (saved?.length_um || 100) : 100, "any", 'min="0.001"')}
    ${lineFields(line)}
    <div class="actions">${tool("在图上取两端","scale")}${submit("保存标尺")}${saved ? button("清除","reset-scale","low") : ""}</div>
    <p class="muted">${saved && saved.mode !== "anisotropic" ? `${fmt(saved.um_per_px, 4)} µm / px（横纵同值）` : "当前以原图像素计"}</p>
  </form>`);
}

function rulerRows(rulers) {
  if (!rulers.length) return `<p class="muted">还没有标尺段。用下方按钮在原图取两端，至少需要两段且方向能分开横纵。</p>`;
  return `<div class="list">${rulers.map((item, index) => `<div class="item">
    <span>标尺 ${index + 1}<small>ΔX ${fmt(Math.abs(item.line.end.x - item.line.start.x), 1)} · ΔY ${fmt(Math.abs(item.line.end.y - item.line.start.y), 1)} px</small></span>
    <span class="actions">
      <input name="ruler-length" data-index="${index}" type="number" step="any" min="0.001" value="${esc(item.length_um)}" aria-label="标尺 ${index + 1} 实长 µm">
      ${button("移除", "remove-ruler", "low", String(index))}
    </span>
  </div>`).join("")}</div>`;
}

function fitTable(fit, rulers) {
  if (!fit) return "";
  const rec = fit.recommendation;
  const head = `<table class="data-table"><thead><tr><th>方案</th><th>横 µm/px</th><th>纵 µm/px</th><th>落误差段数</th><th>长尺加权 RMS µm</th></tr></thead><tbody>`;
  const rows = fit.candidates.slice(0, 5).map((cand, i) => {
    const chosen = rec && cand === rec;
    return `<tr${chosen ? ' class="selected-row"' : ""}><td>${chosen ? "建议 " : ""}${cand.subset.map(k => k + 1).join("+")}</td>
      <td>${fmt(cand.um_per_px_x, 4)}</td><td>${fmt(cand.um_per_px_y, 4)}</td>
      <td>${cand.within_count} / ${rulers.length}</td><td>${fmt(cand.weighted_rms_um, 3)}</td></tr>`;
  }).join("");
  const iso = `<tr><td>单标尺（各向同性）</td><td colspan="2">${fmt(fit.isotropic.um_per_px, 4)}</td>
    <td>${fit.isotropic.within_count} / ${rulers.length}</td><td>${fmt(fit.isotropic.weighted_rms_um, 3)}</td></tr>`;
  const detail = rec ? `<div class="scroll-data"><table class="data-table"><thead><tr><th>标尺</th><th>实长 µm</th><th>预测 µm</th><th>偏差 µm</th><th>在误差内</th></tr></thead><tbody>
    ${rec.residuals.map((r, i) => `<tr><td>${i + 1}</td><td>${fmt(r.actual_um, 3)}</td><td>${fmt(r.predicted_um, 3)}</td>
      <td>${fmt(r.error_um, 3)}</td><td>${rec.within_tolerance[i] ? "是" : "否"}</td></tr>`).join("")}
  </tbody></table></div>` : "";
  return `<div class="fit-box ${fit.accepted ? "fit-ok" : "fit-bad"}">
    <p>${esc(fit.message)}</p>
    ${fit.full_rank ? "" : '<p class="muted">方向提示：当前标尺没有同时覆盖横向和纵向；正负镜像的两条斜线属于同一方向族。</p>'}
    <div class="scroll-data">${head}${rows}${iso}</tbody></table></div>${detail}
  </div>`;
}

function multiScaleCard(doc) {
  const saved = doc.state.scale?.mode === "anisotropic" ? doc.state.scale : null;
  const rulers = work.draftRulers;
  const fit = work.fitReport;
  return card("显微标尺（横纵分别标定）", `<form class="card inner-form" id="ruler-form">
    <div class="inline-group">
      ${field("可接受实长误差 µm", "tolerance_um", fit?.tolerance_um ?? 1, "any", 'min="0"')}
      ${field("新段默认实长 µm", "ruler_default_length", 100, "any", 'min="0.001"')}
    </div>
    ${rulerRows(rulers)}
    <div class="actions">
      ${tool("在图上取标尺两端", "ruler")}
      ${button("试算横纵比例", "fit-rulers")}
      ${fit?.accepted ? submit("采用建议并保存") : ""}
      ${saved ? button("恢复已保存标尺", "load-saved-rulers", "low") : ""}
      ${rulers.length ? button("清空草稿", "clear-rulers", "low") : ""}
      ${doc.state.scale ? button("清除全部标尺", "reset-scale", "low") : ""}
    </div>
    ${saved ? `<p class="muted">已保存：横 ${fmt(saved.um_per_px_x, 4)} · 纵 ${fmt(saved.um_per_px_y, 4)} µm/px · ${saved.rulers.length} 段 · ${saved.fit.within_count} 段在误差内</p>` : ""}
    ${fitTable(fit, rulers)}
  </form>`);
}

function measurePanel(doc) {
  const state = doc.state;
  const mode = scaleModeFor(doc);
  const rulerCard = mode === "multi" ? multiScaleCard(doc) : singleScaleCard(doc);
  const modeSwitch = `<div class="actions mode-switch">
    <button type="button" data-scale-mode="single" class="${mode === "single" ? "primary" : "low"}">单标尺模式</button>
    <button type="button" data-scale-mode="multi" class="${mode === "multi" ? "primary" : "low"}">横纵分别标定（2–4 段）</button>
  </div>`;
  const spacingField = state.scale ? `
    <label>微米采样间隔 µm（留空按像素间隔）<input name="spacing_um" type="number" step="any" min="0.001" placeholder="例如 5"></label>` : "";
  const transect = card("测线与剖面", `<form class="card inner-form" data-form="transect">
    ${lineFields(state.transect)}
    <div class="actions">${tool("在图上布线","transect")}${submit("保存测线")}</div>
    <p class="muted">${fmt(doc.ruler.length_px,2)} px · ${fmt(doc.ruler.length_um,2)} µm · ${fmt(doc.ruler.bearing_deg,2)}°</p>
  </form>
  <form class="card inner-form" data-form="report" data-operation="profile" id="quick-profile">
    ${spacingField}
    <div class="actions">${submit("亮度剖面")}</div>
  </form>`);
  const grains = list(state.grains, item => `<div class="item">
    <span>${esc(item.name)}<small>${esc(doc.palette[item.mineral].label)} · ${fmt(item.metrics.area_px2)} px² · 长短比 ${fmt(item.metrics.aspect_ratio,2)}</small></span>
    <span class="actions">${button("量测","show-grain","low",item.id)}${button("移除","delete-grain","low",item.id)}</span>
  </div>`, "暂无已圈矿粒");
  const shape = card("矿粒轮廓", `<form class="card inner-form" id="grain-form">
    <label>矿粒名称<input name="name" value="矿粒 ${state.grains.length+1}" maxlength="80"></label>
    ${choice("类别","mineral",Object.fromEntries(Object.entries(doc.palette).map(([k,v])=>[k,v.label])),"quartz")}
    <div class="actions">${tool("圈轮廓","grain")}${button("完成轮廓","finish-grain","primary")}${button("方向玫瑰图","rose")}</div>
  </form>${grains}`);
  return modeSwitch + rulerCard + transect + shape;
}

function filesPanel(doc) {
  const sources = list(doc.assets, item => `<div class="item">
    <span>${esc(item.name)}<small>${item.kind === "primary" ? "原图" : item.kind === "crosscheck" ? "偏光对照" : `载物台 ${item.angle}°`}</small></span>
    <span class="actions"><a class="button low" href="${esc(item.url)}" download>PNG</a>${item.kind === "primary" ? "" : button("移除","delete-source","low",item.id)}</span>
  </div>`, "暂无照片");
  const attachments = card("同视域照片", `${sources}<form class="card inner-form" data-form="source">
    ${choice("照片种类","kind",{crosscheck:"偏光对照",rotation:"有角度的偏光序列"},"crosscheck")}
    <div class="inline-group"><label>照片标签<input name="name" maxlength="95" required></label>${field("载物台角 °","angle",0,"any",'min="0" max="360"')}</div>
    <label>选择原图<input type="file" name="image" accept=".png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" required></label>
    <div class="actions">${submit("加入同视域照片")}</div>
  </form>`);
  const checkpointList = list(work.checkpoints, item => `<div class="item">
    <span>${esc(item.title)}<small>来源 r${item.revision} · ${esc(item.created.slice(0,10))}</small></span>
    ${button("恢复","restore-checkpoint","low",item.id)}
  </div>`, "暂无观察存档");
  const checkpoint = card("观察存档", `<form class="card inner-form" data-form="checkpoint">
    <label>存档名称<input name="name" maxlength="95" required></label>
    <div class="actions">${submit("保存当前标注")}</div>
  </form>${checkpointList}`);
  const metadata = card("工作信息", `<form class="card inner-form" data-form="metadata">
    <label>工作名称<input name="title" value="${esc(doc.title)}" maxlength="95" required></label>
    <label>样品编号<input name="specimen" value="${esc(doc.specimen)}" maxlength="80"></label>
    <label>岩片备注<textarea name="notes" maxlength="1800" rows="2">${esc(doc.notes)}</textarea></label>
    <div class="actions">${submit("保存工作信息")}</div>
  </form>`);
  const exportLinks = [["proof.png","图像校样"],["study.zip","完整观察包"],["points.csv","点数 CSV"],["grains.csv","矿粒 CSV"],["profile.csv","剖面 CSV"]];
  const downloads = card("导出当前版本", `<div class="actions">${exportLinks.map(([route,label]) => `<a class="button" href="${esc(withRevision(resource(`/export/${route}`)))}">${label}</a>`).join("")}</div>`);
  const preparation = card("照片准备", `<p>把交偏光原图复制到照片准备工作区，可在独立工程内校正或整理。</p>
    <div class="actions">${button("打开照片准备工作区","photo-preparation")}</div>`);
  return downloads + attachments + preparation + checkpoint + metadata;
}

const panelTools = {esc, fmt, card, field, options, button, choice, submit, list, tool, resource, withRevision};

function shapeFrom(data) {
  return Object.fromEntries(["x","y","width","height","angle"].map(key => [key, +data.get(key)]));
}

function lineFrom(data) {
  return {start:{x:+data.get("x0"),y:+data.get("y0")},end:{x:+data.get("x1"),y:+data.get("y1")}};
}

async function guarded(action) {
  if (work.mutate) return;
  work.mutate = true;
  try {
    await action();
  } finally {
    work.mutate = false;
  }
}

async function update(kind, value, id) {
  return guarded(async () => {
    const doc = await api(resource("/state"), {method:"POST",data:{revision:work.document.revision,kind,value,id}});
    showDocument(doc);
    tell("已保存");
  });
}

async function scopeAction(kind, value) {
  if (kind === "field" || kind === "transect") await update(kind, value);
  if (kind === "defect") {
    const data = new FormData($("#defect-form"));
    await update(kind, {kind:data.get("kind"), name:data.get("name"), shape:value}, $("#defect-form").dataset.id);
  }
  if (kind === "point") {
    const data = new FormData($("#point-form"));
    const chosen = data.get("mineral");
    await update(kind, {...value, mineral:chosen, note:data.get("note")});
    const form = $("#point-form");
    if (form) form.elements.mineral.value = chosen;
  }
  if (kind === "grain") {
    const data = new FormData($("#grain-form"));
    await update(kind, {name:data.get("name"), mineral:data.get("mineral"), vertices:value});
    selectTool(null);
  }
  if (kind === "scale") {
    const data = new FormData($("#scale-form"));
    await update(kind, {line:value,length_um:+data.get("length_um")});
  }
  if (kind === "ruler") {
    if (work.draftRulers.length >= 4) {
      tell("至多记录 4 段标尺");
      return;
    }
    const form = $("#ruler-form");
    const fallback = +form?.elements.ruler_default_length?.value || 100;
    work.draftRulers.push({line: value, length_um: fallback});
    work.fitReport = null;
    renderPanel();
  }
  if (kind === "pixel") await runReport("pixel", {...value,compare:work.source !== "primary"});
}

async function applyForm(form) {
  const kind = form.dataset.form;
  const data = new FormData(form);
  if (kind === "field") return update(kind, shapeFrom(data));
  if (kind === "grid") return update(kind,{step:+data.get("step"),phase_u:data.get("phase_u"),phase_v:data.get("phase_v")});
  if (kind === "tone") return update(kind,{tone:data.get("tone"),low:+data.get("low"),high:+data.get("high")});
  if (kind === "defect") return update(kind,{kind:data.get("kind"),name:data.get("name"),shape:shapeFrom(data)},form.dataset.id);
  if (kind === "point") return update(kind,{x:+data.get("x"),y:+data.get("y"),mineral:data.get("mineral"),note:data.get("note")},form.dataset.id);
  if (kind === "transect") return update(kind,lineFrom(data));
  if (kind === "scale") return update(kind,{line:lineFrom(data),length_um:+data.get("length_um")});
  if (kind === "report") {
    const params = {};
    for (const node of form.elements) {
      if (!node.name) continue;
      if (node.type === "number") {
        if (node.value === "") continue;
        params[node.name] = +node.value;
      } else {
        params[node.name] = node.type === "checkbox" ? node.checked : node.value;
      }
    }
    return runReport(form.dataset.operation, params);
  }
  if (kind === "source") {
    data.set("revision", work.document.revision);
    return guarded(async () => {
      showDocument(await api(resource("/sources"),{method:"POST",data}));
      tell("同视域照片已加入");
    });
  }
  if (kind === "metadata") {
    return guarded(async () => {
      showDocument(await api(resource(),{method:"PATCH",data:{...Object.fromEntries(data),revision:work.document.revision}}));
      tell("工作信息已保存");
    });
  }
  if (kind === "checkpoint") {
    return guarded(async () => {
      const result = await api(resource("/checkpoints"),{method:"POST",data:{name:data.get("name"),revision:work.document.revision}});
      work.checkpoints = result.checkpoints;
      renderPanel();
      tell("观察存档已保存");
    });
  }
}

async function refreshCheckpoints() {
  const studyId = work.document.id;
  const result = await api(resource("/checkpoints"));
  if (work.document?.id === studyId && work.tab === "files") {
    work.checkpoints = result.checkpoints;
    renderPanel();
  }
}

function populate(form, object) {
  form.dataset.id = object.id;
  const fields = {...object, ...(object.shape || {})};
  for (const name in fields) {
    if (form.elements[name]) form.elements[name].value = typeof fields[name] === "number" ? pixelValue(fields[name]) : fields[name];
  }
  form.querySelector("details")?.setAttribute("open","");
  form.scrollIntoView({block:"nearest"});
  tell("正在编辑已有记录");
}

function openCrop() {
  const url = withRevision(`${work.document.preview_url}?mode=crop`);
  $("#crop-image").src = url;
  $("#crop-download").href = url;
  $("#crop-download").download = "view-field.png";
  $("#crop-dialog").showModal();
}

function showReport(operation, result, preview) {
  const output = $("#stage-output");
  output.hidden = false;
  output.innerHTML = `<section class="card"><div class="output-head"><h2>${analysisTitle(operation)}</h2>${button("收起","close-output","low")}</div><div id="report-body"></div></section>`;
  drawAnalysis($("#report-body"), operation, result, preview, work.document, panelTools);
  work.currentReport = operation;
  if (operation === "pixel" || innerWidth > 850) return;
  output.scrollIntoView({block:"nearest"});
}

async function runReport(operation, params = {}) {
  const revision = work.document.revision;
  const id = work.document.id;
  const result = await api(withRevision(resource(`/analysis/${operation}`)),{method:"POST",data:params});
  if (work.document?.id === id && work.document.revision === revision) {
    showReport(operation, result.result, result.preview);
  }
}

async function applyAction(node) {
  const id = node.dataset.id;
  const action = node.dataset.action;
  if (action === "crop") return openCrop();
  if (action === "photo-preparation") {
    return guarded(async () => {
      const prepared = await api(withRevision(resource("/photo-preparation")), {method:"POST"});
      location.assign(prepared.url);
    });
  }
  if (action === "report") return runReport(id);
  if (action === "pixel") return selectTool("pixel");
  if (action === "finish-grain") return scope.finishPolygon();
  if (action === "reset-scale") return update("scale",null);
  if (action === "fit-rulers") return fitDraftRulers();
  if (action === "remove-ruler") {
    work.draftRulers.splice(+id, 1);
    work.fitReport = null;
    return renderPanel();
  }
  if (action === "clear-rulers") {
    work.draftRulers = [];
    work.fitReport = null;
    return renderPanel();
  }
  if (action === "load-saved-rulers") {
    const saved = work.document.state.scale;
    if (saved?.mode === "anisotropic") {
      work.draftRulers = saved.rulers.map(item => ({line: item.line, length_um: item.length_um}));
      work.fitReport = null;
      renderPanel();
      tell("已载入保存的标尺段");
    }
    return;
  }
  if (action === "close-output") { $("#stage-output").hidden = true; return; }
  if (action === "rose") return showReport("rose",work.document.rose);
  if (action === "show-grain") return showReport("grain",work.document.state.grains.find(item=>item.id===id));
  if (action === "clear-point") return renderPanel();
  if (action === "edit-defect") return populate($("#defect-form"),work.document.state.defects.find(item=>item.id===id));
  if (action === "edit-point") return populate($("#point-form"),work.document.state.points.find(item=>item.id===id));
  if (action === "delete-defect") return update("defect",null,id);
  if (action === "delete-point") return update("point",null,id);
  if (action === "delete-grain") return update("grain",null,id);
  if (action === "delete-source") {
    return guarded(async () => {
      showDocument(await api(resource(`/sources/${id}`),{method:"DELETE",data:{revision:work.document.revision}}));
      tell("照片已移除");
    });
  }
  if (action === "restore-checkpoint") {
    if (!confirm("用所选存档的标注覆盖当前标注？")) return;
    return guarded(async () => {
      showDocument(await api(resource("/checkpoints/restore"),{method:"POST",data:{id,revision:work.document.revision}}));
      tell("已恢复观察存档");
    });
  }
}

async function fitDraftRulers() {
  const form = $("#ruler-form");
  if (!form) return;
  const tolerance = +form.elements.tolerance_um.value;
  if (!work.draftRulers.length) { tell("请先在原图记录至少两段标尺"); return; }
  const payload = {rulers: work.draftRulers, tolerance_um: tolerance};
  const revision = work.document.revision;
  const id = work.document.id;
  const result = await api(withRevision(resource(`/analysis/calibration-fit`)), {method:"POST", data: payload});
  if (work.document?.id === id && work.document.revision === revision) {
    work.fitReport = result.result;
    renderPanel();
    tell(result.result.message);
  }
}

async function adoptCalibration(form) {
  if (!work.fitReport?.accepted) { tell(work.fitReport?.message || "请先试算并得到可用方案"); return; }
  await guarded(async () => {
    const doc = await api(resource("/state"), {method:"POST", data:{
      revision: work.document.revision,
      kind: "scale",
      value: {
        mode: "anisotropic",
        rulers: work.draftRulers,
        tolerance_um: work.fitReport.tolerance_um,
      },
    }});
    work.fitReport = null;
    showDocument(doc);
    tell("已保存横纵标定，测线实长和剖面已按新比例计算");
  });
}

async function refreshLibrary() {
  const result = await api("/api/studies");
  const root = $("#studies");
  root.innerHTML = result.studies.length ? result.studies.map(item =>
    `<button class="study-card" type="button" data-study="${esc(item.id)}"><strong>${esc(item.title)}</strong><small>${item.synthetic ? "合成练习" : esc(item.specimen || "原图")} · ${item.width} × ${item.height} px · r${item.revision}</small></button>`
  ).join("") : '<p class="muted">工作柜为空。导入一张原图或打开合成练习。</p>';
}

async function goHome() {
  work.document = null;
  selectTool(null);
  $("#workspace").hidden = true;
  $("#main-nav").hidden = true;
  $("#library").hidden = false;
  $("#top-status").innerHTML = "<i></i>本地工作柜";
  history.replaceState({},"",location.pathname);
  await refreshLibrary();
}

$("#workspace").addEventListener("click", event => {
  const button = event.target.closest("button[data-action],button[data-tool]");
  if (!button) return;
  if (button.dataset.tool) selectTool(button.dataset.tool);
  else applyAction(button).catch(present);
});
$("#panel").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.target;
  if (form.id === "ruler-form") return adoptCalibration(form).catch(present);
  applyForm(form).catch(present);
});
$("#panel").addEventListener("change", event => {
  const key = event.target.dataset.overlay;
  if (key) { work.overlays[key] = event.target.checked; renderPanel(); }
  if (event.target.name === "ruler-length") {
    const index = +event.target.dataset.index;
    const value = +event.target.value;
    if (Number.isFinite(value) && value > 0) {
      work.draftRulers[index].length_um = value;
      work.fitReport = null;
    }
  }
});
$("#panel").addEventListener("input", event => {
  if (event.target.name !== "ruler-length") return;
  const index = +event.target.dataset.index;
  const value = +event.target.value;
  if (Number.isFinite(value) && value > 0) work.draftRulers[index].length_um = value;
});
$("#panel").addEventListener("click", event => {
  const modeButton = event.target.closest("button[data-scale-mode]");
  if (modeButton) {
    work.scaleMode = modeButton.dataset.scaleMode;
    work.fitReport = null;
    if (work.scaleMode === "multi" && work.document.state.scale?.mode === "anisotropic" && !work.draftRulers.length) {
      work.draftRulers = work.document.state.scale.rulers.map(item => ({line: item.line, length_um: item.length_um}));
    }
    selectTool(null);
    renderPanel();
  }
});
$("#main-nav").addEventListener("click", event => {
  if (event.target.dataset.tab) tab(event.target.dataset.tab);
});
$("#source-mode").addEventListener("change", event => { work.source = event.target.value; applySource(); });
$("#mix-amount").addEventListener("input", applySource);
$("#reset-canvas").addEventListener("click", () => scope.fit());
$("#crop-close").addEventListener("click", () => $("#crop-dialog").close());
$("#show-upload").addEventListener("click", () => { $("#upload").hidden = false; $("#upload [name=title]").focus(); });
$("#cancel-upload").addEventListener("click", () => { $("#upload").hidden = true; });
$("#home").addEventListener("click", () => goHome().catch(present));
$("#practice").addEventListener("click", async event => {
  event.target.disabled = true;
  try { showDocument(await api("/api/studies/practice",{method:"POST"}),true); }
  catch (error) { present(error); }
  finally { event.target.disabled = false; }
});
$("#upload").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  const confirm = form.querySelector("[type=submit],button:not([type])");
  confirm.disabled = true;
  try {
    showDocument(await api("/api/studies",{method:"POST",data:new FormData(form)}),true);
    form.reset();
    form.hidden = true;
  } catch (error) { present(error); }
  finally { confirm.disabled = false; }
});
$("#studies").addEventListener("click", event => {
  const id = event.target.closest("[data-study]")?.dataset.study;
  if (id) api(`/api/studies/${id}`).then(doc => showDocument(doc,true)).catch(present);
});

const initial = new URLSearchParams(location.hash.slice(1)).get("study");
if (initial && /^[a-f0-9]{12,32}$/.test(initial)) {
  api(`/api/studies/${initial}`).then(doc => showDocument(doc,true)).catch(error => { present(error); goHome().catch(present); });
} else refreshLibrary().catch(present);
