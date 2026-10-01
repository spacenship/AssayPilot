const byId = (id) => document.getElementById(id);
const campaignSelect = byId("campaign");
const selectorSelect = byId("selector");
const seedField = byId("seed-field");
const formError = byId("form-error");
let activeRunId = null;
let pollTimer = null;
let latestRun = null;

const examples = {
  a: { campaign: "revision-20260917-r2", selector: "fixed_order", seed: "", budget: "5", max_steps: "5" },
  b: { campaign: "revision-20260918-primary-active-all", selector: "fixed_order", seed: "", budget: "5", max_steps: "10" },
  c: { campaign: "revision-20260918-primary-active-all", selector: "seeded_random_priority", seed: "3", budget: "5", max_steps: "30" },
};
const statusLabels = { queued: "대기", running: "실행 중", completed: "완료", interrupted: "중단", failed: "실패" };
const stepLabels = { released: "공개됨", no_record: "후속 기록 없음", failed: "실행 실패", rejected: "승인 거절", cancelled: "취소", proposed: "제안", approved: "승인됨", pending_release: "공개 대기" };
const stopLabels = {
  max_steps: "최대 step 도달", max_duration: "최대 실행 시간 도달", deadline: "실행 기한 도달",
  budget_exhausted: "예산으로 실행 가능한 action 없음", no_executable_actions: "실행 가능한 action 없음",
  prerequisites_unmet: "선행조건을 충족한 action 없음", selector_stop: "selector가 종료 선택",
  retry_exhausted: "재시도 한도 도달", user_interrupt: "작업자 중단",
};

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined && text !== null) item.textContent = String(text);
  if (className) item.className = className;
  return item;
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const detail = payload?.error;
    throw new Error(detail?.message || `요청 실패 (${response.status})`);
  }
  return payload;
}

async function loadReadiness() {
  const badge = byId("readiness");
  try {
    const data = await api("/api/readiness");
    badge.textContent = data.ready ? "실행 환경 사용 가능" : "실행 환경 확인 필요";
    badge.className = `readiness ${data.ready ? "ready" : "not-ready"}`;
    const previous = campaignSelect.value;
    campaignSelect.replaceChildren();
    for (const campaign of data.campaigns || []) {
      const option = node("option", `${campaign.label} · ${campaign.candidate_count ?? "확인 불가"}개`);
      option.value = campaign.campaign;
      option.disabled = !campaign.available;
      campaignSelect.append(option);
    }
    if ([...campaignSelect.options].some((option) => option.value === previous)) campaignSelect.value = previous;
    byId("start").disabled = !data.ready || ![...campaignSelect.options].some((option) => !option.disabled);
    if (!data.ready) {
      const missing = (data.campaigns || []).filter((campaign) => !campaign.available).map((campaign) => campaign.campaign);
      badge.title = `sandbox: ${data.selector_sandbox?.code || (data.selector_sandbox?.available ? "available" : "unavailable")}; snapshot: ${missing.join(", ") || "확인 필요"}`;
    }
  } catch (error) {
    badge.textContent = "실행 환경 확인 실패";
    badge.className = "readiness not-ready";
    badge.title = error.message;
    byId("start").disabled = true;
  }
}

function applyExample(key) {
  const sample = examples[key];
  campaignSelect.value = sample.campaign;
  selectorSelect.value = sample.selector;
  byId("seed").value = sample.seed;
  byId("budget").value = sample.budget;
  byId("max-steps").value = sample.max_steps;
  updateSeedVisibility();
  formError.textContent = "";
}

function updateSeedVisibility() {
  const enabled = selectorSelect.value === "seeded_random_priority";
  seedField.classList.toggle("hidden", !enabled);
  byId("seed").required = enabled;
}

function clearPolling() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
}

function requestNextPoll(status) {
  clearPolling();
  if (["queued", "running"].includes(status)) pollTimer = window.setTimeout(refreshRun, 1500);
}

async function refreshRun() {
  if (!activeRunId) return;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(activeRunId)}`);
    renderRun(data);
    requestNextPoll(data.service_status);
    await loadHistory(false);
  } catch (error) {
    byId("run-error").textContent = error.message;
    clearPolling();
  }
}

function metric(label, value) {
  const card = node("div", undefined, "metric");
  card.append(node("span", label), node("strong", value));
  return card;
}

function renderRun(data) {
  latestRun = data;
  activeRunId = data.run_id;
  byId("empty-run").classList.add("hidden");
  byId("run-view").classList.remove("hidden");
  byId("run-id").textContent = data.run_id;
  const status = byId("run-status");
  status.textContent = statusLabels[data.service_status] || data.service_status;
  status.className = `status ${data.service_status}`;
  byId("run-error").textContent = data.error ? `${data.error.code}: ${data.error.message}` : "";
  const progress = data.progress || {};
  const budget = progress.budget || {};
  byId("metrics").replaceChildren(
    metric("처리 step / 상한", `${progress.selection_steps ?? data.steps?.length ?? 0} / ${data.configuration.max_steps}`),
    metric("공개 결과", `${progress.released_executions ?? 0} released`),
    metric("H / L", `${progress.new_followup_active_H ?? 0} / ${progress.new_followup_binary_L ?? 0}`),
    metric("Active / binary", progress.observed_active_fraction_H_over_L == null ? "—" : Number(progress.observed_active_fraction_H_over_L).toFixed(3)),
    metric("예산 spent", `${budget.spent ?? "0"} / ${budget.total ?? data.configuration.budget} ${budget.unit || data.configuration.budget_unit}`),
    metric("예약 / 사용 가능", `${budget.reserved ?? "0"} / ${budget.available ?? data.configuration.budget}`),
    metric("후속 기록 없음", progress.no_record_executions ?? 0),
    metric("실패 / 거절", `${progress.failed_executions ?? 0} / ${progress.rejected_steps ?? 0}`),
  );
  const stop = data.stop_reason;
  byId("stop-reason").textContent = stop ? `종료 사유: ${stopLabels[stop] || stop}` : "실행 종료 사유를 기다리는 중입니다.";
  byId("download").disabled = !data.download_url;
  renderSteps(data.steps || []);
  renderResults(data.published_results || []);
}

function renderSteps(steps) {
  const tbody = byId("steps");
  tbody.replaceChildren();
  if (!steps.length) {
    const row = node("tr");
    const cell = node("td", "공개 실행 기록이 없습니다.", "empty-cell");
    cell.colSpan = 6;
    row.append(cell); tbody.append(row); return;
  }
  for (const step of steps) {
    const row = node("tr");
    const values = [step.step_no, step.candidate_id, step.assay_id, step.selection_reason || "—", stepLabels[step.status] || step.status,
      `${step.budget_after_step?.available ?? "—"} ${step.budget_after_step?.unit || ""}`];
    for (const value of values) row.append(node("td", value));
    tbody.append(row);
  }
}

function renderResults(executions) {
  const root = byId("results");
  root.replaceChildren();
  const observations = executions.flatMap((execution) => (execution.observations || []).map((observation) => ({ execution, observation })));
  if (!observations.length) {
    root.append(node("p", "공개된 후속 Observation이 없습니다.", "empty"));
    return;
  }
  for (const { execution, observation } of observations) {
    const card = node("details", undefined, "result-card");
    const summary = node("summary");
    summary.append(node("strong", `${observation.candidate_id} · ${observation.assay_id}`));
    const verdict = (observation.verdict || "unspecified").toLowerCase();
    const verdictName = ["active", "inactive"].includes(verdict) ? verdict : "unknown";
    summary.append(node("span", verdictName === "unknown" ? `unknown (${verdict})` : verdictName, `verdict ${verdictName}`));
    card.append(summary);
    const content = node("div", undefined, "observation");
    const amount = observation.value == null ? "범주형 관측" : `${observation.comparison || ""} ${observation.value} ${observation.unit || ""}`;
    content.append(node("p", `Observation ${observation.observation_id} · ${amount} · 공개 시각 ${observation.released_at}`));
    const refs = (execution.evidence || []).filter((item) => (observation.evidence_ids || []).includes(item.reference?.evidence_id));
    if (refs.length) {
      const sourceText = refs.map((item) => `${item.reference.source_kind} ${item.reference.source_id} · evidence ${item.reference.evidence_id} · SHA-256 ${item.sha256}`).join("\n");
      content.append(node("pre", sourceText));
      for (const item of refs) {
        const details = node("details");
        details.append(node("summary", "공개 evidence payload 보기"));
        details.append(node("pre", JSON.stringify(item.payload, null, 2)));
        content.append(details);
      }
    }
    card.append(content);
    root.append(card);
  }
}

async function loadHistory(restore = true) {
  try {
    const data = await api("/api/runs");
    const history = byId("run-history");
    const selected = history.value;
    history.replaceChildren(node("option", "최근 실행 불러오기"));
    history.options[0].value = "";
    for (const run of data.runs || []) {
      const option = node("option", `${run.service_status} · ${run.campaign} · ${run.run_id.slice(-8)}`);
      option.value = run.run_id;
      history.append(option);
    }
    if ([...history.options].some((option) => option.value === selected)) history.value = selected;
    if (restore && !activeRunId) {
      const saved = localStorage.getItem("assaypilot-stage4-last-run");
      if (saved && (data.runs || []).some((run) => run.run_id === saved)) await openRun(saved);
    }
  } catch (_) {
    // The run view remains usable when history lookup is temporarily unavailable.
  }
}

async function openRun(runId) {
  clearPolling();
  activeRunId = runId;
  const data = await api(`/api/runs/${encodeURIComponent(runId)}`);
  localStorage.setItem("assaypilot-stage4-last-run", runId);
  renderRun(data);
  requestNextPoll(data.service_status);
}

byId("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.textContent = "";
  const request = {
    campaign: campaignSelect.value,
    selector: selectorSelect.value,
    budget: byId("budget").value,
    max_steps: Number(byId("max-steps").value),
  };
  if (request.selector === "seeded_random_priority") request.seed = Number(byId("seed").value);
  byId("start").disabled = true;
  try {
    const response = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(request),
    });
    await openRun(response.run.run_id);
    await loadHistory(false);
  } catch (error) {
    formError.textContent = error.message;
    await loadReadiness();
  } finally {
    byId("start").disabled = !byId("readiness").classList.contains("ready");
  }
});

document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => applyExample(button.dataset.example)));
selectorSelect.addEventListener("change", updateSeedVisibility);
byId("run-history").addEventListener("change", async (event) => {
  if (!event.target.value) return;
  try { await openRun(event.target.value); }
  catch (error) { byId("run-error").textContent = error.message; }
});
byId("download").addEventListener("click", () => {
  if (latestRun?.download_url) window.location.assign(latestRun.download_url);
});

updateSeedVisibility();
Promise.all([loadReadiness(), loadHistory()]);
