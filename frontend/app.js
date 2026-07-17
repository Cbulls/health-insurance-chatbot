// harag — 창구 직원용 프론트
const API = "";

const LS_OWNER = "harag_owner";
const LS_JWT = "harag_jwt";

function ensureOwnerId() {
  let id = localStorage.getItem(LS_OWNER);
  if (!id) {
    id = "u-" + Math.random().toString(36).slice(2, 10) +
      Date.now().toString(36).slice(-4);
    localStorage.setItem(LS_OWNER, id);
  }
  return id;
}

function getJwt() {
  // sessionStorage 우선(XSS 시 장기 유출 면적 축소). 레거시 localStorage 마이그레이션.
  let v = (sessionStorage.getItem(LS_JWT) || "").trim();
  if (!v) {
    v = (localStorage.getItem(LS_JWT) || "").trim();
    if (v) {
      sessionStorage.setItem(LS_JWT, v);
      localStorage.removeItem(LS_JWT);
    }
  }
  return v;
}

function headers(extra) {
  const h = Object.assign({}, extra || {});
  const jwt = getJwt();
  if (jwt) {
    h.Authorization = jwt.startsWith("Bearer ") ? jwt : "Bearer " + jwt;
  } else {
    h["X-Owner-Id"] = ensureOwnerId();
  }
  return h;
}

function newConversationId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : "c-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}
let conversationId = newConversationId();
let streamAbort = null;
let readyCount = 0;
let uploading = false;

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const uploadBusy = document.getElementById("uploadBusy");
const docList = document.getElementById("docList");
const docsEmpty = document.getElementById("docsEmpty");
const messages = document.getElementById("messages");
const emptyState = document.getElementById("emptyState");
const composer = document.getElementById("composer");
const queryInput = document.getElementById("queryInput");
const sendBtn = document.getElementById("sendBtn");
const stopBtn = document.getElementById("stopBtn");
const newChatBtn = document.getElementById("newChatBtn");
const composerHint = document.getElementById("composerHint");
const capacityBanner = document.getElementById("capacityBanner");
const citePanel = document.getElementById("citePanel");
const citePanelBody = document.getElementById("citePanelBody");
const citeCloseBtn = document.getElementById("citeCloseBtn");
const sidebar = document.getElementById("sidebar");
const menuBtn = document.getElementById("menuBtn");
const sidebarBackdrop = document.getElementById("sidebarBackdrop");
const jwtInput = document.getElementById("jwtInput");
const jwtSaveBtn = document.getElementById("jwtSaveBtn");
const jwtClearBtn = document.getElementById("jwtClearBtn");
const authStatus = document.getElementById("authStatus");
const authModeLabel = document.getElementById("authModeLabel");
const ownerIdEl = document.getElementById("ownerId");

ownerIdEl.textContent = ensureOwnerId();
jwtInput.value = getJwt();
updateAuthUi();

function updateAuthUi() {
  const jwt = getJwt();
  if (jwt) {
    authModeLabel.textContent = "JWT 로그인";
    authStatus.textContent = "토큰이 저장되어 있습니다.";
  } else {
    authModeLabel.textContent = "데모 세션";
    authStatus.textContent = "";
  }
}

jwtSaveBtn.addEventListener("click", () => {
  const v = jwtInput.value.trim();
  if (!v) {
    toast("토큰을 입력하세요.");
    return;
  }
  sessionStorage.setItem(LS_JWT, v);
  localStorage.removeItem(LS_JWT);
  updateAuthUi();
  toast("토큰을 저장했습니다(이 탭 세션).");
  refreshDocs();
});
jwtClearBtn.addEventListener("click", () => {
  sessionStorage.removeItem(LS_JWT);
  localStorage.removeItem(LS_JWT);
  jwtInput.value = "";
  updateAuthUi();
  toast("토큰을 지웠습니다. 데모 세션을 사용합니다.");
  refreshDocs();
});

function openSidebar() {
  sidebar.classList.add("open");
  sidebarBackdrop.hidden = false;
}
function closeSidebar() {
  sidebar.classList.remove("open");
  sidebarBackdrop.hidden = true;
}
menuBtn.addEventListener("click", openSidebar);
sidebarBackdrop.addEventListener("click", closeSidebar);

newChatBtn.addEventListener("click", () => {
  conversationId = newConversationId();
  messages.querySelectorAll(".msg, .citations").forEach((el) => el.remove());
  if (emptyState) emptyState.style.display = "";
  citePanel.hidden = true;
  toast("새 대화를 시작합니다.");
});

citeCloseBtn.addEventListener("click", () => {
  citePanel.hidden = true;
});

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

function setUploading(on) {
  uploading = on;
  dropzone.classList.toggle("busy", on);
  uploadBusy.hidden = !on;
}

function updateComposerGate() {
  const ok = readyCount > 0 && !uploading;
  queryInput.disabled = !ok;
  sendBtn.disabled = !ok || !!streamAbort;
  composerHint.textContent = readyCount > 0
    ? "업로드한 문서 내용에 근거해 답합니다."
    : "준비된 문서가 없습니다. PDF·Word·한글 문서를 업로드하세요.";
}

dropzone.addEventListener("click", () => {
  if (!uploading) fileInput.click();
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
  fileInput.value = "";
});
["dragover", "dragenter"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    if (!uploading) dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
  })
);
dropzone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f && !uploading) uploadFile(f);
});

function allowedFile(name) {
  const n = (name || "").toLowerCase();
  return [".pdf", ".hwpx", ".docx", ".hwp", ".doc"].some((s) => n.endsWith(s));
}

async function uploadFile(file) {
  if (!allowedFile(file.name)) {
    toast("PDF, HWPX, DOCX, HWP, DOC만 업로드할 수 있습니다.");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  setUploading(true);
  updateComposerGate();
  try {
    const res = await fetch(`${API}/v1/documents`, {
      method: "POST",
      headers: headers(),
      body: form,
    });
    if (res.status === 401) {
      toast("인증이 필요합니다. JWT 토큰을 저장해 주세요.");
      openSidebar();
      return;
    }
    if (!res.ok) {
      let detail = "upload " + res.status;
      try {
        const err = await res.json();
        detail = err.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const data = await res.json();
    toast(
      data.status === "duplicate"
        ? "이미 업로드된 문서입니다."
        : `업로드됨: ${file.name} — 처리 중…`
    );
    await refreshDocs();
    pollDoc(data.document_id);
  } catch (err) {
    toast("업로드 실패: " + err.message);
  } finally {
    setUploading(false);
    updateComposerGate();
  }
}

async function pollDoc(id, tries = 0) {
  if (tries > 60) return;
  try {
    const res = await fetch(`${API}/v1/documents/${id}`, { headers: headers() });
    if (!res.ok) return;
    const d = await res.json();
    await refreshDocs();
    if (d.status === "processing") {
      setTimeout(() => pollDoc(id, tries + 1), 1500);
    } else if (d.status === "ready") {
      toast(`준비 완료: ${d.filename} (${d.n_chunks}개 청크)`);
    } else if (d.status === "failed") {
      toast(`처리 실패: ${d.filename} — ${errorLabel(d.error)}`);
    }
  } catch (_) {}
}

async function refreshDocs() {
  try {
    const res = await fetch(`${API}/v1/documents`, { headers: headers() });
    if (res.status === 401) {
      toast("인증이 필요합니다. JWT 토큰을 저장해 주세요.");
      return;
    }
    if (!res.ok) return;
    const docs = await res.json();
    docList.innerHTML = "";
    readyCount = docs.filter((d) => d.status === "ready").length;
    docsEmpty.hidden = docs.length > 0;
    docs.forEach((d) => {
      const li = document.createElement("li");
      li.className = "doc-item";
      const name = document.createElement("span");
      name.className = "doc-name";
      name.title = d.filename || "";
      name.textContent = d.filename || "";
      const meta = document.createElement("span");
      meta.className = "doc-meta";
      const badge = document.createElement("span");
      badge.className = "badge " + d.status;
      badge.textContent = statusLabel(d.status);
      meta.appendChild(badge);
      if (d.status === "ready") {
        const chunks = document.createElement("span");
        chunks.textContent = d.n_chunks + " 청크";
        meta.appendChild(chunks);
      }
      if (d.status !== "processing") {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "doc-delete";
        del.textContent = "삭제";
        del.addEventListener("click", (e) => {
          e.stopPropagation();
          deleteDoc(d.document_id, d.filename);
        });
        meta.appendChild(del);
      }
      li.appendChild(name);
      li.appendChild(meta);
      if (d.error) {
        const err = document.createElement("span");
        err.className = "doc-error";
        err.textContent = errorLabel(d.error);
        li.appendChild(err);
      }
      docList.appendChild(li);
    });
    updateComposerGate();
  } catch (_) {}
}

async function deleteDoc(id, filename) {
  if (!confirm(`「${filename || id}」문서를 삭제할까요?\n검색 인덱스에서도 제거됩니다.`))
    return;
  try {
    const res = await fetch(`${API}/v1/documents/${id}`, {
      method: "DELETE",
      headers: headers(),
    });
    if (res.status === 409) {
      toast("문서가 아직 처리 중입니다. 잠시 후 다시 시도하세요.");
      return;
    }
    if (!res.ok) throw new Error("delete " + res.status);
    toast(`삭제됨: ${filename || id}`);
    refreshDocs();
  } catch (err) {
    toast("삭제 실패: " + err.message);
  }
}

function statusLabel(s) {
  return { processing: "처리중", ready: "준비됨", failed: "실패" }[s] || s;
}

function errorLabel(err) {
  const e = err || "";
  if (e.startsWith("capacity_exceeded"))
    return "저장 공간 한도 — 기존 문서를 삭제하세요.";
  if (e.startsWith("scan_pdf_no_ocr"))
    return "스캔본은 OCR 미지원 — 텍스트 PDF로 재업로드하세요.";
  if (e.startsWith("encrypted_pdf"))
    return "암호 PDF — 암호 해제 후 재업로드하세요.";
  if (e.startsWith("hwpx_parse_failed"))
    return "HWPX 텍스트 추출 실패.";
  if (e.startsWith("docx_parse_failed"))
    return "Word(DOCX) 텍스트 추출 실패.";
  if (e.startsWith("hwp5_parse_failed"))
    return "HWP 추출 실패 — 가능하면 HWPX로 변환하세요.";
  if (e.startsWith("hwp5_table_limited"))
    return "표 인식 제한(검색 가능) — HWPX 변환을 권장합니다.";
  if (e.startsWith("doc_convert_failed"))
    return "구형 DOC 변환 실패 — DOCX로 저장 후 다시 올려 주세요.";
  return e;
}

async function refreshCapacity() {
  try {
    const res = await fetch(`${API}/health`);
    if (!res.ok) return;
    const body = await res.json();
    const cap = body.capacity;
    if (!cap || cap.budget_mb == null) {
      capacityBanner.hidden = true;
      return;
    }
    const used = Number(cap.used_mb || 0);
    const budget = Number(cap.budget_mb || 0);
    if (budget <= 0) {
      capacityBanner.hidden = true;
      return;
    }
    const pct = used / budget;
    if (pct >= 0.75) {
      capacityBanner.hidden = false;
      capacityBanner.textContent =
        `저장 용량 주의: 약 ${(pct * 100).toFixed(0)}% 사용 중 ` +
        `(${used.toFixed(0)} / ${budget.toFixed(0)} MB). 필요 시 문서를 삭제하세요.`;
    } else {
      capacityBanner.hidden = true;
    }
  } catch (_) {
    capacityBanner.hidden = true;
  }
}

queryInput.addEventListener("input", () => {
  queryInput.style.height = "auto";
  queryInput.style.height = Math.min(queryInput.scrollHeight, 160) + "px";
});
queryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

stopBtn.addEventListener("click", () => {
  if (streamAbort) streamAbort.abort();
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = queryInput.value.trim();
  if (!q || readyCount === 0) return;
  queryInput.value = "";
  queryInput.style.height = "auto";
  addMessage("user", q);
  await streamAnswer(q);
});

function addMessage(role, text) {
  if (emptyState) emptyState.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  messages.appendChild(wrap);
  messages.scrollTop = messages.scrollHeight;
  return { wrap, bubble };
}

async function streamAnswer(query) {
  sendBtn.disabled = true;
  stopBtn.hidden = false;
  streamAbort = new AbortController();
  const { wrap, bubble } = addMessage("bot", "");
  bubble.classList.add("thinking");
  bubble.textContent = "검색 중…";
  let answer = "";
  let started = false;

  try {
    const res = await fetch(`${API}/v1/query/stream`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ query, conversation_id: conversationId }),
      signal: streamAbort.signal,
    });
    if (res.status === 401) {
      bubble.classList.remove("thinking");
      bubble.classList.add("abstain");
      bubble.textContent = "인증이 필요합니다. 왼쪽에서 JWT 토큰을 저장해 주세요.";
      openSidebar();
      return;
    }
    if (res.status === 429) {
      bubble.classList.remove("thinking");
      bubble.classList.add("abstain");
      bubble.textContent = "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.";
      return;
    }
    if (!res.ok || !res.body) throw new Error("query " + res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const evt = JSON.parse(line.slice(5).trim());
        handleEvent(evt);
      }
    }

    function handleEvent(evt) {
      if (evt.kind === "token") {
        if (!started) {
          bubble.classList.remove("thinking");
          bubble.textContent = "";
          started = true;
        }
        answer += evt.data;
        bubble.textContent = answer;
      } else if (evt.kind === "abstain") {
        bubble.classList.remove("thinking");
        bubble.classList.add("abstain");
        bubble.textContent = abstainMessage(evt.data);
      } else if (evt.kind === "error") {
        bubble.classList.remove("thinking");
        bubble.classList.add("abstain");
        bubble.textContent =
          "서버 오류로 답변을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.";
      } else if (evt.kind === "citations") {
        renderCitations(wrap, evt.data);
      }
      messages.scrollTop = messages.scrollHeight;
    }
  } catch (err) {
    if (err.name === "AbortError") {
      if (!started) {
        bubble.classList.remove("thinking");
        bubble.textContent = "응답을 중지했습니다.";
      } else {
        bubble.textContent = answer + "\n\n(응답 중지됨)";
      }
    } else {
      bubble.classList.remove("thinking");
      bubble.classList.add("abstain");
      bubble.textContent = "오류가 발생했습니다: " + err.message;
    }
  } finally {
    streamAbort = null;
    stopBtn.hidden = true;
    updateComposerGate();
  }
}

function abstainMessage(reason) {
  if (reason === "empty_context" || reason === "low_score")
    return "업로드한 문서에서 근거를 찾지 못했습니다. (지어내지 않고 답변을 보류합니다)";
  if ((reason || "").startsWith("fabricated_citation"))
    return "답변 검증에 실패하여 응답을 보류합니다.";
  if (reason === "llm_unavailable")
    return "답변 생성 서비스가 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요.";
  if (reason === "llm_cost_limit")
    return "질의 컨텍스트가 비용 상한을 초과해 답변을 보류합니다. 질문을 더 구체적으로 줄여 보세요.";
  if (reason === "no_evidence_in_documents")
    return "문서에 근거가 없어 답변을 보류합니다.";
  return "답변을 제공할 수 없습니다: " + (reason || "unknown");
}

function renderCitations(wrap, data) {
  let items = [];
  if (Array.isArray(data)) {
    items = data;
  } else if (typeof data === "string" && data.trim()) {
    // 구형 서버 호환
    items = data.split(";").map((s) => s.trim()).filter(Boolean).map((l) => ({
      citation_label: l, source_document: "", page: null,
      chunk_id: "", struct_path: "", snippet: "",
    }));
  }
  if (!items.length) return;
  const box = document.createElement("div");
  box.className = "citations";
  items.forEach((c) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "cite";
    const page = formatPage(c.page);
    const path = c.struct_path ? ` · ${c.struct_path}` : "";
    const src = c.source_document || c.citation_label || "출처";
    chip.textContent = src + page + path;
    chip.title = c.citation_label || "";
    chip.addEventListener("click", () => showCitePanel(c));
    box.appendChild(chip);
  });
  wrap.appendChild(box);
}

function formatPage(page) {
  if (page == null || page === "") return "";
  if (Array.isArray(page)) return " p." + page.join(",");
  return " p." + page;
}

function showCitePanel(c) {
  const page = formatPage(c.page);
  citePanelBody.innerHTML = "";
  const doc = document.createElement("div");
  doc.className = "cite-doc";
  doc.textContent = c.source_document || c.citation_label || "출처";
  citePanelBody.appendChild(doc);
  const meta = document.createElement("div");
  meta.textContent = [
    c.struct_path ? "경로: " + c.struct_path : "",
    page ? "페이지:" + page : "",
    c.chunk_id ? "청크: " + c.chunk_id : "",
  ].filter(Boolean).join(" · ");
  citePanelBody.appendChild(meta);
  if (c.snippet) {
    const snip = document.createElement("div");
    snip.className = "cite-snip";
    snip.textContent = c.snippet;
    citePanelBody.appendChild(snip);
  }
  citePanel.hidden = false;
}

refreshDocs();
refreshCapacity();
