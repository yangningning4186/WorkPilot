const API = "/api/v1/annotation";
const state = { datasets: [], dataset: null, documents: [], document: null, blocks: [], blockTotal: 0, items: [], spans: [], editingId: null, tab: "documents" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, { headers: { "content-type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { const payload = await response.json(); detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail); } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function escapeHtml(value = "") { const node = document.createElement("div"); node.textContent = value; return node.innerHTML; }
function short(value = "") { return value.length > 120 ? `${value.slice(0, 120)}…` : value; }
function terms(value) { return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean); }
function toast(message) { const node = $("#toast"); node.textContent = message; node.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("show"), 2600); }
function setSaveState(label, kind) { const node = $("#saveState"); node.textContent = label; node.className = `save-state ${kind}`; }
function setError(message = "") { $("#formError").textContent = message; $("#formError").classList.toggle("hidden", !message); if (message) setSaveState("需要修正", "error"); }
function debounce(fn, wait = 260) { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); }; }

async function initialize() {
  try {
    state.datasets = await api("/datasets");
    let preferred = state.datasets.find((item) => item.name === "core-dev");
    if (!preferred) {
      await api("/datasets", { method: "POST", body: JSON.stringify({ name: "core-dev", split: "dev", version: "1", description: "人工确认的 dense/RAG 开发集" }) });
      state.datasets = await api("/datasets");
      preferred = state.datasets.find((item) => item.name === "core-dev");
    }
    state.dataset = preferred || state.datasets[0];
    renderDatasets();
    await Promise.all([loadDocuments(), loadItems()]);
  } catch (error) { setError(error.message); }
}

function renderDatasets() {
  $("#datasetSelect").innerHTML = state.datasets.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · v${escapeHtml(item.version)}</option>`).join("");
  $("#datasetSelect").value = state.dataset?.id || "";
  const count = state.dataset?.valid_count || 0;
  $("#progressCount").textContent = `${count} / 20`;
  $("#progressBar").style.width = `${Math.min(100, count / 20 * 100)}%`;
}

async function loadDocuments() {
  state.documents = await api(`/documents?query=${encodeURIComponent($("#documentSearch").value)}`);
  $("#docCount").textContent = state.documents.length;
  $("#documentList").innerHTML = state.documents.map((doc) => `<button type="button" class="rail-card ${state.document?.version_id === doc.version_id ? "active" : ""}" data-version="${doc.version_id}"><strong>${escapeHtml(doc.title)}</strong><small><span>${escapeHtml(doc.source_uri)}</span><span>${doc.block_count} blocks</span></small></button>`).join("") || `<p class="inline-empty">没有匹配资料</p>`;
  $$("#documentList [data-version]").forEach((button) => button.addEventListener("click", () => selectDocument(button.dataset.version)));
}

async function selectDocument(versionId) {
  state.document = state.documents.find((doc) => doc.version_id === versionId);
  state.blocks = [];
  $("#documentTitle").textContent = state.document.title;
  $("#documentMeta").textContent = `${state.document.source_uri} · ${state.document.parser} ${state.document.parser_version} · ${state.document.block_count} blocks`;
  $("#openSourceButton").classList.remove("hidden");
  $("#openSourceButton").onclick = () => window.open(`${API}/documents/${versionId}/file`, "_blank", "noopener");
  await loadBlocks(true);
  renderDocuments();
}

async function loadBlocks(reset = false) {
  if (!state.document) return;
  const offset = reset ? 0 : state.blocks.length;
  const params = new URLSearchParams({ offset, limit: 50, query: $("#blockSearch").value, block_type: $("#blockType").value });
  const page = await api(`/documents/${state.document.version_id}/blocks?${params}`);
  state.blocks = reset ? page.items : [...state.blocks, ...page.items];
  state.blockTotal = page.total;
  renderBlocks();
}

function renderDocuments() { loadDocuments().catch((error) => setError(error.message)); }

function renderBlocks() {
  const list = $("#blockList");
  list.classList.remove("empty-state");
  if (!state.blocks.length) { list.innerHTML = `<p class="inline-empty">当前筛选没有 block。</p>`; return; }
  list.innerHTML = state.blocks.map((block) => {
    const pages = [...new Set(block.locations.map((item) => item.page_no))];
    return `<article class="block-card" data-block="${block.block_id}"><header class="block-header"><span><b class="block-type">${escapeHtml(block.block_type)}</b> · #${block.block_idx}</span><span>${pages.length ? `P.${pages.join(", ")}` : "无页面"} · ${block.char_start}–${block.char_end}</span></header><pre class="block-text">${escapeHtml(block.text)}</pre><footer class="block-actions"><button type="button" data-action="preview">查看定位</button><button type="button" data-action="selection">加入选区</button><button type="button" data-action="whole">整块证据</button></footer></article>`;
  }).join("");
  $$(".block-card").forEach((card) => {
    const block = state.blocks.find((item) => item.block_id === card.dataset.block);
    card.querySelector('[data-action="preview"]').addEventListener("click", () => previewBlock(block));
    card.querySelector('[data-action="whole"]').addEventListener("click", () => addWholeBlock(block));
    card.querySelector('[data-action="selection"]').addEventListener("click", () => addSelection(block, card.querySelector(".block-text")));
  });
  $("#loadMoreButton").classList.toggle("hidden", state.blocks.length >= state.blockTotal);
  $("#loadMoreButton").textContent = `加载更多（${state.blocks.length} / ${state.blockTotal}）`;
}

async function addWholeBlock(block) { await resolveAndAdd(block, 0, block.text.length, block.text); }

async function addSelection(block, textNode) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return toast("请先在当前 block 内拖选原文");
  const range = selection.getRangeAt(0);
  if (!textNode.contains(range.commonAncestorContainer)) return toast("选区必须完全位于当前 block");
  const before = range.cloneRange(); before.selectNodeContents(textNode); before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length; const quote = range.toString();
  await resolveAndAdd(block, start, start + quote.length, quote);
  selection.removeAllRanges();
}

async function resolveAndAdd(block, start, end, quote) {
  try {
    const span = await api("/spans/resolve", { method: "POST", body: JSON.stringify({ block_id: block.block_id, utf16_start: start, utf16_end: end, quote }) });
    if (state.spans.some((item) => item.version_id === span.version_id && item.char_start === span.char_start && item.char_end === span.char_end)) return toast("这段证据已经添加");
    state.spans.push(span); renderSpans(); previewBlock(block); setSaveState("未保存", "idle");
  } catch (error) { setError(error.message); }
}

function renderSpans() {
  $("#spanCount").textContent = `${state.spans.length} 条证据`;
  $("#spanList").innerHTML = state.spans.length ? state.spans.map((span, index) => `<article class="span-card"><small>${escapeHtml(span.title || span.source_uri || span.version_id.slice(0, 8))} · ${span.char_start}–${span.char_end}</small><p>${escapeHtml(short(span.quote))}</p><button type="button" data-remove="${index}" aria-label="移除证据">×</button></article>`).join("") : `<p class="inline-empty">从中栏原文添加证据。</p>`;
  $$('[data-remove]').forEach((button) => button.addEventListener("click", () => { state.spans.splice(Number(button.dataset.remove), 1); renderSpans(); setSaveState("未保存", "idle"); }));
}

function previewBlock(block) {
  if (!block.locations.length || !state.document || state.document.page_count == null) { $("#previewLabel").textContent = "当前 block 没有 PDF 页面"; $("#pageStage").innerHTML = `<div class="preview-empty">Markdown 证据使用字符区间定位</div>`; return; }
  const pageNo = block.locations[0].page_no;
  $("#previewLabel").textContent = `第 ${pageNo} 页 · ${block.locations.length} 个位置`;
  const locations = block.locations.filter((item) => item.page_no === pageNo);
  $("#pageStage").innerHTML = `<img alt="PDF 第 ${pageNo} 页预览" src="${API}/documents/${state.document.version_id}/pages/${pageNo}.png" />${locations.map((location) => { const [x0,y0,x1,y1] = location.bbox_norm; return `<i class="bbox" style="left:${x0*100}%;top:${y0*100}%;width:${(x1-x0)*100}%;height:${(y1-y0)*100}%"></i>`; }).join("")}`;
}

async function loadItems() {
  if (!state.dataset) return;
  const payload = await api(`/items?dataset_id=${state.dataset.id}`); state.items = payload.items;
  $("#itemCount").textContent = payload.total;
  $("#itemList").innerHTML = state.items.map((item) => `<button type="button" class="rail-card ${state.editingId === item.id ? "active" : ""}" data-item="${item.id}"><strong>${escapeHtml(item.question)}</strong><small><span class="status-dot ${item.status}"></span><span>${item.category}</span><span>${item.status}</span></small></button>`).join("") || `<p class="inline-empty">还没有样本</p>`;
  $$('[data-item]').forEach((button) => button.addEventListener("click", () => editItem(button.dataset.item)));
  const dataset = state.datasets.find((item) => item.id === state.dataset.id); if (dataset) { dataset.item_count = payload.total; dataset.valid_count = state.items.filter((item) => item.status === "valid").length; dataset.stale_count = state.items.filter((item) => item.status === "stale").length; } renderDatasets();
}

function editItem(itemId) {
  const item = state.items.find((entry) => entry.id === itemId); if (!item) return;
  state.editingId = item.id; state.spans = item.gold_spans.map((span) => ({ ...span, title: "已保存证据" }));
  $("#itemId").value = item.id; $(`input[name="category"][value="${item.category}"]`).checked = true;
  $("#question").value = item.question; $("#goldAnswer").value = item.gold_answer || "";
  $("#mustInclude").value = item.must_include.join(", "); $("#mustNotInclude").value = item.must_not_include.join(", ");
  $("#difficulty").value = item.difficulty; $("#origin").value = item.origin;
  $("#deleteButton").classList.remove("hidden"); renderSpans(); syncAnswerability(); setSaveState(item.status === "stale" ? "证据失效" : "已保存", item.status === "stale" ? "error" : "saved");
  document.querySelector('.rail-tabs [data-tab="items"]').click();
}

function resetForm() {
  state.editingId = null; state.spans = []; $("#annotationForm").reset(); $("#itemId").value = ""; $("#deleteButton").classList.add("hidden"); renderSpans(); syncAnswerability(); setError(""); setSaveState("未保存", "idle");
}

function syncAnswerability() {
  const category = $('input[name="category"]:checked').value; const unavailable = category === "unanswerable";
  $("#spanRequirement").textContent = unavailable ? "必须为空" : "至少 1 条"; $("#answerRequirement").textContent = unavailable ? "留空" : "必填";
  $("#goldAnswer").disabled = unavailable; $("#goldAnswer").placeholder = unavailable ? "不可答样本不填写答案" : "只写被所选证据完整支撑的答案…";
  setSaveState("未保存", "idle");
}

async function saveItem(event) {
  event.preventDefault(); setError(""); setSaveState("保存中", "saving");
  const category = $('input[name="category"]:checked').value;
  const payload = { dataset_id: state.dataset.id, category, question: $("#question").value, gold_answer: category === "unanswerable" ? null : $("#goldAnswer").value, gold_spans: state.spans.map(({version_id,char_start,char_end,quote,note}) => ({version_id,char_start,char_end,quote,note})), must_include: terms($("#mustInclude").value), must_not_include: terms($("#mustNotInclude").value), difficulty: Number($("#difficulty").value), origin: $("#origin").value };
  try {
    const path = state.editingId ? `/items/${state.editingId}` : "/items"; const method = state.editingId ? "PUT" : "POST";
    const saved = await api(path, { method, body: JSON.stringify(payload) }); state.editingId = saved.id; $("#itemId").value = saved.id; $("#deleteButton").classList.remove("hidden"); setSaveState("已保存", "saved"); await loadItems(); toast("金标已保存并通过 quote 校验");
  } catch (error) { setError(error.message); }
}

async function deleteCurrent() {
  if (!state.editingId || !confirm("删除这个评测样本？此操作只影响标注，不会修改原始资料。")) return;
  try { await api(`/items/${state.editingId}`, { method: "DELETE" }); resetForm(); await loadItems(); toast("样本已删除"); } catch (error) { setError(error.message); }
}

function bindEvents() {
  $("#datasetSelect").addEventListener("change", async (event) => { state.dataset = state.datasets.find((item) => item.id === event.target.value); resetForm(); await loadItems(); });
  $("#documentSearch").addEventListener("input", debounce(() => loadDocuments().catch((error) => setError(error.message))));
  $("#blockSearch").addEventListener("input", debounce(() => loadBlocks(true).catch((error) => setError(error.message))));
  $("#blockType").addEventListener("change", () => loadBlocks(true).catch((error) => setError(error.message)));
  $("#loadMoreButton").addEventListener("click", () => loadBlocks(false).catch((error) => setError(error.message)));
  $("#refreshButton").addEventListener("click", () => Promise.all([loadDocuments(), loadItems()]).then(() => toast("已刷新")));
  $("#newItemButton").addEventListener("click", resetForm); $("#annotationForm").addEventListener("submit", saveItem); $("#deleteButton").addEventListener("click", deleteCurrent);
  $$('input[name="category"]').forEach((input) => input.addEventListener("change", syncAnswerability));
  $$(".rail-tabs button").forEach((button) => button.addEventListener("click", () => { state.tab = button.dataset.tab; $$(".rail-tabs button").forEach((item) => item.classList.toggle("active", item === button)); $("#documentList").classList.toggle("hidden", state.tab !== "documents"); $("#itemList").classList.toggle("hidden", state.tab !== "items"); }));
  document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") $("#annotationForm").requestSubmit(); });
  $$('textarea, #mustInclude, #mustNotInclude, #difficulty, #origin').forEach((input) => input.addEventListener("input", () => setSaveState("未保存", "idle")));
}

bindEvents(); initialize();
