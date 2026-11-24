const TITLES = {
  survey:"可用视域摘要", histogram:"视域直方图", profile:"测线亮度剖面",
  components:"阈值连通域", focus:"图像锐度分布", texture:"纹理方向",
  compare:"偏光对照差异", sequence:"旋转序列读数", lighting:"亮度均匀性",
  pixel:"原图像素", rose:"矿粒方向玫瑰图", grain:"矿粒量测",
  paired:"同视域偏光色差", "grain-optics":"矿粒内部读数", "edge-sweep":"测带边界读数"
};
const CHANNELS = {luminance:"亮度",red:"红色",green:"绿色",blue:"蓝色",saturation:"饱和度"};
const COLORS = {cream:"#e8c288", mint:"#a5ddbd", teal:"#79c6cc", zero:"#4b646e", dim:"#91a6a9"};

export function analysisTitle(operation) {
  return TITLES[operation] || "分析读数";
}

export function analysisPanel(doc, ui) {
  const {field, choice, submit, card} = ui;
  const channel = () => choice("通道","channel",CHANNELS,"luminance");
  const report = (name, inner, hint = "") => card(TITLES[name],
    `<form class="card inner-form" data-form="report" data-operation="${name}">
      ${inner}${hint ? `<p class="muted">${hint}</p>` : ""}
      <div class="actions">${submit("计算" + TITLES[name])}</div>
    </form>`);
  const histogram = report("histogram", `<div class="inline-group">${channel()}${field("箱数","bins",64,"1",'min="8" max="128"')}</div>`);
  const profile = report("profile", `${channel()}<div class="inline-group">
    ${field("采样间隔 px","spacing",1,"any",'min="0.25" max="20"')}${field("平滑半径","smoothing",0,"1",'min="0" max="30"')}
    </div><label><span><input type="checkbox" name="mask" checked> 应用视域与废区</span></label>`);
  const components = report("components", `${channel()}<div class="inline-group">
    ${field("阈值","threshold",88,"1",'min="0" max="255"')}${choice("选取","polarity",{dark:"低于等于阈值",bright:"高于等于阈值"},"dark")}
    ${field("最小区域 px²","minimum",20,"1",'min="1"')}${field("去噪半径","cleanup",1,"1",'min="0" max="5"')}
    </div><label>连通方式<select name="connectivity" data-numeric><option value="2">八邻接</option><option value="1">四邻接</option></select></label>`);
  const focus = report("focus", field("窗口 σ px","sigma",3,"any",'min="1" max="24"'),
    "以梯度能量观察相对清晰度。");
  const texture = report("texture", `<div class="inline-group">
    ${field("窗口 σ px","sigma",4,"any",'min="1" max="24"')}${field("最低一致性","coherence",0.45,"any",'min="0" max="1"')}
    </div>`, "仅使用满足一致性阈值的纹理；方向按 180° 周期合并。");
  const compare = report("compare","",doc.assets.some(item=>item.kind==="crosscheck") ? "对同位置的原始亮度进行比较。" : "请先在工作与导出加入同视域对照。");
  const hasReference = doc.assets.some(item => item.kind === "crosscheck");
  const paired = hasReference ? report("paired", `
    ${choice("读数","mode",{brightness:"亮度",color:"Lab 色差",chroma:"彩度变化"},"brightness")}
    <div class="inline-group">
      ${field("曝光下界","lower",2,"1",'min="0" max="254"')}
      ${field("曝光上界","upper",253,"1",'min="1" max="255"')}
    </div><div class="inline-group">
      ${field("变化阈值","limit",12,"any",'min="0" max="255"')}
      ${field("各轴分带数","bands",4,"1",'min="2" max="12"')}
    </div><label><span><input type="checkbox" name="signed"> 显示明暗变化方向</span></label>`) : "";
  const grainOptics = doc.state.grains.length ? report("grain-optics", `
    ${choice("矿粒","grain_id",Object.fromEntries(doc.state.grains.map(item => [item.id,item.name])),doc.state.grains.at(-1).id)}
    <div class="inline-group">${channel()}${field("分带数","rings",4,"1",'min="2" max="8"')}</div>
    ${hasReference ? '<label><span><input type="checkbox" name="compare"> 加入偏光对照</span></label>' : ""}`) : "";
  const edgeSweep = report("edge-sweep", `${channel()}<div class="inline-group">
    ${field("测带宽度 px","width",7,"1",'min="1" max="21"')}
    ${field("最小亮度变化","minimum",10,"any",'min="0.1" max="255"')}
    </div>${choice("变化方向","polarity",{both:"升降沿",rising:"升沿",falling:"降沿"},"both")}`,
    "沿当前测线做多道取样，废区处不连接信号。");
  const sequence = report("sequence", choice("测量范围","scope",{field:"可用视域",grain:"最近圈出的矿粒"},"field"),
    "记录每个已拍角度的亮度统计。");
  const lighting = report("lighting", field("边长分格数","tiles",4,"1",'min="2" max="10"'));
  return [histogram, profile, edgeSweep, components, focus, texture, compare, paired, sequence, grainOptics, lighting].join("");
}

function metrics(ui, entries) {
  return `<div class="metrics">${entries.map(([label,value,unit]) => `<div class="metric"><span class="muted">${label}</span><strong>${ui.esc(value)} <small>${unit||""}</small></strong></div>`).join("")}</div>`;
}

function grid(headers, rows, ui) {
  return `<div class="scroll-data"><table class="data-table"><thead><tr>${headers.map(value=>`<th>${ui.esc(value)}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(value=>`<td>${ui.esc(value)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function plotting(area) {
  const chart = area.querySelector("canvas");
  if (!chart) return null;
  const bounds = chart.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  chart.width = Math.max(1, Math.round(bounds.width * scale));
  chart.height = Math.max(1, Math.round(bounds.height * scale));
  const ctx = chart.getContext("2d");
  ctx.scale(scale, scale);
  return {ctx, w:bounds.width, h:bounds.height};
}

function cartesian(area, items, {xmax, ymax, kind, xlabel, xstart = 0}) {
  const data = plotting(area);
  if (!data) return;
  const {ctx,w,h} = data;
  const x = value => 39 + (w - 62) * (value - xstart) / Math.max(xmax - xstart,0.001);
  const y = value => h - 30 - value / Math.max(ymax,1) * (h - 51);
  ctx.font = "10px system-ui";
  for (let i=0;i<=4;i++) {
    const v = ymax * i / 4;
    ctx.fillStyle = COLORS.dim;
    ctx.fillText(String(Math.round(v*10)/10),5,y(v)+3);
    ctx.strokeStyle = COLORS.zero;
    ctx.beginPath();
    ctx.moveTo(x(xstart),y(v));
    ctx.lineTo(x(xmax),y(v));
    ctx.stroke();
  }
  ctx.fillStyle = COLORS.dim;
  ctx.fillText(String(Math.round(xstart)),x(xstart),h-14);
  ctx.textAlign = "right";
  ctx.fillText(`${Math.round(xmax*100)/100} ${xlabel||""}`,x(xmax),h-14);
  if (kind === "bars") {
    ctx.fillStyle = COLORS.mint;
    for (const item of items) {
      const bottom = y(0);
      const top = y(item.value);
      ctx.fillRect(x(item.from),top,Math.max(1,x(item.to)-x(item.from)-1),bottom-top);
    }
    return;
  }
  ctx.lineWidth = 2;
  ctx.strokeStyle = COLORS.mint;
  ctx.beginPath();
  let has = false;
  for (const item of items) {
    if (item.value == null) { has = false; continue; }
    if (has) ctx.lineTo(x(item.x),y(item.value));
    else ctx.moveTo(x(item.x),y(item.value));
    has = true;
  }
  ctx.stroke();
  if (items.length < 25) {
    ctx.fillStyle = COLORS.cream;
    for (const item of items.filter(item=>item.value!=null)) {
      ctx.beginPath();
      ctx.arc(x(item.x),y(item.value),3,0,2*Math.PI);
      ctx.fill();
    }
  }
}

function rose(area, bins, key) {
  const data = plotting(area);
  if (!data) return;
  const {ctx,w,h} = data;
  const polarity = Math.max(1, ...bins.map(item=>item[key]));
  const radius = Math.min(w/2-38,h/2-18);
  const midX = w / 2, midY = h / 2;
  ctx.strokeStyle = COLORS.zero;
  for (const scale of [0.25,0.50,0.75,1]) {
    ctx.beginPath(); ctx.arc(midX,midY,radius*scale,0,2*Math.PI); ctx.stroke();
  }
  ctx.fillStyle = COLORS.mint;
  for (const item of bins) {
    const left = item.from / 180 * Math.PI;
    const right = item.to / 180 * Math.PI;
    for (const offset of [0, Math.PI]) {
      ctx.beginPath(); ctx.moveTo(midX,midY);
      ctx.arc(midX,midY,radius * item[key] / polarity,left+offset,right+offset);
      ctx.closePath(); ctx.fill();
    }
  }
  ctx.font = "11px system-ui";
  ctx.fillStyle = COLORS.dim;
  ctx.fillText("0°",midX+radius+7,midY+4);
  ctx.fillText("90°",midX+5,midY+radius+10);
}

function numericPreview(preview, name) {
  return preview ? `<img class="analysis-preview" src="${preview}" alt="${name}">` : "";
}

export function drawAnalysis(area, operation, res, preview, doc, ui) {
  const f = ui.fmt;
  const plot = '<canvas class="plot" aria-label="数值图表"></canvas>';
  let content = "";
  let draw;
  if (operation === "survey") {
    content = metrics(ui,[["视域",f(res.field_pixels,0),"px²"],["可用",f(res.usable_pixels,0),"px²"],["废区并集",f(res.excluded_pixels,0),"px²"],["可用比例",f(res.usable_percent),"%"]]);
    content += grid(["废区类型","与视域重叠 px²"],res.defects.map(item=>[item.label,f(item.visible_pixels,0)]),ui);
  } else if (operation === "histogram") {
    const s = res.statistics;
    content = metrics(ui,[["参与像素",f(res.pixels,0)],["平均",f(s.mean)],["中位数",f(s.median)],["标准差",f(s.stddev)]]) + plot;
    draw = () => cartesian(area,res.histogram.map(item=>({...item,value:item.count})),{xmax:256,ymax:Math.max(...res.histogram.map(item=>item.count)),kind:"bars",xlabel:"色阶"});
  } else if (operation === "profile") {
    const physical = res.length_um != null;
    const samples = res.samples.map(item=>({x:physical?item.distance_um:item.distance_px,value:item.value}));
    content = metrics(ui,[["测线长度",f(res.length_px),"px"],["实长",f(res.length_um),"µm"],["可用采样",f(res.valid_samples,0)],["全部采样",f(res.samples.length,0)]]) + plot;
    const link = ui.withRevision(ui.resource("/export/profile.csv"));
    content += `<p><a href="${ui.esc(link)}">下载默认剖面 CSV</a></p>`;
    draw = () => cartesian(area,samples,{xmax:physical?res.length_um:res.length_px,ymax:255,xlabel:physical?"µm":"px"});
  } else if (operation === "components") {
    content = metrics(ui,[["连通块",f(res.component_count,0)],["纳入",f(res.selected_pixels,0),"px²"],["可用区占比",f(res.coverage_percent),"%"]]);
    content += numericPreview(preview,"阈值掩膜与可用区");
    content += grid(["连通块","面积 px²","中心 X,Y","接触边界"],res.components.map(item=>[
      item.label,f(item.area_px2,0),`${f(item.centroid.x)}, ${f(item.centroid.y)}`,item.on_edge?"是":"否"
    ]),ui);
  } else if (operation === "focus") {
    content = metrics(ui,[["清晰度中位数",f(res.gradient?.median,2)],["梯度 95 分位",f(res.gradient?.p95,2)],["参与像素",f(res.interpreted_pixels,0)]]);
    content += numericPreview(preview,"梯度能量热图");
  } else if (operation === "texture") {
    content = metrics(ui,[["主方向",f(res.preferred_deg),"°"],["方向强度",f(res.strength,2)],["有效像素",f(res.sample_count,0)],["有效占比",f(res.acceptance_percent),"%"]]);
    content += numericPreview(preview,"方向纹理图") + plot;
    draw = () => rose(area,res.bins,"weight");
  } else if (operation === "compare") {
    content = metrics(ui,[["平均绝对差",f(res.mean_absolute_difference,2)],["对照更亮",f(res.brighter_percent),"%"],["对照更暗",f(res.darker_percent),"%"],["相关",f(res.correlation,3)]]);
    content += grid(["图层","中位数","平均","最暗","最亮"],[["交偏原图",res.original],["对照图",res.reference],["差值",res.difference]].map(([label,s])=>[label,f(s.median),f(s.mean),f(s.min),f(s.max)]),ui);
  } else if (operation === "paired") {
    content = metrics(ui,[["参与比较",f(res.paired_pixels,0),"px²"],["曝光端点",f(res.clipped_pixels,0),"px²"],["明显变化",f(res.changed_percent),"%"],["差异中位数",f(res.magnitude.median,2)]]);
    content += numericPreview(preview,"同视域变化掩膜");
    content += grid(["轴向","区间 px","参与像素","变化占比"],Object.entries(res.field_bands).flatMap(([axis, bands]) =>
      bands.map(band=>[axis === "along_u" ? "U" : "V",`${f(band.from_px)}–${f(band.to_px)}`,band.comparable,f(band.changed_percent)])),ui);
  } else if (operation === "grain-optics") {
    const source = res.source_pixel;
    content = metrics(ui,[["矿粒",res.name],["参与统计",f(res.region.sampled_pixels,0),"px²"],
      ["轮廓内覆盖",f(res.region.coverage_percent),"%"],["代表像素 X, Y",source ? `${f(source.source_x)}, ${f(source.source_y)}` : "—"]]);
    content += numericPreview(preview,"矿粒边界和内部色带");
    content += grid(["由外至内","范围 px","参与像素","通道中位数"],res.shells.map(shell =>
      [shell.shell,`${f(shell.from_px)}–${f(shell.to_px)}`,shell.sampled_pixels,f(shell.stats?.median)]),ui);
  } else if (operation === "edge-sweep") {
    content = metrics(ui,[["测线长度",f(res.length_px),"px"],["有效采样",f(res.accepted_count,0)],["边界",f(res.edges_detected,0)]]);
    content += numericPreview(preview,"测带像素和废区");
    content += grid(["变化方向","原图 X, Y","沿测线距离 px","10%–90% 宽度 px"],res.edges.map(edge=>
      [edge.kind === "rising" ? "升沿" : "降沿",`${f(edge.x)}, ${f(edge.y)}`,f(edge.distance_px),f(edge.rise_px)]),ui);
  } else if (operation === "sequence") {
    content = metrics(ui,[["范围内像素",f(res.pixels_per_frame,0)],["照片数",res.values.length],["最暗实拍角",f(res.darkest_recorded),"°"],["实拍均值跨度",f(res.range,2)]]) + plot;
    content += grid(["照片","角度 °","均值","中位数"],res.values.map(item=>[item.name,f(item.angle),f(item.samples.mean),f(item.samples.median)]),ui);
    draw = () => cartesian(area,res.values.map(item=>({x:item.angle,value:item.samples.mean})),{xmax:Math.max(1,...res.values.map(item=>item.angle)),ymax:255,xlabel:"°"});
  } else if (operation === "lighting") {
    content = metrics(ui,[["参与比较格",f(res.comparable,0)],["格中位数跨度",f(res.spread,2)]]);
    content += grid(["行","列","占格比例","中位数"],res.tiles.map(item=>[item.row+1,item.column+1,`${f(item.coverage_percent)}%`,f(item.median)]),ui);
  } else if (operation === "grain") {
    const m = res.metrics;
    content = `<p>${ui.esc(res.name)} · ${ui.esc(doc.palette[res.mineral].label)}</p>`;
    content += metrics(ui,[["面积",f(m.area_px2,2),"px²"],["周长",f(m.perimeter_px,2),"px"],["最大 Feret",f(m.feret_max_px,2),"px"],["最小 Feret",f(m.feret_min_px,2),"px"],["长轴",f(m.long_axis_deg),"°"],["圆度",f(m.circularity,3)],["实面积",f(m.area_um2),"µm²"]]);
  } else if (operation === "rose") {
    content = metrics(ui,[["已圈矿粒",f(res.count,0)],["面积加权主方向",f(res.preferred_deg),"°"],["方向强度",f(res.strength,3)]]) + plot;
    draw = () => rose(area,res.bins,"area");
  } else if (operation === "pixel") {
    content = metrics(ui,[["X",res.pixel_x,"px"],["Y",res.pixel_y,"px"],["红",res.r],["绿",res.g],["蓝",res.b],["亮度",f(res.luminance,3)]]);
    if (res.comparison) content += grid(["对照 R","对照 G","对照 B","亮度差"],[[...res.comparison.reference,f(res.comparison.luminance_delta,2)]],ui);
  }
  area.innerHTML = content;
  if (draw) draw();
}
