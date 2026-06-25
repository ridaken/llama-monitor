// Launcher UI for llama-monitor: browse to a .gguf, set flags, launch/stop/
// restart llama-server, and save/load named configurations. Talks to the
// backend's /api/launcher/* , /api/configs and /api/browse routes.

(() => {
  const $ = (id) => document.getElementById(id);

  // Common llama-server flags offered in the "Add flag" dropdown. Names only —
  // values are free text. The list isn't exhaustive; anything can be typed.
  const COMMON_FLAGS = [
    ["-c", "context size (n_ctx)"],
    ["-ngl", "GPU layers (n-gpu-layers)"],
    ["-fa", "flash attention (on/off)"],
    ["--parallel", "parallel slots"],
    ["-a", "model alias"],
    ["-ts", "tensor split (e.g. 3,1)"],
    ["-t", "CPU threads"],
    ["-b", "batch size"],
    ["-ub", "micro-batch size"],
    ["-np", "n parallel"],
    ["--no-mmap", "disable mmap (no value)"],
    ["--mlock", "lock model in RAM (no value)"],
    ["--spec-type", "speculative type (e.g. draft-mtp)"],
    ["-md", "draft model path"],
    ["--cache-type-k", "KV cache type, keys"],
    ["--cache-type-v", "KV cache type, values"],
    ["--host", "bind address"],
    ["--rope-scaling", "RoPE scaling type"],
  ];

  // Loaded = the saved config currently mirrored in the form (the clean
  // baseline for dirty detection), or null for a fresh / unsaved form.
  const LX = { state: null, loaded: null };

  // --- API helpers ------------------------------------------------------- //
  async function api(path, opts) {
    const r = await fetch(path, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
    return body;
  }
  const getJSON = (p) => api(p);
  const postJSON = (p, obj) =>
    api(p, { method: "POST", headers: { "Content-Type": "application/json" },
             body: JSON.stringify(obj || {}) });

  function setMsg(text, kind) {
    const el = $("lx-msg");
    el.textContent = text || "";
    el.className = "note" + (kind ? " " + kind : "");
  }

  // --- form <-> config --------------------------------------------------- //
  function basename(p) {
    if (!p) return "";
    const parts = p.split(/[\\/]/);
    return parts[parts.length - 1] || "";
  }
  function readFlags() {
    const flags = [];
    $("lx-flags").querySelectorAll(".lx-flag-row").forEach((row) => {
      const flag = row.querySelector(".lx-flag").value.trim();
      const value = row.querySelector(".lx-val").value.trim();
      if (flag) flags.push({ flag, value });
    });
    return flags;
  }
  function readForm() {
    return {
      name: $("lx-name").value.trim(),
      model_path: $("lx-model").value.trim(),
      port: $("lx-port").value === "" ? null : Number($("lx-port").value),
      flags: readFlags(),
    };
  }
  // Canonical JSON of the comparable fields, for dirty detection.
  function canon(cfg) {
    if (!cfg) return null;
    return JSON.stringify({
      name: (cfg.name || "").trim(),
      model_path: (cfg.model_path || "").trim(),
      port: cfg.port == null || cfg.port === "" ? null : Number(cfg.port),
      flags: (cfg.flags || []).map((f) => ({ flag: (f.flag || "").trim(),
                                             value: (f.value || "").trim() })),
    });
  }
  function isDirty() {
    const cur = readForm();
    if (LX.loaded) return canon(cur) !== canon(LX.loaded);
    // No saved config loaded: dirty only if the user has entered something.
    return !!(cur.model_path || cur.name || cur.flags.length);
  }
  // Default config name: alias flag (-a/--alias) value, else the .gguf basename.
  function defaultName() {
    const flags = readFlags();
    const alias = flags.find((f) => f.flag === "-a" || f.flag === "--alias");
    if (alias && alias.value) return alias.value;
    const base = basename($("lx-model").value.trim());
    return base.replace(/\.gguf$/i, "");
  }

  // --- flags editor ------------------------------------------------------ //
  function addFlagRow(flag = "", value = "") {
    const row = document.createElement("div");
    row.className = "lx-flag-row";
    row.innerHTML =
      `<input class="lx-flag" type="text" placeholder="flag" />` +
      `<input class="lx-val" type="text" placeholder="value (optional)" />` +
      `<button class="x" title="remove">×</button>`;
    row.querySelector(".lx-flag").value = flag;
    row.querySelector(".lx-val").value = value;
    row.querySelector(".x").addEventListener("click", () => row.remove());
    $("lx-flags").appendChild(row);
  }
  function renderFlags(flags) {
    $("lx-flags").innerHTML = "";
    (flags || []).forEach((f) => addFlagRow(f.flag, f.value));
  }

  // --- load a config into the form -------------------------------------- //
  function fillForm(cfg) {
    const settings = (LX.state && LX.state.settings) || {};
    $("lx-model").value = (cfg && cfg.model_path) || "";
    $("lx-port").value = (cfg && cfg.port) || settings.default_port || 8001;
    $("lx-name").value = (cfg && cfg.name) || "";
    renderFlags((cfg && cfg.flags) || []);
  }
  function loadConfig(name) {
    if (!name) {                       // "— New configuration —"
      LX.loaded = null;
      fillForm(null);
      $("lx-config").value = "";
      setMsg("");
      return;
    }
    const cfg = (LX.state.configs || []).find((c) => c.name === name);
    if (!cfg) return;
    LX.loaded = JSON.parse(JSON.stringify(cfg));
    fillForm(cfg);
    $("lx-config").value = name;
    setMsg("");
  }

  // --- modal helpers ----------------------------------------------------- //
  function closeModal() { $("lx-modal").hidden = true; }
  function openModal(title, bodyHtml, actions) {
    $("lx-modal-title").textContent = title;
    $("lx-modal-body").innerHTML = bodyHtml;
    const bar = $("lx-modal-actions");
    bar.innerHTML = "";
    actions.forEach((a) => {
      const b = document.createElement("button");
      b.className = "btn" + (a.primary ? " primary" : "");
      b.textContent = a.label;
      b.addEventListener("click", () => a.onClick());
      bar.appendChild(b);
    });
    $("lx-modal").hidden = false;
    return $("lx-modal-body");
  }

  // Prompt for a name (used by Save as new). Resolves to a trimmed name or null.
  function promptName(deflt) {
    return new Promise((resolve) => {
      const body = openModal(
        "Save as new configuration",
        `<input id="lx-name-input" class="lx-modal-input" type="text" />`,
        [
          { label: "Cancel", onClick: () => { closeModal(); resolve(null); } },
          { label: "Save", primary: true, onClick: () => {
              const v = body.querySelector("#lx-name-input").value.trim();
              if (!v) return;
              closeModal(); resolve(v);
            } },
        ]
      );
      const input = body.querySelector("#lx-name-input");
      input.value = deflt || "";
      input.focus();
      input.select();
    });
  }

  // --- file browser modal ------------------------------------------------ //
  // Returns the chosen file path, or null if cancelled. `ext` filters files.
  function browse(title, ext, startPath) {
    return new Promise((resolve) => {
      let resolved = false;
      const finish = (val) => { if (!resolved) { resolved = true; closeModal(); resolve(val); } };
      const body = openModal(
        title,
        `<div class="lx-browse-path" id="lx-browse-path"></div>
         <div class="lx-list" id="lx-browse-list"></div>`,
        [{ label: "Cancel", onClick: () => finish(null) }]
      );
      const pathEl = body.querySelector("#lx-browse-path");
      const listEl = body.querySelector("#lx-browse-list");

      async function go(path) {
        let data;
        try {
          data = await getJSON(`/api/browse?path=${encodeURIComponent(path || "")}` +
                               (ext ? `&ext=${encodeURIComponent(ext)}` : ""));
        } catch (e) {
          pathEl.textContent = "Cannot open: " + e.message;
          return;
        }
        pathEl.textContent = data.path || "This PC";
        listEl.innerHTML = "";
        if (data.parent !== null && data.parent !== undefined) {
          addItem("⮤", "..", () => go(data.parent), false);
        }
        (data.dirs || []).forEach((d) =>
          addItem("📁", basename(d) || d, () => go(d), false));
        (data.files || []).forEach((f) =>
          addItem("📄", basename(f), () => finish(f), true));
      }
      function addItem(icon, label, onClick, isFile) {
        const el = document.createElement("div");
        el.className = "lx-item" + (isFile ? " file" : "");
        el.innerHTML = `<span class="ic">${icon}</span><span>${label}</span>`;
        el.addEventListener("click", onClick);
        listEl.appendChild(el);
      }
      go(startPath || "");
    });
  }

  // --- dirty guard on config switch ------------------------------------- //
  function guardSwitch(targetName) {
    // Switching to the same selection or when not dirty: just load.
    if (!isDirty()) { loadConfig(targetName); return; }

    const overwriteLabel = LX.loaded ? `Save (overwrite "${LX.loaded.name}")` : "Save";
    openModal(
      "Unsaved changes",
      `<div class="note">You have unsaved changes. What would you like to do before switching?</div>`,
      [
        { label: "Discard changes", onClick: () => { closeModal(); loadConfig(targetName); } },
        { label: "Save as new…", onClick: async () => {
            closeModal();
            const ok = await doSaveAsNew();
            if (ok) loadConfig(targetName);
            else $("lx-config").value = LX.loaded ? LX.loaded.name : "";
          } },
        { label: overwriteLabel, primary: true, onClick: async () => {
            closeModal();
            const ok = await doSave();
            if (ok) loadConfig(targetName);
            else $("lx-config").value = LX.loaded ? LX.loaded.name : "";
          } },
      ]
    );
  }

  // --- save operations --------------------------------------------------- //
  async function persist(cfg) {
    const res = await postJSON("/api/configs", cfg);
    LX.state.configs = res.configs;
    renderConfigOptions();
    LX.loaded = JSON.parse(JSON.stringify(cfg));
    $("lx-config").value = cfg.name;
    return true;
  }
  async function doSave() {
    const cfg = readForm();
    if (!cfg.name) cfg.name = defaultName();
    if (!cfg.name) { setMsg("Enter a config name first.", "bad"); return false; }
    $("lx-name").value = cfg.name;
    try { await persist(cfg); setMsg(`Saved "${cfg.name}".`, "good"); return true; }
    catch (e) { setMsg("Save failed: " + e.message, "bad"); return false; }
  }
  async function doSaveAsNew() {
    const name = await promptName(defaultName());
    if (!name) return false;
    const cfg = readForm();
    cfg.name = name;
    $("lx-name").value = name;
    try { await persist(cfg); setMsg(`Saved "${name}".`, "good"); return true; }
    catch (e) { setMsg("Save failed: " + e.message, "bad"); return false; }
  }

  // --- launcher actions -------------------------------------------------- //
  async function doLaunch() {
    const cfg = readForm();
    if (!cfg.model_path) { setMsg("Select a model (.gguf) first.", "bad"); return; }
    if (!cfg.port) { setMsg("Port is required.", "bad"); return; }
    setMsg("Launching…");
    try {
      const res = await postJSON("/api/launcher/launch", cfg);
      applyState(res);
      setMsg("Launched. Monitoring the new server below.", "good");
    } catch (e) { setMsg("Launch failed: " + e.message, "bad"); }
  }
  async function doStop() {
    try { applyState(await postJSON("/api/launcher/stop", {})); setMsg("Stopped."); }
    catch (e) { setMsg("Stop failed: " + e.message, "bad"); }
  }
  async function doRestart() {
    setMsg("Restarting…");
    try { applyState(await postJSON("/api/launcher/restart", {})); setMsg("Restarted.", "good"); }
    catch (e) { setMsg("Restart failed: " + e.message, "bad"); }
  }
  async function doDelete() {
    const name = LX.loaded && LX.loaded.name;
    if (!name) { setMsg("No saved configuration selected to delete.", "bad"); return; }
    openModal("Delete configuration",
      `<div class="note">Delete the saved configuration "${name}"?</div>`,
      [
        { label: "Cancel", onClick: closeModal },
        { label: "Delete", primary: true, onClick: async () => {
            closeModal();
            try {
              const res = await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
              LX.state.configs = res.configs;
              renderConfigOptions();
              loadConfig("");
              setMsg(`Deleted "${name}".`);
            } catch (e) { setMsg("Delete failed: " + e.message, "bad"); }
          } },
      ]);
  }

  // --- binary path ------------------------------------------------------- //
  async function changeBinary() {
    const start = (LX.state.settings && LX.state.settings.llama_server_path) || "";
    const startDir = start ? start.replace(/[\\/][^\\/]*$/, "") : "";
    const picked = await browse("Select llama-server executable",
      navigator.platform.startsWith("Win") ? ".exe" : "", startDir);
    if (!picked) return;
    try {
      const res = await postJSON("/api/launcher/settings", { llama_server_path: picked });
      applyState(res);
      setMsg("llama-server path updated.", "good");
    } catch (e) { setMsg("Could not set path: " + e.message, "bad"); }
  }
  async function changeModel() {
    const settings = LX.state.settings || {};
    const start = $("lx-model").value.trim()
      ? $("lx-model").value.trim().replace(/[\\/][^\\/]*$/, "")
      : (settings.models_dir || "");
    const picked = await browse("Select a model (.gguf)", ".gguf", start);
    if (!picked) return;
    $("lx-model").value = picked;
    // Remember the directory and default the name if the user hasn't set one.
    const dir = picked.replace(/[\\/][^\\/]*$/, "");
    postJSON("/api/launcher/settings", { models_dir: dir }).catch(() => {});
    if (!$("lx-name").value.trim()) $("lx-name").value = defaultName();
  }

  // --- rendering state --------------------------------------------------- //
  function renderConfigOptions() {
    const sel = $("lx-config");
    const keep = sel.value;
    sel.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = ""; blank.textContent = "— New configuration —";
    sel.appendChild(blank);
    (LX.state.configs || []).forEach((c) => {
      const o = document.createElement("option");
      o.value = c.name; o.textContent = c.name;
      sel.appendChild(o);
    });
    sel.value = keep;
  }
  function renderBinary() {
    const s = LX.state.settings || {};
    $("lx-bin-path").textContent = s.llama_server_path || "not set";
    const valid = LX.state.binary_valid;
    $("lx-bin-warn").hidden = valid;
    $("lx-bin-msg").hidden = valid;
    $("lx-bin-path").className = "lx-path" + (valid ? " muted" : " lx-warn");
  }
  function renderStatus() {
    const st = (LX.state.status) || { state: "stopped" };
    const pill = $("lx-status");
    let text, cls;
    if (st.state === "running") {
      text = st.config_name ? `running · ${st.config_name}` : "running";
      cls = "on";
    } else if (st.state === "exited") {
      text = `exited (code ${st.exit_code})`;
      cls = "warn";
    } else {
      text = "stopped"; cls = "off";
    }
    pill.textContent = text;
    pill.className = "pill " + cls;
    $("lx-stop").disabled = st.state !== "running";
    $("lx-restart").disabled = !st.config_name;
    $("lx-launch").disabled = !LX.state.binary_valid;
  }
  // Apply backend state without clobbering the user's in-progress form edits.
  function applyState(state) {
    LX.state = state;
    renderConfigOptions();
    renderBinary();
    renderStatus();
  }

  // --- flag picker ------------------------------------------------------- //
  function initFlagPicker() {
    const sel = $("lx-flag-pick");
    const custom = document.createElement("option");
    custom.value = ""; custom.textContent = "— custom flag —";
    sel.appendChild(custom);
    COMMON_FLAGS.forEach(([flag, desc]) => {
      const o = document.createElement("option");
      o.value = flag; o.textContent = `${flag} — ${desc}`;
      sel.appendChild(o);
    });
  }

  // --- wire up ----------------------------------------------------------- //
  function init() {
    initFlagPicker();

    $("lx-head").addEventListener("click", (e) => {
      if (e.target.id === "lx-config" || e.target.closest("select")) return;
      const body = $("lx-body");
      body.hidden = !body.hidden;
      $("lx-toggle").textContent = body.hidden ? "▸" : "▾";
    });

    $("lx-bin-browse").addEventListener("click", changeBinary);
    $("lx-model-browse").addEventListener("click", changeModel);
    $("lx-flag-add").addEventListener("click", () => {
      addFlagRow($("lx-flag-pick").value, "");
    });
    $("lx-config").addEventListener("change", (e) => guardSwitch(e.target.value));
    $("lx-launch").addEventListener("click", doLaunch);
    $("lx-stop").addEventListener("click", doStop);
    $("lx-restart").addEventListener("click", doRestart);
    $("lx-save").addEventListener("click", doSave);
    $("lx-saveas").addEventListener("click", doSaveAsNew);
    $("lx-delete").addEventListener("click", doDelete);
    $("lx-modal").addEventListener("click", (e) => {
      if (e.target.id === "lx-modal") closeModal();   // click backdrop to dismiss
    });

    refresh().then(() => fillForm(null));
    // Poll status so a server that exits on its own (bad flag/OOM) is reflected.
    setInterval(refresh, 3000);
  }

  async function refresh() {
    try { applyState(await getJSON("/api/launcher/state")); }
    catch (e) { /* dashboard may be momentarily unreachable */ }
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();
})();
