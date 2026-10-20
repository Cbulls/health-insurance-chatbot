const API = "";

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

function newConversationId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : "c-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}
let conversationId = newConversationId();

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const docList = document.getElementById("docList");
const messages = document.getElementById("messages");
const emptyState = document.getElementById("emptyState");
const composer = document.getElementById("composer");
const queryInput = document.getElementById("queryInput");
const sendBtn = document.getElementById("sendBtn");
const newChatBtn = document.getElementById("newChatBtn");
const interview = document.getElementById("interview");
const askPane = document.getElementById("askPane");
const policyPanel = document.getElementById("policyPanel");

document.getElementById("policyToggle").addEventListener("click", () => {
  policyPanel.classList.toggle("hidden");
});
document.getElementById("askToggle").addEventListener("click", () => {
  interview.classList.add("hidden");
  askPane.classList.remove("hidden");
  policyPanel.classList.remove("hidden");
});
document.getElementById("backInterview").addEventListener("click", () => {
  askPane.classList.add("hidden");
  interview.classList.remove("hidden");
});

const SYMPTOM_LABELS = {
  headache: "두통",
  leg_cramp: "다리 쥐",
  back_pain: "요통",
  indigestion: "소화불량",
  skin: "피부 증상",
};
const selectedSymptoms = new Set();
let selectedOcc = null;
let selectedConcern = null;
let lastAssess = null;

document.querySelectorAll("[data-symptom]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const code = btn.dataset.symptom;
    if (selectedSymptoms.has(code)) selectedSymptoms.delete(code);
    else selectedSymptoms.add(code);
    btn.classList.toggle("on", selectedSymptoms.has(code));
  });
});
document.querySelectorAll("[data-occ]").forEach((btn) => {
  btn.addEventListener("click", () => {
    selectedOcc = selectedOcc === btn.dataset.occ ? null : btn.dataset.occ;
    document.querySelectorAll("[data-occ]").forEach((b) =>
      b.classList.toggle("on", b.dataset.occ === selectedOcc));
  });
});
document.querySelectorAll("[data-concern]").forEach((btn) => {
  btn.addEventListener("click", () => {
    selectedConcern = selectedConcern === btn.dataset.concern ? null : btn.dataset.concern;
    document.querySelectorAll("[data-concern]").forEach((b) =>
      b.classList.toggle("on", b.dataset.concern === selectedConcern));
  });
});

const assessBtn = document.getElementById("assessBtn");
const guideBtn = document.getElementById("guideBtn");
const stateCard = document.getElementById("stateCard");
const guideResult = document.getElementById("guideResult");

assessBtn.addEventListener("click", runAssess);
guideBtn.addEventListener("click", runGuidance);

function composeSymptomText() {
  const chips = [...selectedSymptoms].map((c) => SYMPTOM_LABELS[c]).filter(Boolean);
  const free = (document.getElementById("symptomInput").value || "").trim();
  return [chips.join(" "), free].filter(Boolean).join(" ").trim();
}

async function runAssess() {
  const text = composeSymptomText();
  if (!text) { toast("증상을 칩으로 고르거나 적어 주세요."); return; }
  assessBtn.disabled = true;
  try {
    const res = await fetch(`${API}/v1/assess`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        text, occupation: selectedOcc, concern: selectedConcern,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error("assess " + res.status);
    lastAssess = { data };
    renderCard(data);
  } catch (err) {
    toast("상태 확인 실패: " + err.message);
  } finally {
    assessBtn.disabled = false;
  }
}

function renderCard(data) {
  stateCard.classList.remove("hidden");
  guideResult.classList.add("hidden");
  document.getElementById("cardPreview").textContent = "";
  const facts = document.getElementById("cardFacts");
  facts.innerHTML = "";
  const add = (s) => {
    const li = document.createElement("li");
    li.textContent = s;
    facts.appendChild(li);
  };
  if (data.abstained) {
    add("지원하는 증상으로 이해하지 못했습니다. 칩 다섯 가지 중에서 고르거나 다시 적어 주세요.");
    guideBtn.disabled = true;
    return;
  }
  guideBtn.disabled = false;
  add("증상: " + (data.symptoms || []).map((s) => s.label).join(", "));
  if (data.occupation_label) add("직업군: " + data.occupation_label);
  if (data.concern_label) add("관심: " + data.concern_label);
  if (data.pii_kinds && Object.keys(data.pii_kinds).length)
    add("식별 정보는 가려졌고 원문은 저장하지 않습니다.");
  stateCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function runGuidance() {
  if (!lastAssess) return;
  guideBtn.disabled = true;
  try {
    const d = lastAssess.data;
    const res = await fetch(`${API}/v1/guidance`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        occupation: selectedOcc,
        concern: selectedConcern,
        symptoms: (d.symptoms || []).map((s) => s.code),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error("guidance " + res.status);
    renderGuidance(data);
  } catch (err) {
    toast("안내 실패: " + err.message);
  } finally {
    guideBtn.disabled = false;
  }
}

function renderGuidance(data) {
  guideResult.classList.remove("hidden");
  if (data.abstained) {
    guideResult.innerHTML = "<p class='muted'>안내를 제공할 수 없습니다: " +
      escapeHtml(data.abstain_reason || "") + "</p>";
    return;
  }
  const areas = (data.areas || []).map((a) =>
    `<div class="area-item"><strong>${escapeHtml(a.label)}</strong>` +
    `<div class="src">신뢰 ${a.confidence} · ${escapeHtml(a.source)}</div></div>`
  ).join("");
  const covs = (data.coverages || []).map((c) =>
    `<div class="cov-item"><strong>${escapeHtml(c.label)}</strong> — ${escapeHtml(c.summary)}` +
    `<div class="why">${escapeHtml((c.why || []).join(" · "))}</div></div>`
  ).join("");
  const cites = (data.citations || []).map((c) =>
    `<span class="cite">${escapeHtml(c)}</span>`).join("");
  const evidence = cites
    ? "<div class='citations'>" + cites + "</div>"
    : "<p class='muted'>업로드된 약관이 없어 근거 라벨을 붙이지 않았습니다. 위쪽 「약관 올리기」를 이용하세요.</p>";
  const disc = escapeHtml(data.disclaimer || "");
  guideResult.innerHTML =
    "<h2>관련 영역과 보장 비교</h2>" + areas + covs +
    "<h3 class='evidence-title'>약관 근거</h3>" + evidence +
    "<h2 class='evidence-title'>설명</h2><div class='explain'>" +
    escapeHtml(data.explanation || "") + "</div>" +
    (disc ? "<p class='disclaimer'>" + disc + "</p>" : "") +
    "<button type='button' id='purgeBtn' class='danger-btn'>안내 종료 · 이 기기에서 내 입력 지우기</button>";
  const purgeBtn = document.getElementById("purgeBtn");
  if (purgeBtn) purgeBtn.addEventListener("click", purgeSession);
  guideResult.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function purgeSession() {
  const oldConv = conversationId;
  try {
    await fetch(`${API}/v1/privacy/purge`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ conversation_id: oldConv }),
    });
  } catch (_) { /* 화면은 무조건 비운다 */ }
  lastAssess = null;
  selectedSymptoms.clear();
  selectedOcc = null;
  selectedConcern = null;
  document.querySelectorAll("[data-symptom], [data-occ], [data-concern]").forEach((b) =>
    b.classList.remove("on"));
  const input = document.getElementById("symptomInput");
  if (input) input.value = "";
  stateCard.classList.add("hidden");
  document.getElementById("cardFacts").innerHTML = "";
  guideResult.classList.add("hidden");
  guideResult.innerHTML = "";
  conversationId = newConversationId();
  toast("이 화면의 입력을 지웠습니다. 약관 문서는 그대로입니다.");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

if (newChatBtn) {
  newChatBtn.addEventListener("click", () => {
    conversationId = newConversationId();
    messages.querySelectorAll(".msg, .citations").forEach((el) => el.remove());
    if (emptyState) emptyState.style.display = "";
    toast("새 대화를 시작합니다.");
  });
}

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

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
      docList.appendChild(li);
    });
    const ready = docs.filter((d) => d.status === "ready").length;
    const count = document.getElementById("kbCount");
    if (count) count.textContent = `약관 ${ready}건`;
    const hint = document.getElementById("kbHint");
    if (hint) {
      hint.textContent = ready
        ? `약관 ${ready}건이 준비되어 있습니다. 증상에 맞는 근거 라벨을 붙입니다.`
        : "약관을 올리면 증상에 맞는 근거 라벨을 붙입니다. 없어도 보장 유형 비교는 됩니다.";
    }
  } catch (_) {}
}

async function deleteDoc(id, filename) {
  if (!confirm(`「${filename || id}」약관을 삭제할까요?`)) return;
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
  if ((err || "").startsWith("embed_rate_limited"))
    return "임베딩 API 한도에 걸렸습니다. 1분 뒤 다시 올려 주세요.";
  return err || "";
}

if (queryInput) {
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
}

if (composer) {
  composer.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = queryInput.value.trim();
    if (!q) return;
    queryInput.value = "";
    queryInput.style.height = "auto";
    addMessage("user", q);
    await streamAnswer(q);
  });
}

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
    return "올린 약관에서 근거를 찾지 못했습니다. 지어내지 않고 답변을 보류합니다.";
  if ((reason || "").startsWith("fabricated_citation"))
    return "답변 검증에 실패하여 응답을 보류합니다.";
  if (reason === "llm_unavailable")
    return "답변 생성 서비스가 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요.";
  if (reason === "llm_cost_limit")
    return "질의 컨텍스트가 비용 상한을 초과해 답변을 보류합니다.";
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
    chip.textContent = l;
    box.appendChild(chip);
  });
  wrap.appendChild(box);
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

refreshDocs();
