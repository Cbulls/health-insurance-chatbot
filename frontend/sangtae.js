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
let askDoc = null; // { document_id, display_name, insurer } | null

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const docList = document.getElementById("docList");
const messages = document.getElementById("messages");
const emptyState = document.getElementById("emptyState");
const emptyStateHint = document.getElementById("emptyStateHint");
const composer = document.getElementById("composer");
const queryInput = document.getElementById("queryInput");
const sendBtn = document.getElementById("sendBtn");
const newChatBtn = document.getElementById("newChatBtn");
const interview = document.getElementById("interview");
const askPane = document.getElementById("askPane");
const policyPanel = document.getElementById("policyPanel");
const brochurePanel = document.getElementById("brochurePanel");
const brochureList = document.getElementById("brochureList");
const kbCountBtn = document.getElementById("kbCount");
const askDocBar = document.getElementById("askDocBar");
const askDocLabel = document.getElementById("askDocLabel");
const askPickOther = document.getElementById("askPickOther");
const askSuggest = document.getElementById("askSuggest");
const askCopy = document.getElementById("askCopy");
const emptyStateCopy = document.getElementById("emptyStateCopy");

const DOC_ASK_SUGGESTS = [
  "보장 개시일과 면책 기간은 어떻게 되나요?",
  "통원·입원 담보 차이를 알려주세요.",
  "해약환급금이 없는 형은 어떤 의미인가요?",
];

const RESULT_CHAT_SUGGESTS = [
  "이 상품들이 증상과 어떻게 연결되나요?",
  "추천된 상품의 매칭 이유를 알려주세요.",
  "통원·입원·검사 담보는 어떻게 안내되나요?",
];

let resultChatConvId = null;
let resultChatBusy = false;
let resultChatDocIds = [];
let lastGuidanceMatch = "";

function setBrochurePanelOpen(open) {
  if (!brochurePanel || !kbCountBtn) return;
  brochurePanel.classList.toggle("hidden", !open);
  kbCountBtn.setAttribute("aria-expanded", open ? "true" : "false");
}

if (kbCountBtn && brochurePanel) {
  kbCountBtn.addEventListener("click", () => {
    setBrochurePanelOpen(brochurePanel.classList.contains("hidden"));
  });
}

document.getElementById("policyToggle").addEventListener("click", () => {
  policyPanel.classList.toggle("hidden");
});
document.getElementById("backInterview").addEventListener("click", () => {
  clearAskSuggest();
  askPane.classList.add("hidden");
  interview.classList.remove("hidden");
});

function resetAskMessages() {
  if (!messages) return;
  messages.querySelectorAll(".msg, .citations").forEach((el) => el.remove());
  if (emptyState) emptyState.style.display = "";
}

function clearAskSuggest() {
  if (!askSuggest) return;
  askSuggest.innerHTML = "";
  askSuggest.classList.add("hidden");
}

function renderAskSuggest(items) {
  if (!askSuggest) return;
  askSuggest.innerHTML = "";
  if (!items || !items.length) {
    askSuggest.classList.add("hidden");
    return;
  }
  items.forEach((q) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip ask-suggest-chip";
    btn.textContent = q;
    btn.addEventListener("click", async () => {
      if (!queryInput || sendBtn.disabled) return;
      queryInput.value = q;
      clearAskSuggest();
      addMessage("user", q);
      queryInput.value = "";
      await streamAnswer(q);
    });
    askSuggest.appendChild(btn);
  });
  askSuggest.classList.remove("hidden");
}

function renderAskDocBar() {
  if (!askDocBar || !askDocLabel) return;
  if (askPickOther) askPickOther.classList.remove("hidden");
  if (!askDoc || !askDoc.document_id) {
    askDocBar.classList.add("hidden");
    if (queryInput) queryInput.placeholder = "이 설명서에서 궁금한 점을 적으세요";
    if (askCopy) {
      askCopy.textContent =
        "선택한 설명서에서만 답합니다. 근거가 없으면 확정 수치를 만들지 않고, 다시 물을 표현을 안내합니다.";
    }
    if (emptyStateCopy) emptyStateCopy.textContent = "설명서를 고른 뒤 질문해 보세요.";
    if (emptyStateHint) {
      emptyStateHint.textContent = "위 「다른 설명서」 또는 헤더 「설명서 N건」에서 고를 수 있어요.";
    }
    return;
  }
  const name = askDoc.display_name || "설명서";
  const insurer = askDoc.insurer ? " · " + askDoc.insurer : "";
  askDocLabel.textContent = "질문 대상: " + name + insurer;
  askDocBar.classList.remove("hidden");
  if (queryInput) {
    queryInput.placeholder = name + "에서 궁금한 점을 적으세요";
  }
  if (askCopy) {
    askCopy.textContent =
      "선택한 설명서에서만 답합니다. 근거가 없으면 확정 수치를 만들지 않고, 다시 물을 표현을 안내합니다.";
  }
  if (emptyStateCopy) {
    emptyStateCopy.textContent = name + " 내용만 보고 답합니다.";
  }
  if (emptyStateHint) {
    emptyStateHint.textContent = "아래 추천 질문으로 바로 시작해 보세요.";
  }
}

function openAskForDoc(d) {
  if (!d || !d.document_id) return;
  askDoc = {
    document_id: d.document_id,
    display_name: d.display_name || d.name || d.title || d.filename || "",
    insurer: d.insurer || "",
  };
  conversationId = newConversationId();
  resetAskMessages();
  renderAskDocBar();
  renderAskSuggest(DOC_ASK_SUGGESTS);
  setBrochurePanelOpen(false);
  if (policyPanel) policyPanel.classList.add("hidden");
  interview.classList.add("hidden");
  askPane.classList.remove("hidden");
  if (queryInput) queryInput.focus();
}

if (askPickOther) {
  askPickOther.addEventListener("click", () => {
    setBrochurePanelOpen(true);
    brochurePanel.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

const SYMPTOMS = {
  headache: { label: "두통", type: "usual", aliases: ["두통", "머리가 아", "머리 아", "골이 울", "편두통", "머리가 지끈"], ref: { kcd: "R51", name: "두통", source: "질병관리청 국가건강정보포털 — 두통(개요)" } },
  leg_cramp: { label: "다리 쥐", type: "hidden", aliases: ["다리에 쥐", "다리 쥐", "종아리 쥐", "하지 경련", "다리 경련", "근육 경련"], ref: { kcd: "R25", name: "이상운동", source: "질병관리청 국가건강정보포털 — 근육경련(개요)" } },
  back_pain: { label: "요통", type: "usual", aliases: ["요통", "허리 아", "허리가 아", "허리 통증", "허리 디스크"], ref: { kcd: "M54", name: "등통증", source: "질병관리청 국가건강정보포털 — 요통(개요)" } },
  indigestion: { label: "소화불량", type: "usual", aliases: ["소화 안", "소화불량", "속 쓰", "체했", "배가 더부룩", "위장"], ref: { kcd: "K30", name: "기능성 소화불량", source: "질병관리청 국가건강정보포털 — 소화불량(개요)" } },
  skin: { label: "피부 증상", type: "usual", aliases: ["피부", "두드러기", "가려움", "발진", "습진", "피부가 가렵"], ref: { kcd: "L29", name: "가려움증", source: "질병관리청 국가건강정보포털 — 두드러기·피부염(개요)" } },
  cough: { label: "기침", type: "usual", aliases: ["기침", "잔기침", "콜록", "기침이"], ref: { kcd: "R05", name: "기침", source: "질병관리청 국가건강정보포털 — 기침(개요)" } },
  fatigue: { label: "피로감", type: "usual", aliases: ["피로", "피곤", "기운이 없", "무기력", "피로감"], ref: { kcd: "R53", name: "권태 및 피로", source: "질병관리청 국가건강정보포털 — 피로(개요)" } },
  neck_pain: { label: "목 통증", type: "usual", aliases: ["목 통증", "목이 아", "목 결", "거북목", "목디스크"], ref: { kcd: "M54.2", name: "경부통", source: "질병관리청 국가건강정보포털 — 경추통(개요)" } },
  dizziness: { label: "어지러움", type: "usual", aliases: ["어지러", "어지럼", "현기증", "핑 돈"], ref: { kcd: "R42", name: "어지럼 및 어지럼증", source: "질병관리청 국가건강정보포털 — 어지럼(개요)" } },
  shoulder_pain: { label: "어깨 통증", type: "usual", aliases: ["어깨 통증", "어깨가 아", "오십견", "어깨 결"], ref: { kcd: "M75", name: "어깨병변", source: "질병관리청 국가건강정보포털 — 어깨통증(개요)" } },
  leg_itch: { label: "다리 가려움", type: "hidden", aliases: ["다리 가려움", "다리가 가려", "다리가 가렵", "종아리 가려"], ref: { kcd: "L29", name: "가려움증", source: "질병관리청 국가건강정보포털 — 가려움증(개요)" } },
  numbness: { label: "손발 저림", type: "hidden", aliases: ["손발 저림", "손이 저", "발이 저", "저려"], ref: { kcd: "R20", name: "피부감각의 장애", source: "질병관리청 국가건강정보포털 — 저림·감각이상(개요)" } },
  thirst: { label: "자주 목마름", type: "hidden", aliases: ["목마름", "갈증", "입이 마", "자꾸 물"], ref: { kcd: "K11.7", name: "구강건조", source: "질병관리청 국가건강정보포털 — 구강건조(개요)" } },
  swelling: { label: "붓기", type: "hidden", aliases: ["붓기", "부었", "부종", "발이 부"], ref: { kcd: "R60", name: "부종", source: "질병관리청 국가건강정보포털 — 부종(개요)" } },
  palpitation: { label: "가슴 두근거림", type: "hidden", aliases: ["두근거림", "심장이 빨리", "가슴이 두근", "빈맥"], ref: { kcd: "R00.2", name: "심계항진", source: "질병관리청 국가건강정보포털 — 두근거림(개요)" } },
  night_sweat: { label: "잘 때 땀", type: "hidden", aliases: ["잘 때 땀", "밤에 땀", "식은땀", "도한"], ref: { kcd: "R61", name: "다한증", source: "질병관리청 국가건강정보포털 — 발한(개요)" } },
  appetite_loss: { label: "식욕 감퇴", type: "hidden", aliases: ["식욕 감퇴", "입맛이 없", "식욕이 없", "밥맛이 없"], ref: { kcd: "R63.0", name: "식욕부진", source: "질병관리청 국가건강정보포털 — 식욕부진(개요) 기반 큐레이션" } },
  urine_color: { label: "소변 색깔 변화", type: "hidden", aliases: ["소변 색깔", "소변이 진해", "붉은 소변", "소변색"], ref: { kcd: "R82", name: "소변의 이상소견", source: "질병관리청 국가건강정보포털 — 배뇨 이상(개요) 기반 큐레이션" } },
  stool_color: { label: "대변 색깔 변화", type: "hidden", aliases: ["대변 색깔", "검은 변", "혈변", "변 색깔"], ref: { kcd: "R19.5", name: "분변이상", source: "질병관리청 국가건강정보포털 — 배변 이상(개요) 기반 큐레이션" } },
  weight_change: { label: "체중 변화", type: "hidden", aliases: ["체중 변화", "살이 빠", "체중 감소", "체중 증가"], ref: { kcd: "R63.4", name: "이상체중감소", source: "질병관리청 국가건강정보포털 — 체중 감소(개요) 기반 큐레이션" } },
  nausea: { label: "구역·메스꺼움", type: "hidden", aliases: ["구역", "메스꺼", "울렁", "오심"], ref: { kcd: "R11", name: "오심 및 구토", source: "질병관리청 국가건강정보포털 — 오심·구토(개요) 기반 큐레이션" } },
  abdominal_pain: { label: "복통", type: "hidden", aliases: ["복통", "배가 아", "속이 아", "아랫배"], ref: { kcd: "R10", name: "복부 및 골반 통증", source: "질병관리청 국가건강정보포털 — 복통(개요) 기반 큐레이션" } },
  breath_short: { label: "숨이 참", type: "hidden", aliases: ["숨이 참", "숨차", "호흡곤란", "숨이 가쁘"], ref: { kcd: "R06.0", name: "호흡곤란", source: "질병관리청 국가건강정보포털 — 호흡곤란(개요) 기반 큐레이션" } },
  sleep_trouble: { label: "잠들기 어려움", type: "hidden", aliases: ["잠들기 어려", "불면증", "잠을 못", "수면 장애"], ref: { kcd: "G47.0", name: "불면증", source: "질병관리청 국가건강정보포털 — 불면증(개요) 기반 큐레이션" } },
};
const MAX_SYMPTOMS = 8;
const selectedSymptoms = new Set();
const extras = [];
const selectedConcerns = new Set();
let selectedOcc = null;
let selectedAge = null;
let selectedSex = null;
let hiddenNone = false;
let lastAssess = null;
let wizardStep = 1;
let maxReached = 1;

const stateCard = document.getElementById("stateCard");
const guideResult = document.getElementById("guideResult");
const toStep2 = document.getElementById("toStep2");
const toResult = document.getElementById("toResult");
const symptomInput = document.getElementById("symptomInput");
const suggest = document.getElementById("suggest");
const sheet = document.getElementById("sheet");

document.querySelectorAll("[data-occ]").forEach((btn) => {
  btn.addEventListener("click", () => {
    selectedOcc = selectedOcc === btn.dataset.occ ? null : btn.dataset.occ;
    document.querySelectorAll("[data-occ]").forEach((b) =>
      b.classList.toggle("on", b.dataset.occ === selectedOcc));
  });
});
document.querySelectorAll("[data-concern]").forEach((btn) => {
  btn.addEventListener("click", () => toggleConcern(btn.dataset.concern));
});
document.querySelectorAll("[data-age]").forEach((btn) => {
  btn.addEventListener("click", () => {
    selectedAge = selectedAge === btn.dataset.age ? null : btn.dataset.age;
    document.querySelectorAll("[data-age]").forEach((b) =>
      b.classList.toggle("on", b.dataset.age === selectedAge));
  });
});
document.querySelectorAll("[data-sex]").forEach((btn) => {
  btn.addEventListener("click", () => {
    selectedSex = selectedSex === btn.dataset.sex ? null : btn.dataset.sex;
    document.querySelectorAll("[data-sex]").forEach((b) =>
      b.classList.toggle("on", b.dataset.sex === selectedSex));
  });
});
document.getElementById("hiddenNoneBtn").addEventListener("click", () => {
  hiddenNone = !hiddenNone;
  document.getElementById("hiddenNoneBtn").classList.toggle("on", hiddenNone);
  if (hiddenNone) {
    Object.entries(SYMPTOMS).forEach(([code, s]) => {
      if (s.type === "hidden") selectedSymptoms.delete(code);
    });
    syncSymptomButtons();
    renderTray();
    updateCareNotice();
  }
});
document.querySelectorAll("[data-goto]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const n = Number(btn.dataset.goto);
    if (n <= maxReached) goToStep(n);
  });
});
toStep2.addEventListener("click", () => {
  if (!hasSelection()) { toast("증상을 칩으로 고르거나 적어 주세요."); return; }
  goToStep(2);
});
document.getElementById("backStep1").addEventListener("click", () => goToStep(1));
document.getElementById("backStep2").addEventListener("click", () => goToStep(2));
toResult.addEventListener("click", runGuidanceFlow);

if (symptomInput) {
  symptomInput.addEventListener("input", onFreeInput);
  symptomInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commitFreeInput();
    }
  });
}
document.addEventListener("click", (e) => {
  if (suggest && !suggest.contains(e.target) && e.target !== symptomInput)
    suggest.classList.add("hidden");
});
document.getElementById("sheetClose").addEventListener("click", closeSheet);
document.getElementById("sheetCloseBg").addEventListener("click", closeSheet);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSheet();
});

function selectionCount() {
  return selectedSymptoms.size + extras.length;
}

function hasSelection() {
  const free = (symptomInput && symptomInput.value.trim()) || "";
  return selectionCount() > 0 || !!free;
}

function toggleSymptom(code) {
  if (!SYMPTOMS[code]) return;
  if (selectedSymptoms.has(code)) {
    selectedSymptoms.delete(code);
  } else {
    if (selectionCount() >= MAX_SYMPTOMS) {
      toast("가장 불편한 것 위주로 골라 주세요.");
      document.getElementById("trayCap").classList.remove("hidden");
      return;
    }
    selectedSymptoms.add(code);
    if (SYMPTOMS[code].type === "hidden") {
      hiddenNone = false;
      document.getElementById("hiddenNoneBtn").classList.remove("on");
    }
  }
  syncSymptomButtons();
  renderTray();
  updateCareNotice();
}

function toggleConcern(code) {
  if (code === "none") {
    selectedConcerns.clear();
    selectedConcerns.add("none");
  } else {
    selectedConcerns.delete("none");
    if (selectedConcerns.has(code)) selectedConcerns.delete(code);
    else selectedConcerns.add(code);
  }
  document.querySelectorAll("[data-concern]").forEach((b) =>
    b.classList.toggle("on", selectedConcerns.has(b.dataset.concern)));
}

function syncSymptomButtons() {
  document.querySelectorAll("[data-symptom]").forEach((b) =>
    b.classList.toggle("on", selectedSymptoms.has(b.dataset.symptom)));
}

function renderTray() {
  const box = document.getElementById("trayChips");
  box.innerHTML = "";
  selectedSymptoms.forEach((code) => {
    box.appendChild(trayChip(SYMPTOMS[code].label, () => toggleSymptom(code)));
  });
  extras.forEach((ex, i) => {
    box.appendChild(trayChip(ex.label, () => {
      extras.splice(i, 1);
      renderTray();
    }));
  });
  document.getElementById("trayCount").textContent = String(selectionCount());
  toStep2.disabled = !hasSelection();
  renderRefPreview();
}

function selectedRefs() {
  const seen = new Set();
  const refs = [];
  selectedSymptoms.forEach((code) => {
    const ref = SYMPTOMS[code] && SYMPTOMS[code].ref;
    if (!ref || !ref.kcd || seen.has(ref.kcd)) return;
    seen.add(ref.kcd);
    refs.push(ref);
  });
  return refs;
}

function renderRefPreview() {
  const box = document.getElementById("refPreview");
  const chips = document.getElementById("refChips");
  if (!box || !chips) return;
  const refs = selectedRefs();
  chips.innerHTML = "";
  if (!refs.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  refs.forEach((ref) => {
    const chip = document.createElement("span");
    chip.className = "cov-chip";
    chip.textContent = ref.name + " (" + ref.kcd + ")";
    chips.appendChild(chip);
  });
}

function trayChip(label, onRemove) {
  const wrap = document.createElement("span");
  wrap.className = "tray-chip";
  wrap.appendChild(document.createTextNode(label));
  const x = document.createElement("button");
  x.type = "button";
  x.setAttribute("aria-label", label + " 해제");
  x.textContent = "✕";
  x.addEventListener("click", onRemove);
  wrap.appendChild(x);
  return wrap;
}

function onFreeInput() {
  const q = (symptomInput.value || "").trim();
  toStep2.disabled = !hasSelection();
  if (q.length < 1) {
    suggest.classList.add("hidden");
    suggest.innerHTML = "";
    return;
  }
  const hits = Object.entries(SYMPTOMS).filter(([, s]) =>
    s.aliases.some((a) => a.includes(q) || q.includes(a)) || s.label.includes(q));
  if (!hits.length) {
    suggest.classList.add("hidden");
    return;
  }
  suggest.innerHTML = "";
  hits.forEach(([code, s]) => {
    const li = document.createElement("li");
    li.textContent = s.label;
    li.setAttribute("role", "option");
    li.addEventListener("click", () => {
      toggleSymptom(code);
      symptomInput.value = "";
      suggest.classList.add("hidden");
    });
    suggest.appendChild(li);
  });
  suggest.classList.remove("hidden");
}

function commitFreeInput() {
  const q = (symptomInput.value || "").trim();
  if (!q) return;
  const match = Object.entries(SYMPTOMS).find(([, s]) =>
    s.label === q || s.aliases.some((a) => a === q || q.includes(a)));
  if (match) {
    toggleSymptom(match[0]);
  } else {
    if (selectionCount() >= MAX_SYMPTOMS) {
      toast("가장 불편한 것 위주로 골라 주세요.");
      return;
    }
    extras.push({ label: q, type: "usual" });
    renderTray();
  }
  symptomInput.value = "";
  suggest.classList.add("hidden");
}

function goToStep(n) {
  wizardStep = n;
  maxReached = Math.max(maxReached, n);
  ["step1", "step2", "step3"].forEach((id, i) => {
    document.getElementById(id).classList.toggle("hidden", i + 1 !== n);
  });
  [1, 2, 3].forEach((i) => {
    const el = document.getElementById("stepDot" + i);
    el.classList.toggle("on", i === n);
    el.classList.toggle("done", i < n);
  });
  if (n === 1) renderTray();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderSymptomChips() {
  const usual = document.getElementById("usualChips");
  const hidden = document.getElementById("hiddenChips");
  if (!usual || !hidden) return;
  usual.innerHTML = "";
  hidden.innerHTML = "";
  Object.entries(SYMPTOMS).forEach(([code, s]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "symptom-btn";
    btn.dataset.symptom = code;
    btn.dataset.type = s.type;
    btn.innerHTML = escapeHtml(s.label) +
      ' <span class="check" aria-hidden="true">✓</span>';
    btn.addEventListener("click", () => toggleSymptom(code));
    if (s.type === "hidden") hidden.appendChild(btn);
    else usual.appendChild(btn);
  });
}

const GROUP_INFO = {
  usual: {
    title: "자주 느끼는 증상 — 분류 근거",
    body:
      "평소에도 자주 호소하는 불편을 모았습니다. 질병관리청 국가건강정보포털에서 흔한 증상으로 다루는 항목(두통·요통·소화불량 등)을 중심으로 큐레이션했습니다. 진단이 아니며, 관련 보험 종류를 안내하기 위한 참고 분류입니다.",
    src: "출처: 질병관리청 국가건강정보포털 증상 개요(큐레이션). 확진·추천이 아닙니다.",
  },
  hidden: {
    title: "놓치기 쉬운 불편 — 분류 근거",
    body:
      "가볍게 넘기기 쉽지만 다른 건강 상태와 함께 나타날 수 있어, 보장 탐색 때 놓치기 쉬운 불편을 모았습니다. 다리 쥐뿐 아니라 식욕·소변·대변 색·체중·구역·복통·숨참·수면 등 전신·소화·배설 신호도 포함합니다. 진단이 아니며, 위험 신호가 보이면 안내 문구를 확인하세요.",
    src: "출처: 질병관리청 국가건강정보포털 — 근육경련·두근거림 등 개요(큐레이션). 확진·추천이 아닙니다.",
  },
  areas: {
    title: "이 조합에서 염두에 둘 영역이란?",
    body:
      "선택하신 불편을 바탕으로, 보험 안내에서 함께 살펴볼 수 있는 참고 영역을 카드로 보여 줍니다. 의료 진단·확진이 아닙니다. 흔함·심각도 참고는 심평원 등 집단 청구 통계의 상대적 경향이고, 직군 요인은 업종 산재 통계에 근사 연결한 참고입니다. 카드에 적힌 한계 문구를 함께 읽어 주세요.",
    src: "근거: KB 영역 카탈로그 · 심평원 질병정보 · KOSIS 보조 통계(해당 시). 확진·상품 권유가 아닙니다.",
  },
};

function openSheet(title, body, src) {
  if (!sheet) return;
  const t = document.getElementById("sheetTitle");
  const b = document.getElementById("sheetBody");
  const s = document.getElementById("sheetSrc");
  if (t) t.textContent = title || "분류 근거";
  if (b) b.textContent = body || "";
  if (s) s.textContent = src || "";
  sheet.classList.remove("hidden");
}

function closeSheet() {
  if (sheet) sheet.classList.add("hidden");
}

document.addEventListener("click", (e) => {
  const btn = e.target && e.target.closest
    ? e.target.closest("[data-group-info]")
    : null;
  if (!btn) return;
  const info = GROUP_INFO[btn.dataset.groupInfo];
  if (!info) return;
  e.preventDefault();
  openSheet(info.title, info.body, info.src);
});

function updateCareNotice() {
  const el = document.getElementById("careNotice");
  if (!el) return;
  const notes = [];
  if (selectedSymptoms.has("palpitation")) {
    notes.push(
      "가슴 두근거림과 함께 심한 가슴 통증·실신·숨이 매우 가쁘면 응급 진료를 받으세요."
    );
  }
  if (selectedSymptoms.has("breath_short")) {
    notes.push(
      "숨이 갑자기 매우 차거나 가슴 통증·입술 청색·실신이 있으면 응급 진료를 받으세요."
    );
  }
  if (selectedSymptoms.has("stool_color")) {
    notes.push(
      "검은 타르변·혈변·회색 변과 황달·심한 복통이 동반되면 즉시 진료를 받으세요."
    );
  }
  if (selectedSymptoms.has("urine_color")) {
    notes.push(
      "소변색 변화와 함께 발열·옆구리 통증·황달(눈·피부 노란빛)이 있으면 진료를 받으세요."
    );
  }
  if (notes.length) {
    el.textContent =
      notes.join(" ") +
      " 진단이 아닌 일반 안내입니다. (질병관리청 국가건강정보포털 개요 기반)";
    el.classList.remove("hidden");
  } else {
    el.textContent = "";
    el.classList.add("hidden");
  }
}

function coverageChipsHtml(coverages) {
  return (coverages || []).map((c) => {
    const label = escapeHtml(c.label || "");
    const hint = (c.summary || (c.why && c.why[0]) || "").trim();
    const why = hint
      ? `<span class="cov-why">${escapeHtml(hint.length > 72 ? hint.slice(0, 70) + "…" : hint)}</span>`
      : "";
    return (
      `<div class="cov-item">` +
      `<strong class="cov-chip">${label}</strong>` +
      why +
      `</div>`
    );
  }).join("");
}

function formatCount(n) {
  if (n == null || n === "") return "";
  const num = Number(n);
  if (!Number.isFinite(num)) return String(n);
  return num.toLocaleString("ko-KR");
}

function publicEvidenceHtml(data) {
  const symptoms = (data.profile && data.profile.symptoms) || [];
  const kcdRows = [];
  const seen = new Set();
  const overviewRows = [];
  symptoms.forEach((s) => {
    (s.ref_conditions || []).forEach((rc) => {
      const key = (rc.kcd || "") + "|" + (rc.name || "");
      if (!rc.kcd || seen.has(key)) return;
      seen.add(key);
      kcdRows.push({
        symptom: s.label || "",
        kcd: rc.kcd,
        name: rc.name || "",
        source: rc.source || "",
      });
    });
    const ov = s.portal_overview;
    if (ov && ov.text) {
      overviewRows.push({
        symptom: s.label || "",
        text: ov.text,
        source: ov.source || "",
      });
    }
  });
  const kcdHtml = kcdRows.length
    ? '<div class="cov-grid evidence-kcd">' +
      kcdRows.map((r) =>
        `<div class="cov-item">` +
        `<strong>${escapeHtml(r.symptom || r.name)} → ${escapeHtml(r.name)} (${escapeHtml(r.kcd)})</strong>` +
        (r.source
          ? `<div class="src">출처: ${escapeHtml(r.source)}</div>`
          : "") +
        `</div>`
      ).join("") +
      "</div>"
    : "<p class='muted'>연결된 참고 상병 코드가 없습니다.</p>";
  const overviewHtml = overviewRows.length
    ? '<h3 class="evidence-sub">질병관리청 포털 개요 (참고)</h3>' +
      '<div class="portal-overview-list">' +
      overviewRows.map((r) =>
        `<blockquote class="portal-quote">` +
        `<strong>${escapeHtml(r.symptom)}</strong>` +
        `<p>${escapeHtml(r.text)}</p>` +
        (r.source
          ? `<footer class="src">출처: ${escapeHtml(r.source)}</footer>`
          : "") +
        `</blockquote>`
      ).join("") +
      "</div>"
    : "";
  return (
    '<section class="evidence-block" aria-label="공공의료 참고">' +
    '<h2 class="evidence-title">참고: 공공·상병 연결</h2>' +
    "<p class='muted'>진단·유병률이 아닙니다. 선택한 불편과 공개 참고 상병·청구 패턴을 연결한 결과입니다.</p>" +
    kcdHtml +
    overviewHtml +
    "</section>"
  );
}

function clusterEvidenceHtml(data) {
  const clusters = data.symptom_clusters || [];
  if (!clusters.length) return "";
  const profile = data.profile || {};
  const ageSexHint = [profile.age_label, profile.sex_label]
    .filter(Boolean).join(" · ");
  const clusterCards = clusters.map((cl) => {
    const fams = (cl.related_families || []).map((f) =>
      `<div class="cov-item">` +
      `<strong>${escapeHtml(f.label || "")}</strong>` +
      (f.blurb ? `<p class="cov-why">${escapeHtml(f.blurb)}</p>` : "") +
      (f.rationale_source
        ? `<div class="src">출처: ${escapeHtml(f.rationale_source)}</div>`
        : "") +
      ((f.example_kcds || []).length
        ? `<div class="src">참고 KCD: ${escapeHtml((f.example_kcds || []).join(", "))}</div>`
        : "") +
      `</div>`
    ).join("");
    return (
      `<div class="cluster-card">` +
      `<strong>${escapeHtml(cl.label || cl.id || "")}</strong>` +
      (cl.matched_symptoms && cl.matched_symptoms.length
        ? `<p class="stat-meta">매칭 증상: ${escapeHtml(cl.matched_symptoms.join(", "))}</p>`
        : "") +
      `<div class="cov-grid evidence-kcd">${fams}</div>` +
      (cl.occupation_note
        ? `<p class="stat-meta">${escapeHtml(cl.occupation_note)}</p>`
        : "") +
      (cl.care_hint
        ? `<p class="care-hint">${escapeHtml(cl.care_hint)}</p>`
        : "") +
      `</div>`
    );
  }).join("");
  const cstats = data.cluster_stats || [];
  const cstatsHtml = cstats.length
    ? cstats.map((st) => {
        const total = formatCount(st.total_count);
        const ageN = formatCount(st.age_sex_count);
        const share = st.inpatient_share != null
          ? Math.round(Number(st.inpatient_share) * 100) + "%"
          : "";
        const meta = [
          total ? `청구 통계상 총 ${total}건` : "",
          ageN
            ? `선택 연령·성별 ${ageN}건` +
              (ageSexHint ? ` (${escapeHtml(ageSexHint)})` : "")
            : "",
          share ? `입원 비중 ${share}` : "",
        ].filter(Boolean).join(" · ");
        return (
          `<div class="stat-card">` +
          `<strong>${escapeHtml(st.name || "")} (${escapeHtml(st.kcd || "")})</strong>` +
          (meta ? `<p class="stat-meta">${meta}</p>` : "") +
          (st.source
            ? `<p class="stat-meta">출처: ${escapeHtml(st.source)}</p>`
            : "") +
          (st.cached
            ? `<p class="stat-meta">${escapeHtml(st.note || "저장본. 시연용 참고 규모이며 실시간 공식 수치가 아님.")}</p>`
            : "") +
          `</div>`
        );
      }).join("")
    : "<p class='muted'>묶음 관련 KCD의 연령·성별 청구 규모를 아직 불러오지 못했습니다.</p>";
  return (
    '<section class="evidence-block cluster-block" aria-label="증상 묶음 참고">' +
    '<h2 class="evidence-title">같이 보면 참고할 수 있는 영역</h2>' +
    "<p class='muted'>전조·확진이 아닙니다. 공개 자료에서 함께 언급되는 관련 가능 영역과 연령·성별 청구 코호트입니다. 직업별 질환 통계는 아직 없습니다.</p>" +
    clusterCards +
    '<h3 class="evidence-sub">묶음 관련 청구 코호트 (연령·성별)</h3>' +
    cstatsHtml +
    "</section>"
  );
}

function composeSymptomText() {
  const chips = [...selectedSymptoms].map((c) => SYMPTOMS[c].label);
  const extra = extras.map((e) => e.label);
  const free = (symptomInput && symptomInput.value || "").trim();
  return [...chips, ...extra, free].filter(Boolean).join(" ").trim();
}

function concernPayload() {
  return [...selectedConcerns].filter((c) => c !== "none");
}

function showGuideLoading() {
  if (!guideResult) return;
  guideResult.classList.remove("hidden");
  guideResult.innerHTML =
    "<div class='guide-loading' role='status' aria-live='polite'>" +
    "<div class='guide-spinner' aria-hidden='true'></div>" +
    "<p>선택한 증상과 상황에 맞는 보장을 찾는 중…</p>" +
    "</div>";
}

async function runGuidanceFlow() {
  const text = composeSymptomText();
  if (!text) { toast("증상을 칩으로 고르거나 적어 주세요."); goToStep(1); return; }
  toResult.disabled = true;
  goToStep(3);
  showGuideLoading();
  try {
    const assessRes = await fetch(`${API}/v1/assess`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        text,
        occupation: selectedOcc,
        concerns: concernPayload(),
        age_band: selectedAge,
        sex: selectedSex,
      }),
    });
    const assessed = await assessRes.json();
    if (!assessRes.ok) throw new Error("assess " + assessRes.status);
    lastAssess = { data: assessed };
    renderCard(assessed);
    if (assessed.abstained) {
      guideResult.classList.add("hidden");
      guideResult.innerHTML = "";
      return;
    }
    const codes = (assessed.symptoms || []).map((s) => s.code);
    const gRes = await fetch(`${API}/v1/guidance`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        occupation: selectedOcc,
        concerns: concernPayload(),
        age_band: selectedAge,
        sex: selectedSex,
        symptoms: codes,
      }),
    });
    const guided = await gRes.json();
    if (!gRes.ok) throw new Error("guidance " + gRes.status);
    renderGuidance(guided);
  } catch (err) {
    if (guideResult) {
      guideResult.classList.remove("hidden");
      guideResult.innerHTML =
        "<p class='muted'>안내를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.</p>";
    }
    toast("안내 실패: " + err.message);
  } finally {
    toResult.disabled = false;
  }
}

function renderCard(data) {
  stateCard.classList.remove("hidden");
  const facts = document.getElementById("cardFacts");
  facts.innerHTML = "";
  const add = (s) => {
    const li = document.createElement("li");
    li.textContent = s;
    facts.appendChild(li);
  };
  if (data.abstained) {
    add("지원하는 증상으로 이해하지 못했습니다. 칩에서 고르거나 다시 적어 주세요.");
    return;
  }
  add("증상: " + (data.symptoms || []).map((s) => s.label).join(", "));
  if (data.occupation_label) add("직업군: " + data.occupation_label);
  const cl = data.concern_labels || [];
  if (cl.length) add("걱정: " + cl.join(", "));
  if (data.pii_kinds && Object.keys(data.pii_kinds).length)
    add("식별 정보는 가려졌고 원문은 저장하지 않습니다.");
}

const INSURER_MARKS = {
  "KB손해보험": "KB",
  "삼성생명": "삼성",
  "메리츠화재": "메리츠",
  "한화손해보험": "한화",
  "ABL생명": "ABL",
  "흥국생명": "흥국",
};

function insurerMarkHtml(insurer) {
  const name = insurer || "";
  const short = INSURER_MARKS[name] || (name ? name.slice(0, 2) : "?");
  return `<span class="insurer-mark" data-insurer="${escapeHtml(name)}" title="${escapeHtml(name || "보험사")}">${escapeHtml(short)}</span>`;
}

function productCompareHtml(products, productWhyHtml) {
  const cards = (products || []).slice(0, 2).map((p) => {
    const kinds = (p.matched_coverages || []).map((c) =>
      `<span class="cite">${escapeHtml(c)}</span>`).join("");
    const cite = p.citation_label
      ? `<div class="product-cite">${escapeHtml(p.citation_label)}</div>`
      : "";
    const badge = p.scope === "library"
      ? '<span class="scope-badge">참고 설명서</span>'
      : "";
    const cats = [p.product_line_label, p.doc_kind_label, p.cost_nature_label]
      .filter(Boolean)
      .map((lab) => `<span class="scope-badge">${escapeHtml(lab)}</span>`)
      .join("");
    const badges = (badge || cats)
      ? `<div class="product-badges">${badge}${cats}</div>`
      : "";
    const heading = p.name || p.title || "";
    const insurer = p.insurer
      ? `<div class="product-insurer">${escapeHtml(p.insurer)}</div>`
      : "";
    const summary = p.summary
      ? `<p class="product-summary">${escapeHtml(p.summary)}</p>`
      : "";
    const link = brochurePdfLink(p.document_id, p.scope === "library");
    const ask = p.document_id
      ? `<button type="button" class="ask-doc-btn" data-ask-doc="${escapeHtml(p.document_id)}" data-ask-name="${escapeHtml(heading)}" data-ask-insurer="${escapeHtml(p.insurer || "")}">질문하기</button>`
      : "";
    const actions = (link || ask)
      ? `<div class="brochure-actions">${link}${ask}</div>`
      : "";
    return `<div class="product-card">` +
      `<div class="product-card-head">${insurerMarkHtml(p.insurer)}<strong>${escapeHtml(heading)}</strong></div>` +
      insurer + badges +
      cite +
      summary +
      actions +
      (kinds ? `<div class="citations">${kinds}</div>` : "") +
      "</div>";
  }).join("");
  const whyBlock = productWhyHtml
    ? "<div class='why-block explain prose product-why'>" + productWhyHtml + "</div>"
    : "";
  if (cards) {
    return "<h2>증상에 알맞은 보험</h2>" +
      "<p class='muted'>상황에 맞춰 고른 상품요약서 2개입니다. 링크는 인덱싱한 PDF입니다.</p>" +
      whyBlock +
      "<div class='product-grid'>" + cards + "</div>";
  }
  return "<h2>증상에 알맞은 보험</h2>" +
    whyBlock +
    "<div class='product-empty'>" +
    "<p>공용 설명서가 아직 없습니다. 내 파일을 올리면 비교할 수 있습니다.</p>" +
    "<button type='button' id='openPolicyFromResult' class='btn-ghost'>약관 올리기</button>" +
    "</div>";
}

/** explanation_why를 공공 해석 / 상품 why로 나눈다. */
function splitBriefSections(text) {
  const raw = String(text || "").trim();
  if (!raw) return { medical: "", products: "" };
  const lines = raw.split("\n");
  const medical = [];
  const products = [];
  let mode = "medical"; // 헤딩 전 본문도 해석 쪽으로
  for (const line of lines) {
    const h = line.trim();
    if (/^##\s*비교할\s*상품/.test(h)) {
      mode = "products";
      products.push(line);
      continue;
    }
    if (/^##\s*무엇을\s*의미하나요/.test(h) ||
        /^##\s*진료[·・]?\s*생활에서\s*보면\s*좋은\s*점/.test(h) ||
        /^##\s*보험에서는\s*보통\s*이렇게\s*봅니다/.test(h) ||
        /^##\s*공공데이터\s*해석/.test(h) ||
        /^##\s*상황\s*종합/.test(h) ||
        /^##\s*주의[·・]?\s*참고/.test(h)) {
      mode = "medical";
      medical.push(line);
      continue;
    }
    if (mode === "products") products.push(line);
    else medical.push(line);
  }
  return {
    medical: medical.join("\n").trim(),
    products: products.join("\n").trim(),
  };
}

function buildMatchContext(data) {
  const parts = [];
  const why = String((data && data.explanation_why) || "").trim();
  if (why) parts.push("【왜 이 보험인가요】\n" + why);
  const products = (data && data.products) || [];
  for (const p of products) {
    if (!p) continue;
    const insurer = String(p.insurer || "").trim();
    const name = String(p.display_name || p.name || "").trim();
    const summary = String(p.summary || "").trim();
    const covs = (p.matched_coverages || [])
      .map((c) => String(c || "").trim())
      .filter(Boolean)
      .join(", ");
    const head = [insurer, name].filter(Boolean).join(" ");
    const lines = [];
    if (head) lines.push("【상품】" + head);
    if (summary) lines.push("요약: " + summary);
    if (covs) lines.push("매칭 담보: " + covs);
    if (lines.length) parts.push(lines.join("\n"));
  }
  return parts.join("\n\n").slice(0, 4000);
}

function resultChatHtml() {
  const chips = RESULT_CHAT_SUGGESTS.map((q) =>
    `<button type="button" class="chip result-chat-suggest">${escapeHtml(q)}</button>`
  ).join("");
  return (
    "<div class='result-chat' id='resultChat'>" +
    "<h3>더 궁금한 점이 있으면 물어보세요</h3>" +
    "<p class='muted'>상황 정리와 상품요약서를 함께 참고해 질문해 보세요.</p>" +
    "<div class='ask-suggest result-chat-suggests'>" + chips + "</div>" +
    "<div class='result-chat-messages' id='resultChatMessages'></div>" +
    "<form class='result-chat-form' id='resultChatForm'>" +
    "<textarea id='resultChatInput' rows='2' placeholder='예: 삼성 상품이 증상과 어떻게 연결되나요?'></textarea>" +
    "<button type='submit' id='resultChatSend' class='primary-btn'>보내기</button>" +
    "</form>" +
    "</div>"
  );
}

function orNone(value) {
  return value
    ? escapeHtml(value)
    : '<span class="muted">해당 없음</span>';
}

function explainHtml(text) {
  if (!text) return "";
  return formatAnswerHtml(text);
}

function factorRows(profile) {
  const p = profile || {};
  const concerns = (p.concern_labels || []).join(", ");
  return (
    '<div class="factor-row"><span class="type-label">상황</span>' +
    `<span>직업 ${orNone(p.occupation_label)}</span>` +
    `<span>걱정 ${orNone(concerns)}</span>` +
    `<span>연령 ${orNone(p.age_label)}</span>` +
    `<span>성별 ${orNone(p.sex_label)}</span>` +
    "</div>"
  );
}

/** A. 입력 확인 — 판단 없이 메아리만. */
function inputEchoHtml(symptoms, profile) {
  const usual = (symptoms || []).filter((s) => (s.type || "usual") !== "hidden");
  const hidden = (symptoms || []).filter((s) => s.type === "hidden");
  const chip = (s, kind) =>
    `<span class="type-badge ${kind}">${escapeHtml(s.label)}</span>`;
  const usualHtml = usual.length
    ? usual.map((s) => chip(s, "usual")).join("")
    : `<span class="muted">해당 없음</span>`;
  const hiddenHtml = hidden.length
    ? hidden.map((s) => chip(s, "hidden-type")).join("")
    : `<span class="muted">해당 없음</span>`;
  return (
    '<section class="input-echo" aria-label="내가 입력한 상태">' +
    "<h2>내가 입력한 상태</h2>" +
    "<p class='muted'>선택하신 내용을 그대로 확인합니다. 판단은 넣지 않습니다.</p>" +
    '<div class="type-row"><span class="type-label">자주 느끼는 증상</span>' +
    usualHtml + "</div>" +
    '<div class="type-row"><span class="type-label">놓치기 쉬운 불편</span>' +
    hiddenHtml + "</div>" +
    factorRows(profile) +
    "</section>"
  );
}

function _evidenceLine(title, block) {
  const b = block || {};
  const missing = b.missing || b.label === "찾지 못했어요";
  const label = escapeHtml(b.label || "찾지 못했어요");
  let body = `<strong>${escapeHtml(title)}</strong> ${label}`;
  if (!missing && b.detail) {
    body += ` <span class="stat-meta">(${escapeHtml(b.detail)})</span>`;
  }
  if (!missing && b.inpatient_share != null && Number.isFinite(Number(b.inpatient_share))) {
    body += ` <span class="stat-meta">입원 비중 ${Math.round(Number(b.inpatient_share) * 100)}%</span>`;
  }
  if (missing && b.reason) {
    body += ` <span class="stat-meta">${escapeHtml(b.reason)}</span>`;
  }
  if (b.source) {
    body += `<div class="src">출처: ${escapeHtml(b.source)}</div>`;
  }
  if (b.note) {
    body += `<div class="src">${escapeHtml(b.note)}</div>`;
  }
  return `<li class="area-evidence">${body}</li>`;
}

function areaSectionTitleHtml() {
  return (
    "<div class='evidence-title-row'>" +
    "<h2 class='evidence-title'>이 조합에서 염두에 둘 영역</h2>" +
    "<button type='button' class='info-btn' data-group-info='areas' " +
    "aria-label='염두에 둘 영역 설명'>?</button>" +
    "</div>"
  );
}

/** B. 염두에 둘 영역 카드 */
function areaCardsHtml(data) {
  const cards = data.area_cards || [];
  if (!cards.length) {
    return (
      '<section class="evidence-block area-cards-block" aria-label="염두에 둘 영역">' +
      areaSectionTitleHtml() +
      "<p class='muted'>지금은 연결할 참고 영역 카드가 없습니다.</p>" +
      "</section>"
    );
  }
  const items = cards.map((card, i) => {
    const linked = (card.linked_symptoms || []).map(escapeHtml).join(", ");
    const groups = (card.candidate_groups || [])
      .map((g) => escapeHtml(g.label || ""))
      .filter(Boolean)
      .join(" · ");
    const occ = card.occupation_factor || {};
    const occText = escapeHtml(occ.text || "찾지 못했어요");
    const occReason = (occ.missing && occ.reason)
      ? ` <span class="stat-meta">${escapeHtml(occ.reason)}</span>`
      : "";
    const occSrc = occ.source
      ? `<div class="src">출처: ${escapeHtml(occ.source)}</div>`
      : "";
    const occLimit = occ.claims_not_incidence
      ? "<div class='src'>산재 처리 건수 기준이며 발생률은 아닙니다.</div>"
      : "";
    const limits = (card.limits || [])
      .map((t) => `<li>${escapeHtml(t)}</li>`)
      .join("");
    const care = card.care_context;
    const careIn = care && care.inpatient;
    const careLi = (care && !care.missing)
      ? _evidenceLine("이용·비용 맥락", care) +
        (careIn && !careIn.missing
          ? `<li class="area-evidence"><strong>입원 이용</strong> ${escapeHtml(careIn.label || "")}` +
            (careIn.detail ? ` — ${escapeHtml(careIn.detail)}` : "") +
            (careIn.source
              ? `<div class="src">출처: ${escapeHtml(careIn.source)}</div>`
              : "") +
            `</li>`
          : "") +
        (care.note
          ? `<li class="area-evidence muted">${escapeHtml(care.note)}</li>`
          : "")
      : "";
    const surg = card.surgery_context;
    const surgLi = (surg && !surg.missing)
      ? _evidenceLine("수술 참고(건강보험)", surg) +
        (surg.note
          ? `<li class="area-evidence muted">${escapeHtml(surg.note)}</li>`
          : "")
      : "";
    const d298 = card.disease_298;
    const d298Li = d298
      ? _evidenceLine("질병분류 급여(298)", d298) +
        (d298.note
          ? `<li class="area-evidence muted">${escapeHtml(d298.note)}</li>`
          : "")
      : "";
    return (
      `<article class="area-card cluster-card">` +
      `<strong>${i + 1}. ${escapeHtml(card.title || "")}</strong>` +
      (linked
        ? `<p class="stat-meta">${linked}과 연결</p>`
        : "") +
      (groups
        ? `<p>살펴볼 수 있는 질환군: ${groups}</p>`
        : "") +
      `<ul class="area-evidence-list">` +
      _evidenceLine("흔함", card.commonness) +
      _evidenceLine("심각도 참고", card.severity) +
      careLi +
      d298Li +
      surgLi +
      `<li class="area-evidence"><strong>직군 요인</strong> ${occText}${occReason}${occSrc}${occLimit}</li>` +
      `</ul>` +
      (limits
        ? `<ul class="area-limits muted">${limits}</ul>`
        : "") +
      `</article>`
    );
  }).join("");
  return (
    '<section class="evidence-block area-cards-block" aria-label="염두에 둘 영역">' +
    areaSectionTitleHtml() +
    "<p class='muted'>진단이 아니라 참고예요. 흔함은 이 안내가 살피는 참고 질환군 안에서의 상대적 흔함입니다. 이용·비용 맥락은 연령·성별 전체 진료 통계(KOSIS·건보공단)이며 특정 상병 유병이 아닙니다.</p>" +
    `<div class="area-card-grid">${items}</div>` +
    "</section>"
  );
}

/** 공공 통계 참고 — 접지 않음 (보험 C와 분리) */
function publicStatsPanelHtml(data) {
  const panel = data.public_stats || {};
  const items = panel.items || [];
  if (!items.length && !(panel.notes || []).length) return "";
  const rows = items.map((it) => {
    const missing = it.missing || it.label === "찾지 못했어요";
    let body = `<strong>${escapeHtml(it.title || "")}</strong> `;
    if (missing) {
      body += escapeHtml(it.label || "찾지 못했어요");
      if (it.reason) {
        body += ` <span class="stat-meta">${escapeHtml(it.reason)}</span>`;
      }
    } else {
      body += escapeHtml(it.detail || it.label || "");
    }
    if (it.source) {
      body += `<div class="src">출처: ${escapeHtml(it.source)}</div>`;
    }
    if (it.note) {
      body += `<div class="src">${escapeHtml(it.note)}</div>`;
    }
    return `<li class="public-stat-item">${body}</li>`;
  }).join("");
  const notes = (panel.notes || [])
    .map((n) => `<li class="muted">${escapeHtml(n)}</li>`)
    .join("");
  return (
    '<section class="public-stats-panel" aria-label="공공 통계 참고">' +
    `<h2>${escapeHtml(panel.title || "공공 통계 참고")}</h2>` +
    "<p class='muted'>진단·유병이 아닙니다. 연령·성별 전체 진료·산재 업종 통계를 한곳에 모았습니다.</p>" +
    (rows ? `<ul class="public-stats-list">${rows}</ul>` : "") +
    (notes ? `<ul class="public-stats-notes">${notes}</ul>` : "") +
    "</section>"
  );
}

/** 왜 이런 보장을 살펴보나요 — 결정론 steps */
function whyInsuranceHtml(data) {
  const r = data.recommendation_rationale || {};
  const steps = r.steps || [];
  if (!steps.length) return "";
  const lis = steps.map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  const miss = (r.missing_notes || [])
    .map((n) => `<li class="muted">${escapeHtml(n)}</li>`)
    .join("");
  return (
    '<section class="why-insurance" aria-label="왜 이런 보장">' +
    "<h2>왜 이런 보장을 살펴보나요</h2>" +
    `<p class="why-principle">${escapeHtml(r.principle || "진단 아님 · 상품 권유 아님 · 가능 영역/보장 유형 연결")}</p>` +
    `<ol class="why-steps">${lis}</ol>` +
    (miss ? `<ul class="why-missing">${miss}</ul>` : "") +
    "</section>"
  );
}

/** C. 보험 연결 — 보장 축 문장만 (KOSIS 통계 블록은 결과 화면에서 숨김) */
function insuranceBridgeHtml(data) {
  const bridge = data.insurance_bridge || {};
  const paras = (bridge.paragraphs || [])
    .map((p) => `<p>${escapeHtml(p)}</p>`)
    .join("");
  if (!paras) return "";
  return (
    '<section class="insurance-bridge" aria-label="보험으로 연결">' +
    "<h2>보험으로 연결해서 보면</h2>" +
    "<p class='muted'>특정 상품 권유가 아닙니다. 보장 축만 점검합니다.</p>" +
    paras +
    "</section>"
  );
}

/** D. 추가 질문 (정적, 제출 없음) */
function deepenQuestionsHtml() {
  return (
    "<details class='policy-fold deepen-fold'>" +
    "<summary>더 알려주시면 정확해져요 (선택)</summary>" +
    "<p class='muted'>지금은 참고용 안내입니다. 답변 제출은 곧 연결됩니다.</p>" +
    "<ul class='deepen-list'>" +
    "<li>증상이 얼마나 이어졌나요? (오늘 · 1주 이내 · 1개월 이상)</li>" +
    "<li>어떤 운전·작업을 하시나요? (장시간·중량물 등)</li>" +
    "<li>기존에 실손·단체보험이 있나요?</li>" +
    "</ul></details>"
  );
}

/** E. 안전·면책 */
function safetyHtml(data) {
  const disc = escapeHtml(data.disclaimer || "");
  let care = "";
  for (const cl of data.symptom_clusters || []) {
    if (cl && cl.care_hint) {
      care = String(cl.care_hint);
      break;
    }
  }
  if (!care) {
    care =
      "갑작스러운 극심한 두통, 힘이 빠지는 증상, 말이 어눌해지는 증상이 있다면 " +
      "바로 응급실을 이용하세요.";
  }
  return (
    '<section class="safety-block" aria-label="주의·안전">' +
    (disc ? `<p class="disclaimer">${disc}</p>` : "") +
    `<p class="care-hint">${escapeHtml(care)}</p>` +
    "</section>"
  );
}

function legacyBriefFoldHtml(medicalHtml, data) {
  const cluster = clusterEvidenceHtml(data);
  const pub = publicEvidenceHtml(data);
  if (!medicalHtml && !cluster && !pub) return "";
  return (
    "<details class='policy-fold legacy-brief-fold'>" +
    "<summary>이전 참고 해석·공공 원자료 (접힘)</summary>" +
    (medicalHtml
      ? "<div class='why-block explain prose'>" + medicalHtml + "</div>"
      : "") +
    cluster +
    pub +
    "</details>"
  );
}

function renderGuidance(data) {
  guideResult.classList.remove("hidden");
  if (!data.abstained) stateCard.classList.add("hidden");
  if (data.abstained) {
    guideResult.innerHTML = "<p class='muted'>안내를 제공할 수 없습니다: " +
      escapeHtml(data.abstain_reason || "") + "</p>";
    return;
  }
  const symptoms = (data.profile && data.profile.symptoms) || [];
  const covs = coverageChipsHtml(data.coverages);
  const cites = (data.citations || []).map((c) =>
    `<span class="cite">${escapeHtml(c)}</span>`).join("");
  const evidence = cites
    ? "<div class='citations'>" + cites + "</div>"
    : "<p class='muted'>업로드된 약관이 없어 근거 라벨을 붙이지 않았습니다. 위쪽 「약관 올리기」를 이용하세요.</p>";
  const brief = splitBriefSections(data.explanation_why || "");
  const medicalHtml = explainHtml(brief.medical);
  const productWhyHtml = explainHtml(brief.products);
  resultChatConvId = newConversationId();
  resultChatDocIds = (data.products || [])
    .map((p) => p && p.document_id)
    .filter(Boolean);
  lastGuidanceMatch = buildMatchContext(data);
  guideResult.innerHTML =
    inputEchoHtml(symptoms, data.profile) +
    areaCardsHtml(data) +
    publicStatsPanelHtml(data) +
    insuranceBridgeHtml(data) +
    whyInsuranceHtml(data) +
    productCompareHtml(data.products, productWhyHtml) +
    deepenQuestionsHtml() +
    safetyHtml(data) +
    legacyBriefFoldHtml(medicalHtml, data) +
    "<details class='policy-fold cov-fold'><summary>연결된 실손 보장 종류</summary>" +
    "<div class='cov-grid cov-grid-items'>" + covs + "</div></details>" +
    resultChatHtml() +
    "<details class='policy-fold'><summary>약관 근거 (선택)</summary>" +
    evidence + "</details>" +
    "<button type='button' id='purgeBtn' class='danger-btn'>안내 종료 · 이 기기에서 내 입력 지우기</button>";
  const purgeBtn = document.getElementById("purgeBtn");
  if (purgeBtn) purgeBtn.addEventListener("click", purgeSession);
  const openPolicy = document.getElementById("openPolicyFromResult");
  if (openPolicy) openPolicy.addEventListener("click", () => {
    const toggle = document.getElementById("policyToggle");
    if (toggle) toggle.click();
  });
  guideResult.querySelectorAll(".ask-doc-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      openAskForDoc({
        document_id: btn.dataset.askDoc,
        display_name: btn.dataset.askName,
        insurer: btn.dataset.askInsurer,
      });
    });
  });
  bindResultChat();
  guideResult.scrollIntoView({ behavior: "smooth", block: "start" });
}

function bindResultChat() {
  const form = document.getElementById("resultChatForm");
  const input = document.getElementById("resultChatInput");
  const box = document.getElementById("resultChatMessages");
  if (!form || !input || !box) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q || resultChatBusy) return;
    input.value = "";
    await sendResultChat(q);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    form.requestSubmit();
  });
  guideResult.querySelectorAll(".result-chat-suggest").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (resultChatBusy) return;
      sendResultChat(btn.textContent.trim());
    });
  });
}

function addResultMessage(role, text) {
  const box = document.getElementById("resultChatMessages");
  if (!box) return null;
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return { wrap, bubble, box };
}

async function sendResultChat(query) {
  if (!query || resultChatBusy) return;
  if (!resultChatDocIds.length) {
    toast("위에 표시된 설명서가 없어 질문할 수 없습니다.");
    return;
  }
  resultChatBusy = true;
  const send = document.getElementById("resultChatSend");
  if (send) send.disabled = true;
  addResultMessage("user", query);
  try {
    await streamAnswerInto({
      query,
      conversationId: resultChatConvId || newConversationId(),
      documentIds: resultChatDocIds,
      matchContext: lastGuidanceMatch || undefined,
      onStart: () => addResultMessage("bot", ""),
      scrollEl: () => document.getElementById("resultChatMessages"),
    });
  } finally {
    resultChatBusy = false;
    if (send) send.disabled = false;
  }
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
  extras.length = 0;
  selectedOcc = null;
  selectedAge = null;
  selectedSex = null;
  selectedConcerns.clear();
  hiddenNone = false;
  maxReached = 1;
  document.querySelectorAll("[data-symptom], [data-occ], [data-concern], [data-age], [data-sex]").forEach((b) =>
    b.classList.remove("on"));
  document.getElementById("hiddenNoneBtn").classList.remove("on");
  if (symptomInput) symptomInput.value = "";
  stateCard.classList.add("hidden");
  document.getElementById("cardFacts").innerHTML = "";
  guideResult.classList.add("hidden");
  guideResult.innerHTML = "";
  conversationId = newConversationId();
  renderTray();
  goToStep(1);
  toast("이 화면의 입력을 지웠습니다. 약관 문서는 그대로입니다.");
}

if (newChatBtn) {
  newChatBtn.addEventListener("click", () => {
    conversationId = newConversationId();
    resetAskMessages();
    toast(askDoc ? "같은 설명서로 새 대화를 시작합니다." : "새 대화를 시작합니다.");
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

function brochurePdfHref(id) {
  return `${API}/v1/documents/${encodeURIComponent(id)}/file`;
}

async function openBrochurePdf(documentId) {
  const id = (documentId || "").trim();
  if (!id) {
    toast("설명서 파일을 열 수 없습니다.");
    return;
  }
  try {
    const res = await fetch(brochurePdfHref(id), { headers: headers() });
    if (!res.ok) throw new Error("pdf " + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const win = window.open(url, "_blank");
    if (win) win.opener = null;
    else toast("팝업이 차단됐습니다. 브라우저에서 허용해 주세요.");
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (err) {
    toast("설명서 PDF를 열지 못했습니다.");
  }
}

document.addEventListener("click", (e) => {
  const matchBtn = e.target && e.target.closest
    ? e.target.closest("[data-match-ref]")
    : null;
  if (matchBtn) {
    e.preventDefault();
    openMatchContextSheet();
    return;
  }
  const a = e.target && e.target.closest
    ? e.target.closest("a[data-doc-id], .ref-mark[data-doc-id], a.cite[data-doc-id]")
    : null;
  if (!a) return;
  const id = a.getAttribute("data-doc-id");
  if (!id || id === "match-context") {
    if (id === "match-context") {
      e.preventDefault();
      openMatchContextSheet();
    }
    return;
  }
  e.preventDefault();
  openBrochurePdf(id);
});

function canOpenBrochurePdf(d) {
  return !!(d && d.document_id && (d.has_file || d.scope === "library"));
}

function brochurePdfAnchor(d) {
  if (!canOpenBrochurePdf(d)) return null;
  const a = document.createElement("a");
  a.className = "product-source";
  a.href = brochurePdfHref(d.document_id);
  a.setAttribute("data-doc-id", d.document_id);
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = "설명서 PDF";
  return a;
}

function brochurePdfLink(id, allow) {
  if (!id || !allow) return "";
  return `<a class="product-source" href="${escapeHtml(brochurePdfHref(id))}" data-doc-id="${escapeHtml(id)}" target="_blank" rel="noopener noreferrer">설명서 PDF</a>`;
}

function fillBrochurePanel(docs) {
  if (!brochureList) return;
  brochureList.innerHTML = "";
  const ready = (docs || []).filter((d) => d.status === "ready");
  if (!ready.length) {
    const empty = document.createElement("li");
    empty.className = "doc-empty";
    empty.innerHTML =
      "아직 준비된 참고 설명서가 없습니다. " +
      "<button type='button' class='btn-ghost' id='brochureEmptyUpload'>약관 올리기</button>";
    brochureList.appendChild(empty);
    const cta = empty.querySelector("#brochureEmptyUpload");
    if (cta) {
      cta.addEventListener("click", () => {
        const toggle = document.getElementById("policyToggle");
        if (toggle) toggle.click();
      });
    }
    return;
  }
  ready.forEach((d) => {
    const li = document.createElement("li");
    li.className = "brochure-card";
    const title = document.createElement("strong");
    title.textContent = d.display_name || d.filename || "";
    li.appendChild(title);
    if (d.insurer) {
      const ins = document.createElement("div");
      ins.className = "product-insurer";
      ins.textContent = d.insurer;
      li.appendChild(ins);
    }
    const badgeLabels = [d.product_line_label, d.doc_kind_label, d.cost_nature_label]
      .filter(Boolean);
    if (badgeLabels.length) {
      const badges = document.createElement("div");
      badges.className = "product-badges";
      badgeLabels.forEach((lab) => {
        const badge = document.createElement("span");
        badge.className = "scope-badge";
        badge.textContent = lab;
        badges.appendChild(badge);
      });
      li.appendChild(badges);
    }
    if (d.summary) {
      const p = document.createElement("p");
      p.className = "brochure-summary";
      p.textContent = d.summary;
      li.appendChild(p);
    }
    const actions = document.createElement("div");
    actions.className = "brochure-actions";
    const pdf = brochurePdfAnchor(d);
    if (pdf) actions.appendChild(pdf);
    const askBtn = document.createElement("button");
    askBtn.type = "button";
    askBtn.className = "ask-doc-btn";
    askBtn.textContent = "질문하기";
    askBtn.addEventListener("click", () => openAskForDoc(d));
    actions.appendChild(askBtn);
    li.appendChild(actions);
    brochureList.appendChild(li);
  });
}

function renderDocItem(d, { canDelete }) {
  const li = document.createElement("li");
  li.className = "doc-item";
  const name = document.createElement("span");
  name.className = "doc-name";
  name.title = d.filename || "";
  name.textContent = d.display_name || d.filename || "";
  const cats = [d.insurer, d.product_line_label, d.doc_kind_label, d.cost_nature_label]
    .filter(Boolean);
  if (cats.length) {
    const cat = document.createElement("span");
    cat.className = "doc-cats";
    cat.textContent = cats.join(" · ");
    name.appendChild(document.createElement("br"));
    name.appendChild(cat);
  }
  if (d.summary) {
    const sum = document.createElement("p");
    sum.className = "brochure-summary";
    sum.textContent = d.summary;
    name.appendChild(sum);
  }
  const pdf = brochurePdfAnchor(d);
  if (pdf) {
    name.appendChild(document.createElement("br"));
    name.appendChild(pdf);
  }
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
  if (canDelete && d.status !== "processing") {
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
  return li;
}

function fillDocList(el, docs, { canDelete, emptyText }) {
  if (!el) return;
  el.innerHTML = "";
  if (!docs.length) {
    const empty = document.createElement("li");
    empty.className = "doc-empty";
    empty.textContent = emptyText;
    el.appendChild(empty);
    return;
  }
  docs.forEach((d) => el.appendChild(renderDocItem(d, { canDelete })));
}

async function refreshDocs() {
  try {
    const res = await fetch(`${API}/v1/documents`, { headers: headers() });
    if (!res.ok) return;
    const docs = await res.json();
    const lib = docs.filter((d) => d.scope === "library");
    const mine = docs.filter((d) => d.scope !== "library");
    fillBrochurePanel(lib);
    fillDocList(docList, mine, {
      canDelete: true,
      emptyText: "올린 파일이 없습니다.",
    });
    const readyLib = lib.filter((d) => d.status === "ready").length;
    const count = document.getElementById("kbCount");
    if (count) count.textContent = `설명서 ${readyLib}건`;
    if (docs.some((d) => d.status === "processing")) {
      setTimeout(refreshDocs, 2000);
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
    if (e.key !== "Enter" || e.shiftKey) return;
    if (e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    composer.requestSubmit();
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
  try {
    await streamAnswerInto({
      query,
      conversationId,
      documentId: askDoc && askDoc.document_id ? askDoc.document_id : null,
      onStart: () => addMessage("bot", ""),
      scrollEl: () => messages,
    });
  } finally {
    sendBtn.disabled = false;
  }
}

async function streamAnswerInto({
  query, conversationId: convId, documentId, documentIds, matchContext,
  onStart, scrollEl,
}) {
  const startedMsg = onStart();
  const wrap = startedMsg.wrap;
  const bubble = startedMsg.bubble;
  bubble.classList.add("thinking");
  bubble.textContent = "검색 중…";
  let answer = "";
  let started = false;
  let failed = false;
  const sourceByN = {};
  const scroll = () => {
    const el = typeof scrollEl === "function" ? scrollEl() : scrollEl;
    if (el) el.scrollTop = el.scrollHeight;
  };

  try {
    const body = {
      query,
      conversation_id: convId,
    };
    if (documentId) body.document_id = documentId;
    if (documentIds && documentIds.length) body.document_ids = documentIds;
    if (matchContext) body.match_context = matchContext;
    const res = await fetch(`${API}/v1/query/stream`, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
    if (res.status === 404) {
      bubble.classList.remove("thinking");
      bubble.classList.add("abstain");
      bubble.textContent = "선택한 설명서를 찾지 못했습니다. 다른 설명서를 골라 주세요.";
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

    if (!failed && answer) {
      bubble.classList.add("prose");
      bubble.innerHTML = formatAnswerHtml(answer, sourceByN);
      scroll();
    }

    function handleEvent(evt) {
      if (evt.kind === "sources" && Array.isArray(evt.data)) {
        evt.data.forEach((s) => {
          if (s && s.n != null) sourceByN[String(s.n)] = s;
        });
      } else if (evt.kind === "token") {
        if (!started) {
          bubble.classList.remove("thinking");
          bubble.textContent = "";
          started = true;
        }
        answer += evt.data;
        bubble.textContent = answer;
      } else if (evt.kind === "abstain") {
        failed = true;
        bubble.classList.remove("thinking");
        bubble.classList.add("abstain");
        bubble.textContent = abstainMessage(evt.data);
      } else if (evt.kind === "error") {
        failed = true;
        bubble.classList.remove("thinking");
        bubble.classList.add("abstain");
        bubble.textContent =
          "서버 오류로 답변을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.";
      } else if (evt.kind === "citations") {
        renderCitations(wrap, evt.data);
      }
      scroll();
    }
  } catch (err) {
    bubble.classList.remove("thinking");
    bubble.classList.add("abstain");
    bubble.textContent = "오류가 발생했습니다: " + err.message;
  }
}

function formatAnswerHtml(text, sourceByN) {
  const escaped = escapeHtml(text || "");
  const lines = escaped.split("\n");
  const out = [];
  let para = [];
  let list = [];

  function flushPara() {
    if (!para.length) return;
    out.push("<p>" + inlineAnswerHtml(para.join("\n"), sourceByN).replace(/\n/g, "<br>") + "</p>");
    para = [];
  }
  function flushList() {
    if (!list.length) return;
    out.push("<ul>" + list.map((item) => "<li>" + inlineAnswerHtml(item, sourceByN) + "</li>").join("") + "</ul>");
    list = [];
  }

  for (const raw of lines) {
    const trimmed = raw.trim();
    const heading = trimmed.match(/^##\s+(.+)$/);
    const bullet = trimmed.match(/^(\*|-)\s+(.+)$/);
    if (!trimmed) {
      flushPara();
      flushList();
      continue;
    }
    if (heading) {
      flushPara();
      flushList();
      out.push("<h3 class='brief-h'>" + inlineAnswerHtml(heading[1], sourceByN) + "</h3>");
      continue;
    }
    if (bullet) {
      flushPara();
      list.push(bullet[2]);
      continue;
    }
    flushList();
    para.push(raw);
  }
  flushPara();
  flushList();
  return out.join("") || "";
}

function isMatchContextSource(src) {
  if (!src) return false;
  const id = String(src.document_id || "");
  const lab = String(src.citation_label || src.source_document || "");
  return id === "match-context" || lab.indexOf("매칭 안내") >= 0;
}

function matchRefMarkHtml() {
  return (
    '<button type="button" class="ref-mark ref-mark-btn" data-match-ref="1">' +
    "[매칭 안내]</button>"
  );
}

function openMatchContextSheet() {
  const body = (lastGuidanceMatch || "").trim()
    ? lastGuidanceMatch.slice(0, 1800)
    : "이 안내는 결과 화면의 증상·상품 연결 요약입니다. PDF 설명서가 아닙니다.";
  openSheet(
    "매칭 안내",
    body,
    "결과 단계의 증상·담보·상품 요약입니다. 약관 PDF와 다릅니다."
  );
}

function docRefMarkHtml(n, sourceByN) {
  const src = sourceByN && sourceByN[String(n)];
  if (isMatchContextSource(src)) {
    return matchRefMarkHtml();
  }
  const page = src && src.page;
  const label = (page != null && page !== "")
    ? "[" + page + "쪽 참고]"
    : "[설명서 참고]";
  const id = src && src.document_id;
  if (id && id !== "match-context") {
    return '<a class="ref-mark" href="' + escapeHtml(brochurePdfHref(id)) +
      '" data-doc-id="' + escapeHtml(id) +
      '" target="_blank" rel="noopener">' + label + "</a>";
  }
  return '<span class="ref-mark">' + label + "</span>";
}

function inlineAnswerHtml(s, sourceByN) {
  return (s || "")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[\s*문서\s*((?:\d+\s*,?\s*)+)\]/g, (_, nums) => {
      const parts = String(nums).match(/\d+/g) || [];
      if (!parts.length) return '<span class="ref-mark">[설명서 참고]</span>';
      return parts.map((n) => docRefMarkHtml(n, sourceByN)).join(", ");
    });
}

function abstainMessage(reason) {
  // Soft abstain: 침묵 보류 대신 다음 행동 안내 (확정 수치는 만들지 않음).
  if (reason === "empty_context" || reason === "low_score"
      || reason === "no_evidence_in_documents" || reason === "missing_citation")
    return (
      "이 설명서에서 질문과 바로 맞는 구절을 찾지 못했습니다. "
      + "날짜·일수 같은 확정 수치는 지어내지 않습니다.\n\n"
      + "「보험기간」「책임개시」「면책」「대기」처럼 약관 용어로 다시 물어보시면 "
      + "찾기가 쉽습니다. 예: 책임개시일은 언제인가요? / 면책·대기기간은 며칠인가요?\n\n"
      + "확정이 필요하면 보험사 고객센터·설계사 상담을 함께 이용해 주세요."
    );
  if ((reason || "").startsWith("fabricated_citation"))
    return "답변 검증에 실패하여 응답을 보류합니다. 질문을 조금 바꿔 다시 시도해 주세요.";
  if (reason === "llm_unavailable")
    return "답변 생성 서비스가 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요.";
  if (reason === "llm_cost_limit")
    return "질문이 너무 넓어 한 번에 답하기 어렵습니다. 담보·기간을 좁혀 다시 물어봐 주세요.";
  if (reason === "embedding_unavailable")
    return (
      "문서 검색 서비스가 일시적으로 불가합니다. "
      + "가입 방법·보험료 확인 경로처럼 기본 안내는 다시 물어보시면 "
      + "설명서 검색 없이 안내할 수 있습니다. 잠시 후 다시 시도해 주세요."
    );
  if (reason === "injection_blocked" || reason === "injection_canary_leak"
      || (reason || "").startsWith("injection"))
    return "보안 정책에 따라 답변을 보류합니다. 질문을 바꿔 다시 시도해 주세요.";
  return (
    "이 설명서만으로는 확답하기 어렵습니다. "
    + "용어를 바꿔 물어보시거나 상품 카드의 질문하기를 이용해 주세요."
  );
}

function renderCitations(wrap, data) {
  let items = [];
  if (Array.isArray(data)) {
    items = data;
  } else if (typeof data === "string" && data.trim()) {
    items = data.split(";").map((s) => s.trim()).filter(Boolean).map((l) => ({
      citation_label: l, source_document: "", page: null,
    }));
  }
  if (!items.length) return;
  const box = document.createElement("div");
  box.className = "citations";
  items.forEach((c) => {
    const page = c.page == null || c.page === ""
      ? ""
      : " p." + (Array.isArray(c.page) ? c.page.join(",") : c.page);
    const path = c.struct_path ? " · " + c.struct_path : "";
    const srcDoc = c.source_document || "";
    const webUrl = (c.document_id === "web" || /^https?:\/\//i.test(srcDoc))
      ? (srcDoc.startsWith("http") ? srcDoc : (c.citation_label || ""))
      : "";
    const label = webUrl
      ? ("공식 사이트 · " + (c.citation_label || srcDoc || "링크"))
      : ((srcDoc || c.citation_label || "출처") + page + path);
    const id = c.document_id || "";
    let chip;
    if (webUrl && /^https?:\/\//i.test(webUrl)) {
      chip = document.createElement("a");
      chip.href = webUrl;
      chip.target = "_blank";
      chip.rel = "noopener noreferrer";
    } else if (id === "match-context" || String(label).indexOf("매칭 안내") >= 0) {
      chip = document.createElement("button");
      chip.type = "button";
      chip.setAttribute("data-match-ref", "1");
      label = "매칭 안내";
    } else if (id && id !== "web" && id !== "faq") {
      chip = document.createElement("a");
      chip.href = brochurePdfHref(id);
      chip.setAttribute("data-doc-id", id);
      chip.target = "_blank";
      chip.rel = "noopener";
    } else {
      chip = document.createElement("span");
    }
    chip.className = "cite";
    chip.textContent = label;
    if (c.citation_label) chip.title = c.citation_label;
    box.appendChild(chip);
  });
  wrap.appendChild(box);
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

renderSymptomChips();
renderTray();
refreshDocs();
