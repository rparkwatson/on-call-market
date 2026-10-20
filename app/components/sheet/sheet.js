/* One column of the preference sheet from oncall_gui.py, as a Streamlit custom component.
   The Streamlit component protocol, done by hand (no component library, no build step):
     - on load, post {type: "streamlit:componentReady", apiVersion: 1} to the parent;
     - the parent answers every script run with {type: "streamlit:render", args, disabled};
     - after rendering, post "streamlit:setFrameHeight" so the frame fits the table;
     - on every change, post "streamlit:setComponentValue" with the cells ({slot_id: "3" | "X"}),
       debounced by DEBOUNCE_MS and flushed when an input loses focus.
   The cells in args are applied once, when the frame loads; later renders keep what the
   participant has typed (the Streamlit side reruns after every reported change). */
(function () {
  "use strict";
  var DEBOUNCE_MS = 300;
  var state = { args: {}, cells: {}, inputs: [], applied: false, signature: null, timer: null, lastHeight: -1 };

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); }); }
    if (text !== undefined && text !== null) { e.textContent = text; }
    return e;
  }
  function send(type, data) {
    var msg = { isStreamlitMessage: true, type: type };
    Object.keys(data || {}).forEach(function (k) { msg[k] = data[k]; });
    window.parent.postMessage(msg, "*");
  }
  function sendHeight() {
    var h = document.body.scrollHeight;
    if (h !== state.lastHeight) { state.lastHeight = h; send("streamlit:setFrameHeight", { height: h }); }
  }
  function sendValue(now) {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    var fire = function () { state.timer = null; send("streamlit:setComponentValue", { value: state.cells, dataType: "json" }); };
    if (now) { fire(); } else { state.timer = setTimeout(fire, DEBOUNCE_MS); }
  }

  // ---- cells: the same cleaning and colours as the original sheet ---------------------
  function cleanValue(v) {
    v = (v || "").trim();
    if (/^x+$/i.test(v)) { return "X"; }
    v = v.replace(/[^0-9.]/g, "");
    if ((state.args.mode || "rank") === "rank") { v = v.replace(/\..*/, ""); }
    return v;
  }
  function setCell(slotId, v) { if (v) { state.cells[slotId] = v; } else { delete state.cells[slotId]; } }
  function styleInput(inp) {
    var v = inp.value;
    inp.classList.toggle("x", v === "X");
    inp.classList.toggle("rank", v !== "" && v !== "X");
  }
  function nextRank() {
    var best = 0;
    Object.keys(state.cells).forEach(function (sid) {
      var v = state.cells[sid];
      if (v !== "X") { best = Math.max(best, parseInt(v, 10) || 0); }
    });
    return best + 1;
  }
  function updateCount() {
    var ranked = 0, xs = 0, need = parseInt(state.args.min_ranks, 10) || 0;
    Object.keys(state.cells).forEach(function (sid) { if (state.cells[sid] === "X") { xs += 1; } else { ranked += 1; } });
    var text = ranked + " ranked" + (ranked < need ? " (at least " + need + " needed)" : "") + (xs ? ", " + xs + " X" : "");
    $("count").textContent = text;
    $("count").classList.toggle("short", ranked < need);
  }
  function changed(inp, slotId, now) { setCell(slotId, inp.value); styleInput(inp); updateCount(); sendValue(now); }
  function clearColumn() {
    state.cells = {};
    state.inputs.forEach(function (inp) { inp.value = ""; styleInput(inp); });
    updateCount();
    sendValue(true);
  }

  // ---- the table: Date (first row of each day), Shift, Time, and the one cell ----------
  function render(args, disabled) {
    var table = $("sheet"), slots = args.slots || [], readonly = Boolean(args.readonly), n = slots.length;
    table.textContent = "";
    state.inputs = [];
    var thead = el("thead"), hr = el("tr");
    ["Date", "Shift", "Time"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
    var th = el("th", { "class": "person" }, args.person || "");
    if (!readonly && !disabled) {
      var clr = el("button", { "class": "clear", type: "button", title: "Clear this column" }, "clear");
      clr.addEventListener("click", clearColumn);
      th.appendChild(clr);
    }
    hr.appendChild(th); thead.appendChild(hr); table.appendChild(thead);
    var tbody = el("tbody");
    slots.forEach(function (s, si) {
      if (s.gap_before) {
        var gap = el("tr", { "class": "gap" });
        gap.appendChild(el("td", { colspan: "4" }, "no coverage (weekend or skipped day)"));
        tbody.appendChild(gap);
      }
      var first = si === 0 || Boolean(s.first_of_day);
      var last = si === n - 1 || Boolean(slots[si + 1].first_of_day);
      var tr = el("tr", last ? { "class": "dayend" } : null);
      tr.appendChild(el("td", first ? null : { "class": "dim" }, first ? s.short : ""));
      tr.appendChild(el("td", null, s.label + " (" + s.code + ")"));
      tr.appendChild(el("td", { "class": "dim time" }, s.time + ", " + s.hours + " h"));
      var td = el("td", { "class": "cell" }), v = state.cells[s.id] || "";
      if (readonly) {
        td.appendChild(el("span", { "class": "val" + (v === "X" ? " x" : (v ? " rank" : "")) }, v));
      } else {
        var inp = el("input", { type: "text", maxlength: "5", autocomplete: "off", autocorrect: "off",
                                autocapitalize: "off", spellcheck: "false", "data-slot": s.id,
                                "aria-label": s.weekday + " " + s.date + " " + s.label });
        inp.value = v;
        styleInput(inp);
        if (disabled) { inp.disabled = true; }
        inp.addEventListener("input", function () {
          var c = cleanValue(inp.value);
          if (inp.value !== c) { inp.value = c; }
          changed(inp, s.id, false);
        });
        inp.addEventListener("click", function () {
          if ($("clickrank").checked && inp.value === "" && (args.mode || "rank") === "rank") {
            inp.value = String(nextRank());
            changed(inp, s.id, false);
          }
        });
        inp.addEventListener("keydown", function (ev) {
          var step = (ev.key === "ArrowDown" || ev.key === "Enter") ? 1 : (ev.key === "ArrowUp" ? -1 : 0);
          if (!step) { return; }
          var target = state.inputs[si + step];
          if (target) { ev.preventDefault(); target.focus(); target.select(); }
        });
        inp.addEventListener("blur", function () { if (state.timer) { sendValue(true); } });
        state.inputs[si] = inp;
        td.appendChild(inp);
      }
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    $("toolbar").hidden = readonly;
    updateCount();
  }

  // ---- protocol -------------------------------------------------------------------------
  function signatureOf(args, disabled) {
    return JSON.stringify([(args.slots || []).map(function (s) { return s.id; }), args.person, args.mode,
                           Boolean(args.readonly), disabled]);
  }
  window.addEventListener("message", function (ev) {
    var data = ev.data || {};
    if (data.type !== "streamlit:render") { return; }
    var args = data.args || {}, disabled = Boolean(data.disabled), first = !state.applied;
    state.args = args;
    if (first) {
      state.cells = {};
      Object.keys(args.cells || {}).forEach(function (k) { if (args.cells[k]) { state.cells[k] = String(args.cells[k]); } });
      state.applied = true;
    }
    var sig = signatureOf(args, disabled);
    if (sig !== state.signature) { state.signature = sig; render(args, disabled); }
    sendHeight();
    if (first && !args.readonly) { sendValue(true); }
  });
  if (window.ResizeObserver) { new ResizeObserver(sendHeight).observe(document.body); }
  window.addEventListener("resize", sendHeight);
  send("streamlit:componentReady", { apiVersion: 1 });
})();
