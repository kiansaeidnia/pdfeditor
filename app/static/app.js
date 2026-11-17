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

async function api(path, body, method = body ? "POST" : "GET") {
  const res = await fetch(path, {method, body}).catch(() => { throw new Error("Can't reach the server."); });
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
  if (config.user) {
    const status = statusEl(), keep = button("Save to My files", () => busy(keep, status, async () => {
      await api("/api/files", formData({files: new File([blob], name, {type})}));
      return "Saved to My files.";
    }));
    result.append(h("div", {class: "row"}, keep), status);
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
  const MODES = ["Edit text", "Add text", "Draw", "Highlight", "Whiteout", "Redact", "Image", "Signature"];
  const TIPS = {"Edit text": "Click any text on the page to change it. It keeps its font, size and colour. Scanned pages are read first.",
    "Add text": "Click where the new text should start.", Draw: "Drag to draw.", Highlight: "Drag a rectangle to highlight it.",
    Whiteout: "Drag a rectangle to paint it white.", Redact: "Drag a rectangle: what is under it is removed for good.",
    Image: "Choose an image, then drag a rectangle where it should go.",
    Signature: "Draw your signature, press Use, then drag a rectangle where it should go."};
  let file, password, count = 0, page = 0, shown = null, live = null, start, mode = "Edit text", inked = false;
  let spans = null;  // the shown page's text, for Edit text: [{text, rect, baseline, size, color, font, uses, bold, ...}]
  const ops = [], cache = new Map(), pics = new Map(), stamps = {}, texts = new Map();  // stamps: the picture waiting to be placed, per mode
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

  function pageText(n) {
    if (!texts.has(n)) {
      texts.set(n, api("/api/tools/text-spans", formData({files: file, page: n + 1, password})).then(r => r.json()).then(d => d.spans)
        .catch(e => { texts.delete(n); throw e; }));
    }
    return texts.get(n);
  }
  // A scanned page has no text to edit until OCR reads it. Each page is read once, on its own, the first time it is
  // shown in Edit text; the read copy of the document then replaces the open one, so later pages and Save use it.
  const scanned = new Set();  // pages already read with OCR in this document
  let reading = Promise.resolve();  // one OCR at a time, each starting from the copy the one before produced
  function readScan(n) {
    const run = reading.then(async () => {
      const f = file;
      const res = await api("/api/tools/ocr", formData({files: f, pages: n + 1, password}));
      const blob = await res.blob();
      if (file !== f) return false;  // another document was opened meanwhile
      file = new File([blob], f.name, {type: "application/pdf"});
      password = "";  // results are always saved without a password
      texts.clear();
      return true;
    });
    reading = run.catch(() => {});
    return run;
  }
  async function loadText() {
    if (mode !== "Edit text" || !file) return;
    const n = page, f = file;
    try {
      const found = await pageText(n);
      if (page !== n || file !== f) return;
      if (!found.length && !scanned.has(n)) {
        scanned.add(n);
        tell(status, "This page is a scan, so its text is being read first (OCR). That takes a few seconds…");
        if (await readScan(n) && page === n) await loadText();
        return;
      }
      spans = found;
      paint();
      if (!found.length) tell(status, "No text found on this page, even by reading the scan.", true);
      else if (found.some(s => s.scanned)) tell(status, "Text read from the scan. Click a line to change it: the new text is written over the scan, in its ink colour.");
    } catch (e) { tell(status, `The text on this page can't be read: ${e.message}`, true); }
  }
  // the look of a piece of text in CSS, at `size` CSS pixels: its own font if this computer has it, else a lookalike
  const look = (s, size) => `${s.italic ? "italic " : ""}${s.bold ? "bold " : ""}${size}px "${s.uses}", ` +
    (s.mono ? '"Courier New", monospace' : s.serif ? '"Times New Roman", Times, serif' : "Arial, Helvetica, sans-serif");
  const sameBox = (a, b) => a.every((v, i) => Math.abs(v - b[i]) < 0.01);

  // Redraw the page picture and every pending edit on it, the way the server will draw them
  function paint() {
    undo.textContent = ops.length ? `Undo (${ops.length})` : "Undo";
    if (!shown) return;
    ctx.setTransform(1 / PT, 0, 0, 1 / PT, 0, 0);  // from here on the canvas is drawn on in PDF points
    ctx.drawImage(shown, 0, 0, cv.width * PT, cv.height * PT);
    const mine = ops.filter(o => o.page === page);
    // the server applies redactions and text changes before everything else, so they go first here too
    const first = o => o.type === "redact" || o.type === "retext";
    for (const o of [...mine.filter(first), ...mine.filter(o => !first(o)), ...(live ? [live] : [])]) {
      ctx.globalAlpha = o.type === "highlight" ? 0.35 : 1;
      ctx.fillStyle = ctx.strokeStyle = o.type === "whiteout" ? "#ffffff" : o.color ?? "#000000";
      if (o.type === "retext") {  // the old text covered, the new one written on its baseline in a lookalike font
        const [x0, y0, x1, y1] = o.rect, pad = o.span.scanned ? [1.5, (y1 - y0) * 0.15] : [0.5, 0];
        ctx.fillStyle = o.span.paper ?? "#ffffff";
        ctx.fillRect(x0 - pad[0], y0 - pad[1], x1 - x0 + 2 * pad[0], y1 - y0 + 2 * pad[1]);
        ctx.fillStyle = o.color;
        ctx.font = look(o.span, o.span.size);
        ctx.fillText(o.text, o.span.baseline[0], o.span.baseline[1]);
      } else if (o.type === "text") {
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
    if (mode === "Edit text" && spans) {  // dashed boxes show what can be changed
      ctx.strokeStyle = "rgba(36, 86, 201, 0.55)";
      ctx.lineWidth = 0.6;
      ctx.setLineDash([2, 2]);
      for (const s of spans) ctx.strokeRect(s.rect[0] - 1, s.rect[1] - 0.5, s.rect[2] - s.rect[0] + 2, s.rect[3] - s.rect[1] + 1);
      ctx.setLineDash([]);
    }
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
      spans = null;
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      paint();
      tell(status, TIPS[mode]);
      loadText();
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
    paint();
    loadText();
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
    if (mode === "Edit text") {  // change existing text in a box over it
      placeText(true);
      retext(true);
      const s = spans?.find(s => p[0] >= s.rect[0] - 1 && p[0] <= s.rect[2] + 1 && p[1] >= s.rect[1] - 1 && p[1] <= s.rect[3] + 1);
      if (!s) return tell(status, spans ? "There's no text there. Click on a word inside a dashed box." : "Still reading this page's text…");
      const k = cv.clientWidth / cv.width / PT, before = ops.find(o => o.type === "retext" && o.page === page && sameBox(o.rect, s.rect));
      Object.assign(retyping, {hidden: false, value: before ? before.text : s.text, span: s});
      Object.assign(retyping.style, {left: `${(s.rect[0] - 1) * k}px`, top: `${(s.rect[1] - 1) * k}px`, color: s.color, font: look(s, s.size * k),
        width: `${Math.max((s.rect[2] - s.rect[0]) * k + 48, 120)}px`});
      const same = s.font.replace(/[^a-z0-9]/gi, "").toLowerCase() === s.uses.replace(/[^a-z0-9]/gi, "").toLowerCase();
      tell(status, (s.scanned ? `Text read from the scan. The new text is written over it in ${s.uses}, in the scan's ink colour.`
        : `${s.font}, ${s.size} pt. ` + (same ? "Its own font is kept." : `Written in ${s.uses}, which matches it.`)) +
        " Enter keeps the change, Escape cancels.");
      return setTimeout(() => { retyping.focus(); retyping.select(); });
    }
    if (mode === "Add text") {  // type right on the page, in a box where the text will sit
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
  const retyping = h("input", {class: "typing", type: "text", hidden: true, "aria-label": "The text, to change. Enter keeps the change, Escape cancels.",
    onblur: () => retext(true),
    onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); retext(true); } else if (e.key === "Escape") retext(false); }});
  // A changed piece of text is one edit, however often it is changed; changed back, the edit goes
  function retext(keep) {
    if (retyping.hidden) return;
    retyping.hidden = true;
    const s = retyping.span, i = ops.findIndex(o => o.type === "retext" && o.page === page && sameBox(o.rect, s.rect));
    if (keep && retyping.value !== (i >= 0 ? ops[i].text : s.text)) {
      if (i >= 0) ops.splice(i, 1);
      if (retyping.value !== s.text) ops.push({type: "retext", page, rect: s.rect, text: retyping.value, color: s.color, size: s.size, span: s});
      tell(status, TIPS[mode]);
    }
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
    const body = JSON.stringify(ops.map(({span, ...o}) => o));  // the preview's copy of the old text stays here
    if (!ops.length) throw new Error("Nothing to save yet: add an edit first.");
    if (body.length > 1_000_000) throw new Error("These edits are too large to send in one go (the pictures add up to more than 1 MB). Undo one, save, and add the rest to the saved file.");
    return api("/api/tools/edit", formData({files: file, ops: body, password}));
  }), {class: "primary"});
  const work = h("div", {hidden: true},
    h("div", {class: "toolbar"}, h("div", {class: "row", role: "group", "aria-label": "Tool"}, modeButtons),
      field("Colour", color), field("Size (text size in points, or pen width)", amount), h("div", {class: "row"}, undo, save)),
    imagePanel, signPanel, h("div", {class: "row"}, prev, where, next, fit), status, h("div", {class: "stage"}, cv, typing, retyping));

  const opener = pdfOpener(async (f, pw) => {
    [file, password, shown, spans] = [f, pw, null, null];
    ops.length = 0;
    cache.clear();
    texts.clear();
    scanned.clear();
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

// --- first-run setup ----------------------------------------------------------------------------

// A group of large radio choices. Changing one redraws its page (draw) and keeps the focus on the choice, so arrow
// keys go on working.
function choices(name, label, options, get, set, draw) {
  return h("div", {class: "stack", role: "radiogroup", "aria-label": label}, options.map(([value, title, hint]) =>
    h("label", {class: "choice"},
      h("input", {type: "radio", name, value, checked: get() === value, onchange: e => {
        set(value);
        draw(false);
        document.querySelector(`input[name="${name}"][value="${value}"]`)?.focus();
        e.stopPropagation();
      }}),
      h("span", {}, h("strong", {}, title), h("small", {}, hint)))));
}

// The setup wizard. With accounts: code, who can use it, the admin account, the sign-up rule, storage, e-mail.
// Without: code, who can use it.
function setupView() {
  const st = {step: 0, login: "accounts", signup: "invite", db: "sqlite", files: "local", mail: false};
  const text = props => h("input", {type: "text", autocomplete: "off", spellcheck: false, autocapitalize: "off", ...props});
  const secret = () => h("input", {type: "password", autocomplete: "new-password"});
  const code = text({});
  const email = h("input", {type: "email", autocomplete: "username", pattern: EMAIL}), pass = secret(), again = secret();
  const dbUrl = text({placeholder: "postgresql://user:password@host:5432/pdfeditor"});
  const s3 = {endpoint: text({placeholder: "https://s3.example.com"}), region: text({placeholder: "us-east-1"}), bucket: text({}),
    access: text({}), key: secret()};
  const host = text({placeholder: "mail.example.com"});
  const port = h("select", {}, h("option", {value: "587"}, "587 (STARTTLS, the usual one)"), h("option", {value: "465"}, "465 (TLS)"));
  const muser = text({}), mpass = secret();
  const from = h("input", {type: "email", autocomplete: "off", placeholder: "pdf@example.com", pattern: EMAIL});
  const status = statusEl(), next = h("button", {class: "primary"}), progress = h("ol", {class: "progress"}), body = h("div", {class: "stack"});
  const back = button("Back", () => { st.step--; draw(); });
  const accounts = () => st.login === "accounts";
  const preset = config.preset ?? {};

  const smtp = () => st.mail && accounts() ? {smtp_host: host.value.trim(), smtp_port: port.value, smtp_user: muser.value.trim(),
    smtp_password: mpass.value, smtp_from: from.value.trim()} : {};
  const where = () => ({
    ...(st.db === "postgres" && !preset.database ? {database: dbUrl.value.trim()} : {}),
    ...(st.files === "s3" && !preset.storage ? {storage: "s3", s3_endpoint: s3.endpoint.value.trim(), s3_region: s3.region.value.trim(),
      s3_bucket: s3.bucket.value.trim(), s3_access_key: s3.access.value.trim(), s3_secret_key: s3.key.value} : {}),
  });
  const tryIt = (label, path, fields, done) => {
    const b = button(label, () => busy(b, status, async () => {
      check(st.step);
      await api(path, formData({code: code.value, ...fields()}));
      return done;
    }));
    return b;
  };
  const useMail = h("input", {type: "checkbox", onchange: () => { st.mail = useMail.checked; draw(false); useMail.focus(); }});
  const mailFields = h("div", {class: "fields"}, field("Mail server", host, "", true), field("Port", port),
    field("User name", muser, "Often the full e-mail address."), field("Password", mpass),
    field("Send from", from, "Leave empty to use the user name, if it is an e-mail address."),
    h("div", {class: "row"}, tryIt("Send a test e-mail", "/api/setup/test-email", () => ({to: email.value.trim(), ...smtp()}),
      "Test e-mail sent. If it doesn't arrive, check the spam folder and the settings.")));
  const dbFields = h("div", {class: "fields"}, field("Postgres address", dbUrl, "The database must exist; pdfeditor creates its tables.", true),
    h("div", {class: "row"}, tryIt("Test the connection", "/api/setup/test-database", where, "Connected: the database is ready.")));
  const s3Fields = h("div", {class: "fields"}, field("Endpoint", s3.endpoint, "Leave empty for Amazon S3. Cloudflare R2, Backblaze B2 and MinIO give you one."),
    field("Region", s3.region, "Leave empty if your provider doesn't use one."), field("Bucket", s3.bucket, "It must exist already.", true),
    field("Access key", s3.access), field("Secret key", s3.key),
    h("div", {class: "row"}, tryIt("Test the bucket", "/api/setup/test-storage", where, "The bucket works: a test file was written, read back and deleted.")));

  const steps = [
    {name: "Setup code", page: () => [h("h2", {tabIndex: -1}, "Enter your setup code"),
      h("p", {}, "This proves you run this server. The code is in the container's log: on the machine running pdfeditor, run"),
      h("pre", {}, "docker compose logs pdfeditor"), field("Setup code", code, "", true)],
    check: () => { if (!code.value.trim()) throw new Error("Enter the setup code."); },
    leave: () => api("/api/setup/code", formData({code: code.value}))},
    {name: "Access", page: () => [h("h2", {tabIndex: -1}, "Who can use pdfeditor?"),
      choices("login", "Who can use pdfeditor", [
        ["accounts", "People with an account", "Everyone signs in with an e-mail address and a password, and gets their own file library. You will be the admin."],
        ["none", "Anyone who can open this page", "No accounts and no file libraries. Fine when only you can reach this computer."],
      ], () => st.login, v => { st.login = v; }, draw)]},
    {name: "Admin", when: accounts, page: () => [h("h2", {tabIndex: -1}, "Your admin account"),
      h("p", {}, "You sign in with this, and you can manage everyone else's accounts."),
      h("div", {class: "fields"}, field("Your e-mail address", email, "", true), field("Password", pass, "At least 8 characters.", true),
        field("Password again", again, "", true))],
    check: () => {
      if (!email.value.trim() || !email.checkValidity()) throw new Error("Enter a valid e-mail address.");
      if (pass.value.length < 8) throw new Error("The password needs at least 8 characters.");
      if (pass.value !== again.value) throw new Error("The two passwords are not the same.");
    }},
    {name: "Sign-up", when: accounts, page: () => [h("h2", {tabIndex: -1}, "How do other people get an account?"),
      choices("signup", "How other people get an account", [
        ["invite", "Only people I invite", "You make invite links on the admin page."],
        ["approval", "Anyone can ask, and an admin approves", "New accounts wait until an admin approves them."],
        ["open", "Anyone can sign up", "New accounts work straight away. Only for a server you are happy to share."],
      ], () => st.signup, v => { st.signup = v; }, draw),
      h("p", {class: "empty"}, "You can change this later on the admin page.")]},
    {name: "Storage", when: accounts, page: () => [h("h2", {tabIndex: -1}, "Where accounts and files are kept"),
      h("h3", {}, "Accounts"),
      preset.database ? h("p", {}, "In the Postgres database chosen in the server's settings file.")
        : [choices("db", "Where accounts are kept", [
          ["sqlite", "A database file on this server", "SQLite, kept in the data volume. Nothing to set up."],
          ["postgres", "A Postgres database", "For a database you already run or back up."],
        ], () => st.db, v => { st.db = v; }, draw), st.db === "postgres" && dbFields],
      h("h3", {}, "Files"),
      preset.storage ? h("p", {}, `In the S3 bucket “${preset.storage}” chosen in the server's settings file.`)
        : [choices("files", "Where files are kept", [
          ["local", "This server's disk", "Kept in the data volume."],
          ["s3", "S3-compatible storage", "Amazon S3, Cloudflare R2, Backblaze B2, MinIO and others."],
        ], () => st.files, v => { st.files = v; }, draw), st.files === "s3" && s3Fields]],
    check: () => {
      if (st.db === "postgres" && !preset.database && !/^postgres(ql)?:\/\//.test(dbUrl.value.trim())) throw new Error("A Postgres address starts with postgresql://");
      if (st.files === "s3" && !preset.storage && !s3.bucket.value.trim()) throw new Error("Enter the bucket's name.");
    },
    leave: async () => {  // a wrong address shows up here, on the step that has it, not at the very end
      await api("/api/setup/test-database", formData({code: code.value, ...where()}));
      await api("/api/setup/test-storage", formData({code: code.value, ...where()}));
    }},
    {name: "E-mail", when: accounts, page: () => [h("h2", {tabIndex: -1}, "Sending e-mail (optional)"),
      h("p", {}, "With a mail server, results can be e-mailed, forgotten passwords reset, invites sent, and admins told about new sign-ups."),
      field("Send e-mail through my mail server", useMail), st.mail ? mailFields : h("p", {class: "empty"}, "You can skip this: everything else works without it.")],
    check: () => {
      if (!st.mail) return;
      if (!host.value.trim()) throw new Error("Enter the mail server, e.g. mail.example.com.");
      if (!(from.value.trim() || muser.value.includes("@"))) throw new Error("Enter the address mail is sent from.");
      if (!from.checkValidity()) throw new Error("“Send from” is not a valid e-mail address.");
    }},
  ];
  const shown = () => steps.filter(s => !s.when || s.when());
  const check = n => shown()[n].check?.();

  function draw(focus = true) {
    const all = shown(), last = st.step === all.length - 1;
    useMail.checked = st.mail;
    progress.replaceChildren(...all.map((s, i) => h("li", {class: i < st.step ? "done" : "", "aria-current": i === st.step ? "step" : null}, s.name)));
    body.replaceChildren(...[all[st.step].page()].flat(3).filter(Boolean));
    next.textContent = last ? "Finish setup" : "Next";
    back.hidden = st.step === 0;
    tell(status, "");
    if (focus) body.querySelector("h2").focus();
  }

  const form = h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(next, status, async () => {
      const step = shown()[st.step];
      step.check?.();
      await step.leave?.();
      if (st.step < shown().length - 1) {
        st.step++;
        return draw();
      }
      const admin = accounts() ? {email: email.value.trim(), password: pass.value, signup: st.signup, ...where()} : {};
      await api("/api/setup", formData({code: code.value, login: st.login, app_url: location.origin, ...admin, ...smtp()}));
      location.hash = "#/";
      await refresh();
      say("Setup is finished.");
    });
  }}, h("h1", {}, `Set up ${config.name}`), progress, body, h("div", {class: "row"}, back, next), status);
  draw(false);
  return form;
}

// --- signing in and up --------------------------------------------------------------------------

const canSignUp = () => config.signup === "open" || config.signup === "approval";

function loginView() {
  const email = h("input", {type: "email", autocomplete: "username", required: true});
  const pass = h("input", {type: "password", autocomplete: "current-password", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Sign in");
  return h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      await api("/api/login", formData({email: email.value, password: pass.value}));
      config.expired = false;
      await refresh();
    });
  }}, h("h1", {}, `Sign in to ${config.name}`), field("E-mail", email), field("Password", pass),
    h("div", {class: "row"}, go),
    h("p", {class: "links"}, config.email && h("a", {href: "#/forgot"}, "Forgot your password?"),
      canSignUp() && h("a", {href: "#/signup"}, "Create an account")),
    config.expired ? h("p", {class: "status", role: "status"}, "You were signed out. Sign in again to carry on: your chosen files are still here.") : status);
}

function signupView(invite = "") {
  if (!invite && !canSignUp()) {
    return h("div", {class: "panel narrow"}, h("h1", {}, "Accounts are by invitation"),
      h("p", {}, "Ask an admin of this server for an invite link."), h("p", {}, h("a", {href: "#/"}, "← Sign in")));
  }
  const email = h("input", {type: "email", autocomplete: "username", required: true, pattern: EMAIL});
  const pass = h("input", {type: "password", autocomplete: "new-password", required: true, minLength: 8});
  const again = h("input", {type: "password", autocomplete: "new-password", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Create account");
  const form = h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      if (pass.value !== again.value) throw new Error("The two passwords are not the same.");
      const r = await (await api("/api/signup", formData({email: email.value.trim(), password: pass.value, invite}))).json();
      if (r.pending) {
        return form.replaceChildren(h("h1", {}, "Thanks, you're nearly in"),
          h("p", {}, `An admin needs to approve your account. ${config.email ? "You'll get an e-mail when it's ready." : "Try signing in again later."}`),
          h("p", {}, h("a", {href: "#/"}, "← Sign in")));
      }
      location.hash = "#/";
      await refresh();
      say("Your account is ready.");
    });
  }}, h("h1", {}, invite ? `You're invited to ${config.name}` : "Create an account"),
    config.signup === "approval" && !invite && h("p", {}, "An admin approves new accounts before they can be used."),
    field("Your e-mail address", email), field("Password", pass, "At least 8 characters."), field("Password again", again),
    h("div", {class: "row"}, go), h("p", {}, "Have an account? ", h("a", {href: "#/"}, "Sign in")), status);
  return form;
}

function forgotView() {
  const email = h("input", {type: "email", autocomplete: "username", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Send me a link");
  return h("form", {class: "panel narrow", onsubmit: e => {
    e.preventDefault();
    busy(go, status, async () => {
      await api("/api/forgot", formData({email: email.value}));
      return "If that address has an account here, a link to set a new password is on its way. It works for 30 minutes.";
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

function accountView() {
  const current = h("input", {type: "password", autocomplete: "current-password", required: true});
  const pass = h("input", {type: "password", autocomplete: "new-password", required: true, minLength: 8});
  const again = h("input", {type: "password", autocomplete: "new-password", required: true});
  const status = statusEl(), go = h("button", {class: "primary"}, "Change password");
  return h("div", {class: "narrow"}, h("h1", {}, "Your account"),
    h("p", {}, `Signed in as ${config.user.email}${config.user.role === "admin" ? ", an admin" : ""}.`),
    h("form", {class: "panel", onsubmit: e => {
      e.preventDefault();
      busy(go, status, async () => {
        if (pass.value !== again.value) throw new Error("The two passwords are not the same.");
        await api("/api/account/password", formData({current: current.value, password: pass.value}));
        current.value = pass.value = again.value = "";
        return "Your password is changed. Other browsers signed in as you have been signed out.";
      });
    }}, h("h2", {}, "Change your password"), field("Current password", current), field("New password", pass, "At least 8 characters."),
      field("New password again", again), h("div", {class: "row"}, go), status));
}

// --- My files -----------------------------------------------------------------------------------

// A delete button that asks for a second press before it acts
function dangerButton(text, name, k, action) {
  let armed = null;
  const set = (t, label) => { b.textContent = t; b.setAttribute("aria-label", label); };
  const b = button(text, () => {
    if (!armed) {
      set("Press again to delete", `Press again to confirm: ${name}`);
      armed = setTimeout(() => { armed = null; set(text, name); }, 4000);
      return;
    }
    clearTimeout(armed);
    armed = null;
    set(text, name);
    action();
  }, {class: "danger", "aria-label": name, "data-k": k});
  return b;
}

const when = t => new Date(t * 1000).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});

function filesView() {
  const status = statusEl(), list = h("ul", {class: "library", "aria-label": "Your files"});
  const meter = h("progress", {max: 1, value: 0}), usage = h("span", {});
  let renaming = null;
  const input = h("input", {type: "file", class: "sr", id: `f${++uid}`, multiple: true, onchange: () => { upload(input.files); input.value = ""; }});
  const drop = h("div", {class: "drop",
    ondragover: e => { e.preventDefault(); drop.classList.add("over"); },
    ondragleave: () => drop.classList.remove("over"),
    ondrop: e => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); }},
    h("label", {for: input.id}, "Add files or drop them here"), input, h("small", {}, `Up to ${config.max_upload_mb} MB at a time.`));

  const act = async (job, done) => {
    try {
      await job();
      await load();
      tell(status, done);
    } catch (e) {
      await load().catch(() => {});  // show what did happen, e.g. the files saved before one failed
      tell(status, e.message, true);
    }
  };
  const upload = files => files.length && act(() => api("/api/files", formData({files: [...files]})), `${files.length === 1 ? "File" : `${files.length} files`} added.`);

  async function open(f) {
    tell(status, `Opening ${f.name}…`);
    try {
      const blob = await (await api(`/api/files/${f.id}`)).blob();
      carry = [new File([blob], f.name, {type: f.type})];
      cat = search.value = "";
      location.hash = "#/";
    } catch (e) { tell(status, e.message, true); }
  }

  function row(f) {
    if (renaming === f.id) {
      const name = h("input", {type: "text", value: f.name, "aria-label": `New name for ${f.name}`, required: true});
      setTimeout(() => { name.focus(); name.select(); });
      return h("li", {}, h("form", {class: "row", onsubmit: e => {
        e.preventDefault();
        renaming = null;
        act(() => api(`/api/files/${f.id}/rename`, formData({name: name.value})), "Renamed.");
      }}, name, h("button", {class: "primary"}, "Save"), button("Cancel", () => { renaming = null; load(); })));
    }
    return h("li", {},
      h("div", {}, h("strong", {}, f.name), h("small", {}, `${size(f.size)} · ${when(f.created)}`)),
      h("div", {class: "row"},
        /^(application\/pdf|image\/)/.test(f.type) && button("Open in a tool", () => open(f), {"data-k": `${f.id}open`}),
        h("a", {class: "button quiet", href: `/api/files/${f.id}`, download: f.name}, "Download"),
        button("Rename", () => { renaming = f.id; load(); }, {"data-k": `${f.id}rename`}),
        dangerButton("Delete", `Delete ${f.name}`, `${f.id}del`, () => act(() => api(`/api/files/${f.id}`, null, "DELETE"), `${f.name} deleted.`))));
  }

  async function load() {
    const d = await (await api("/api/files")).json();
    const limit = d.quota_mb * 2 ** 20;
    meter.hidden = !limit;
    if (limit) meter.value = Math.min(1, d.used / limit);
    const usedText = d.used ? size(d.used) : "Nothing";
    usage.textContent = limit ? `${usedText} of ${d.quota_mb >= 1024 ? `${+(d.quota_mb / 1024).toFixed(1)} GB` : `${d.quota_mb} MB`} used` : `${usedText} used`;
    keepFocus(list, () => list.replaceChildren(...(d.files.length ? d.files.map(row)
      : [h("li", {class: "empty"}, "No files yet. Add some here, or keep a tool's result with “Save to My files”.")])));
  }
  load().catch(e => tell(status, e.message, true));
  return h("div", {}, h("h1", {}, "My files"), h("p", {class: "row usage"}, meter, usage), drop, status, list);
}

// --- admin --------------------------------------------------------------------------------------

function adminView() {
  const status = statusEl(), people = h("div", {}), pending = h("div", {});
  const ACTIONS = {approve: "Approve", disable: "Turn off", enable: "Turn on", make_admin: "Make admin", make_user: "Remove admin"};
  const act = (u, action, done) => busyAll(async () => {
    await api(`/api/admin/users/${u.id}`, formData({action}));
    await load();
    return done;
  });
  const busyAll = job => job().then(m => tell(status, m), e => tell(status, e.message, true));

  function person(u) {
    const self = u.email === config.user.email;
    const buttons = self ? [h("small", {}, "You")] : [
      u.status === "pending" && button(ACTIONS.approve, () => act(u, "approve", `${u.email} approved.`), {class: "primary", "data-k": `${u.id}a`}),
      u.status === "active" && button(ACTIONS.disable, () => act(u, "disable", `${u.email} is turned off.`), {"data-k": `${u.id}d`}),
      u.status === "disabled" && button(ACTIONS.enable, () => act(u, "enable", `${u.email} is turned on.`), {"data-k": `${u.id}e`}),
      u.status !== "pending" && (u.role === "admin"
        ? button(ACTIONS.make_user, () => act(u, "make_user", `${u.email} is no longer an admin.`), {"data-k": `${u.id}r`})
        : button(ACTIONS.make_admin, () => act(u, "make_admin", `${u.email} is now an admin.`), {"data-k": `${u.id}r`})),
      dangerButton(u.status === "pending" ? "Refuse" : "Delete", `Delete ${u.email} and their files`, `${u.id}x`,
        () => act(u, "delete", `${u.email} deleted.`)),
    ];
    return h("tr", {},
      h("th", {scope: "row"}, u.email), h("td", {}, u.role === "admin" ? "Admin" : "User"),
      h("td", {}, {active: "Active", pending: "Waiting", disabled: "Turned off"}[u.status]),
      h("td", {}, u.files ? `${u.files} (${size(u.bytes)})` : "None"), h("td", {}, h("div", {class: "row"}, buttons)));
  }
  const table = users => h("div", {class: "scroll"}, h("table", {},
    h("thead", {}, h("tr", {}, ["Account", "Role", "Status", "Files", ""].map(t => h("th", {scope: "col"}, t)))),
    h("tbody", {}, users.map(person))));

  async function load() {
    const users = await (await api("/api/admin/users")).json();
    const waiting = users.filter(u => u.status === "pending");
    keepFocus(main, () => {
      pending.replaceChildren(...(waiting.length ? [h("h2", {}, `Waiting for approval (${waiting.length})`), table(waiting)] : []));
      people.replaceChildren(h("h2", {}, `Accounts (${users.length})`), table(users.filter(u => u.status !== "pending")));
    });
  }

  const inviteTo = h("input", {type: "email", autocomplete: "off", pattern: EMAIL}), linkBox = h("div", {class: "stack"});
  const inviteStatus = statusEl(), rulesStatus = statusEl();
  const make = h("button", {class: "primary"}, "Make an invite link");
  const invite = h("form", {class: "panel", onsubmit: e => {
    e.preventDefault();
    busy(make, inviteStatus, async () => {
      const r = await (await api("/api/admin/invites", formData({email: inviteTo.value.trim()}))).json();
      const out = h("input", {type: "text", readOnly: true, value: r.link, "aria-label": "Invite link"});
      const copy = button("Copy", async () => { try { await navigator.clipboard.writeText(r.link); copy.textContent = "Copied"; } catch { out.select(); } });
      linkBox.replaceChildren(h("div", {class: "row"}, out, copy));
      return r.sent ? `Invite sent to ${inviteTo.value.trim()}. The link works once, for 7 days.` : "Send this link to the person you're inviting. It works once, for 7 days.";
    });
  }}, h("h2", {}, "Invite someone"),
    field("Their e-mail address (optional)", inviteTo, config.email ? "With an address the invite is e-mailed and only works for it." : "With an address the link only works for it."),
    h("div", {class: "row"}, make), linkBox, inviteStatus);

  const st = {signup: config.signup};
  const quota = h("input", {type: "number", min: 0, step: 1, value: config.quota_mb ?? 1024});
  const save = h("button", {class: "primary"}, "Save");
  const rules = h("div", {});
  const drawRules = () => rules.replaceChildren(choices("signup", "How people get an account", [
    ["invite", "Only people I invite", "Invite links from this page."],
    ["approval", "Anyone can ask, and an admin approves", "New accounts wait here until approved."],
    ["open", "Anyone can sign up", "New accounts work straight away."],
  ], () => st.signup, v => { st.signup = v; }, drawRules));
  drawRules();
  const settingsForm = h("form", {class: "panel", onsubmit: e => {
    e.preventDefault();
    busy(save, rulesStatus, async () => {
      await api("/api/admin/settings", formData({signup: st.signup, quota_mb: quota.value || "0"}));
      config.signup = st.signup;
      config.quota_mb = +quota.value;
      return "Saved.";
    });
  }}, h("h2", {}, "Rules"), h("h3", {}, "How people get an account"), rules,
    field("Storage per account (MB)", quota, "0 means no limit."), h("div", {class: "row"}, save), rulesStatus);

  load().catch(e => tell(status, e.message, true));
  return h("div", {}, h("h1", {}, "Admin"), status, pending, people, invite, settingsForm);
}

// --- shell --------------------------------------------------------------------------------------

const gated = () => config.setup || (config.login === "accounts" && !config.user);

function route(e) {
  const [, name = "", arg = ""] = location.hash.split("/");
  const signed = config.user, admin = signed?.role === "admin";
  const view = config.setup ? setupView
    : name === "reset" ? resetView
    : name === "signup" && (gated() || arg) ? signupView
    : gated() ? (name === "forgot" ? forgotView : loginView)
    : {tool: toolView, organizer, editor, pipeline, files: signed && filesView, account: signed && accountView, admin: admin && adminView}[name] || home;
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
  out.title = config.user ? `Signed in as ${config.user.email}` : "";
  for (const [id, show] of [["nav-files", config.user], ["nav-account", config.user], ["nav-admin", config.user?.role === "admin"]]) {
    document.getElementById(id).hidden = !show;
  }
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
  try { config = {...config, ...await (await api("/api/config")).json()}; } catch (e) {
    document.body.classList.add("bare");
    return main.replaceChildren(h("div", {class: "panel narrow"}, h("h1", {}, "pdfeditor can't start"),
      h("p", {class: "status error", role: "alert"}, e.message), button("Try again", () => location.reload())));
  }
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
