// harag PDF RAG MVP — 프론트엔드 로직
const API = ""; // 같은 오리진(FastAPI가 정적 서빙)

// ── 세션(소유자) ID: 브라우저별 고정 → 본인 문서만 조회 ──
function ownerId() {
  let id = localStorage.getItem("harag_owner");
  if (!id) {
    id = "u-" + Math.random().toString(36).slice(2, 10) +
      Date.now().toString(36).slice(-4);
    localStorage.setItem("harag_owner", id);
  }
  return id;
}
const OWNER = ownerId();

function headers(extra) {
  return Object.assign({ "X-Owner-Id": OWNER }, extra || {});
}

// ── 대화 ID: 멀티턴 질의 재작성용("그건 언제까지?" 같은 후속 질의 해소) ──
function newConversationId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : "c-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}
let conversationId = newConversationId();

// ── DOM ──
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const docList = document.getElementById("docList");
const messages = document.getElementById("messages");
const emptyState = document.getElementById("emptyState");
const composer = document.getElementById("composer");
const queryInput = document.getElementById("queryInput");
const sendBtn = document.getElementById("sendBtn");
const newChatBtn = document.getElementById("newChatBtn");
document.getElementById("ownerId").textContent = OWNER;

// 새 대화: 이전 턴 맥락(질의 재작성 이력)을 리셋하고 화면을 비운다.
newChatBtn.addEventListener("click", () => {
  conversationId = newConversationId();
  messages.querySelectorAll(".msg, .citations").forEach((el) => el.remove());
  if (emptyState) emptyState.style.display = "";
  toast("새 대화를 시작합니다.");
});

// ── 토스트 ──
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

// ── 업로드 ──
dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
  fileInput.value = "";
});
["dragover", "dragenter"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
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
  if (f) uploadFile(f);
});

async function uploadFile(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    toast("PDF 파일만 업로드할 수 있습니다.");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch(`${API}/v1/documents`, {
      method: "POST",
      headers: headers(),
      body: form,
    });
    if (!res.ok) throw new Error("upload " + res.status);
    const data = await res.json();
    toast(
      data.status === "duplicate"
        ? "이미 업로드된 문서입니다."
        : `업로드됨: ${file.name} — 처리 중…`
    );
    refreshDocs();
    pollDoc(data.document_id);
  } catch (err) {
    toast("업로드 실패: " + err.message);
  }
}

async function pollDoc(id, tries = 0) {
  if (tries > 60) return;
  try {
    const res = await fetch(`${API}/v1/documents/${id}`, { headers: headers() });
    if (!res.ok) return;
    const d = await res.json();
    refreshDocs();
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
    if (!res.ok) return;
    const docs = await res.json();
    docList.innerHTML = "";
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
      if (d.error) {
        const warn = document.createElement("span");
        warn.title = errorLabel(d.error);
        warn.textContent = "⚠";
        meta.appendChild(warn);
      }
      if (d.status !== "processing") {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "doc-delete";
        del.title = "문서 삭제";
        del.textContent = "삭제";
        del.addEventListener("click", (e) => {
          e.stopPropagation();
          deleteDoc(d.document_id, d.filename);
        });
        meta.appendChild(del);
      }
      li.appendChild(name);
      li.appendChild(meta);
      docList.appendChild(li);
    });
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
  if ((err || "").startsWith("capacity_exceeded"))
    return "저장 공간 한도에 도달했습니다. 기존 문서를 삭제한 뒤 다시 시도해 주세요.";
  return err || "";
}

// ── 채팅 ──
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

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = queryInput.value.trim();
  if (!q) return;
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
    });
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
    bubble.classList.remove("thinking");
    bubble.classList.add("abstain");
    bubble.textContent = "오류가 발생했습니다: " + err.message;
  } finally {
    sendBtn.disabled = false;
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
  return "답변을 제공할 수 없습니다: " + (reason || "unknown");
}

function renderCitations(wrap, data) {
  if (!data) return;
  const labels = data.split(";").map((s) => s.trim()).filter(Boolean);
  if (!labels.length) return;
  const box = document.createElement("div");
  box.className = "citations";
  labels.forEach((l) => {
    const chip = document.createElement("span");
    chip.className = "cite";
    chip.textContent = "📎 " + l;
    box.appendChild(chip);
  });
  wrap.appendChild(box);
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// 초기 로드
refreshDocs();
