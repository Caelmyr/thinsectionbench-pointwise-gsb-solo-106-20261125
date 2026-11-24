const radians = angle => angle * Math.PI / 180;

export function sourcePoint(box, u, v) {
  const cosine = Math.cos(radians(box.angle));
  const sine = Math.sin(radians(box.angle));
  return {x: box.x + u * cosine - v * sine, y: box.y + u * sine + v * cosine};
}

export function boxCorners(box) {
  return [
    sourcePoint(box, 0, 0), sourcePoint(box, box.width, 0),
    sourcePoint(box, box.width, box.height), sourcePoint(box, 0, box.height)
  ];
}

export function pixelValue(number) {
  return Math.round(number * 100) / 100;
}

export class Scope {
  constructor(element, callbacks) {
    this.element = element;
    this.callbacks = callbacks;
    this.context = element.getContext("2d");
    this.image = null;
    this.doc = null;
    this.scale = 1;
    this.offset = {x: 0, y: 0};
    this.tool = null;
    this.anchor = null;
    this.pointer = null;
    this.drag = null;
    this.panKey = false;
    this.vertices = [];
    this.settings = {};
    this.sizeObserver = new ResizeObserver(() => this.resize());
    this.sizeObserver.observe(element);
    element.addEventListener("wheel", event => this.wheel(event), {passive: false});
    element.addEventListener("pointerdown", event => this.down(event));
    element.addEventListener("pointermove", event => this.move(event));
    element.addEventListener("pointerup", event => this.up(event));
    element.addEventListener("pointercancel", () => this.abort());
    element.addEventListener("pointerleave", () => {
      if (!this.drag) { this.pointer = null; this.paint(); }
    });
    element.addEventListener("contextmenu", event => event.preventDefault());
    element.addEventListener("keydown", event => this.keyDown(event));
    element.addEventListener("keyup", event => { if (event.code === "Space") this.panKey = false; });
    element.addEventListener("blur", () => { this.panKey = false; });
  }

  beginDocument(doc) {
    const same = this.doc?.id === doc.id;
    this.doc = doc;
    if (!same) {
      this.image = null;
      this.fit();
    }
    this.paint();
  }

  setTool(tool) {
    this.tool = tool;
    this.anchor = null;
    this.vertices = [];
    this.paint();
  }

  finishPolygon() {
    if (this.tool !== "grain" || this.vertices.length < 3) {
      this.callbacks.tip("至少选取三个轮廓顶点");
      return;
    }
    this.callbacks.action("grain", this.vertices.slice());
    this.setTool(null);
  }

  setSettings(settings) {
    this.settings = settings;
    this.paint();
  }

  setImage(url) {
    if (url === this.url) return;
    this.url = url;
    const image = new Image();
    image.onload = () => {
      if (url === this.url) {
        this.image = image;
        this.callbacks.image(true);
        this.paint();
      }
    };
    image.onerror = () => {
      if (url === this.url) {
        this.url = null;
        this.callbacks.image(false);
      }
    };
    image.src = url;
  }

  resize() {
    const bounds = this.element.getBoundingClientRect();
    const density = Math.max(1, window.devicePixelRatio || 1);
    this.w = bounds.width;
    this.h = bounds.height;
    this.element.width = Math.round(this.w * density);
    this.element.height = Math.round(this.h * density);
    this.context.setTransform(density, 0, 0, density, 0, 0);
    this.fit();
  }

  fit() {
    if (!this.doc || !this.w || !this.h) return;
    this.fitScale = Math.min(
      Math.max(1, this.w - 30) / this.doc.width,
      Math.max(1, this.h - 30) / this.doc.height
    );
    this.scale = this.fitScale;
    this.offset = {
      x: (this.w - this.doc.width * this.scale) / 2,
      y: (this.h - this.doc.height * this.scale) / 2
    };
    this.callbacks.zoom(100);
    this.paint();
  }

  screen(point) {
    return {x: this.offset.x + point.x * this.scale, y: this.offset.y + point.y * this.scale};
  }

  coordinates(event) {
    const bounds = this.element.getBoundingClientRect();
    return {x: event.clientX - bounds.left, y: event.clientY - bounds.top};
  }

  position(screen) {
    return {x: (screen.x - this.offset.x) / this.scale, y: (screen.y - this.offset.y) / this.scale};
  }

  drawable(point) {
    return point.x >= 0 && point.y >= 0 && point.x < this.doc.width && point.y < this.doc.height;
  }

  limit(point) {
    return {
      x: pixelValue(Math.min(this.doc.width - 0.02, Math.max(0, point.x))),
      y: pixelValue(Math.min(this.doc.height - 0.02, Math.max(0, point.y)))
    };
  }

  wheel(event) {
    if (!this.doc) return;
    event.preventDefault();
    const xy = this.coordinates(event);
    const point = this.position(xy);
    const newScale = this.scale * Math.exp(-event.deltaY * 0.0016);
    this.scale = Math.min(this.fitScale * 20, Math.max(this.fitScale * 0.5, newScale));
    this.offset = {x: xy.x - point.x * this.scale, y: xy.y - point.y * this.scale};
    this.callbacks.zoom(Math.round(100 * this.scale / this.fitScale));
    this.paint();
  }

  abort() {
    this.drag = null;
    this.anchor = null;
    this.paint();
  }

  down(event) {
    if (!this.doc) return;
    const screen = this.coordinates(event);
    if (event.button === 1 || event.button === 2 || this.panKey) {
      event.preventDefault();
      this.element.setPointerCapture(event.pointerId);
      this.drag = {
        kind: "pan",
        screen,
        offset: {...this.offset}
      };
      return;
    }
    if (event.button !== 0) return;
    const position = this.position(screen);
    if (!this.drawable(position)) return;
    this.element.focus({preventScroll: true});
    if (this.tool === "field" || this.tool === "defect") {
      this.drag = {kind: this.tool, screen, point: this.limit(position)};
      this.element.setPointerCapture(event.pointerId);
    }
  }

  move(event) {
    if (!this.doc) return;
    const screen = this.coordinates(event);
    if (this.drag?.kind === "pan") {
      this.offset = {
        x: this.drag.offset.x + screen.x - this.drag.screen.x,
        y: this.drag.offset.y + screen.y - this.drag.screen.y
      };
    }
    this.pointer = this.position(screen);
    this.callbacks.position(this.drawable(this.pointer) ? this.pointer : null);
    this.paint();
  }

  up(event) {
    if (!this.doc || event.button !== 0) {
      this.drag = null;
      return;
    }
    const position = this.position(this.coordinates(event));
    if (this.drag) {
      if (this.drag.kind !== "pan") {
        const first = this.drag.point;
        const second = this.limit(position);
        const box = this.dragBox(first, second);
        if (box.width >= 4 && box.height >= 4) {
          this.callbacks.action(this.drag.kind, box);
        }
      }
      this.drag = null;
      this.paint();
      return;
    }
    if (!this.drawable(position)) return;
    const xy = this.limit(position);
    if (this.tool === "grain") {
      this.vertices.push(xy);
      this.callbacks.tip(`已选择 ${this.vertices.length} 个轮廓顶点`);
    } else if (this.tool === "scale" || this.tool === "transect") {
      if (this.anchor) {
        this.callbacks.action(this.tool, {start: this.anchor, end: xy});
        this.anchor = null;
      } else {
        this.anchor = xy;
        this.callbacks.tip("在原图点击终点");
      }
    } else if (this.tool === "point" || this.tool === "pixel") {
      this.callbacks.action(this.tool, xy);
    }
    this.paint();
  }

  keyDown(event) {
    if (event.code === "Space") {
      event.preventDefault();
      this.panKey = true;
    } else if (event.key === "Escape") {
      this.setTool(null);
      this.callbacks.cancel();
    } else if (event.key === "Enter" && this.tool === "grain") {
      event.preventDefault();
      this.finishPolygon();
    }
  }

  dragBox(first, second) {
    return {
      x: Math.min(first.x, second.x), y: Math.min(first.y, second.y),
      width: Math.abs(first.x - second.x), height: Math.abs(first.y - second.y), angle: 0
    };
  }

  shape(points, color, fill, dashed = false) {
    if (!points.length) return;
    const ctx = this.context;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.35;
    if (dashed) ctx.setLineDash([6, 4]);
    ctx.beginPath();
    points.forEach((point, i) => {
      const xy = this.screen(point);
      if (i === 0) ctx.moveTo(xy.x, xy.y); else ctx.lineTo(xy.x, xy.y);
    });
    if (fill) {
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    }
    ctx.stroke();
    ctx.restore();
  }

  dot(point, radius, color, outline = "#101820", width = 1.3) {
    const xy = this.screen(point);
    const ctx = this.context;
    ctx.beginPath();
    ctx.arc(xy.x, xy.y, radius, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
    if (outline) {
      ctx.strokeStyle = outline;
      ctx.lineWidth = width;
      ctx.stroke();
    }
  }

  callout(point, text, ink = "#edf0e6") {
    const xy = this.screen(point);
    const ctx = this.context;
    ctx.save();
    ctx.font = "12px system-ui";
    const width = ctx.measureText(text).width;
    ctx.fillStyle = "rgba(12, 26, 33, .85)";
    ctx.fillRect(xy.x + 5, xy.y - 18, width + 10, 18);
    ctx.fillStyle = ink;
    ctx.fillText(text, xy.x + 10, xy.y - 5);
    ctx.restore();
  }

  paint() {
    const ctx = this.context;
    if (!this.w || !this.h) return;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.fillStyle = "#0b161d";
    ctx.fillRect(0, 0, this.w, this.h);
    if (!this.doc) return;
    if (this.image) {
      ctx.imageSmoothingEnabled = this.scale < 2.5;
      ctx.drawImage(this.image, this.offset.x, this.offset.y, this.doc.width * this.scale, this.doc.height * this.scale);
    }
    const state = this.doc.state;
    if (this.settings.field) {
      const xy = boxCorners(state.field);
      this.shape(xy, "#b9e5c9", "rgba(105, 180, 141, .10)");
      this.callout(xy[0], "视域");
      this.shape([xy[0], sourcePoint(state.field, Math.min(33, state.field.width), 0)], "#fff1b3");
      this.shape([xy[0], sourcePoint(state.field, 0, Math.min(33, state.field.height))], "#a5d6f6");
    }
    if (this.settings.defects) {
      for (const item of state.defects) {
        const corners = boxCorners(item.shape);
        this.shape(corners, "#efada6", "rgba(198, 83, 76, .20)", true);
        this.callout(corners[0], item.name || this.doc.defect_types[item.kind], "#f1b5a8");
      }
    }
    if (this.settings.grid) {
      for (const position of this.doc.guide.positions) {
        this.dot(position, 2.9, "#c4eac5", "#173130", 0.9);
      }
    }
    if (this.settings.points) {
      for (const record of state.points) {
        const color = this.doc.palette[record.mineral].color;
        this.dot(record, 5, color, "#101920");
      }
    }
    if (this.settings.measure) {
      const line = state.transect;
      this.shape([line.start, line.end], "#fcdf9b", null, true);
      this.dot(line.start, 3.8, "#fcdf9b");
      this.dot(line.end, 3.8, "#fcdf9b");
      if (state.scale) {
        this.shape([state.scale.line.start, state.scale.line.end], "#e6e7ee");
        this.callout(state.scale.line.start, `${state.scale.length_um} µm`);
      }
      for (const grain of state.grains) {
        const color = this.doc.palette[grain.mineral].color;
        this.shape(grain.vertices, color, color + "40");
        this.callout(grain.metrics.centroid, grain.name);
      }
    }
    if (this.drag?.point && this.pointer) {
      this.shape(boxCorners(this.dragBox(this.drag.point, this.limit(this.pointer))), "#fff0bb", "#ffffcc21", true);
    }
    if (this.anchor && this.pointer) this.shape([this.anchor, this.pointer], "#fff0bb", null, true);
    if (this.vertices.length) {
      this.shape(this.pointer ? [...this.vertices, this.pointer] : this.vertices, "#f5ce86", null, true);
      for (const point of this.vertices) this.dot(point, 3, "#fcdf9b");
    }
  }
}
