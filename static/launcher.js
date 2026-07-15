// Launcher UI for llama-monitor: browse to a .gguf, set flags, launch/stop/
// restart llama-server, and save/load named configurations. Talks to the
// backend's /api/launcher/* , /api/configs and /api/browse routes.

(() => {
  const $ = (id) => document.getElementById(id);

  // Flags supported by the installed llama-server, fetched from the backend
  // (parsed from `llama-server --help`, or a bundled fallback). FLAG_INDEX maps
  // every alias -> {desc, value_hint} so a custom-typed known flag still shows
  // its description. FLAG_LIST is the alphabetised dropdown order.
  let FLAG_LIST = [];                 // [{flags, value_hint, desc}], sorted
  const FLAG_INDEX = Object.create(null);

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
      if (!flag) return;
      const rec = { flag, value };
      // Only record enabled when the flag is toggled OFF; omitting it when on
      // keeps re-saving an untouched (legacy) config byte-identical.
      if (!row.querySelector(".lx-en").checked) rec.enabled = false;
      flags.push(rec);
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
      // Normalise enabled on BOTH sides (default = enabled) so a legacy config
      // with no `enabled` key compares clean against a form row whose box is on.
      flags: (cfg.flags || []).map((f) => ({ flag: (f.flag || "").trim(),
                                             value: (f.value || "").trim(),
                                             enabled: f.enabled !== false })),
    });
  }
  function isDirty() {
    const cur = readForm();
    if (LX.loaded) return canon(cur) !== canon(LX.loaded);
    // No saved config loaded: dirty only if the user has entered something.
    return !!(cur.model_path || cur.name || cur.flags.length);
  }

  // Paint the per-row unsaved-change indicators (warm tint) and the global marks
  // (asterisk by the config select + emphasised Save) by comparing the live form
  // against the loaded baseline — or, with nothing loaded, the blank-form values
  // fillForm(null) produces (empty model/name, the default port).
  function updateDirtyUI() {
    const settings = (LX.state && LX.state.settings) || {};
    const defPort = Number(settings.default_port || 8001);
    const b = LX.loaded;

    const baseModel = b ? (b.model_path || "").trim() : "";
    const baseName = b ? (b.name || "").trim() : "";
    const basePort = b && b.port != null && b.port !== "" ? Number(b.port) : defPort;
    const curModel = $("lx-model").value.trim();
    const curName = $("lx-name").value.trim();
    const curPort = $("lx-port").value === "" ? defPort : Number($("lx-port").value);

    $("lx-model").closest(".lx-row").classList.toggle("dirty", curModel !== baseModel);
    $("lx-name").closest(".lx-row").classList.toggle("dirty", curName !== baseName);
    $("lx-port").closest(".lx-row").classList.toggle("dirty", curPort !== basePort);

    // Per flag-row positional compare against the baseline's flags (order matters
    // in argv, so a reorder is legitimately dirty).
    const baseFlags = (b && b.flags) || [];
    $("lx-flags").querySelectorAll(".lx-flag-row").forEach((row, i) => {
      const flag = row.querySelector(".lx-flag").value.trim();
      const value = row.querySelector(".lx-val").value.trim();
      const enabled = row.querySelector(".lx-en").checked;
      const bf = baseFlags[i];
      const clean = !!bf && (bf.flag || "").trim() === flag &&
        (bf.value || "").trim() === value && (bf.enabled !== false) === enabled;
      row.classList.toggle("dirty", !clean);
    });

    // Whole-array mismatch also tints the flags label/summary — this covers
    // deleted rows (which have no element to mark) and the collapsed view.
    const flagsDirty = canon({ flags: readFlags() }) !== canon({ flags: baseFlags });
    $("lx-flags-label").classList.toggle("dirty", flagsDirty);
    $("lx-flags-summary").classList.toggle("dirty", flagsDirty);

    const dirty = isDirty();
    $("lx-dirty-mark").hidden = !dirty;
    $("lx-save").classList.toggle("dirty", dirty);
  }
  // Default config name: alias flag (-a/--alias) value, else the .gguf basename.
  function defaultName() {
    const flags = readFlags();
    const alias = flags.find((f) => (f.flag === "-a" || f.flag === "--alias") &&
                                    f.enabled !== false);
    if (alias && alias.value) return alias.value;
    const base = basename($("lx-model").value.trim());
    return base.replace(/\.gguf$/i, "");
  }

  // --- flags editor ------------------------------------------------------ //
  function flagInfo(flag) {
    return FLAG_INDEX[(flag || "").trim()] || null;
  }
  function addFlagRow(flag = "", value = "", enabled = true) {
    const row = document.createElement("div");
    row.className = "lx-flag-row";
    // Controls cluster on the left (toggle, flag, value, ×); the description
    // fills the rest — so the × stays next to the inputs on wide monitors.
    row.innerHTML =
      `<input class="lx-en" type="checkbox" title="include this flag when launching" />` +
      `<input class="lx-flag" type="text" placeholder="flag" />` +
      `<input class="lx-val" type="text" placeholder="value" />` +
      `<button class="x" title="remove">×</button>` +
      `<span class="lx-flag-desc"></span>`;
    const enEl = row.querySelector(".lx-en");
    const flagEl = row.querySelector(".lx-flag");
    const valEl = row.querySelector(".lx-val");
    const descEl = row.querySelector(".lx-flag-desc");
    // Show the known-flag description after the value, and use its value hint as
    // the placeholder — both update live as the flag field is typed/changed.
    const sync = () => {
      const info = flagInfo(flagEl.value);
      descEl.textContent = info ? info.desc : "";
      valEl.placeholder = info && info.value_hint ? info.value_hint : "value";
    };
    const syncEnabled = () => row.classList.toggle("disabled", !enEl.checked);
    enEl.checked = enabled !== false;
    flagEl.value = flag;
    valEl.value = value;
    flagEl.addEventListener("input", sync);
    enEl.addEventListener("change", syncEnabled);
    row.querySelector(".x").addEventListener("click", () => {
      row.remove();
      updateDirtyUI();       // the delegated listener won't fire for a removed row
    });
    $("lx-flags").appendChild(row);
    sync();
    syncEnabled();
  }
  function renderFlags(flags) {
    $("lx-flags").innerHTML = "";
    (flags || []).forEach((f) => addFlagRow(f.flag, f.value, f.enabled !== false));
    if (flagsCollapsed()) renderFlagsSummary();   // keep the summary in sync
  }

  // --- collapse / expand the flags editor -------------------------------- //
  const FLAGS_COLLAPSED_KEY = "lx-flags-collapsed";
  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function flagsCollapsed() { return $("lx-flags-editor").hidden; }

  // Read-only view of the flags currently set (read from the editor rows, which
  // stay the source of truth even while hidden). Makes clear you must expand.
  function renderFlagsSummary() {
    const box = $("lx-flags-summary");
    const flags = readFlags();
    if (!flags.length) {
      box.innerHTML = `<span class="empty">No flags set</span>` +
                      `<span class="hint">▸ expand to add</span>`;
      return;
    }
    box.innerHTML = flags.map((f) =>
      `<span class="chip${f.enabled === false ? " disabled" : ""}">${esc(f.flag)}` +
      (f.value ? ` <span class="v">${esc(f.value)}</span>` : "") + `</span>`
    ).join("") + `<span class="hint">▸ expand to edit</span>`;
  }

  function setFlagsCollapsed(collapsed) {
    $("lx-flags-editor").hidden = collapsed;
    $("lx-flags-summary").hidden = !collapsed;
    $("lx-flags-toggle").classList.toggle("collapsed", collapsed);
    if (collapsed) renderFlagsSummary();
    try { localStorage.setItem(FLAGS_COLLAPSED_KEY, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
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
      renderDefault();
      updateDirtyUI();
      return;
    }
    const cfg = (LX.state.configs || []).find((c) => c.name === name);
    if (!cfg) return;
    LX.loaded = JSON.parse(JSON.stringify(cfg));
    fillForm(cfg);
    $("lx-config").value = name;
    setMsg("");
    renderDefault();
    updateDirtyUI();
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
    updateDirtyUI();       // saved -> baseline now matches the form (no longer dirty)
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
      // Re-parse the new binary's --help so the flag list reflects this build
      // (rather than a stale/bundled list from when the path was invalid).
      loadFlags();
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
    const def = (LX.state.settings || {}).default_config;
    (LX.state.configs || []).forEach((c) => {
      const o = document.createElement("option");
      o.value = c.name;
      o.textContent = (c.name === def ? "★ " : "") + c.name;   // mark the default
      sel.appendChild(o);
    });
    sel.value = keep;
  }
  // The ★ toggle: filled/active when the selected config is the default.
  function renderDefault() {
    const btn = $("lx-default");
    const name = $("lx-config").value;
    const def = ((LX.state && LX.state.settings) || {}).default_config;
    const isDef = !!name && def === name;
    btn.textContent = isDef ? "★" : "☆";
    btn.classList.toggle("active", isDef);
    btn.disabled = !name;
    btn.title = !name
      ? "Select a saved configuration to set it as the default"
      : isDef
        ? `Default — "${name}" auto-loads when no server is running (click to unset)`
        : `Set "${name}" as default (auto-loads when no server is running)`;
  }
  async function toggleDefault() {
    const name = $("lx-config").value;
    if (!name) { setMsg("Select a saved configuration first.", "bad"); return; }
    const cur = ((LX.state.settings) || {}).default_config;
    const next = cur === name ? "" : name;   // clicking the current default clears it
    try {
      applyState(await postJSON("/api/configs/default", { name: next }));
      setMsg(next ? `"${name}" is now the default (auto-loads when idle).` : "Default cleared.", "good");
    } catch (e) { setMsg("Could not set default: " + e.message, "bad"); }
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
    renderDefault();
  }

  // Find the saved config that best matches a running server's model: first by
  // an enabled -a/--alias flag equal to the reported name, then by the model
  // file's basename (case-insensitive, for Windows paths).
  function findConfigForModel(configs, m) {
    const name = (m.name || "").trim();
    const base = (p) => basename(p || "").toLowerCase();
    if (name) {
      const byAlias = (configs || []).find((c) => (c.flags || []).some((f) =>
        (f.flag === "-a" || f.flag === "--alias") && f.enabled !== false &&
        (f.value || "").trim() === name));
      if (byAlias) return byAlias;
    }
    if (m.path) {
      const byPath = (configs || []).find((c) => base(c.model_path) === base(m.path));
      if (byPath) return byPath;
    }
    return null;
  }

  // Initial form load:
  //  * Server running & launched by us  -> load the config it was launched with
  //    (status.config_name), so reopening the page restores that config.
  //  * Server running, no name match    -> match the live model by alias / file.
  //  * Idle                             -> auto-load the default/favourite config.
  async function loadInitial() {
    const st = (LX.state.status) || {};
    const configs = LX.state.configs || [];

    if (st.state === "running") {
      const byName = st.config_name && configs.find((c) => c.name === st.config_name);
      if (byName) {
        loadConfig(byName.name);
        setMsg(`Loaded "${byName.name}" (running server).`);
        return;
      }
      // No launched-config record (e.g. an externally started / adopted server):
      // match the live model reported by /api/stats.
      try {
        const stats = await getJSON("/api/stats?lite=1");
        const match = findConfigForModel(LX.state.configs || [], stats.model || {});
        if (match) {
          loadConfig(match.name);
          setMsg(`Loaded "${match.name}" (matches the running model).`);
          return;
        }
      } catch (e) { /* server unreachable -> fall through to a blank form */ }
      fillForm(null);
      renderDefault();
      updateDirtyUI();
      return;
    }

    // Idle: auto-load the default/favourite config if it still exists.
    const def = (LX.state.settings || {}).default_config;
    if (def && configs.some((c) => c.name === def)) {
      loadConfig(def);
      setMsg(`Loaded default configuration "${def}".`);
    } else {
      fillForm(null);
      renderDefault();
      updateDirtyUI();
    }
  }

  // --- combobox flag picker ---------------------------------------------- //
  // A filterable text input + dropdown replacing the 200+-option <select>: type
  // to narrow by a "contains" match over aliases and descriptions, keyboard
  // navigate, and still add any custom (unlisted) flag by typing it.
  const COMBO_CAP = 50;                 // most matches shown at once
  let comboItems = [];                  // the currently-filtered FLAG_LIST slice
  let comboSel = -1;                    // highlighted index (-1 = none)

  function comboOpen() { return !$("lx-flag-list").hidden; }

  function filterFlags(q) {
    q = (q || "").trim().toLowerCase();
    if (!q) return FLAG_LIST.slice(0, COMBO_CAP);
    const pre = [], aliasHit = [], descHit = [];
    for (const f of FLAG_LIST) {
      const aliases = (f.flags || []).map((a) => a.toLowerCase());
      const stripped = aliases.map((a) => a.replace(/^-+/, ""));
      if (stripped.some((a) => a.startsWith(q)) || aliases.some((a) => a.startsWith(q)))
        pre.push(f);
      else if (aliases.some((a) => a.includes(q))) aliasHit.push(f);
      else if ((f.desc || "").toLowerCase().includes(q)) descHit.push(f);
      if (pre.length >= COMBO_CAP) break;
    }
    return pre.concat(aliasHit, descHit).slice(0, COMBO_CAP);
  }

  function renderComboList() {
    const box = $("lx-flag-list");
    if (!comboItems.length) {
      box.innerHTML = `<div class="lx-combo-empty">no matches — “Add flag” inserts it as a custom flag</div>`;
      return;
    }
    box.innerHTML = comboItems.map((f, i) => {
      const hint = f.value_hint ? " " + esc(f.value_hint) : "";
      const short = f.desc && f.desc.length > 90 ? f.desc.slice(0, 89) + "…" : (f.desc || "");
      const desc = short ? ` <span class="d">— ${esc(short)}</span>` : "";
      return `<div class="lx-combo-item${i === comboSel ? " sel" : ""}" role="option" data-i="${i}">` +
             `<span class="f">${esc(f.flags.join(", "))}${hint}</span>${desc}</div>`;
    }).join("");
  }

  function openCombo() {
    comboItems = filterFlags($("lx-flag-pick").value);
    comboSel = comboItems.length ? 0 : -1;
    renderComboList();
    $("lx-flag-list").hidden = false;
    $("lx-flag-pick").setAttribute("aria-expanded", "true");
  }
  function closeCombo() {
    $("lx-flag-list").hidden = true;
    $("lx-flag-pick").setAttribute("aria-expanded", "false");
  }
  function moveCombo(delta) {
    if (!comboItems.length) return;
    comboSel = (comboSel + delta + comboItems.length) % comboItems.length;
    renderComboList();
    const el = $("lx-flag-list").querySelector(".lx-combo-item.sel");
    if (el) el.scrollIntoView({ block: "nearest" });
  }
  // Add a flag row from a picked FLAG_LIST entry (single gesture: pick -> row).
  function pickComboItem(i) {
    const f = comboItems[i];
    if (!f) return;
    addFlagRow(f.flags[0], "");
    $("lx-flag-pick").value = "";
    closeCombo();
    $("lx-flag-pick").focus();
    updateDirtyUI();
  }
  // Resolve the raw typed text to a flag: exact known alias as-is, else try the
  // --/- prefixed forms, else add it verbatim as a custom flag.
  function addFromCombo() {
    const raw = $("lx-flag-pick").value.trim();
    if (!raw) return;
    let flag = raw;
    if (!flagInfo(raw)) {
      if (flagInfo("--" + raw)) flag = "--" + raw;
      else if (flagInfo("-" + raw)) flag = "-" + raw;
    }
    addFlagRow(flag, "");
    $("lx-flag-pick").value = "";
    closeCombo();
    $("lx-flag-pick").focus();
    updateDirtyUI();
  }

  // Sort key: the long flag (--…) if present, else the first alias, stripped of
  // leading dashes so "-c"/"--ctx-size" sort under "ctx-size".
  function flagSortKey(f) {
    return (f.flags.find((x) => x.startsWith("--")) || f.flags[0] || "")
      .replace(/^-+/, "").toLowerCase();
  }

  async function loadFlags() {
    let data;
    try { data = await getJSON("/api/launcher/flags"); }
    catch (e) { data = { flags: [], source: "bundled" }; }
    const list = (data && data.flags) || [];

    for (const k in FLAG_INDEX) delete FLAG_INDEX[k];
    list.forEach((f) => {
      (f.flags || []).forEach((alias) => {
        FLAG_INDEX[alias] = { desc: f.desc || "", value_hint: f.value_hint || null };
      });
    });
    FLAG_LIST = list.filter((f) => f.flags && f.flags.length)
                    .sort((a, b) => flagSortKey(a).localeCompare(flagSortKey(b)));

    // Warn when we're on the small bundled fallback (binary path unset/invalid)
    // rather than the full list parsed from the user's build.
    $("lx-flags-src").hidden = ((data && data.source) || "bundled") === "help";

    // Backfill descriptions on rows rendered before the flags arrived.
    $("lx-flags").querySelectorAll(".lx-flag").forEach((el) =>
      el.dispatchEvent(new Event("input")));
  }

  // --- console viewer ---------------------------------------------------- //
  let consoleTimer = null;
  let consoleOffset = 0;
  let consoleStick = true;        // keep pinned to the bottom while tailing
  let consoleHasContent = false;
  const ANSI = /\x1b\[[0-9;]*m/g;  // llama-server colourises its log; strip it

  function openConsole() {
    const out = $("console-out");
    out.textContent = "";
    consoleOffset = 0;
    consoleStick = true;
    consoleHasContent = false;
    $("console-modal").hidden = false;
    out.onscroll = () => {
      consoleStick = out.scrollHeight - out.scrollTop - out.clientHeight < 24;
    };
    pollConsole();
    consoleTimer = setInterval(pollConsole, 1000);
  }
  function closeConsole() {
    $("console-modal").hidden = true;
    if (consoleTimer) { clearInterval(consoleTimer); consoleTimer = null; }
  }
  async function pollConsole() {
    let data;
    try { data = await getJSON(`/api/launcher/console?offset=${consoleOffset}`); }
    catch (e) { return; }
    const out = $("console-out");
    $("console-path").textContent = data.path || "";
    if (data.content) {
      const text = data.content.replace(ANSI, "");
      // A backwards jump in offset means the log rotated/truncated -> replace.
      if (!consoleHasContent || data.offset < consoleOffset) out.textContent = text;
      else out.textContent += text;
      consoleHasContent = true;
      consoleOffset = data.offset;
      if (consoleStick) out.scrollTop = out.scrollHeight;
    } else {
      if (data.offset != null) consoleOffset = data.offset;
      if (!consoleHasContent) {
        out.textContent = data.available
          ? "(log is empty)"
          : "(no log yet — launch a server from the panel)";
      }
    }
  }

  // --- wire up ----------------------------------------------------------- //
  function init() {
    loadFlags();

    $("lx-head").addEventListener("click", (e) => {
      if (e.target.id === "lx-config" || e.target.closest("select")) return;
      const body = $("lx-body");
      body.hidden = !body.hidden;
      $("lx-toggle").textContent = body.hidden ? "▸" : "▾";
    });

    $("lx-bin-browse").addEventListener("click", changeBinary);
    $("lx-model-browse").addEventListener("click", changeModel);

    // Combobox flag picker.
    $("lx-flag-add").addEventListener("click", addFromCombo);
    const pick = $("lx-flag-pick");
    pick.addEventListener("input", () => { openCombo(); });
    pick.addEventListener("focus", () => { openCombo(); });
    pick.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { if (!comboOpen()) openCombo(); else moveCombo(1); e.preventDefault(); }
      else if (e.key === "ArrowUp") { moveCombo(-1); e.preventDefault(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (comboOpen() && comboSel >= 0) pickComboItem(comboSel);
        else addFromCombo();
      } else if (e.key === "Escape") { closeCombo(); }
    });
    pick.addEventListener("blur", () => closeCombo());
    // Select on mousedown (before blur) + preventDefault so the input keeps focus
    // and the pick lands — the classic combobox blur-vs-click fix, no timers.
    $("lx-flag-list").addEventListener("mousedown", (e) => {
      const item = e.target.closest(".lx-combo-item");
      if (!item) return;               // clicking the empty-state row: ignore
      e.preventDefault();
      pickComboItem(Number(item.dataset.i));
    });

    // Recompute the unsaved-change indicators on any form edit. `change` (not
    // just `input`) is needed for the flag enable checkboxes.
    $("lx-body").addEventListener("input", updateDirtyUI);
    $("lx-body").addEventListener("change", updateDirtyUI);
    // Collapse/expand the flag editor: the label/caret toggles, and clicking the
    // read-only summary expands straight into the editor.
    $("lx-flags-label").addEventListener("click", () => setFlagsCollapsed(!flagsCollapsed()));
    $("lx-flags-summary").addEventListener("click", () => setFlagsCollapsed(false));
    let startCollapsed = false;
    try { startCollapsed = localStorage.getItem(FLAGS_COLLAPSED_KEY) === "1"; } catch (e) { /* ignore */ }
    setFlagsCollapsed(startCollapsed);
    $("lx-config").addEventListener("change", (e) => guardSwitch(e.target.value));
    $("lx-launch").addEventListener("click", doLaunch);
    $("lx-stop").addEventListener("click", doStop);
    $("lx-restart").addEventListener("click", doRestart);
    $("lx-console").addEventListener("click", openConsole);
    $("lx-save").addEventListener("click", doSave);
    $("lx-saveas").addEventListener("click", doSaveAsNew);
    $("lx-delete").addEventListener("click", doDelete);
    $("lx-default").addEventListener("click", toggleDefault);
    $("lx-modal").addEventListener("click", (e) => {
      if (e.target.id === "lx-modal") closeModal();   // click backdrop to dismiss
    });
    $("console-close").addEventListener("click", closeConsole);
    $("console-modal").addEventListener("click", (e) => {
      if (e.target.id === "console-modal") closeConsole();
    });

    // Warn before closing/reloading only when there are unsaved configuration
    // edits. A launched server is intentionally left running either way (it's a
    // detached process), so a running server alone is not a reason to warn.
    window.addEventListener("beforeunload", (e) => {
      if (isDirty()) {
        e.preventDefault();
        e.returnValue = "";   // required for the native confirmation to show
      }
    });

    refresh().then(loadInitial);
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
