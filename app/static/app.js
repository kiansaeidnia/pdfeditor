// pdfeditor web UI: one ES module, no build step. Every form is rendered from the schema in /api/tools.
const ORDER = ["Pages", "Convert", "Edit", "Security", "Inspect"];
const EMAIL = "[^@\\s,;<>]+@[^@\\s,;<>]+\\.[^@\\s,;<>]+";  // the server's rule: the domain needs a dot
const DPI = 110, PT = 72 / DPI;  // editor page pictures, and the size of one of their pixels in PDF points
let config = {name: "pdfeditor", email: false, max_upload_mb: 200};
let tools = [], uid = 0, resultUrl;
let carry = [];  // the files being worked on: they follow the user from tool to tool
let cat = "";    // category filter on the home page

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
const pwField = input => h("details", {class: "pw"}, h("summary", {}, "This PDF has a password"),
  field("Password", input, "Results are always saved without a password."));
const say = text => { document.getElementById("announce").textContent = text; };  // for screen readers only
// preferences in localStorage; where that is unavailable (private mode, blocked) they simply don't stick
const pref = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const setPref = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* not kept */ } };

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
  if (res.status === 401 && config.user && path !== "/api/login") {  // the session ran out: back to the sign-in page
    config.user = null;
    config.expired = true;
    setTimeout(enter);
  }
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
  // the seconds are shown through CSS from an attribute, so screen readers are not told every tick
  const began = Date.now(), tick = setInterval(() => { status.dataset.elapsed = ` ${Math.round((Date.now() - began) / 1000)} s`; }, 1000);
  try {
    const out = await job();
    if (!(out instanceof Response)) tell(status, out ?? "");
    else if (status.isConnected) {  // not connected: the user left this view, so the file is dropped
      showResult(await download(out));
      tell(status, "Done.");
    }
  } catch (e) {
    tell(status, e.message, true);
    const box = /password/i.test(e.message) && status.closest("form")?.querySelector("details.pw");
    if (box) {  // the file turned out to be protected: show the field the user now needs
      box.open = true;
      box.querySelector("input").focus();
    }
  }
  clearInterval(tick);
  delete status.dataset.elapsed;
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

// File chooser with drag and drop and an ordered list of what was chosen. It starts with the files the user
// is already working on (those of a kind it accepts) and keeps `carry` up to date for the next tool.
function filePicker({accept = "", multiple = true, hint = "", onchange} = {}) {
  const fits = f => !accept || accept.split(",").some(a => f.name.toLowerCase().endsWith(a.trim()));
  let items = carry.filter(fits).slice(0, multiple ? undefined : 1).map(f => ({k: ++uid, f}));  // {k: key for keepFocus, f: File}
  const input = h("input", {type: "file", class: "sr", id: `f${++uid}`, accept, multiple, onchange: () => { add(input.files); input.value = ""; }});
  const list = h("ul", {class: "files", "aria-label": "Chosen files"});
  const changed = tell => {
    carry = items.map(it => it.f);
    draw();
    if (tell) onchange?.();
  };
  const add = files => {
    const fresh = [...files].map(f => ({k: ++uid, f}));
    if (!fresh.length) return;
    items = multiple ? [...items, ...fresh] : fresh.slice(0, 1);
    changed(true);
  };
  const draw = () => keepFocus(list, () => list.replaceChildren(...items.map((it, i) => h("li", {},
    h("span", {}, it.f.name), h("small", {}, size(it.f.size)),
    multiple && i > 0 && iconButton("↑", `Move ${it.f.name} up`, `${it.k}up`, () => { items.splice(i - 1, 2, it, items[i - 1]); changed(); }),
    iconButton("×", `Remove ${it.f.name}`, `${it.k}rm`, () => { items.splice(i, 1); changed(true); })))), input);
  draw();
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
  result.hidden = false;
  result.classList.toggle("none", !r);  // CSS shows the empty panel only beside a tool form on wide screens
  result.replaceChildren(h("h2", {}, "Result"));
  if (!r) return result.append(h("p", {class: "empty"}, "Your result will show up here, with a preview and a download button."));
  const {name, blob} = r, type = blob.type, url = resultUrl = URL.createObjectURL(blob);
  const pre = h("pre", {tabIndex: 0});  // focusable so a long preview can be scrolled from the keyboard
  const onward = /^(application\/pdf|image\/)/.test(type) && button("Use in another tool", () => {
    carry = [new File([blob], name, {type})];
    cat = search.value = "";
    location.hash = "#/";
  });
  result.append(h("div", {class: "row"}, h("a", {class: "button", href: url, download: name}, `Download ${name}`), onward, h("small", {}, size(blob.size))));
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
  const listed = tools.filter(t => t.category !== "Internal");
  const found = listed.filter(t => (!cat || t.category === cat) && `${t.title} ${t.description} ${t.id}`.toLowerCase().includes(q));
  const first = d => /^.*?[.!?](?=\s+[A-Z"]|$)/.exec(d)?.[0] ?? d;  // first sentence; "e.g. 2" does not end one
  const group = (name, list) => h("section", {}, h("h2", {}, name, h("small", {}, ` ${list.length}`)), h("ul", {class: "cards"},
    list.map(t => h("li", {"data-c": t.category}, h("a", {href: `#/tool/${t.id}`}, h("strong", {}, t.title), h("span", {}, first(t.description.replace(/\s+/g, " "))))))));
  const cats = [...new Set([...ORDER, ...listed.map(t => t.category)])];
  const chips = h("div", {class: "chips", role: "group", "aria-label": "Show category"}, ["", ...cats].map(c =>
    button(c || "All", () => { cat = c; route(); main.querySelector(".chips [aria-pressed=true]")?.focus(); }, {"aria-pressed": String(cat === c)})));
  const recent = q || cat ? [] : pref("recent", []).map(byId).filter(t => t && t.category !== "Internal");
  const sections = cats.map(c => [c, found.filter(t => t.category === c)]).filter(([, list]) => list.length).map(([c, list]) => group(c, list));
  if (q) say(`${found.length} tools match.`);
  return h("div", {}, h("h1", {class: "sr"}, "Tools"),
    filePicker({hint: "Optional: start with your files. They follow you into whichever tool you open."}).el, chips,
    recent.length ? group("Recently used", recent) : null,
    sections.length ? sections : h("p", {}, `No tools match “${search.value.trim()}”.`));
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
      const res = await api(`/api/tools/${t.id}`, formData({files: picker ? picker.files : [], ...fields.values(), ...more, password: pw ? pw.value : ""}));
      setPref("recent", [t.id, ...pref("recent", []).filter(id => id !== t.id)].slice(0, 6));
      return res;
    });
  }},
    h("p", {class: "crumb"}, h("a", {href: "#/"}, "← All tools"), ` · ${t.category}`),
    h("h1", {}, t.title), h("p", {}, t.description.replace(/\s+/g, " ")), picker && picker.el,
    t.files.map((x, i) => field(label(x.name), extras[i], x.kind === "files" ? "You can choose several files." : "", x.required)),
    fields.el, pw && pwField(pw), h("div", {class: "row"}, run), status);
}

// "Choose one PDF" form of the organizer and the editor. open(file, password) loads it and returns a message;
// it is also called with no file when the chosen one is removed.
function pdfOpener(open) {
  const pw = pwInput(), status = statusEl(), go = h("button", {}, "Open");
  const picker = filePicker({accept: ".pdf", multiple: false, onchange: () => busy(go, status, () => open(picker.files[0], pw.value))});
  if (picker.files.length) queueMicrotask(() => go.click());  // arrived with a file already chosen: open it straight away
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
    apply.hidden = bulk.hidden = !thumbs.length;
    apply.disabled = !order.length;
  }
  const bulk = h("div", {class: "row"},
    button("Rotate all", () => { for (const p of new Set(order.map(it => it.p))) turn.set(p, ((turn.get(p) ?? 0) + 90) % 360); draw(); }),
    button("Reverse order", () => { order.reverse(); draw(); }),
    button("Start over", () => { order = thumbs.map((_, p) => ({k: ++uid, p})); turn.clear(); draw(); }));

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
  return h("div", {}, h("h1", {}, "Organize pages"), opener, bulk, grid, h("div", {class: "row"}, apply), status);
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
  const cv = h("canvas", {class: "page fit", "aria-label": "The page being edited. Edits are placed with a mouse, pen or finger."}), ctx = cv.getContext("2d");
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
    undo.textContent = ops.length ? `Undo (${ops.length})` : "Undo";
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
    if (mode === "Text") {  // type right on the page, in a box where the text will sit
      placeText(true);  // text still being typed elsewhere is kept, not wiped by this click
      const k = cv.clientWidth / cv.width / PT;  // CSS pixels per point at the current zoom
      Object.assign(typing, {hidden: false, value: "", at: p, size: sized(500), ink: color.value});
      Object.assign(typing.style, {left: `${p[0] * k}px`, top: `${p[1] * k}px`, color: color.value, font: `${typing.size * k}px Helvetica, Arial, sans-serif`});
      return setTimeout(() => typing.focus());  // after this click has finished moving the focus
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

  const typing = h("input", {class: "typing", type: "text", hidden: true, "aria-label": "Text to add. Enter places it, Escape cancels.",
    onblur: () => placeText(true),
    onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); placeText(true); } else if (e.key === "Escape") placeText(false); }});
  function placeText(keep) {
    if (typing.hidden) return;
    typing.hidden = true;
    if (keep && typing.value.trim()) ops.push({type: "text", page, color: typing.ink, points: [typing.at], text: typing.value, size: typing.size});
    paint();
  }
  const fit = button("Fit to screen", () => fit.setAttribute("aria-pressed", String(cv.classList.toggle("fit"))), {"aria-pressed": "true"});
  const onKey = e => {  // Ctrl+Z / Cmd+Z undoes the last edit while the editor is open
    if (!cv.isConnected) return document.removeEventListener("keydown", onKey);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) {
      e.preventDefault();
      undo.click();
    }
  };
  document.addEventListener("keydown", onKey);

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
    imagePanel, signPanel, h("div", {class: "row"}, prev, where, next, fit), status, h("div", {class: "stage"}, cv, typing));

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

// --- first-run setup and sign-in ----------------------------------------------------------------

// The setup wizard: setup code, then who may use the app, then (with a sign-in) an optional mail server.
function setupView() {
  const st = {step: 0, login: "password", mail: false};
  const text = props => h("input", {type: "text", autocomplete: "off", spellcheck: false, autocapitalize: "off", ...props});
  const code = text({});
  const email = h("input", {type: "email", autocomplete: "username", pattern: EMAIL});
  const pass = h("input", {type: "password", autocomplete: "new-password"});
  const again = h("input", {type: "password", autocomplete: "new-password"});
  const host = text({placeholder: "mail.example.com"});
  const port = h("select", {}, h("option", {value: "587"}, "587 (STARTTLS, the usual one)"), h("option", {value: "465"}, "465 (TLS)"));
  const muser = text({});
  const mpass = h("input", {type: "password", autocomplete: "off"});
  const from = h("input", {type: "email", autocomplete: "off", placeholder: "pdf@example.com", pattern: EMAIL});
  const status = statusEl(), next = h("button", {class: "primary"}), progress = h("ol", {class: "progress"}), body = h("div", {class: "stack"});
  const back = button("Back", () => { st.step--; draw(); });
  const choice = (value, title, hint) => h("label", {class: "choice"},
    h("input", {type: "radio", name: "login", value, checked: st.login === value, onchange: () => {
      st.login = value;
      draw(false);  // keep the focus on the choice, so arrow keys go on working
      body.querySelector(`input[value=${value}]`).focus();
    }}),
    h("span", {}, h("strong", {}, title), h("small", {}, hint)));
  const smtp = () => st.mail && st.login === "password" ? {smtp_host: host.value.trim(), smtp_port: port.value, smtp_user: muser.value.trim(), smtp_password: mpass.value, smtp_from: from.value.trim()} : {};
  const useMail = h("input", {type: "checkbox", onchange: () => { st.mail = useMail.checked; draw(false); useMail.focus(); }});
  const test = button("Send a test e-mail", () => busy(test, status, async () => {
    check(2);
    await api("/api/setup/test-email", formData({code: code.value, to: email.value.trim(), ...smtp()}));
    return `Test e-mail sent to ${email.value.trim()}. If it doesn't arrive, check the spam folder and the settings.`;
  }));
  const mailFields = h("div", {class: "fields"}, field("Mail server", host, "", true), field("Port", port),
    field("User name", muser, "Often the full e-mail address."), field("Password", mpass),
    field("Send from", from, "Leave empty to use the user name, if it is an e-mail address."), h("div", {class: "row"}, test));
  const names = () => st.login === "password" ? ["Setup code", "Sign-in", "E-mail"] : ["Setup code", "Sign-in"];
  const pages = [
    () => [h("h2", {tabIndex: -1}, "Enter your setup code"),
      h("p", {}, "This proves you run this server. The code is in the container's log: on the machine running pdfeditor, run"),
      h("pre", {}, "docker compose logs pdfeditor"), field("Setup code", code, "", true)],
    () => [h("h2", {tabIndex: -1}, "Who can use pdfeditor?"),
      h("div", {class: "stack", role: "radiogroup", "aria-label": "Who can use pdfeditor"},
        choice("password", "Only people with the password", "Sign in with an e-mail address and a password. Needed to send e-mail."),
        choice("none", "Anyone who can open this page", "No sign-in. Fine when only you can reach this computer.")),
      st.login === "password" && h("div", {class: "fields"}, field("Your e-mail address", email, "You sign in with it.", true),
        field("Password", pass, "At least 8 characters.", true), field("Password again", again, "", true))],
    () => [h("h2", {tabIndex: -1}, "Sending e-mail (optional)"),
      h("p", {}, "With a mail server, results can be e-mailed from the result panel, and a forgotten password can be reset by e-mail."),
      field("Send e-mail through my mail server", useMail), st.mail ? mailFields : h("p", {class: "empty"}, "You can skip this: everything else works without it.")],
  ];

  // throws the first problem on step n, so the user is told before moving on
  function check(n) {
    if (n === 0 && !code.value.trim()) throw new Error("Enter the setup code.");
    if (n === 1 && st.login === "password") {
      if (!email.value.trim() || !email.checkValidity()) throw new Error("Enter a valid e-mail address.");
      if (pass.value.length < 8) throw new Error("The password needs at least 8 characters.");
      if (pass.value !== again.value) throw new Error("The two passwords are not the same.");
    }
    if (n === 2 && st.mail) {
      if (!host.value.trim()) throw new Error("Enter the mail server, e.g. mail.example.com.");
      if (!(from.value.trim() || muser.value.includes("@"))) throw new Error("Enter the address mail is sent from.");
      if (!from.checkValidity()) throw new Error("“Send from” is not a valid e-mail address.");
    }
  }

  function draw(focus = true) {
    const all = names(), last = st.step === all.length - 1;
    useMail.checked = st.mail;
    progress.replaceChildren(...all.map((n, i) => h("li", {class: i < st.step ? "done" : "", "aria-current": i === st.step ? "step" : null}, n)));
    body.replaceChildren(...pages[st.step]().flat().filter(Boolean));
    next.textContent = last ? "Finish setup" : "Next";
    back.hidden = st.step === 0;
    tell(status, "");
    if (focus) body.querySelector("h2").focus();
  }

  const form = h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(next, status, async () => {
      check(st.step);
      if (st.step === 0) await api("/api/setup/code", formData({code: code.value}));
      if (st.step < names().length - 1) {
        st.step++;
        return draw();
      }
      const login = st.login === "password" ? {email: email.value.trim(), password: pass.value} : {};
      await api("/api/setup", formData({code: code.value, login: st.login, app_url: location.origin, ...login, ...smtp()}));
      location.hash = "#/";
      await refresh();
      say("Setup is finished.");
    });
  }}, h("h1", {}, `Set up ${config.name}`), progress, body, h("div", {class: "row"}, back, next), status);
  draw(false);
  return form;
}

function loginView() {
  const email = h("input", {type: config.user_label === "E-mail" ? "email" : "text", autocomplete: "username", required: true});
  const pass = h("input", {type: "password", autocomplete: "current-password", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Sign in");
  return h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      await api("/api/login", formData({email: email.value, password: pass.value}));
      config.expired = false;
      await refresh();
    });
  }}, h("h1", {}, `Sign in to ${config.name}`), field(config.user_label, email), field("Password", pass),
    h("div", {class: "row"}, go), config.reset && h("p", {}, h("a", {href: "#/forgot"}, "Forgot your password?")),
    config.expired ? h("p", {class: "status", role: "status"}, "You were signed out. Sign in again to carry on: your chosen files are still here.") : status);
}

function forgotView() {
  const email = h("input", {type: "email", autocomplete: "username", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Send me a link");
  return h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      await api("/api/forgot", formData({email: email.value}));
      return "If that address can sign in here, a link to set a new password is on its way. It works for 30 minutes.";
    });
  }}, h("p", {class: "crumb"}, h("a", {href: "#/"}, "← Sign in")), h("h1", {}, "Forgot your password?"),
    h("p", {}, "We'll e-mail you a link to set a new one."), field("Your e-mail address", email), h("div", {class: "row"}, go), status);
}

function resetView(token) {
  const pass = h("input", {type: "password", autocomplete: "new-password", required: true, minLength: 8});
  const again = h("input", {type: "password", autocomplete: "new-password", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Set password");
  return h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      if (pass.value !== again.value) throw new Error("The two passwords are not the same.");
      await api("/api/reset", formData({token, password: pass.value}));
      location.hash = "#/";
      await refresh();
      say("Your new password is set.");
    });
  }}, h("h1", {}, "Set a new password"), field("New password", pass, "At least 8 characters."), field("New password again", again),
    h("div", {class: "row"}, go), status);
}

// --- shell --------------------------------------------------------------------------------------

const gated = () => config.setup || (config.login === "password" && !config.user);

function route(e) {
  const [, name = "", arg = ""] = location.hash.split("/");
  const view = config.setup ? setupView
    : name === "reset" ? resetView
    : gated() ? (name === "forgot" ? forgotView : loginView)
    : {tool: toolView, organizer, editor, pipeline}[name] ?? home;
  main.className = view === toolView && byId(arg) && byId(arg).category !== "Internal" ? "split" : "";  // form and result side by side
  showResult(null);
  main.replaceChildren(view(arg), result);
  document.title = [main.querySelector("h1:not(.sr)")?.textContent, config.name].filter(Boolean).join(" · ");
  const here = `#/${name === "tool" ? "" : name}`;
  for (const a of document.querySelectorAll("nav a")) {
    if (a.getAttribute("href") === here) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (e) scrollTo(0, 0);  // a new view starts at its top
  if (e && document.activeElement !== search) main.focus({preventScroll: true});  // on navigation, not on first load or while searching
}

// Show setup, the sign-in page or the app, whichever the server's state calls for
async function enter(e = {}) {  // e: as for route(); start() passes null, so a reload keeps its scroll
  const bare = gated();
  document.body.classList.toggle("bare", bare);  // no navigation or search before setup and sign-in
  const out = document.getElementById("signout");
  out.hidden = !config.user;
  out.title = config.user ? `Signed in as ${config.user}` : "";
  if (!bare && !tools.length) {
    try { tools = await (await api("/api/tools")).json(); }
    catch (e) { return main.replaceChildren(h("p", {class: "status error", role: "alert"}, `The tool list could not be loaded. ${e.message}`)); }
  }
  route(e);
}

async function refresh() {
  config = {...config, ...await (await api("/api/config")).json()};
  await enter();
}

async function start() {
  try { config = {...config, ...await (await api("/api/config")).json()}; } catch { /* keep the defaults */ }
  document.getElementById("brand").textContent = config.name;
  document.getElementById("signout").addEventListener("click", async () => {
    await api("/api/logout", new FormData()).catch(() => {});
    location.hash = "#/";
    await refresh();
  });
  search.addEventListener("input", () => { if (location.hash.length > 2) location.hash = "#/"; else route(); });
  search.addEventListener("keydown", e => { if (e.key === "Escape") { search.value = ""; search.dispatchEvent(new Event("input")); } });
  window.addEventListener("keydown", e => {  // "/" jumps to the search box, as on most sites
    if (e.key === "/" && !e.ctrlKey && !e.metaKey && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) { e.preventDefault(); search.focus(); }
  });
  const theme = document.getElementById("theme"), THEMES = ["system", "light", "dark"];
  const setTheme = t => {
    if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    theme.title = `Colour theme: ${t}. Press to change.`;
    theme.setAttribute("aria-label", theme.title);
    setPref("theme", t);
  };
  theme.addEventListener("click", () => setTheme(THEMES[(THEMES.indexOf(pref("theme", "system")) + 1) % 3]));
  setTheme(THEMES.includes(pref("theme")) ? pref("theme") : "system");
  document.getElementById("skip").addEventListener("click", () => main.focus());
  // a file dropped next to a drop zone must not make the browser leave the page
  for (const type of ["dragover", "drop"]) window.addEventListener(type, e => e.preventDefault());
  window.addEventListener("hashchange", route);
  await enter(null);
}

start();
