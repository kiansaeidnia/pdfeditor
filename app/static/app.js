// pdfeditor web UI: one ES module, no build step. Every form is rendered from the schema in /api/tools.
const ORDER = ["Pages", "Convert", "Edit", "Security", "Inspect"];
const DPI = 110, PT = 72 / DPI;  // editor page pictures, and the size of one of their pixels in PDF points
let config = {name: "pdfeditor", email: false, max_upload_mb: 200};
let tools = [], uid = 0, resultUrl;

const main = document.getElementById("main"), search = document.getElementById("search");
const result = h("section", {class: "result", "aria-label": "Result", hidden: true});  // shared by every view

// --- helpers ------------------------------------------------------------------------------------

// h("a", {href: "#/", class: "x", onclick: fn}, ...children). Props become properties where the element
// has one and attributes otherwise; children are nodes or strings, and strings are always text, never markup.
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  el.append(...kids.flat(2).filter(k => k != null && k !== false && k !== ""));
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  return el;
}

const label = name => (name[0].toUpperCase() + name.slice(1)).replaceAll("_", " ");
const size = n => n < 1e6 ? `${Math.max(1, Math.round(n / 1e3))} kB` : `${(n / 1e6).toFixed(1)} MB`;
const byId = id => tools.find(t => t.id === id);
const statusEl = () => h("p", {class: "status", role: "status", "aria-live": "polite"});
const pwInput = () => h("input", {type: "password", autocomplete: "off"});
const pwField = input => field("Password of the PDF (if it has one)", input, "Results are always saved without a password.");
const say = text => { document.getElementById("announce").textContent = text; };  // for screen readers only

function tell(status, text, error = false) {
  status.className = error ? "status error" : "status";
  status.textContent = text;
}

const button = (text, onclick, props = {}) => h("button", {type: "button", onclick, ...props}, text);
const iconButton = (text, name, k, onclick, disabled) =>
  button(text, onclick, {class: "icon", title: name, "aria-label": name, "data-k": k, disabled});

async function api(path, body) {
  const res = await fetch(path, body && {method: "POST", body}).catch(() => { throw new Error("Can't reach the server."); });
  if (res.ok) return res;
  const detail = (await res.json().catch(() => ({}))).detail;
  if (Array.isArray(detail)) throw new Error(detail.map(d => `${d.loc?.at(-1) ?? "input"}: ${d.msg}`).join("\n"));
  throw new Error(typeof detail === "string" ? detail : `The server answered ${res.status} ${res.statusText}`);
}

async function download(res) {
  const m = /filename\*=UTF-8''([^;]+)/i.exec(res.headers.get("content-disposition") ?? "");
  return {name: m ? decodeURIComponent(m[1]) : "result", blob: await res.blob()};
}

// Multipart body from {field: value or [values]}. Empty values are left out so the server applies its defaults.
function formData(fields) {
  const fd = new FormData();
  let bytes = 0;
  for (const [k, v] of Object.entries(fields)) {
    for (const x of [v].flat()) {
      if (x == null || x === "") continue;
      bytes += x.size ?? 0;
      fd.append(k, x);
    }
  }
  if (bytes > config.max_upload_mb * 2 ** 20) throw new Error(`These files add up to more than ${config.max_upload_mb} MB, the most this server accepts.`);
  return fd;
}

// Run a job from a button: busy state while it runs, then its message or its error in the live region.
// A job that returns a Response has produced a file, which goes to the result panel.
async function busy(btn, status, job) {
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  tell(status, "Working…");
  try {
    const out = await job();
    if (!(out instanceof Response)) tell(status, out ?? "");
    else if (status.isConnected) {  // not connected: the user left this view, so the file is dropped
      showResult(await download(out));
      tell(status, "Done. The result is below.");
    }
  } catch (e) {
    tell(status, e.message, true);
  }
  btn.disabled = false;
  btn.removeAttribute("aria-busy");
}

// Lists are redrawn from scratch after every action; this puts keyboard focus back on the same control
// (found by its data-k key), or on the nearest usable button if that control is gone or disabled.
function keepFocus(root, render, fallback) {
  const k = root.contains(document.activeElement) && document.activeElement.dataset.k;
  render();
  if (!k) return;
  const el = root.querySelector(`[data-k="${k}"]`);
  (el && !el.disabled ? el : (el?.parentElement ?? root).querySelector("button:enabled") ?? fallback)?.focus();
}

// --- form controls ------------------------------------------------------------------------------

function control(p, value) {
  const number = p.type === "integer" || p.type === "number";
  let el;
  if (p.enum) el = h("select", {}, p.enum.map(v => h("option", {value: v}, String(v))));
  else if (p.type === "boolean") return h("input", {type: "checkbox", checked: value === true || value === "true"});
  else if (p.format === "textarea") el = h("textarea", {rows: 4});
  else if (p.format === "color") el = h("input", {type: "color"});
  else if (p.format === "password") el = h("input", {type: "password", autocomplete: "new-password"});
  else if (number) el = h("input", {type: "number", min: p.minimum ?? p.exclusiveMinimum, max: p.maximum, step: p.type === "integer" ? 1 : "any"});
  else el = h("input", {type: "text", pattern: p.pattern});
  if (value != null) el.value = value;
  return el;
}

function field(text, input, hint, required) {
  input.id ||= `f${++uid}`;
  const note = hint && h("small", {id: `${input.id}h`}, hint);
  if (note) input.setAttribute("aria-describedby", note.id);
  if (input.type === "checkbox") return h("div", {class: "field check"}, input, h("label", {for: input.id}, text), note);
  input.required = !!required;
  return h("div", {class: "field"},
    h("label", {for: input.id}, text, required && h("span", {class: "req", title: "required", "aria-hidden": "true"}, " *")), input, note);
}

// The controls for a tool's settings, and values() to read them back: as form strings, or with json = true
// as a pipeline step wants them (real booleans and numbers). Empty fields are left out.
function paramFields(t, given = {}) {
  const inputs = t.params.map(p => control(p, given?.[p.name] ?? p.default));
  const read = (p, el) => {
    if (p.type === "boolean") return json => json ? el.checked : String(el.checked);
    return json => el.value === "" ? undefined : json && p.type !== "string" ? Number(el.value) : el.value;
  };
  const readers = t.params.map((p, i) => read(p, inputs[i]));
  return {
    el: h("div", {class: "fields"}, t.params.map((p, i) => field(label(p.name), inputs[i], p.description, p.required))),
    values: json => Object.fromEntries(t.params.map((p, i) => [p.name, readers[i](json)]).filter(([, v]) => v !== undefined)),
  };
}

// File chooser with drag and drop and an ordered list of what was chosen
function filePicker({accept = "", multiple = true, hint = "", onchange} = {}) {
  let items = [];  // {k: key for keepFocus, f: File}
  const input = h("input", {type: "file", class: "sr", id: `f${++uid}`, accept, multiple, onchange: () => { add(input.files); input.value = ""; }});
  const list = h("ul", {class: "files", "aria-label": "Chosen files"});
  const add = files => {
    const fresh = [...files].map(f => ({k: ++uid, f}));
    if (!fresh.length) return;
    items = multiple ? [...items, ...fresh] : fresh.slice(0, 1);
    draw();
    onchange?.();
  };
  const draw = () => keepFocus(list, () => list.replaceChildren(...items.map((it, i) => h("li", {},
    h("span", {}, it.f.name), h("small", {}, size(it.f.size)),
    multiple && i > 0 && iconButton("↑", `Move ${it.f.name} up`, `${it.k}up`, () => { items.splice(i - 1, 2, it, items[i - 1]); draw(); }),
    iconButton("×", `Remove ${it.f.name}`, `${it.k}rm`, () => { items.splice(i, 1); draw(); onchange?.(); })))), input);
  const drop = h("div", {class: "drop",
    ondragover: e => { e.preventDefault(); drop.classList.add("over"); },
    ondragleave: () => drop.classList.remove("over"),
    ondrop: e => { e.preventDefault(); drop.classList.remove("over"); add(e.dataTransfer.files); }},
    h("label", {for: input.id}, multiple ? "Choose files or drop them here" : "Choose a PDF or drop it here"), input,
    h("small", {}, `${hint} Up to ${config.max_upload_mb} MB.`.trim()));
  return {el: h("div", {class: "picker"}, drop, list), get files() { return items.map(it => it.f); }};
}

// --- result panel -------------------------------------------------------------------------------

function showResult(r) {
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  resultUrl = null;
  result.hidden = !r;
  result.replaceChildren();
  if (!r) return;
  const {name, blob} = r, type = blob.type, url = resultUrl = URL.createObjectURL(blob);
  const pre = h("pre", {tabIndex: 0});  // focusable so a long preview can be scrolled from the keyboard
  result.append(h("h2", {}, "Result"),
    h("p", {}, h("a", {class: "button", href: url, download: name}, `Download ${name}`), ` ${size(blob.size)}`));
  if (type.startsWith("application/json")) {
    blob.text().then(t => { try { t = JSON.stringify(JSON.parse(t), null, 2); } catch { /* show it as it came */ } pre.textContent = t; });
    result.append(pre);
  } else if (type.startsWith("text/html")) {
    const frame = h("iframe", {title: `Preview of ${name}`, src: url});
    frame.setAttribute("sandbox", "");  // empty sandbox: no scripts, no forms, no access to this page
    result.append(frame);
  } else if (type.startsWith("application/pdf")) {
    result.append(h("iframe", {title: `Preview of ${name}`, src: url}));
  } else if (type.startsWith("image/")) {
    result.append(h("img", {src: url, alt: `Preview of ${name}`}));
  } else if (type.startsWith("text/")) {
    blob.slice(0, 100_000).text().then(t => { pre.textContent = t; });
    result.append(pre);
  }
  if (config.email) {
    const to = h("input", {type: "email", autocomplete: "email"}), status = statusEl(), send = h("button", {}, "Send");
    result.append(h("form", {class: "row", onsubmit: e => {
      e.preventDefault();
      busy(send, status, async () => {
        await api("/api/email", formData({file: new File([blob], name, {type}), to: to.value}));
        return `Sent to ${to.value}.`;
      });
    }}, field("E-mail this file to", to, "", true), send), status);
  }
  result.scrollIntoView({block: "nearest"});
}

// --- views --------------------------------------------------------------------------------------

function home() {
  const q = search.value.trim().toLowerCase();
  const found = tools.filter(t => t.category !== "Internal" && `${t.title} ${t.description} ${t.id}`.toLowerCase().includes(q));
  const first = d => /^.*?[.!?](?=\s+[A-Z"]|$)/.exec(d)?.[0] ?? d;  // first sentence; "e.g. 2" does not end one
  const sections = [...new Set([...ORDER, ...found.map(t => t.category)])].map(c => [c, found.filter(t => t.category === c)])
    .filter(([, list]) => list.length).map(([c, list]) => h("section", {}, h("h2", {}, c), h("ul", {class: "cards"},
      list.map(t => h("li", {}, h("a", {href: `#/tool/${t.id}`}, h("strong", {}, t.title), h("span", {}, first(t.description.replace(/\s+/g, " ")))))))));
  if (q) say(`${found.length} tools match.`);
  return h("div", {}, h("h1", {class: "sr"}, "Tools"), sections.length ? sections : h("p", {}, `No tools match “${search.value.trim()}”.`));
}

function toolView(id) {
  const t = byId(id);
  if (!t || t.category === "Internal") return h("div", {}, h("h1", {}, "No such tool"), h("p", {}, h("a", {href: "#/"}, "Back to all tools")));
  const single = t.input === "pdf" || t.input === "file";
  const picker = t.input && filePicker({accept: t.accept, hint: single
    ? "Several files are processed one by one and come back as a zip."
    : "Several files are used together, in the order listed: use the arrows to reorder."});
  const extras = t.files.map(x => h("input", {type: "file", accept: x.accept, multiple: x.kind === "files"}));
  const fields = paramFields(t), status = statusEl(), run = h("button", {class: "primary"}, "Run");
  const pw = (t.input.startsWith("pdf") || t.files.some(x => x.kind === "pdf")) && pwInput();
  return h("form", {class: "panel", onsubmit: e => {
    e.preventDefault();
    busy(run, status, async () => {
      if (picker && !picker.files.length) throw new Error("Choose at least one file.");
      const more = Object.fromEntries(t.files.map((x, i) => [x.name, [...extras[i].files]]));
      return api(`/api/tools/${t.id}`, formData({files: picker ? picker.files : [], ...fields.values(), ...more, password: pw ? pw.value : ""}));
    });
  }},
    h("h1", {}, t.title), h("p", {}, t.description.replace(/\s+/g, " ")), picker && picker.el,
    t.files.map((x, i) => field(label(x.name), extras[i], x.kind === "files" ? "You can choose several files." : "", x.required)),
    fields.el, pw && pwField(pw), h("div", {class: "row"}, run), status);
}

// "Choose one PDF" form of the organizer and the editor. open(file, password) loads it and returns a message;
// it is also called with no file when the chosen one is removed.
function pdfOpener(open) {
  const pw = pwInput(), status = statusEl(), go = h("button", {}, "Open");
  const picker = filePicker({accept: ".pdf", multiple: false, onchange: () => busy(go, status, () => open(picker.files[0], pw.value))});
  return h("form", {class: "panel", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      if (!picker.files.length) throw new Error("Choose a PDF first.");
      return open(picker.files[0], pw.value);
    });
  }}, picker.el, pwField(pw), h("div", {class: "row"}, go), status);
}

function organizer() {
  let file, password, thumbs = [], order = [], from = null;  // order: [{k: key, p: original page index}]
  const turn = new Map();  // original page index -> pending clockwise turn; copies of a page share it
  const grid = h("ol", {class: "pages", "aria-label": "Pages"}), status = statusEl();
  const apply = button("Apply and download", () => busy(apply, status, async () => {
    const steps = [90, 180, 270].map(angle => [angle, [...turn].filter(([, a]) => a === angle).map(([p]) => p + 1)])
      .filter(([, pages]) => pages.length).map(([angle, pages]) => ({tool: "rotate", params: {angle: String(angle), pages: pages.join(",")}}));
    steps.push({tool: "organize", params: {pages: order.map(it => it.p + 1).join(",")}});
    return api("/api/pipeline", formData({files: file, steps: JSON.stringify(steps), password}));
  }).then(draw), {class: "primary"});

  function draw() {
    keepFocus(grid, () => grid.replaceChildren(...order.map((it, i) => {
      const n = it.p + 1, angle = turn.get(it.p) ?? 0;
      const act = (text, name, change, off) => iconButton(text, `${name}: page ${n}, position ${i + 1}`, it.k + name, () => {
        change();
        draw();
        say(order.includes(it) ? `Page ${n} is at position ${order.indexOf(it) + 1} of ${order.length}, turned ${turn.get(it.p) ?? 0} degrees.` : `Page ${n} deleted.`);
      }, off);
      const move = to => order.splice(to, 0, ...order.splice(i, 1));
      return h("li", {draggable: true,
        ondragstart: e => { from = i; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(n)); },
        ondragover: e => e.preventDefault(),
        ondrop: e => { e.preventDefault(); if (from !== null) order.splice(i, 0, ...order.splice(from, 1)); from = null; draw(); }},
        h("div", {class: "thumb"}, h("img", {class: `r${angle}`, src: thumbs[it.p].image, alt: `Page ${n}${angle ? `, turned ${angle} degrees` : ""}`})),
        h("span", {}, `Page ${n}`),
        h("div", {class: "row"},
          act("←", "Move earlier", () => move(i - 1), i === 0),
          act("→", "Move later", () => move(i + 1), i === order.length - 1),
          act("↻", "Rotate 90 degrees clockwise", () => turn.set(it.p, (angle + 90) % 360)),
          act("+", "Duplicate", () => order.splice(i + 1, 0, {k: ++uid, p: it.p})),
          act("×", "Delete", () => order.splice(i, 1))));
    })));
    apply.hidden = !thumbs.length;
    apply.disabled = !order.length;
  }

  const opener = pdfOpener(async (f, pw) => {
    [file, password, thumbs, order] = [f, pw, [], []];
    turn.clear();
    draw();
    if (!f) return "";
    const data = await (await api("/api/tools/preview", formData({files: f, dpi: 40, password: pw}))).json();
    if (file !== f) return "";  // another file was chosen while this one was loading
    thumbs = data.pages;
    order = thumbs.map((_, p) => ({k: ++uid, p}));
    draw();
    return `${data.count} pages. Use the buttons under each page, or drag pages, then apply.`;
  });
  draw();
  return h("div", {}, h("h1", {}, "Organize pages"), opener, grid, h("div", {class: "row"}, apply), status);
}

// The server keeps a form field under 1 MB and the editor sends pictures inside one, so a chosen image is made
// small first: at most 1000 pixels on its longer side, as PNG, or as JPEG on white if the PNG is still large.
async function shrink(file) {
  const bmp = await createImageBitmap(file), k = Math.min(1, 1000 / Math.max(bmp.width, bmp.height));
  const c = h("canvas", {width: Math.round(bmp.width * k), height: Math.round(bmp.height * k)}), g = c.getContext("2d");
  g.drawImage(bmp, 0, 0, c.width, c.height);
  const png = c.toDataURL("image/png");
  if (png.length < 250_000) return png;
  g.globalCompositeOperation = "destination-over";
  g.fillStyle = "#ffffff";
  g.fillRect(0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.8);
}

function editor() {
  const MODES = ["Text", "Draw", "Highlight", "Whiteout", "Redact", "Image", "Signature"];
  const TIPS = {Text: "Click where the text should start.", Draw: "Drag to draw.", Highlight: "Drag a rectangle to highlight it.",
    Whiteout: "Drag a rectangle to paint it white.", Redact: "Drag a rectangle: what is under it is removed for good.",
    Image: "Choose an image, then drag a rectangle where it should go.",
    Signature: "Draw your signature, press Use, then drag a rectangle where it should go."};
  let file, password, count = 0, page = 0, shown = null, live = null, start, mode = "Text", inked = false;
  const ops = [], cache = new Map(), pics = new Map(), stamps = {};  // stamps: the picture waiting to be placed, per mode
  const prefs = {Draw: ["#000000", 2], Highlight: ["#ffe600", 14]};  // colour and size last used in each mode
  const status = statusEl(), where = h("span", {});
  const cv = h("canvas", {class: "page", "aria-label": "The page being edited. Edits are placed with a mouse, pen or finger."}), ctx = cv.getContext("2d");
  const color = h("input", {type: "color", value: "#000000"});
  const amount = h("input", {type: "number", min: 0.5, max: 500, step: 0.5, value: 14});
  const sized = max => Math.min(+amount.value > 0 ? +amount.value : 2, max);

  const pic = url => pics.get(url) ?? pics.set(url, Object.assign(new Image(), {onload: paint, src: url})).get(url);
  // shortcut: every page visited stays in memory as a JPEG data URL; evict old ones if very long PDFs get edited
  function pageImage(n) {
    if (!cache.has(n)) {
      cache.set(n, api("/api/tools/preview", formData({files: file, dpi: DPI, pages: n + 1, password})).then(r => r.json())
        .then(async d => { const img = new Image(); img.src = d.pages[0].image; await img.decode(); return img; })
        .catch(e => { cache.delete(n); throw e; }));
    }
    return cache.get(n);
  }

  // Redraw the page picture and every pending edit on it, the way the server will draw them
  function paint() {
    if (!shown) return;
    ctx.setTransform(1 / PT, 0, 0, 1 / PT, 0, 0);  // from here on the canvas is drawn on in PDF points
    ctx.drawImage(shown, 0, 0, cv.width * PT, cv.height * PT);
    const mine = ops.filter(o => o.page === page);
    // the server applies redactions before everything else, so they go first here too
    for (const o of [...mine.filter(o => o.type === "redact"), ...mine.filter(o => o.type !== "redact"), ...(live ? [live] : [])]) {
      ctx.globalAlpha = o.type === "highlight" ? 0.35 : 1;
      ctx.fillStyle = ctx.strokeStyle = o.type === "whiteout" ? "#ffffff" : o.color ?? "#000000";
      if (o.type === "text") {
        ctx.font = `${o.size}px Helvetica, Arial, sans-serif`;
        ctx.fillText(o.text, o.points[0][0], o.points[0][1] + o.size * 0.85);  // the server's baseline: 0.85 of the size below the top
      } else if (o.type === "ink") {
        ctx.lineWidth = o.width;
        ctx.lineCap = ctx.lineJoin = "round";
        ctx.beginPath();
        for (const [x, y] of o.points) ctx.lineTo(x, y);
        ctx.stroke();
      } else {
        const [x0, y0, x1, y1] = o.rect, w = x1 - x0, hgt = y1 - y0;
        if (o.type !== "image") ctx.fillRect(x0, y0, w, hgt);
        else {  // fitted inside the box, aspect kept, centred
          const im = pic(o.image), k = Math.min(w / im.naturalWidth, hgt / im.naturalHeight);
          if (im.naturalWidth) ctx.drawImage(im, x0 + (w - im.naturalWidth * k) / 2, y0 + (hgt - im.naturalHeight * k) / 2, im.naturalWidth * k, im.naturalHeight * k);
        }
      }
    }
    ctx.globalAlpha = 1;
  }

  async function show(n) {
    page = n;
    shown = null;  // no edits until the new page is on screen
    where.textContent = `Page ${n + 1} of ${count}`;
    prev.disabled = n === 0;
    next.disabled = n >= count - 1;
    tell(status, "Loading the page…");
    const f = file;
    try {
      const img = await pageImage(n);
      if (page !== n || file !== f) return;  // the user moved on while this page was loading
      shown = img;
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      paint();
      tell(status, TIPS[mode]);
    } catch (e) {
      tell(status, e.message, true);
    }
  }

  function setMode(m) {
    prefs[mode] = [color.value, amount.value];
    mode = m;
    [color.value, amount.value] = prefs[m] ?? ["#000000", 14];
    modeButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(MODES[i] === m)));
    imagePanel.hidden = m !== "Image";
    signPanel.hidden = m !== "Signature";
    tell(status, TIPS[m]);
  }

  // pointer position in PDF points: CSS pixels -> canvas pixels (canvas.width / client width) -> points (72 / dpi)
  function at(e) {
    const box = cv.getBoundingClientRect(), k = cv.width / cv.clientWidth * PT;
    const fit = (v, max) => Math.round(Math.max(0, Math.min(v * k, max * PT)) * 10) / 10;
    return [fit(e.clientX - box.left, cv.width), fit(e.clientY - box.top, cv.height)];
  }
  cv.addEventListener("pointerdown", e => {
    if (!shown || e.button) return;
    const p = at(e), base = {page, color: color.value};
    if (mode === "Text") {
      const text = prompt("Text to add here");
      if (text?.trim()) ops.push({type: "text", ...base, points: [p], text, size: sized(500)});
      return paint();
    }
    if (mode === "Draw") live = {type: "ink", ...base, points: [p], width: sized(100)};
    else if (mode !== "Image" && mode !== "Signature") live = {type: mode.toLowerCase(), ...base, rect: [...p, ...p]};
    else if (stamps[mode]) live = {type: "image", page, rect: [...p, ...p], image: stamps[mode]};
    else return tell(status, TIPS[mode], true);
    start = p;
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener("pointermove", e => {
    if (!live) return;
    const p = at(e);
    if (live.type === "ink") live.points.push(p);
    else live.rect = [Math.min(start[0], p[0]), Math.min(start[1], p[1]), Math.max(start[0], p[0]), Math.max(start[1], p[1])];
    paint();
  });
  const finish = e => {
    const o = live;
    live = null;
    if (!o) return;
    if (o.type === "ink" && o.points.length === 1) o.points.push([o.points[0][0] + 0.1, o.points[0][1]]);  // a tap is a dot
    if (e.type === "pointerup" && (o.type === "ink" || (o.rect[2] - o.rect[0] > 2 && o.rect[3] - o.rect[1] > 2))) ops.push(o);
    paint();
  };
  cv.addEventListener("pointerup", finish);
  cv.addEventListener("pointercancel", finish);

  const pad = h("canvas", {class: "pad", width: 500, height: 180, "aria-label": "Signature pad. Draw with a mouse, pen or finger."}), ink = pad.getContext("2d");
  const stroke = e => {
    const box = pad.getBoundingClientRect(), k = pad.width / pad.clientWidth;
    ink.lineTo((e.clientX - box.left) * k, (e.clientY - box.top) * k);
    ink.stroke();
  };
  pad.addEventListener("pointerdown", e => {
    pad.setPointerCapture(e.pointerId);
    ink.beginPath();
    ink.strokeStyle = color.value;
    ink.lineWidth = 3;
    ink.lineCap = ink.lineJoin = "round";
    inked = true;
    stroke(e);
  });
  pad.addEventListener("pointermove", e => { if (pad.hasPointerCapture(e.pointerId)) stroke(e); });
  const signPanel = h("div", {class: "panel", hidden: true}, pad, h("div", {class: "row"},
    button("Clear", () => { ink.clearRect(0, 0, pad.width, pad.height); inked = false; delete stamps.Signature; }),
    button("Use", () => {
      if (!inked) return tell(status, "Draw your signature first.", true);
      stamps.Signature = pad.toDataURL("image/png");  // the pad is never filled, so the PNG is transparent
      tell(status, "Now drag a rectangle on the page where the signature should go.");
    })));
  const chooser = h("input", {type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", onchange: async () => {
    if (!chooser.files[0]) return;
    try {
      stamps.Image = await shrink(chooser.files[0]);
      tell(status, "Now drag a rectangle on the page where the image should go.");
    } catch {
      delete stamps.Image;
      tell(status, "That image can't be read.", true);
    }
  }});
  const imagePanel = h("div", {class: "panel", hidden: true}, field("Image to place", chooser));

  const modeButtons = MODES.map(m => button(m, () => setMode(m), {"aria-pressed": String(m === mode)}));
  const prev = button("Previous", () => show(page - 1)), next = button("Next", () => show(page + 1));
  const undo = button("Undo", () => {
    const o = ops.pop();
    if (!o) return tell(status, "Nothing to undo.");
    if (o.page === page) paint(); else show(o.page);
  });
  const save = button("Save PDF", () => busy(save, status, async () => {
    const body = JSON.stringify(ops);
    if (!ops.length) throw new Error("Nothing to save yet: add an edit first.");
    if (body.length > 1_000_000) throw new Error("These edits are too large to send in one go (the pictures add up to more than 1 MB). Undo one, save, and add the rest to the saved file.");
    return api("/api/tools/edit", formData({files: file, ops: body, password}));
  }), {class: "primary"});
  const work = h("div", {hidden: true},
    h("div", {class: "toolbar"}, h("div", {class: "row", role: "group", "aria-label": "Tool"}, modeButtons),
      field("Colour", color), field("Size (text size in points, or pen width)", amount), h("div", {class: "row"}, undo, save)),
    imagePanel, signPanel, h("div", {class: "row"}, prev, where, next), status, cv);

  const opener = pdfOpener(async (f, pw) => {
    [file, password, shown] = [f, pw, null];
    ops.length = 0;
    cache.clear();
    work.hidden = true;
    if (!f) return "";
    const data = await (await api("/api/tools/preview", formData({files: f, dpi: 20, pages: 1, password: pw}))).json();
    if (file !== f) return "";  // another file was chosen while this one was loading
    count = data.count;
    work.hidden = false;
    show(0);
    return `${count} pages.`;
  });
  return h("div", {}, h("h1", {}, "Editor"), opener, work);
}

function pipeline() {
  const eligible = tools.filter(t => !t.files.length && t.input && t.category !== "Internal");
  let steps = [];  // [{k: key, t: tool, fields: its controls}]
  const picker = filePicker({hint: "Every file a step produces is fed to the next step."});
  const pw = pwInput(), status = statusEl(), run = h("button", {class: "primary"}, "Run");
  const plan = h("textarea", {rows: 8});
  const current = () => steps.map(s => ({tool: s.t.id, params: s.fields.values(true)}));
  const sync = () => { plan.value = JSON.stringify(current(), null, 2); };
  const list = h("ol", {class: "steps", oninput: sync, onchange: sync});
  const step = (id, params) => ({k: ++uid, t: byId(id), fields: paramFields(byId(id), params)});
  const addButton = button("Add step", () => { steps.push(step(eligible[0].id)); draw(); });

  function draw() {
    keepFocus(list, () => list.replaceChildren(...steps.map((s, i) => {
      const act = (text, name, change, off) => iconButton(text, `${name}: step ${i + 1}, ${s.t.title}`, s.k + name, () => { change(); draw(); }, off);
      const pick = h("select", {value: s.t.id, "data-k": `${s.k}tool`, onchange: () => { steps[i] = {...step(pick.value), k: s.k}; draw(); }},
        eligible.map(t => h("option", {value: t.id}, `${t.category}: ${t.title}`)));
      return h("li", {}, h("div", {class: "row"}, field(`Step ${i + 1}`, pick),
        act("↑", "Move up", () => steps.splice(i - 1, 0, ...steps.splice(i, 1)), i === 0),
        act("↓", "Move down", () => steps.splice(i + 1, 0, ...steps.splice(i, 1)), i === steps.length - 1),
        act("×", "Remove", () => steps.splice(i, 1))), s.fields.el);
    })), addButton);
    sync();
  }

  function load() {
    let data;
    try { data = JSON.parse(plan.value); } catch (e) { return tell(status, `The plan is not valid JSON: ${e.message}`, true); }
    if (!Array.isArray(data)) return tell(status, "The plan must be a JSON list of steps.", true);
    const bad = data.findIndex(s => !eligible.some(t => t.id === s?.tool));
    if (bad >= 0) return tell(status, `Step ${bad + 1}: "${data[bad]?.tool}" is not a tool that can run in a pipeline.`, true);
    steps = data.map(s => step(s.tool, s.params));
    draw();
    tell(status, `Plan loaded: ${steps.length} steps.`);
  }

  steps.push(step(eligible[0].id));
  draw();
  return h("form", {class: "panel", onsubmit: e => {
    e.preventDefault();
    busy(run, status, async () => {
      if (!picker.files.length) throw new Error("Choose at least one file.");
      if (!steps.length) throw new Error("Add at least one step.");
      return api("/api/pipeline", formData({files: picker.files, steps: JSON.stringify(current()), password: pw.value}));
    });
  }},
    h("h1", {}, "Pipeline"), h("p", {}, "Run several tools in a row on the same files."), picker.el, pwField(pw),
    h("h2", {}, "Steps"), list, h("div", {class: "row"}, addButton),
    field("Plan (JSON)", plan, "Always shows the steps above. Paste a plan here and press Load to rebuild them."),
    h("div", {class: "row"}, button("Load", load), run), status);
}

// --- shell --------------------------------------------------------------------------------------

function route(e) {
  const [, name = "", arg = ""] = location.hash.split("/");
  const view = {tool: toolView, organizer, editor, pipeline}[name] ?? home;
  showResult(null);
  main.replaceChildren(view(arg), result);
  document.title = [main.querySelector("h1:not(.sr)")?.textContent, config.name].filter(Boolean).join(" · ");
  const here = `#/${name === "tool" ? "" : name}`;
  for (const a of document.querySelectorAll("nav a")) {
    if (a.getAttribute("href") === here) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (e && document.activeElement !== search) main.focus();  // on navigation, not on first load or while searching
}

async function start() {
  try { config = {...config, ...await (await api("/api/config")).json()}; } catch { /* keep the defaults */ }
  document.getElementById("brand").textContent = config.name;
  try { tools = await (await api("/api/tools")).json(); }
  catch (e) { return main.replaceChildren(h("p", {class: "status error", role: "alert"}, `The tool list could not be loaded. ${e.message}`)); }
  search.addEventListener("input", () => { if (location.hash.length > 2) location.hash = "#/"; else route(); });
  document.getElementById("skip").addEventListener("click", () => main.focus());
  // a file dropped next to a drop zone must not make the browser leave the page
  for (const type of ["dragover", "drop"]) window.addEventListener(type, e => e.preventDefault());
  window.addEventListener("hashchange", route);
  route();
}

start();
