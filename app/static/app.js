/* Video Search with Gemini — frontend */

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  selectedFile: null,
  currentVideoId: null,
};

// ── Config ───────────────────────────────────────────────────────────────
(async () => {
  try {
    const c = await (await fetch("/api/config")).json();
    $("#configLine").textContent =
      `catalog=${c.catalog} | schema=${c.schema} | gemini=${c.gemini_endpoint} | embed=${c.embedding_endpoint} | vs_index=${c.vs_index}`;
  } catch (e) { /* ignore */ }
})();

// ── File selection ───────────────────────────────────────────────────────
const dz = $("#dropzone");
const fileInput = $("#fileInput");
const fileName = $("#fileName");
const uploadBtn = $("#uploadBtn");

dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault(); dz.classList.remove("drag");
  if (e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", (e) => {
  if (e.target.files[0]) selectFile(e.target.files[0]);
});

function selectFile(f) {
  state.selectedFile = f;
  fileName.textContent = `${f.name}  (${(f.size / 1024 / 1024).toFixed(1)} MB)`;
  uploadBtn.disabled = false;
}

// ── Section 1: Upload + Process ──────────────────────────────────────────
uploadBtn.addEventListener("click", async () => {
  if (!state.selectedFile) return;
  uploadBtn.disabled = true;
  setStatus("#uploadStatus", "spinner", "アップロード中…");
  $("#uploadLogs").classList.remove("hidden");
  $("#uploadLogs").innerHTML = "";
  appendLog("#uploadLogs", "1/2: アップロード開始");

  const fd = new FormData();
  fd.append("file", state.selectedFile);

  let uploaded;
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text());
    uploaded = await r.json();
    state.currentVideoId = uploaded.video_id;
    appendLog("#uploadLogs", `アップロード完了 video_id=${uploaded.video_id}  duration=${uploaded.duration.toFixed(1)}s`);
  } catch (e) {
    setStatus("#uploadStatus", "err", "アップロード失敗");
    appendLog("#uploadLogs", `ERROR: ${e.message}`, "err");
    uploadBtn.disabled = false;
    return;
  }

  setStatus("#uploadStatus", "spinner", "シーン分割中…");
  appendLog("#uploadLogs", "2/2: PySceneDetect でシーン分割 & 音声抽出");

  try {
    const r = await fetch(`/api/process/${uploaded.video_id}`, { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    appendLog("#uploadLogs", `完了: ${data.num_scenes} シーン`);

    // 解析ログを表示
    const job = await (await fetch(`/api/jobs/proc-${uploaded.video_id}`)).json();
    (job.events || []).forEach(e => appendLog("#uploadLogs", e.message, e.level));

    setStatus("#uploadStatus", "ok", `${data.num_scenes} シーン作成`);

    // Section 2 を有効化し、自動で開始 (= section 1 -> section 2 必須フロー)
    $("#section-analyze").classList.remove("disabled");
    $("#analyzeBtn").disabled = false;
    $("#analyzeBanner").textContent = "Section 1 完了。自動で Gemini 解析を開始します…";
    refreshLibrary();
    setTimeout(() => runAnalyze(uploaded.video_id), 800);
  } catch (e) {
    setStatus("#uploadStatus", "err", "シーン分割失敗");
    appendLog("#uploadLogs", `ERROR: ${e.message}`, "err");
  } finally {
    uploadBtn.disabled = false;
  }
});

// ── Section 2: Analyze + Sync ────────────────────────────────────────────
$("#analyzeBtn").addEventListener("click", () => {
  if (state.currentVideoId) runAnalyze(state.currentVideoId);
});

async function runAnalyze(videoId) {
  $("#analyzeBtn").disabled = true;
  setStatus("#analyzeStatus", "spinner", `Gemini 解析中 (video_id=${videoId})…`);
  $("#analyzeLogs").classList.remove("hidden");
  $("#analyzeLogs").innerHTML = "";
  setSyncState("hidden");

  let analyzedBefore = await getIndexedRowCount();

  try {
    const r = await fetch(`/api/analyze/${videoId}`, { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();

    const job = await (await fetch(`/api/jobs/ana-${videoId}`)).json();
    (job.events || []).forEach(e => appendLog("#analyzeLogs", e.message, e.level));
    appendLog("#analyzeLogs", `${data.analyzed} シーンを解析しました。同期待機中…`);

    setStatus("#analyzeStatus", "ok", `${data.analyzed} シーン解析完了。Sync 待機中…`);
    $("#analyzeBanner").textContent = "Vector Search への同期中。完了すると検索可能になります。";

    await pollIndexStatus(data.analyzed);
    refreshLibrary();
  } catch (e) {
    setStatus("#analyzeStatus", "err", "解析失敗");
    setSyncState("err", { title: "同期エラー", detail: e.message });
    appendLog("#analyzeLogs", `ERROR: ${e.message}`, "err");
  } finally {
    $("#analyzeBtn").disabled = false;
  }
}

async function getIndexedRowCount() {
  try {
    const s = await (await fetch("/api/index/status")).json();
    return s.indexed_row_count ?? 0;
  } catch (e) { return 0; }
}

function setSyncState(kind, info) {
  const card = $("#syncCard");
  const icon = $("#syncIcon");
  const title = $("#syncTitle");
  const detail = $("#syncDetail");
  const bar = $("#syncBarFill");
  card.classList.remove("hidden", "running", "done", "err");
  if (kind === "hidden") { card.classList.add("hidden"); return; }
  card.classList.add(kind);
  if (info?.title) title.textContent = info.title;
  if (info?.detail) detail.textContent = info.detail;
  if (info?.icon) icon.textContent = info.icon;
  if (info?.fill != null) bar.style.width = `${Math.min(100, info.fill)}%`;
}

async function pollIndexStatus(expectedDelta) {
  setSyncState("running", {
    title: "Vector Search 同期中",
    detail: "Embedding をベクトルインデックスに反映しています…",
    icon: "⏳", fill: 5,
  });

  const startRows = await getIndexedRowCount();
  const target = startRows + (expectedDelta || 0);
  let lastState = "";

  for (let i = 0; i < 60; i++) {
    let s;
    try {
      s = await (await fetch("/api/index/status")).json();
    } catch (e) { await sleep(3000); continue; }

    const state = s.detailed_state || "";
    const rows = s.indexed_row_count ?? 0;
    lastState = state;

    // 進捗バーは「経過 or rows 進捗」のうち大きい方
    const rowsProgress = target > startRows
      ? Math.min(100, ((rows - startRows) / (target - startRows)) * 100)
      : 0;
    const timeProgress = Math.min(90, 5 + (i / 60) * 85);
    const fill = Math.max(rowsProgress, timeProgress);

    setSyncState("running", {
      title: "Vector Search 同期中",
      detail: `state=${state} | indexed_rows=${rows}${target ? "/" + target : ""}`,
      icon: "⏳", fill,
    });

    if (state.startsWith("ONLINE_NO_PENDING_UPDATE")) {
      setSyncState("done", {
        title: "Vector Search 同期 完了 ✓",
        detail: `indexed_rows=${rows} — 画面上部の検索バーから検索できます`,
        icon: "✅", fill: 100,
      });
      $("#analyzeBanner").textContent = "同期完了。画面上部の検索バーからシーンを検索できます。";
      setStatus("#analyzeStatus", "ok", `同期完了 (${rows} rows)`);
      return;
    }
    if (state.includes("FAILED")) {
      setSyncState("err", {
        title: "同期失敗", detail: state, icon: "✖", fill: 100,
      });
      return;
    }
    await sleep(3000);
  }
  // タイムアウト
  setSyncState("err", {
    title: "同期がタイムアウト",
    detail: `最後の state: ${lastState}`,
    icon: "⏱", fill: 100,
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Library ───────────────────────────────────────────────────────────────
$("#refreshLibrary").addEventListener("click", refreshLibrary);
refreshLibrary();

async function refreshLibrary() {
  try {
    const data = await (await fetch("/api/videos")).json();
    const tbody = $("#libraryTable tbody");
    tbody.innerHTML = "";
    (data.videos || []).forEach(v => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code>${v.video_id}</code></td>
        <td>${v.filename || ""}</td>
        <td>${v.duration ? v.duration.toFixed(1) + "s" : ""}</td>
        <td>${v.num_scenes ?? "-"} / ${v.analyzed_scenes ?? 0}</td>
        <td>${v.status || ""}</td>
        <td><button class="ghost" data-id="${v.video_id}">解析を実行</button></td>`;
      tr.querySelector("button").addEventListener("click", () => {
        state.currentVideoId = v.video_id;
        $("#section-analyze").classList.remove("disabled");
        runAnalyze(v.video_id);
        window.scrollTo({ top: $("#section-analyze").offsetTop - 80, behavior: "smooth" });
      });
      tbody.appendChild(tr);
    });
  } catch (e) { /* ignore */ }
}

// ── Section 3: Search (固定バー) ─────────────────────────────────────────
$("#searchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#searchInput").value.trim();
  if (!q) return;
  await runSearch(q);
});

async function runSearch(q) {
  $("#searchBtn").disabled = true;
  $("#searchBtn").textContent = "検索中…";
  $("#searchResults").classList.remove("hidden");
  $("#resultsBody").innerHTML = '<div class="muted" style="padding:12px">検索中…</div>';

  try {
    const r = await fetch("/api/search", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ query: q, num_results: 10 })
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    renderResults(data.results || []);
  } catch (e) {
    $("#resultsBody").innerHTML = `<div style="padding:12px;color:var(--err)">${e.message}</div>`;
  } finally {
    $("#searchBtn").disabled = false;
    $("#searchBtn").textContent = "検索";
  }
}

function renderResults(rows) {
  $("#resultsCount").textContent = `(${rows.length})`;
  if (!rows.length) {
    $("#resultsBody").innerHTML = '<div class="muted" style="padding:12px">該当シーンなし</div>';
    return;
  }
  $("#resultsBody").innerHTML = "";
  rows.forEach(r => {
    const card = document.createElement("div");
    card.className = "result-card";
    const score = r.search_score ?? r._score ?? r["score"];
    card.innerHTML = `
      <div class="meta">
        <span class="score">${score != null ? score.toFixed(3) : ""}</span>
        <span>video=${r.video_id}</span>
        <span>scene #${r.scene_index}</span>
        <span>${(r.start_sec ?? 0).toFixed(1)}-${(r.end_sec ?? 0).toFixed(1)}s</span>
      </div>
      <div class="summary">${escapeHTML(r.summary || "")}</div>
      <div class="features">tags: ${escapeHTML(r.features || "")}</div>
      <div class="transcript">${escapeHTML((r.transcript || "").slice(0, 200))}</div>`;
    card.addEventListener("click", () => openScene(r));
    $("#resultsBody").appendChild(card);
  });
}

function openScene(r) {
  const dlg = $("#videoModal");
  $("#modalVideo").src = `/api/scene/${r.scene_id}/video`;
  $("#modalCaption").innerHTML =
    `<strong>${escapeHTML(r.summary || "")}</strong><br>
     <span class="muted">tags: ${escapeHTML(r.features || "")}</span><br>
     <span class="muted">${escapeHTML(r.transcript || "")}</span>`;
  dlg.showModal();
}
$("#closeModal").addEventListener("click", () => {
  $("#videoModal").close();
  $("#modalVideo").pause();
  $("#modalVideo").src = "";
});
$("#closeResults").addEventListener("click", () => {
  $("#searchResults").classList.add("hidden");
  $("#toggleResults").style.display = "inline-block";
});
$("#toggleResults").addEventListener("click", () => {
  $("#searchResults").classList.remove("hidden");
  $("#toggleResults").style.display = "none";
});

// ── helpers ───────────────────────────────────────────────────────────────
function setStatus(sel, kind, msg) {
  const el = $(sel);
  el.className = `status ${kind}`;
  el.innerHTML = (kind === "spinner" ? '<span class="spinner"></span>' : "") + escapeHTML(msg);
}
function appendLog(sel, msg, level) {
  const el = $(sel);
  const r = document.createElement("div");
  r.className = `log-row ${level || ""}`;
  r.textContent = msg;
  el.appendChild(r);
  el.scrollTop = el.scrollHeight;
}
function escapeHTML(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, m => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]
  ));
}
