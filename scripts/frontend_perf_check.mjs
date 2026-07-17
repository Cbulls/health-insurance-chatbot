/**
 * Lab B축 — 프론트엔드 스트리밍 성능 측정 (FRONTEND_PERFORMANCE_PLAN §3·§6)
 *
 * 전제: API가 http://127.0.0.1:8000 에서 떠 있고, 샘플 PDF를 업로드할 수 있어야 한다.
 *
 *   node scripts/frontend_perf_check.mjs
 *   BASE=http://127.0.0.1:8000 node scripts/frontend_perf_check.mjs
 *
 * 산출: stdout JSON + tmp/perf/frontend-b-axis.json
 */
import { chromium, devices } from "playwright";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { execSync } from "child_process";

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT_DIR = join(ROOT, "tmp", "perf");
const STALL_MS = 500;
const PDF_PATH = join(ROOT, "sample_travel_expense.pdf");

function pct(sorted, p) {
  if (!sorted.length) return null;
  const i = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[i];
}

function ensurePdf() {
  if (existsSync(PDF_PATH)) return;
  execSync("python scripts/make_sample_pdf.py", { cwd: ROOT, stdio: "inherit" });
}

async function uploadPdf(ownerId) {
  ensurePdf();
  const buf = readFileSync(PDF_PATH);
  const form = new FormData();
  form.append("file", new Blob([buf], { type: "application/pdf" }), "sample.pdf");
  const res = await fetch(`${BASE}/v1/documents`, {
    method: "POST",
    headers: { "X-Owner-Id": ownerId },
    body: form,
  });
  if (!res.ok) throw new Error(`upload ${res.status}`);
  const data = await res.json();
  const id = data.document_id;
  for (let i = 0; i < 60; i++) {
    const st = await fetch(`${BASE}/v1/documents/${id}`, {
      headers: { "X-Owner-Id": ownerId },
    }).then((r) => r.json());
    if (st.status === "ready") return { id, chunks: st.n_chunks };
    if (st.status === "failed") throw new Error(`ingest failed: ${st.error}`);
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error("ingest timeout");
}

/** 페이지에서 SSE 스트림 계측 + 질의 전송 */
async function measureQuery(page, query, { expectAbstain = false } = {}) {
  const result = await page.evaluate(
    async ({ query, stallMs, expectAbstain }) => {
      const t0 = performance.now();
      const tokenTs = [];
      let firstTokenAt = null;
      let abstainAt = null;
      let citationsAt = null;
      let doneAt = null;
      let errorAt = null;
      let answer = "";
      let abstainReason = null;
      let fpsSamples = [];
      let fpsRunning = true;
      let lastFrame = performance.now();
      function frameLoop(now) {
        if (!fpsRunning) return;
        const dt = now - lastFrame;
        if (dt > 0) fpsSamples.push(1000 / dt);
        lastFrame = now;
        requestAnimationFrame(frameLoop);
      }
      requestAnimationFrame(frameLoop);

      const res = await fetch("/v1/query/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Owner-Id": localStorage.getItem("harag_owner"),
        },
        body: JSON.stringify({
          query,
          conversation_id: crypto.randomUUID(),
        }),
      });
      if (!res.ok || !res.body) {
        fpsRunning = false;
        return { error: `http ${res.status}`, t0 };
      }
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
          const now = performance.now();
          if (evt.kind === "token") {
            if (firstTokenAt == null) firstTokenAt = now;
            tokenTs.push(now);
            answer += evt.data;
          } else if (evt.kind === "abstain") {
            abstainAt = now;
            abstainReason = evt.data;
          } else if (evt.kind === "citations") {
            citationsAt = now;
          } else if (evt.kind === "done") {
            doneAt = now;
          } else if (evt.kind === "error") {
            errorAt = now;
          }
        }
      }
      fpsRunning = false;
      const end = performance.now();
      if (doneAt == null) doneAt = end;

      const gaps = [];
      for (let i = 1; i < tokenTs.length; i++) gaps.push(tokenTs[i] - tokenTs[i - 1]);
      const stalls = gaps.filter((g) => g > stallMs).length;
      const fps =
        fpsSamples.length > 5
          ? fpsSamples.slice(5).reduce((a, b) => a + b, 0) / (fpsSamples.length - 5)
          : null;

      return {
        ttft_ms: firstTokenAt != null ? firstTokenAt - t0 : null,
        abstain_ms: abstainAt != null ? abstainAt - t0 : null,
        citations_ms: citationsAt != null ? citationsAt - t0 : null,
        e2e_ms: doneAt - t0,
        token_count: tokenTs.length,
        stall_count: stalls,
        gap_p50_ms: gaps.length
          ? gaps.slice().sort((a, b) => a - b)[Math.floor(gaps.length * 0.5)]
          : null,
        gap_p95_ms: gaps.length
          ? gaps.slice().sort((a, b) => a - b)[Math.min(gaps.length - 1, Math.ceil(gaps.length * 0.95) - 1)]
          : null,
        tokens_per_sec:
          firstTokenAt != null && tokenTs.length > 1
            ? (tokenTs.length - 1) / ((tokenTs[tokenTs.length - 1] - firstTokenAt) / 1000)
            : null,
        avg_fps: fps,
        answer_len: answer.length,
        abstain_reason: abstainReason,
        error_at_ms: errorAt != null ? errorAt - t0 : null,
        expect_abstain: expectAbstain,
        got_abstain: abstainAt != null,
      };
    },
    { query, stallMs: STALL_MS, expectAbstain }
  );
  return { query, ...result };
}

/** UI 경로: composer로 보내고 DOM 반응·INP 대리 측정 */
async function measureUiPath(page, query) {
  await page.fill("#queryInput", query);
  const t0 = await page.evaluate(() => performance.now());
  const sendPromise = page.click("#sendBtn");
  // 첫 봇 버블(thinking 또는 토큰) 등장
  await page.waitForSelector(".msg.bot .bubble", { timeout: 60000 });
  const thinkingAt = await page.evaluate(() => performance.now());
  // thinking 해제 = 첫 토큰 또는 abstain
  await page.waitForFunction(
    () => {
      const bubbles = document.querySelectorAll(".msg.bot .bubble");
      const last = bubbles[bubbles.length - 1];
      return last && !last.classList.contains("thinking");
    },
    { timeout: 120000 }
  );
  const firstContentAt = await page.evaluate(() => performance.now());
  await sendPromise;
  // 스트림 종료: send 버튼 재활성화
  await page.waitForFunction(() => !document.getElementById("sendBtn").disabled, {
    timeout: 120000,
  });
  const end = await page.evaluate(() => performance.now());
  const citations = await page.locator(".citations").count();
  const abstain = await page.locator(".msg.bot .bubble.abstain").count();
  return {
    ui_to_thinking_ms: thinkingAt - t0,
    ui_ttft_ms: firstContentAt - t0,
    ui_e2e_ms: end - t0,
    citations_visible: citations > 0,
    abstain_visible: abstain > 0,
  };
}

async function coldLoadMetrics(page) {
  const nav = await page.goto(BASE, { waitUntil: "networkidle" });
  const timing = await page.evaluate(() => {
    const nav = performance.getEntriesByType("navigation")[0];
    const paints = Object.fromEntries(
      performance.getEntriesByType("paint").map((p) => [p.name, p.startTime])
    );
    const inputReady = (() => {
      const el = document.getElementById("queryInput");
      return !!(el && !el.disabled);
    })();
    return {
      domContentLoaded: nav?.domContentLoadedEventEnd,
      loadEvent: nav?.loadEventEnd,
      fcp: paints["first-contentful-paint"] ?? null,
      input_ready: inputReady,
      transfer_size: nav?.transferSize,
    };
  });
  // 입력창 포커스 가능까지 (콜드 로드 TTI 대리)
  const tFocus0 = await page.evaluate(() => performance.now());
  await page.focus("#queryInput");
  await page.type("#queryInput", "x");
  await page.fill("#queryInput", "");
  const tFocus1 = await page.evaluate(() => performance.now());
  return {
    status: nav.status(),
    ...timing,
    input_interactive_ms: tFocus1 - tFocus0,
  };
}

async function multiTurnDom(page, n = 5) {
  const sizes = [];
  for (let i = 0; i < n; i++) {
    await measureUiPath(page, `출장비 한도는 얼마인가요? (턴 ${i + 1})`);
    const snap = await page.evaluate(() => ({
      msg_count: document.querySelectorAll(".msg").length,
      node_count: document.getElementById("messages")?.querySelectorAll("*").length ?? 0,
      messages_html_bytes: (document.getElementById("messages")?.innerHTML || "").length,
    }));
    sizes.push(snap);
  }
  return sizes;
}

async function runThrottled(browser, ownerReady) {
  const context = await browser.newContext({
    ...devices["Moto G4"],
  });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    downloadThroughput: (1.6 * 1024 * 1024) / 8, // ~1.6 Mbps
    uploadThroughput: (750 * 1024) / 8,
    latency: 150,
  });
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });

  await page.goto(BASE, { waitUntil: "networkidle" });
  // 같은 owner로 문서가 보이도록 — 새 컨텍스트면 새 owner. 업로드 필요.
  const owner = await page.evaluate(() => localStorage.getItem("harag_owner"));
  await uploadPdf(owner);
  const related = await measureQuery(page, "국내 출장비 한도는 얼마인가요?");
  await context.close();
  return { label: "throttle_cpu4_slow3g", ...related };
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  const report = {
    base: BASE,
    measured_at: new Date().toISOString(),
    stall_threshold_ms: STALL_MS,
  };

  // A 보조: 콜드 로드 + 입력창
  report.cold_load = await coldLoadMetrics(page);
  const owner = await page.evaluate(() => localStorage.getItem("harag_owner"));
  report.owner = owner;
  report.ingest = await uploadPdf(owner);

  // B축: fetch 경로 계측 (정확한 TTFT/stall)
  const relatedQueries = [
    "국내 출장비 한도는 얼마인가요?",
    "하루 출장비 상한 금액이 얼마인지 알려주세요.",
    "출장비 규정을 요약해 주세요. 한도와 적용 범위를 포함해서 자세히 설명해 주세요.",
  ];
  report.related = [];
  for (const q of relatedQueries) {
    report.related.push(await measureQuery(page, q));
  }

  report.abstain = await measureQuery(page, "김치찌개 레시피를 알려주세요.", {
    expectAbstain: true,
  });

  // UI 경로 (렌더 포함 TTFT)
  await page.goto(BASE, { waitUntil: "networkidle" });
  report.ui_related = await measureUiPath(page, "국내 출장비 한도는 얼마인가요?");
  report.ui_unrelated = await measureUiPath(page, "김치찌개 레시피를 알려주세요.");

  // 멀티턴 DOM
  await page.click("#newChatBtn");
  report.multi_turn_dom = await multiTurnDom(page, 4);

  // 약한 환경
  try {
    report.throttled = await runThrottled(browser, true);
  } catch (e) {
    report.throttled = { error: String(e) };
  }

  // 집계
  const ttfts = report.related.map((r) => r.ttft_ms).filter((x) => x != null).sort((a, b) => a - b);
  const stalls = report.related.map((r) => r.stall_count);
  const fps = report.related.map((r) => r.avg_fps).filter((x) => x != null);
  report.summary = {
    ttft_p50_ms: pct(ttfts, 50),
    ttft_p75_ms: pct(ttfts, 75),
    ttft_p95_ms: pct(ttfts.slice(), 95),
    stall_max: Math.max(0, ...stalls),
    avg_fps_min: fps.length ? Math.min(...fps) : null,
    cold_fcp_ms: report.cold_load.fcp,
    input_interactive_delta_ms: report.cold_load.input_interactive_ms,
    abstain_ms: report.abstain.abstain_ms ?? report.abstain.ttft_ms,
    ui_ttft_ms: report.ui_related.ui_ttft_ms,
    dom_after_turns: report.multi_turn_dom.at(-1),
  };

  // SLO §5.2 (Lab 단일 표본 — p75 목표와 비교)
  const s = report.summary;
  report.slo = {
    lcp_proxy_fcp: {
      metric: "cold FCP (Lighthouse LCP separately)",
      value_ms: s.cold_fcp_ms,
      target_p75_ms: 2500,
      pass: s.cold_fcp_ms != null && s.cold_fcp_ms <= 2500,
    },
    input_tti: {
      value_ms: report.cold_load.loadEvent,
      target_p75_ms: 2000,
      pass: report.cold_load.loadEvent != null && report.cold_load.loadEvent <= 2000,
    },
    query_ttft: {
      value_ms: s.ttft_p75_ms,
      target_p75_ms: 1500,
      limit_p95_ms: 3000,
      pass: s.ttft_p75_ms != null && s.ttft_p75_ms <= 1500,
      pass_limit: s.ttft_p75_ms != null && s.ttft_p75_ms <= 3000,
    },
    streaming_stall: {
      value: s.stall_max,
      target: 0,
      limit: 2,
      pass: s.stall_max <= 0,
      pass_limit: s.stall_max <= 2,
    },
    streaming_fps: {
      value: s.avg_fps_min,
      target: 50,
      limit: 30,
      pass: s.avg_fps_min == null || s.avg_fps_min >= 50,
      pass_limit: s.avg_fps_min == null || s.avg_fps_min >= 30,
    },
  };

  const outPath = join(OUT_DIR, "frontend-b-axis.json");
  writeFileSync(outPath, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report.summary, null, 2));
  console.log("SLO:", JSON.stringify(report.slo, null, 2));
  console.log("wrote", outPath);

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
