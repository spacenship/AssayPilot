const byId = (id) => document.getElementById(id);
const campaignSelect = byId("campaign");
const selectorSelect = byId("selector");
const seedField = byId("seed-field");
const formError = byId("form-error");
let activeRunId = null;
let pollTimer = null;
let latestRun = null;
let readinessInfo = null;
const detailStateByRun = new Map();

const examples = {
  a: { campaign: "revision-20260917-r2", selector: "fixed_order", seed: "", budget: "5", max_steps: "5" },
  b: { campaign: "revision-20260918-primary-active-all", selector: "fixed_order", seed: "", budget: "5", max_steps: "10" },
  c: { campaign: "revision-20260918-primary-active-all", selector: "seeded_random_priority", seed: "3", budget: "5", max_steps: "30" },
};
const statusLabels = { queued: "대기", running: "실행 중", completed: "완료", interrupted: "중단", failed: "오류로 중단" };
const stopLabels = {
  max_steps: "설정한 최대 행동 수에 도달했습니다.", max_duration: "설정한 실행 시간에 도달했습니다.",
  deadline: "저장된 실행 deadline에 도달했습니다.", budget_exhausted: "예산으로 실행할 수 있는 action이 없습니다.",
  no_executable_actions: "실행 가능한 action이 없습니다.", prerequisites_unmet: "선행조건을 충족한 action이 없습니다.",
  selector_stop: "판단기가 실행 종료를 선택했습니다.", llm_call_limit: "설정한 LLM 호출 상한에 도달했습니다.",
  scientific_finalization_complete: "공개 관측 해석을 마쳤습니다.", interpretation_complete: "공개 관측 해석을 마쳤습니다.",
  retry_exhausted: "재시도 한도에 도달했습니다.", user_interrupt: "작업자가 실행을 중단했습니다.",
};
const verdictLabels = { active: "Active", inactive: "Inactive", inconclusive: "Inconclusive", unspecified: "분류되지 않음" };
const statusNames = { proposed: "proposed · 검증 전 가설", supported: "supported · 공개 관측이 지지", weakened: "weakened · 공개 관측이 약화", rejected: "rejected · 검증 규칙상 기각", unknown: "상태 미상" };

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined && text !== null) item.textContent = String(text);
  if (className) item.className = className;
  return item;
}

function detailState(runId) {
  if (!detailStateByRun.has(runId)) detailStateByRun.set(runId, new Map());
  return detailStateByRun.get(runId);
}

function detailsNode(runId, key, className = "", defaultOpen = false) {
  const item = node("details", undefined, className);
  const states = detailState(runId);
  item.dataset.persistKey = key;
  item.open = states.has(key) ? states.get(key) : defaultOpen;
  item.addEventListener("toggle", () => states.set(key, item.open));
  return item;
}

function displayCount(value) {
  return Number.isInteger(value) && value >= 0 ? value.toLocaleString() : "미보고";
}

function shortenedId(value, start = 15, end = 8) {
  if (typeof value !== "string" || value.length <= start + end + 1) return value || "미기록";
  return `${value.slice(0, start)}…${value.slice(-end)}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.error?.message || `요청 실패 (${response.status})`);
  return payload;
}

function currentMode() { return byId("mode").value; }
function runUrl(runId) {
  return runId.startsWith("stage5b-")
    ? `/api/scientific-runs/${encodeURIComponent(runId)}`
    : `/api/runs/${encodeURIComponent(runId)}`;
}
function refreshStartAvailability() {
  if (!readinessInfo) { byId("start").disabled = true; return; }
  const selected = (readinessInfo.campaigns || []).find((item) => item.campaign === campaignSelect.value);
  const available = currentMode() === "scientific_reasoner"
    ? readinessInfo.scientific_reasoner?.available
    : readinessInfo.ready;
  byId("start").disabled = !available || !selected?.available;
}

async function loadReadiness() {
  const badge = byId("readiness");
  try {
    const data = await api("/api/readiness");
    readinessInfo = data;
    const science = data.scientific_reasoner || {};
    badge.textContent = data.ready || science.available ? "실행 설정 준비됨 · provider 사전 연결 검사 미실시" : "실행 환경 확인 필요";
    badge.className = `readiness ${(data.ready || science.available) ? "ready" : "not-ready"}`;
    badge.title = `과학 실행 provider 설정: ${science.provider_settings_configured ? "존재" : "확인 필요"}; 연결: 미검사`;
    const previous = campaignSelect.value;
    campaignSelect.replaceChildren();
    for (const campaign of data.campaigns || []) {
      const option = node("option", `${campaign.label} · ${campaign.candidate_count ?? "확인 불가"}개`);
      option.value = campaign.campaign;
      option.disabled = !campaign.available;
      campaignSelect.append(option);
    }
    if ([...campaignSelect.options].some((option) => option.value === previous)) campaignSelect.value = previous;
    refreshStartAvailability();
  } catch (error) {
    readinessInfo = null;
    badge.textContent = "실행 환경 확인 실패";
    badge.className = "readiness not-ready";
    badge.title = error.message;
    byId("start").disabled = true;
  }
}

function applyExample(key) {
  const sample = examples[key];
  byId("mode").value = "baseline";
  campaignSelect.value = sample.campaign;
  selectorSelect.value = sample.selector;
  byId("seed").value = sample.seed;
  byId("budget").value = sample.budget;
  byId("max-steps").value = sample.max_steps;
  updateModeVisibility();
  updateSeedVisibility();
  refreshStartAvailability();
  formError.textContent = "";
}

function updateSeedVisibility() {
  const enabled = currentMode() === "baseline" && selectorSelect.value === "seeded_random_priority";
  seedField.classList.toggle("hidden", !enabled);
  byId("seed").required = enabled;
}

function updateModeVisibility() {
  const scientific = currentMode() === "scientific_reasoner";
  byId("baseline-fields").classList.toggle("hidden", scientific);
  byId("scientific-advanced").classList.toggle("hidden", !scientific);
  byId("research-scope").classList.toggle("hidden", !scientific);
  byId("baseline-examples").classList.toggle("hidden", scientific);
  byId("max-steps").max = scientific ? "10" : "50";
  byId("max-steps").value = scientific ? "10" : "5";
  byId("mode-note").textContent = scientific
    ? "기본값: 예산 5, 최대 10 actions, 300초, LLM 최대 24회, shortlist 24 / seed 3. Replay 실행은 실제 실험이나 활성 예측이 아닙니다."
    : "예산·실행 가능 action에 따라 최대 step보다 먼저 종료할 수 있습니다. 실제 실험이나 활성 예측이 아닙니다.";
  updateSeedVisibility();
  refreshStartAvailability();
}

function clearPolling() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
}
function requestNextPoll(status) {
  clearPolling();
  if (["queued", "running"].includes(status)) pollTimer = window.setTimeout(refreshRun, 1200);
}
async function refreshRun() {
  if (!activeRunId) return;
  const requestedRunId = activeRunId;
  try {
    const data = await api(runUrl(requestedRunId));
    if (activeRunId !== requestedRunId) return;
    renderRun(data);
    requestNextPoll(data.service_status);
    await loadHistory(false);
  } catch (error) {
    if (activeRunId !== requestedRunId) return;
    byId("run-error").textContent = error.message;
    clearPolling();
  }
}

function metric(label, value, note = null) {
  const card = node("div", undefined, "metric");
  card.append(node("span", label), node("strong", value));
  if (note) card.append(node("small", note));
  return card;
}
function isScientific(data) {
  return data.configuration?.selector === "scientific_reasoner" || data.schema_version === "assaypilot.scientific-public-projection.v1";
}

function renderRun(data) {
  latestRun = data;
  activeRunId = data.run_id;
  byId("empty-run").classList.add("hidden");
  byId("run-view").classList.remove("hidden");
  byId("run-id").textContent = data.run_id;
  const origin = byId("run-origin");
  const originLabels = {
    live_web_run: "웹 실행 기록",
    stored_actual_run: data.stored_label || "저장된 실제 실행 기록",
  };
  const sourceLabel = originLabels[data.origin] || (data.stored_record ? data.stored_label || "저장된 실행 기록" : null);
  origin.textContent = sourceLabel || "출처 미확인";
  origin.classList.toggle("hidden", !sourceLabel);
  const status = byId("run-status");
  status.textContent = statusLabels[data.service_status] || data.service_status;
  status.className = `status ${data.service_status}`;
  byId("run-error").textContent = data.error ? `${data.error.code}: ${data.error.message}` : "";
  byId("download").disabled = !data.download_url;
  byId("resume-run").classList.toggle("hidden", !(data.service_status === "interrupted" && data.resume_available));
  byId("stop-reason").textContent = data.stop_reason
    ? `종료 사유: ${stopLabels[data.stop_reason] || data.stop_reason}`
    : data.service_status === "completed" ? "종료 사유를 기록하지 않았습니다." : "실행 종료 사유를 기다리는 중입니다.";
  byId("science-view").classList.toggle("hidden", !isScientific(data));
  byId("baseline-trace").classList.toggle("hidden", isScientific(data));
  byId("baseline-results").classList.toggle("hidden", isScientific(data));
  if (isScientific(data)) renderScientific(data);
  else renderBaseline(data);
}

function renderBaseline(data) {
  const progress = data.progress || {};
  const budget = progress.budget || {};
  byId("metrics").replaceChildren(
    metric("처리 step / 상한", `${progress.selection_steps ?? data.steps?.length ?? 0} / ${data.configuration.max_steps}`),
    metric("공개 결과", `${progress.released_executions ?? 0} released`),
    metric("H / L", `${progress.new_followup_active_H ?? 0} / ${progress.new_followup_binary_L ?? 0}`),
    metric("Active / binary", progress.observed_active_fraction_H_over_L == null ? "자료 없음" : Number(progress.observed_active_fraction_H_over_L).toFixed(3)),
    metric("예산 spent", `${budget.spent ?? "0"} / ${budget.total ?? data.configuration.budget} ${budget.unit || data.configuration.budget_unit}`),
    metric("예약 / 사용 가능", `${budget.reserved ?? "0"} / ${budget.available ?? data.configuration.budget}`),
    metric("후속 기록 없음", progress.no_record_executions ?? 0),
    metric("실패 / 거절", `${progress.failed_executions ?? 0} / ${progress.rejected_steps ?? 0}`),
  );
  renderSteps(data.steps || []);
  renderResults(data.published_results || []);
}

function renderScientific(data) {
  const summary = data.summary || {};
  const budget = summary.replay_budget || {};
  const token = summary.api_token_usage || {};
  const apiCalls = Array.isArray(data.api_calls) ? data.api_calls : null;
  const executionStatuses = (data.decisions || []).map((item) => item.execution?.status).filter((value) => typeof value === "string");
  const statusCount = (status) => executionStatuses.filter((value) => value === status).length;
  const pendingRelease = executionStatuses.filter((value) => ["pending_release", "ready_for_release"].includes(value)).length;
  const knownExecutionStatuses = new Set(["released", "no_record", "failed", "rejected", "cancelled", "pending_release", "ready_for_release"]);
  const otherStatuses = executionStatuses.filter((value) => !knownExecutionStatuses.has(value)).length;
  const fields = token.by_field && typeof token.by_field === "object" ? token.by_field : {};
  const reportedCalls = Number.isInteger(token.reported_calls) ? token.reported_calls : null;
  const unreportedCalls = Number.isInteger(token.unreported_calls) ? token.unreported_calls : null;
  const completedCalls = apiCalls?.filter((item) => item.status === "completed").length ?? null;
  const unknownCalls = apiCalls?.filter((item) => item.status === "unknown").length ?? null;
  const pendingCalls = apiCalls?.filter((item) => ["pending", "in_progress", "awaiting_response"].includes(item.status)).length ?? null;
  const running = ["queued", "running"].includes(data.service_status);
  const responseWait = running
    ? pendingCalls === null ? "미확인 · API 호출 자료 없음" : pendingCalls ? String(pendingCalls) : unknownCalls ? `미확인 (${unknownCalls}건 상태 미상)` : "미확인"
    : "해당 없음 · run 종료";
  const usageReportText = unreportedCalls === null
    ? "미보고"
    : running
      ? `${displayCount(reportedCalls)} 보고 · ${displayCount(unreportedCalls)} 현재 미보고 (종료 후 확정 전)`
      : `${displayCount(reportedCalls)} 보고 · ${displayCount(unreportedCalls)} 종료 후 미보고`;
  const executionText = executionStatuses.length
    ? `${statusCount("released")} / ${statusCount("no_record")} / ${statusCount("failed")}`
    : "공개 execution 기록 없음";
  byId("metrics").replaceChildren(
    metric("행동 실행 / 상한", `${displayCount(summary.action_steps)} / ${displayCount(summary.max_steps ?? data.configuration.max_steps)}`),
    metric("Replay 결과 · released / no_record / failed", executionText, `배포 대기 ${pendingRelease} · 기타 상태 ${otherStatuses}`),
    metric("LLM 호출 / 상한", `${displayCount(summary.llm_calls?.used)} / ${displayCount(summary.llm_calls?.limit ?? data.configuration.max_llm_calls)}`),
    metric("공개 관측", displayCount(summary.public_observations)),
    metric("관측 해석 완료 / 대기", `${displayCount(summary.interpreted_observations)} / ${displayCount(summary.pending_interpretations)}`, "공개 Observation 기준; pending release와 별도"),
    metric("Replay 예산 spent / 잔여", `${budget.spent ?? "미보고"} / ${budget.available ?? "미보고"} ${budget.unit || data.configuration.budget_unit || ""}`),
    metric("Replay 예산 예약", `${budget.reserved ?? "미보고"} ${budget.unit || data.configuration.budget_unit || ""}`),
    metric("보고된 input tokens", displayCount(fields.input_tokens), "합산값; 미보고는 0 token으로 계산하지 않음"),
    metric("보고된 output tokens", displayCount(fields.output_tokens)),
    metric("보고된 token 합계", displayCount(fields.total_tokens), "provider가 usage를 보고한 값"),
    metric("호출별 token usage", usageReportText, "reported / usage 미보고 호출 · replay 예산과 별도"),
    metric("provider 응답 대기", responseWait, running ? "공개 상태가 unknown이면 대기 여부를 확인할 수 없음" : "선택 run 기록 기준"),
  );
  const providerRecord = completedCalls === null
    ? "선택 run의 API 호출 기록 미보고"
    : `선택 run에서 완료로 기록된 API 호출 ${completedCalls}건${unknownCalls ? ` · 상태 미상 ${unknownCalls}건` : ""}`;
  const providerNote = `provider 사전 연결 검사: 미실시. ${providerRecord}; 과거 기록은 현재 연결 상태를 보장하지 않습니다.`;
  const interpretationNote = summary.no_observations_to_interpret
    ? "해석할 신규 관측 없음 · 과학적 가설을 입증하지 않습니다."
    : Number.isInteger(summary.public_observations) && summary.public_observations === 0
      ? "현재 공개 관측이 없습니다. no_record는 Inactive나 실험 실패가 아닙니다."
      : Number.isInteger(summary.public_observations)
        ? `관측 해석 상태: ${summary.interpretation_complete ? "공개된 관측 해석 완료" : "미완료 또는 진행 중"}. proposed 가설 수와 해석 대기 수는 별개입니다.`
        : "공개 관측·해석 상태 미보고.";
  byId("science-summary-note").textContent = `${providerNote} ${interpretationNote} Controller 검증·적용은 과학적 가설의 입증을 뜻하지 않습니다.`;
  renderScientificSummary(data);
  renderTimeline(data);
}

function renderScientificSummary(data) {
  const root = byId("scientific-summary");
  root.replaceChildren();
  const counts = data.summary?.verdict_counts;
  const card = node("div", undefined, "science-stat-card");
  card.append(node("h3", "공개 관측 범주"));
  card.append(node("p", `Active ${displayCount(counts?.active)} · Inactive ${displayCount(counts?.inactive)} · Inconclusive ${displayCount(counts?.inconclusive)} · 분류되지 않음 ${displayCount(counts?.unspecified)}`));
  root.append(card);
  for (const [kind, label] of [["assay_activity", "Assay activity 가설"], ["data_availability", "Data availability 가설"]]) {
    const record = data.summary?.hypotheses?.[kind];
    const byStatus = record?.by_status && typeof record.by_status === "object" ? Object.entries(record.by_status) : null;
    const statusText = byStatus?.length
      ? byStatus.map(([key, count]) => `${statusNames[key] || key}: ${displayCount(count)}`).join(" · ")
      : Number.isInteger(record?.count) && record.count === 0 ? "없음" : "상태 분포 미보고";
    const item = node("div", undefined, "science-stat-card");
    item.append(node("h3", label), node("p", `현재 ${displayCount(record?.count)}개 · ${statusText}`));
    root.append(item);
  }
}

function renderTimeline(data) {
  const root = byId("science-timeline");
  root.replaceChildren();
  if (!(data.decisions || []).length) {
    root.append(node("p", "공개 판단 기록을 기다리는 중입니다.", "empty"));
    return;
  }
  const observations = new Map((data.observations || []).map((item) => [item.observation_id, item]));
  const evidence = new Map((data.evidence || []).map((item) => [item.evidence_id, item]));
  const hypotheses = new Map((data.hypotheses || []).map((item) => [item.hypothesis_id, item]));
  const candidates = new Map();
  const assays = new Map();
  for (const decision of data.decisions) {
    for (const candidate of decision.context?.candidates || []) candidates.set(candidate.candidate_id, candidate);
    for (const assay of decision.context?.assays || []) assays.set(assay.assay_id, assay);
  }
  for (const item of data.evidence || []) {
    for (const row of item.public_content?.rows || []) {
      const existing = candidates.get(row.candidate_id) || { candidate_id: row.candidate_id };
      const raw = row.raw_row || {};
      candidates.set(row.candidate_id, {
        ...existing,
        source_id: existing.source_id || (raw.SID ? `SID:${raw.SID}` : undefined),
        source_cid: existing.source_cid || raw.CID,
      });
    }
  }
  const candidateLabel = (candidateId, includeInternalId = true) => {
    if (!candidateId) return "후보 미기록";
    const candidate = candidates.get(candidateId) || {};
    const sid = candidate.source_id?.replace(/^SID:/, "") || "미기록";
    const cid = candidate.source_cid || "미기록";
    const readable = [];
    if (sid !== "미기록") readable.push(`SID ${sid}`);
    if (cid !== "미기록") readable.push(`CID ${cid}`);
    if (includeInternalId || !readable.length) readable.push(includeInternalId ? candidateId : shortenedId(candidateId, 18, 6));
    return readable.join(" · ");
  };
  const assayLabel = (assayId) => {
    const assay = assays.get(assayId);
    return assay?.name ? `${assay.name} (${assayId})` : assayId || "assay 미기록";
  };
  const refButton = (label, kind, id) => {
    const button = node("button", label, "record-ref");
    button.type = "button";
    button.dataset.openTarget = recordDomId(kind, id);
    button.title = id;
    return button;
  };
  const appendRefs = (parent, label, ids, kind, index) => {
    const line = node("p", `${label}: `);
    const valid = (ids || []).filter((id) => index.has(id));
    if (!valid.length) {
      const missing = (ids || []).filter((id) => !index.has(id));
      line.append(document.createTextNode(missing.length ? `공개 자료에서 확인 불가: ${missing.join(", ")}` : "참조 없음"));
    }
    valid.forEach((id, position) => {
      if (position) line.append(document.createTextNode(" · "));
      line.append(refButton(`${label} ${shortenedId(id)}`, kind, id));
    });
    parent.append(line);
  };
  const decisionImpact = (decision) => {
    const body = decision.decision;
    if (!body) return { interpretations: 0, updates: 0 };
    const interpreted = (body.interpretations || []).filter((item) => item.observation_id && observations.has(item.observation_id)).length;
    const updates = new Set((body.prior_updates || []).map((item) => item.hypothesis_id).filter(Boolean)).size;
    return { interpretations: interpreted, updates };
  };
  const firstExpandedDecision = data.decisions.find((decision) => {
    const impact = decisionImpact(decision);
    return impact.interpretations > 0 || impact.updates > 0;
  })?.decision_id;
  const historyByDecision = new Map();
  for (const item of data.hypothesis_history || []) {
    const rows = historyByDecision.get(item.decision_id) || [];
    rows.push(item); historyByDecision.set(item.decision_id, rows);
  }
  for (const decision of data.decisions) {
    const impact = decisionImpact(decision);
    const details = detailsNode(data.run_id, `decision:${decision.decision_id}`, "timeline-card", decision.decision_id === firstExpandedDecision);
    details.id = recordDomId("decision", decision.decision_id);
    const title = node("summary");
    const chosenAssay = decision.context?.assays?.find((item) => item.assay_id === decision.selected_assay_id);
    const titleCandidate = decision.selected_candidate_id ? candidateLabel(decision.selected_candidate_id, false) : "선택 없음";
    title.append(node("strong", `판단 ${decision.decision_no ?? "—"} · ${titleCandidate} · ${chosenAssay?.name || decision.selected_assay_id || "assay 미기록"}`));
    title.append(node("span", `${decision.controller_validation === "applied" ? "controller 검증·적용됨" : "controller가 적용하지 않음"}`, `status ${decision.controller_validation === "applied" ? "completed" : "interrupted"}`));
    if (impact.interpretations) title.append(node("span", `공개 관측 해석 ${impact.interpretations}`, "trace-badge"));
    if (impact.updates) title.append(node("span", `가설 갱신 ${impact.updates}`, "trace-badge hypothesis-badge"));
    details.append(title);
    const content = node("div", undefined, "timeline-content");
    content.append(node("p", `Decision ID: ${decision.decision_id} · state version: ${decision.state_version ?? "—"}`));
    if (decision.decision) {
      const candidateLine = decision.selected_candidate_id ? candidateLabel(decision.selected_candidate_id) : "action을 선택하지 않음";
      const action = decision.decision.action || {};
      content.append(node("h3", "판단 입력과 선택"));
      content.append(node("p", `선택 후보: ${candidateLine}`));
      content.append(node("p", `선택 assay: ${assayLabel(decision.selected_assay_id)}`));
      content.append(node("p", `검증할 가설: ${(decision.decision.hypotheses || []).map((item) => `${shortenedId(item.hypothesis_id)} · ${item.expected_outcome || "예상 결과 미기록"} · ${item.statement || "원문 미기록"}`).join(" / ") || "이 판단에 새 가설 제안 없음"}`));
      content.append(node("p", `Decision basis: ${decision.decision.decision_basis || "기록 없음"}`));
      content.append(node("p", `LLM rationale: ${decision.decision.concise_rationale || "기록 없음"}`));
      content.append(node("p", `Action: ${action.kind || "—"} · ${candidateLabel(action.candidate_id)} · ${assayLabel(action.assay_id)}`));
      const expectedInformation = decision.decision.expected_information;
      content.append(node("p", `기대 정보: ${Array.isArray(expectedInformation) ? expectedInformation.join(" · ") : expectedInformation || "기록 없음"}`));
      content.append(node("p", `Controller 처리: ${decision.controller_validation} · 형식·상태·action 검증 결과이며 가설의 입증이 아님`));
      if (decision.context?.candidates?.length) {
        const candidateDetails = detailsNode(data.run_id, `candidate-context:${decision.decision_id}`, ""); candidateDetails.append(node("summary", "판단 당시 공개 후보 정보"));
        candidateDetails.append(node("pre", JSON.stringify(decision.context.candidates, null, 2))); content.append(candidateDetails);
      }
      if (decision.context?.assays?.length) {
        const assayDetails = detailsNode(data.run_id, `assay-context:${decision.decision_id}`, ""); assayDetails.append(node("summary", "판단 당시 공개 assay 범위"));
        assayDetails.append(node("pre", JSON.stringify(decision.context.assays, null, 2))); content.append(assayDetails);
      }
      if (decision.context?.evidence?.length) {
        const evidenceDetails = detailsNode(data.run_id, `evidence-context:${decision.decision_id}`, ""); evidenceDetails.append(node("summary", `판단 당시 공개 근거 ${decision.context.evidence.length}건`));
        for (const item of decision.context.evidence) evidenceDetails.append(renderEvidence(item, data.run_id, `context:${decision.decision_id}:${item.evidence_id}`));
        content.append(evidenceDetails);
      }
      if (decision.execution) {
        const execution = decision.execution;
        const verdict = execution.status === "no_record" ? "no_record · 이 replay 자료에서 연결된 결과 없음" : execution.status;
        content.append(node("h3", "실행 결과"));
        content.append(node("p", `실행 pair: ${candidateLabel(execution.candidate_id)} · ${assayLabel(execution.assay_id)}`));
        content.append(node("p", `저장된 실행 참조: decision ${decision.decision_id} · Step ${execution.step_no ?? "—"} · ${verdict} · 예산 사용 ${execution.budget_after_step?.spent ?? "미보고"} / 잔여 ${execution.budget_after_step?.available ?? "미보고"} ${execution.budget_after_step?.unit || ""}`));
        const linkedIds = new Set(execution.observation_ids || []);
        const linked = (data.observations || []).filter((obs) => linkedIds.has(obs.observation_id));
        if (linked.length) {
          appendRefs(content, "실행으로 공개된 관측", linked.map((item) => item.observation_id), "observation", observations);
        } else if (execution.status === "no_record") {
          content.append(node("p", "no_record는 음성 결과(Inactive), 실험 실패 또는 후보 탈락이 아닙니다.", "notice"));
        }
      }
      const decisionInterpretations = decision.decision.interpretations || [];
      if (decisionInterpretations.length) {
        content.append(node("h3", "이전 관측 또는 기록 부재 해석"));
        for (const interpretation of decisionInterpretations) {
          const observation = observations.get(interpretation.observation_id);
          const linked = node("div", undefined, "linked-interpretation");
          linked.append(node("p", `해석 대상 pair: ${candidateLabel(interpretation.candidate_id)} · ${assayLabel(interpretation.assay_id)}`));
          linked.append(node("p", `저장된 outcome: ${interpretation.outcome || "미기록"}${observation ? ` · 공개 관측 결과: ${verdictLabels[observation.verdict] || observation.verdict}` : " · 공개 관측에 연결되지 않은 기록"}`));
          linked.append(node("p", interpretation.interpretation || "해석 기록 없음"));
          if (observation) {
            linked.append(node("p", `관측 pair: ${candidateLabel(observation.candidate_id)} · ${assayLabel(observation.assay_id)}`));
            appendRefs(linked, "공개 observation", [observation.observation_id], "observation", observations);
            appendRefs(linked, "근거", interpretation.evidence_refs || [], "evidence", evidence);
          }
          content.append(linked);
        }
      }
      const updates = decision.decision.prior_updates || [];
      const hypothesisEvents = historyByDecision.get(decision.decision_id) || [];
      if (updates.length || hypothesisEvents.length) {
        content.append(node("h3", "가설 상태 변화"));
        for (const update of updates) {
          const event = hypothesisEvents.find((item) => item.hypothesis_id === update.hypothesis_id);
          const hypothesis = hypotheses.get(update.hypothesis_id);
          const oldStatus = update.previous_status || event?.previous_status || "기록 없음";
          const newStatus = update.new_status || event?.new_status || "기록 없음";
          content.append(node("p", `${update.hypothesis_id}: ${oldStatus} → ${newStatus}`, "hypothesis-transition"));
          if (hypothesis) content.append(node("p", `대상 pair: ${candidateLabel(hypothesis.candidate_id)} · ${assayLabel(hypothesis.assay_id)}`));
          content.append(node("p", `갱신 해석: ${update.rationale || update.interpretation || "저장된 해석 없음"}`));
          appendRefs(content, "갱신 관측 근거", update.observation_refs || [], "observation", observations);
          appendRefs(content, "갱신 evidence 근거", update.evidence_refs || [], "evidence", evidence);
        }
        for (const event of hypothesisEvents.filter((item) => !updates.some((update) => update.hypothesis_id === item.hypothesis_id))) {
          content.append(node("p", `${event.hypothesis_id}: ${event.previous_status || "기록 없음"} → ${event.new_status || "기록 없음"} (${event.event_type || "상태 이력"})`, "hypothesis-transition"));
        }
      }
      if (decision.decision.information_gaps?.length) content.append(node("p", `남은 정보 공백: ${decision.decision.information_gaps.join(" · ")}`));
      if (decision.decision.limitations?.length) content.append(node("p", `한계: ${decision.decision.limitations.join(" · ")}`));
    } else content.append(node("p", "검증된 ScientificDecision이 연결되지 않았습니다."));
    details.append(content);
    root.append(details);
  }
  const current = node("section", undefined, "current-hypotheses");
  current.append(node("h3", "현재 가설 상태"));
  if (!(data.hypotheses || []).length) current.append(node("p", "현재 가설이 없습니다."));
  const firstWeakened = (data.hypotheses || []).find((item) => item.status === "weakened" && item.updated_decision_id && (item.last_updated_observation_ids || []).some((id) => observations.has(id)));
  for (const hypothesis of data.hypotheses || []) {
    const linkedDecision = (data.decisions || []).find((item) => item.decision_id === hypothesis.updated_decision_id);
    const isUpdated = Boolean(hypothesis.updated_decision_id && linkedDecision);
    const item = detailsNode(data.run_id, `hypothesis:${hypothesis.hypothesis_id}`, "hypothesis-card", hypothesis.hypothesis_id === firstWeakened?.hypothesis_id);
    const readable = `${candidateLabel(hypothesis.candidate_id, false)} · ${assayLabel(hypothesis.assay_id)}`;
    const summary = node("summary");
    summary.append(node("strong", `${shortenedId(hypothesis.hypothesis_id, 18, 6)} · ${statusNames[hypothesis.status] || hypothesis.status}`));
    summary.append(node("span", readable, "hypothesis-pair"));
    item.append(summary);
    item.append(node("p", `가설 ID: ${hypothesis.hypothesis_id}`, "full-id"));
    item.append(node("p", `검증 전 가설: ${hypothesis.statement || "원문 미기록"}`));
    item.append(node("p", `현재 상태: ${statusNames[hypothesis.status] || hypothesis.status || "상태 미상"}`));
    if (isUpdated) {
      const event = (data.hypothesis_history || []).find((record) => record.decision_id === hypothesis.updated_decision_id && record.hypothesis_id === hypothesis.hypothesis_id);
      item.append(node("p", `갱신 이력: ${event?.previous_status || "기록 없음"} → ${event?.new_status || hypothesis.status || "기록 없음"} · decision ${hypothesis.updated_decision_id}`));
      item.append(node("p", `갱신 근거 해석: ${hypothesis.interpretation || "저장된 해석 없음"}`));
      appendRefs(item, "공개 관측", hypothesis.last_updated_observation_ids || [], "observation", observations);
      appendRefs(item, "공개 evidence", hypothesis.last_updated_evidence_refs || [], "evidence", evidence);
    } else {
      item.append(node("p", "아직 갱신 없음"));
    }
    const proposal = (data.decisions || []).flatMap((record) => record.decision?.hypotheses || []).find((record) => record.hypothesis_id === hypothesis.hypothesis_id);
    if (proposal) item.append(node("p", `최초 제안 decision: ${(data.decisions || []).find((record) => (record.decision?.hypotheses || []).some((entry) => entry.hypothesis_id === hypothesis.hypothesis_id))?.decision_id || "미기록"} · 당시 상태 ${proposal.status || "미기록"}`));
    current.append(item);
  }
  root.append(current);

  const publicRecords = node("section", undefined, "public-records");
  publicRecords.append(node("h3", "공개 관측과 원자료 근거"));
  if (!(data.observations || []).length) publicRecords.append(node("p", "공개 observation 기록이 없습니다."));
  for (const observation of data.observations || []) {
    const item = detailsNode(data.run_id, `public-observation:${observation.observation_id}`, "public-record-card");
    item.id = recordDomId("observation", observation.observation_id);
    const title = node("summary");
    title.append(node("strong", `${candidateLabel(observation.candidate_id)} · ${assayLabel(observation.assay_id)}`));
    title.append(node("span", verdictLabels[observation.verdict] || observation.verdict, `verdict ${observation.verdict || "unknown"}`));
    title.append(node("span", shortenedId(observation.observation_id), "record-id"));
    item.append(title);
    const value = observation.value == null ? "범주형 관측 · 수치값 없음" : `${observation.comparison || ""} ${observation.value} ${observation.unit || ""}`.trim();
    item.append(node("p", `실제 결과: ${observation.raw_verdict || verdictLabels[observation.verdict] || observation.verdict || "미기록"} · ${value}`));
    item.append(node("p", `Observation ID: ${observation.observation_id} · assay ID: ${observation.assay_id || "미기록"} · 공개 시각: ${observation.released_at || "미기록"}`));
    if (observation.interpretation) item.append(node("p", `저장된 해석: ${observation.interpretation.interpretation || "해석 없음"} · decision ${observation.interpretation.decision_id || "미기록"}`));
    appendRefs(item, "원자료 evidence", observation.evidence_refs || [], "evidence", evidence);
    root.append(item);
  }
  if ((data.evidence || []).length) {
    const evidenceSection = node("div", undefined, "public-evidence-list");
    evidenceSection.append(node("h4", "공개 evidence 상세"));
    for (const item of data.evidence) evidenceSection.append(renderEvidence(item, data.run_id, `public:${item.evidence_id}`, true));
    publicRecords.append(evidenceSection);
  }
  root.append(publicRecords);
}

function recordDomId(kind, id) { return `${kind}-${encodeURIComponent(id || "missing")}`; }

function renderEvidence(item, runId = null, persistKey = "", target = false) {
  const details = runId ? detailsNode(runId, `evidence:${persistKey}`, "evidence-item") : node("details", undefined, "evidence-item");
  if (target) details.id = recordDomId("evidence", item.evidence_id);
  const title = node("summary");
  title.append(node("strong", `${item.source_id || item.source_kind} · ${shortenedId(item.evidence_id)}`));
  title.append(node("span", `SHA-256 ${shortenedId(item.sha256 || "", 20, 8)}`, "record-id"));
  details.append(title);
  if (target) details.append(node("p", `Evidence ID: ${item.evidence_id} · source: ${item.source_id || item.source_kind || "미기록"} · SHA-256: ${item.sha256 || "미기록"}`, "full-id"));
  const definition = item.public_content?.assay_definition;
  if (definition?.protocol_location && /^https:\/\/pubchem\.ncbi\.nlm\.nih\.gov\/bioassay\/\d+$/.test(definition.protocol_location)) {
    const link = node("a", `PubChem AID ${definition.aid} 출처 (외부 자료)`);
    link.href = definition.protocol_location; link.target = "_blank"; link.rel = "noopener noreferrer"; details.append(link);
  }
  const rows = item.public_content?.rows || [];
  for (const row of rows) details.append(node("pre", `후보 ${row.candidate_id || "미기록"} · assay ${row.assay_id || "미기록"} · observation ${row.observation_id || "미기록"}\n${JSON.stringify(row.raw_row || {}, null, 2)}\nsource row ${row.source_row_id || "미기록"} #${row.source_row_number ?? "미기록"} · source file SHA-256 ${row.source_file_sha256 || "미기록"}`));
  if (!rows.length) details.append(node("p", "이 판단에 포함된 허용 공개 근거의 원자료 행이 없습니다."));
  return details;
}

byId("science-timeline").addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-target]");
  if (!button) return;
  const target = document.getElementById(button.dataset.openTarget);
  if (!target) return;
  event.preventDefault();
  for (let parent = target; parent; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") {
      parent.open = true;
      const key = parent.dataset.persistKey;
      if (key && activeRunId) detailState(activeRunId).set(key, true);
    }
  }
  target.scrollIntoView({ behavior: "smooth", block: "center" });
});

function renderSteps(steps) {
  const tbody = byId("steps"); tbody.replaceChildren();
  if (!steps.length) {
    const row = node("tr"), cell = node("td", "공개 실행 기록이 없습니다.", "empty-cell");
    cell.colSpan = 6; row.append(cell); tbody.append(row); return;
  }
  for (const step of steps) {
    const row = node("tr");
    const values = [step.step_no, step.candidate_id, step.assay_id, step.selection_reason || "—", step.status,
      `${step.budget_after_step?.available ?? "—"} ${step.budget_after_step?.unit || ""}`];
    for (const value of values) row.append(node("td", value));
    tbody.append(row);
  }
}
function renderResults(executions) {
  const root = byId("results"); root.replaceChildren();
  const observations = executions.flatMap((execution) => (execution.observations || []).map((observation) => ({ execution, observation })));
  if (!observations.length) { root.append(node("p", "공개된 후속 Observation이 없습니다.", "empty")); return; }
  for (const { execution, observation } of observations) {
    const card = node("details", undefined, "result-card"), summary = node("summary");
    summary.append(node("strong", `${observation.candidate_id} · ${observation.assay_id}`));
    summary.append(node("span", verdictLabels[observation.verdict] || observation.verdict, `verdict ${observation.verdict}`));
    card.append(summary, node("p", `Observation ${observation.observation_id} · ${observation.released_at}`));
    for (const item of execution.evidence || []) card.append(node("pre", JSON.stringify(item, null, 2)));
    root.append(card);
  }
}

async function loadHistory(restore = true) {
  try {
    const data = await api("/api/runs"), history = byId("run-history"), selected = history.value;
    history.replaceChildren(node("option", "최근 실행 불러오기")); history.options[0].value = "";
    for (const run of data.runs || []) {
      const origin = run.origin === "live_web_run" ? "웹 실행 기록" : run.origin === "stored_actual_run" ? "저장 기록" : run.mode === "scientific_reasoner" ? "과학 run" : "baseline";
      const option = node("option", `${origin} · ${run.service_status} · ${run.run_id.slice(-8)}`);
      option.value = run.run_id; history.append(option);
    }
    if ([...history.options].some((option) => option.value === selected)) history.value = selected;
    if (restore && !activeRunId) {
      const saved = localStorage.getItem("assaypilot-stage4-last-run");
      if (saved && (data.runs || []).some((run) => run.run_id === saved)) await openRun(saved);
    }
  } catch (_) { /* History lookup can fail temporarily without blocking the current run view. */ }
}
async function openRun(runId) {
  clearPolling();
  if (activeRunId !== runId) {
    activeRunId = runId;
    latestRun = null;
    byId("empty-run").classList.remove("hidden");
    byId("empty-run").textContent = "선택한 run을 불러오는 중입니다.";
    byId("run-view").classList.add("hidden");
    byId("science-view").classList.add("hidden");
    byId("baseline-trace").classList.add("hidden");
    byId("baseline-results").classList.add("hidden");
    byId("run-error").textContent = "";
  }
  try {
    const data = await api(runUrl(runId));
    if (activeRunId !== runId) return;
    localStorage.setItem("assaypilot-stage4-last-run", runId);
    byId("run-history").value = runId;
    renderRun(data); requestNextPoll(data.service_status);
  } catch (error) {
    if (activeRunId === runId) {
      byId("empty-run").textContent = `선택한 run 조회 실패: ${error.message}`;
      byId("empty-run").classList.remove("hidden");
    }
    throw error;
  }
}

byId("run-form").addEventListener("submit", async (event) => {
  event.preventDefault(); formError.textContent = "";
  const science = currentMode() === "scientific_reasoner";
  const request = science ? {
    mode: "scientific_reasoner", campaign: campaignSelect.value, budget: byId("budget").value,
    max_steps: Number(byId("max-steps").value), max_duration_seconds: Number(byId("max-duration-seconds").value),
    max_llm_calls: Number(byId("max-llm-calls").value), shortlist_size: Number(byId("shortlist-size").value),
    shortlist_seed: Number(byId("shortlist-seed").value),
  } : {
    mode: "baseline", campaign: campaignSelect.value, selector: selectorSelect.value,
    budget: byId("budget").value, max_steps: Number(byId("max-steps").value),
  };
  if (!science && request.selector === "seeded_random_priority") request.seed = Number(byId("seed").value);
  byId("start").disabled = true;
  try {
    const response = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(request),
    });
    await openRun(response.run.run_id); await loadHistory(false);
  } catch (error) {
    formError.textContent = error.message; await loadReadiness();
  } finally { refreshStartAvailability(); }
});

document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => applyExample(button.dataset.example)));
selectorSelect.addEventListener("change", updateSeedVisibility);
byId("mode").addEventListener("change", updateModeVisibility);
campaignSelect.addEventListener("change", refreshStartAvailability);
byId("run-history").addEventListener("change", async (event) => {
  if (!event.target.value) return;
  try { await openRun(event.target.value); }
  catch (error) { byId("run-error").textContent = error.message; }
});
byId("download").addEventListener("click", () => {
  if (latestRun?.download_url) window.location.assign(latestRun.download_url);
});
byId("resume-run").addEventListener("click", async () => {
  if (!activeRunId || !latestRun?.resume_available) return;
  byId("resume-run").disabled = true;
  try {
    const response = await api(`/api/runs/${encodeURIComponent(activeRunId)}/resume`, { method: "POST" });
    renderRun(response.run);
    requestNextPoll(response.run.service_status);
  } catch (error) {
    byId("run-error").textContent = error.message;
  } finally {
    byId("resume-run").disabled = false;
  }
});

updateModeVisibility();
Promise.all([loadReadiness(), loadHistory()]);
