// Sortable, filterable view of generations observed in llama-server logs.
(() => {
  const $ = id => document.getElementById(id);
  const fields = ["model", "state", "from", "to", "sort", "order"];
  let pageCursors = [null];
  let page = 0;
  let nextCursor = null;
  const fmt = (v, digits = 0) => v == null ? "—" : Number(v).toFixed(digits);

  function closePrompt() { $("history-prompt-panel").hidden = true; }
  function reset() { closePrompt(); pageCursors = [null]; page = 0; load(); }
  function params() {
    const p = new URLSearchParams({sort: $("history-sort").value, order: $("history-order").value});
    if ($("history-model").value) p.set("model", $("history-model").value);
    if ($("history-state").value) p.set("state", $("history-state").value);
    if ($("history-from").value) p.set("from_ts", String(new Date($("history-from").value + "T00:00:00").getTime()/1000));
    if ($("history-to").value) {
      const end = new Date($("history-to").value + "T00:00:00");
      end.setDate(end.getDate() + 1);
      p.set("to_ts", String(end.getTime()/1000));
    }
    if (pageCursors[page]) p.set("cursor", pageCursors[page]);
    return p;
  }
  function cell(row, value) {
    const td = document.createElement("td");
    td.textContent = value;
    row.appendChild(td);
  }
  async function showPrompt(item) {
    const panel = $("history-prompt-panel");
    const output = $("history-prompt-text");
    panel.hidden = false;
    output.textContent = "Loading prompt…";
    $("history-prompt-note").textContent = item.state === "prompt_only"
      ? "No reliable timing match for this prompt." : "";
    try {
      const response = await fetch(`/api/history/${encodeURIComponent(item.id)}/prompt`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      output.textContent = data.prompt_text;
      if (data.truncated) $("history-prompt-note").textContent += " First 1 MiB saved; prompt was longer.";
    } catch (error) { output.textContent = "Could not load prompt: " + error.message; }
  }
  async function load() {
    try {
      const response = await fetch("/api/history?" + params());
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const oldModel = $("history-model").value;
      $("history-model").replaceChildren(new Option("All models", ""),
        ...data.models.map(m => new Option(m, m)));
      $("history-model").value = oldModel;
      nextCursor = data.next_cursor;
      $("history-prev").disabled = page === 0;
      $("history-next").disabled = !nextCursor;
      $("history-path").textContent = "Database: " + data.database_path;
      const status = data.log_status;
      $("history-status").textContent = status.gap ?
        "Coverage gap: a log segment was unavailable or rotated before it could be read." :
        !status.configured ? "No timing log attached; prompt files can still be saved when configured." :
        !status.available ? "Log unavailable; live monitoring uses HTTP until it returns." :
        status.prompts_configured ?
          "Observed generations and native prompt files. Unmatched prompts appear as Prompt only." :
          "Observed generations. Enable prompt logging in the launch configuration to save prompt text.";
      const body = $("history-body");
      body.replaceChildren();
      for (const item of data.items) {
        const tr = document.createElement("tr");
        cell(tr, new Date(item.observed_at*1000).toLocaleString());
        cell(tr, item.model || "—");
        cell(tr, item.state === "prompt_only" ? "Prompt only" : item.state);
        cell(tr, `${item.run_id.slice(0,8)} / ${item.slot_id ?? "?"} / ${item.task_id ?? "?"}`);
        const promptCell = document.createElement("td");
        if (item.has_prompt) {
          const button = document.createElement("button");
          button.className = "btn";
          button.textContent = "View prompt";
          button.addEventListener("click", () => showPrompt(item));
          promptCell.appendChild(button);
        } else promptCell.textContent = "—";
        tr.appendChild(promptCell);
        cell(tr, fmt(item.prompt_tokens));
        cell(tr, fmt(item.generated_tokens));
        cell(tr, item.prompt_seconds == null ? "—" : fmt(item.prompt_seconds, 2) + "s");
        cell(tr, item.decode_seconds == null ? "—" : fmt(item.decode_seconds, 2) + "s");
        cell(tr, item.total_seconds == null ? "—" : fmt(item.total_seconds, 2) + "s");
        cell(tr, item.draft_generated == null ? "—" : `${item.draft_accepted}/${item.draft_generated}`);
        body.appendChild(tr);
      }
      if (!data.items.length) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 11;
        td.textContent = "No observed generations match these filters.";
        tr.appendChild(td);
        body.appendChild(tr);
      }
    } catch (error) { $("history-status").textContent = "Could not load history: " + error.message; }
  }
  $("history-open").addEventListener("click", () => { $("history-modal").hidden = false; reset(); });
  $("history-close").addEventListener("click", () => { $("history-modal").hidden = true; });
  $("history-prompt-close").addEventListener("click", closePrompt);
  $("history-modal").addEventListener("click", e => { if (e.target.id === "history-modal") $("history-modal").hidden = true; });
  for (const field of fields) $("history-" + field).addEventListener("change", reset);
  $("history-next").addEventListener("click", () => { if (nextCursor) { pageCursors[++page] = nextCursor; load(); } });
  $("history-prev").addEventListener("click", () => { if (page > 0) { page--; load(); } });
  $("history-clear").addEventListener("click", async () => {
    if (!confirm("Delete saved generations and prompt text? This cannot be undone. Managed prompt files will also be removed; external server files are left alone.")) return;
    try {
      const response = await fetch("/api/history", {method:"DELETE"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      reset();
    } catch (error) { $("history-status").textContent = "Could not clear history: " + error.message; }
  });
})();
