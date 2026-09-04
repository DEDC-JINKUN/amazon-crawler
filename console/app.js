const initialTenant = new URL(window.location.href).searchParams.get('tenant') || '';
const state = { overview: null, batches: [], operations: [], items: [], tenant: initialTenant, selectedRun: '', timer: null, loading: false, apiKey: '', authCancelled: false };
const $ = (id) => document.getElementById(id);
const number = (value) => new Intl.NumberFormat('zh-CN').format(Number(value || 0));
const dateTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const bytes = (value) => {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KiB`;
  if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(2)} MiB`;
  return `${(size / 1024 ** 3).toFixed(2)} GiB`;
};
const trafficBytes = (category) => category && category.bytes !== null && category.bytes !== undefined ? bytes(category.bytes) : 'unknown';
const trafficSub = (category) => `${number(category?.known_records)} known · ${number(category?.unknown_records)} unknown`;
const text = (tag, value, className = '') => {
  const node = document.createElement(tag);
  node.textContent = value ?? '—';
  if (className) node.className = className;
  return node;
};
const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

function apiKey() { return state.apiKey; }
function consoleRequestError(code, message) { const error = new Error(message); error.code = code; return error; }
function promptForApiKey(force = false) {
  if (state.authCancelled && !force) return false;
  const supplied = window.prompt('该控制台需要本地API Key。Key只保存在当前标签页内存。');
  if (!supplied) {
    state.apiKey = '';
    state.authCancelled = true;
    return false;
  }
  state.apiKey = supplied;
  state.authCancelled = false;
  return true;
}
async function request(path) {
  const sentKey = apiKey();
  const headers = sentKey ? { 'X-Collection-API-Key': sentKey } : {};
  const target = new URL(path, window.location.origin);
  if (state.tenant && target.pathname !== '/api/tenants') target.searchParams.set('tenant', state.tenant);
  let response;
  try { response = await fetch(target, { headers, cache: 'no-store' }); }
  catch (_error) { throw consoleRequestError('offline', '网络离线，无法连接本机Console'); }
  if (response.status === 401) {
    if (apiKey() && apiKey() !== sentKey) return request(path);
    if (promptForApiKey()) return request(path);
    throw consoleRequestError('locked', '需要本地API Key；Console已锁定');
  }
  if (!response.ok) throw consoleRequestError('api', `Console API返回HTTP ${response.status}`);
  return response.json();
}

function selectTenant(tenantId, refreshNow = true) {
  state.tenant = tenantId;
  state.selectedRun = '';
  const url = new URL(window.location.href);
  if (tenantId) url.searchParams.set('tenant', tenantId); else url.searchParams.delete('tenant');
  window.history.replaceState({}, '', url);
  $('tenantSelector').value = tenantId;
  if (refreshNow) refresh();
}

function renderBatches(data) {
  state.batches = data.items || [];
  const selector = $('tenantSelector'); clear(selector);
  for (const batch of state.batches) selector.append(new Option(batch.tenant_id, batch.tenant_id));
  if (!state.tenant && state.batches.length) state.tenant = state.batches[0].tenant_id;
  if (state.tenant) selector.value = state.tenant;
  const body = $('batchRows'); clear(body);
  for (const batch of state.batches) {
    const row = document.createElement('tr');
    if (batch.tenant_id === state.tenant) row.classList.add('selected');
    row.append(
      taskCell(batch.tenant_id, 'asin'),
      taskCell(`${number(batch.requested)} / ${number(batch.recorded)}`),
      taskCell(number(batch.product_succeeded)),
      taskCell(number(batch.variant_redirect)),
      taskCell(number(batch.failed)),
      taskCell(number(batch.blocked)),
      taskCell(`${number(batch.pending)} / ${number(batch.running)}`),
      taskCell(`${bytes(batch.known_transfer_bytes)}${batch.unknown_transfer_records ? ` · ${number(batch.unknown_transfer_records)} unknown` : ''}`),
      taskCell(batch.active_duration_seconds === null ? '—' : `${number(batch.active_duration_seconds)}s`),
      taskCell(batch.wall_span_seconds === null ? '—' : `${number(batch.wall_span_seconds)}s（含等待）`),
      taskCell(batch.terminal_status),
    );
    row.addEventListener('click', () => selectTenant(batch.tenant_id));
    body.append(row);
  }
  if (!state.batches.length) {
    const row = document.createElement('tr'); const cell = taskCell('PostgreSQL中没有可见tenant', 'muted'); cell.colSpan = 11; row.append(cell); body.append(row);
  }
}

async function loadBatches() {
  const data = await request('/api/tenants');
  renderBatches(data);
  return data;
}

function renderChips(id, values, alert = false) {
  const root = $(id); clear(root);
  const entries = Object.entries(values || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) root.append(text('span', '无', 'muted'));
  for (const [key, value] of entries) root.append(text('span', `${key} · ${number(value)}`, `chip${alert && key !== 'none' ? ' alert' : ''}`));
}

function renderOverview(data) {
  state.overview = data;
  const selectedBatch = state.batches.find((batch) => batch.tenant_id === data.tenant_id) || {};
  $('tenantLabel').textContent = `Tenant · ${data.tenant_id}`;
  $('observedAt').textContent = `更新 ${dateTime(data.observed_at)}`;
  $('progressMetric').textContent = `${data.progress.percent}%`;
  $('progressSub').textContent = `${number(data.progress.touched)} / ${number(data.progress.total)} ASIN`;
  $('productsMetric').textContent = number(data.progress.successful_products);
  $('partialMetric').textContent = number(data.context_quality_counts?.partial);
  $('blockedMetric').textContent = number(selectedBatch.blocked ?? data.status_counts.blocked);
  $('failedMetric').textContent = number(selectedBatch.failed ?? data.status_counts.failed);
  $('actionsMetric').textContent = number(data.four_scale_metrics.page_actions);
  $('rowsMetric').textContent = number(data.four_scale_metrics.database_rows);
  $('rawMetric').textContent = data.traffic.saved_raw_html_bytes === null ? 'unknown' : bytes(data.traffic.saved_raw_html_bytes);
  $('rawSub').textContent = data.traffic.raw_html_files === null ? '跨输出目录不聚合；以evidence路径为准' : `${number(data.traffic.raw_html_files)} files`;
  $('httpTrafficMetric').textContent = trafficBytes(data.traffic.http_compressed_response);
  $('httpTrafficSub').textContent = trafficSub(data.traffic.http_compressed_response);
  $('firefoxMainMetric').textContent = trafficBytes(data.traffic.firefox_main_document);
  $('firefoxMainSub').textContent = trafficSub(data.traffic.firefox_main_document);
  $('firefoxSubresourceMetric').textContent = trafficBytes(data.traffic.firefox_subresources);
  $('firefoxSubresourceSub').textContent = trafficSub(data.traffic.firefox_subresources);
  $('proxyBillMetric').textContent = trafficBytes(data.traffic.proxy_dashboard_bill);
  const statusRoot = $('statusList'); clear(statusRoot);
  const total = Math.max(1, Object.values(data.status_counts).reduce((sum, value) => sum + value, 0));
  for (const [key, value] of Object.entries(data.status_counts)) {
    const row = text('div', '', 'status-row');
    row.append(text('span', key));
    const track = text('div', '', 'status-track');
    const fill = text('div', '', 'status-fill'); fill.style.width = `${Math.max(1, value / total * 100)}%`; track.append(fill);
    row.append(track, text('strong', number(value))); statusRoot.append(row);
  }
  renderChips('errorList', data.error_counts, true);
  renderChips('blockList', data.block_counts, true);
  renderChips('sourceList', data.source_counts);
  const runs = $('runList'); clear(runs);
  for (const run of data.recent_runs || []) {
    const node = text('div', '', 'run-item');
    node.append(text('strong', run.run_id), text('small', `${number(run.actions)} actions · ${dateTime(run.ended_at)} · blocked ${number(run.blocked)} · partial ${number(run.partial)}`));
    node.tabIndex = 0; node.addEventListener('click', () => selectRun(run.run_id));
    runs.append(node);
  }
  if (!(data.recent_runs || []).length) runs.append(text('span', '暂无运行', 'muted'));
}

function renderRun(data) {
  state.selectedRun = data.run_id;
  const outcomes = data.outcome_counts || (data.items || []).reduce((value, item) => { value[item.outcome] = (value[item.outcome] || 0) + 1; return value; }, {});
  const proxyPool = data.proxy_session_pool || {}; const proxySessions = proxyPool.sessions || [];
  const proxyTotals = proxySessions.reduce((sum, item) => { for (const key of ['request_count', 'completed', 'variant_redirect', 'failed', 'blocked', 'bytes', 'latency_ms']) sum[key] = (sum[key] || 0) + Number(item[key] || 0); return sum; }, {});
  const capacity = data.capacity_authorization || {};
  const connectivity = data.proxy_connectivity || {};
  const business = data.amazon_business || {};
  $('runSummary').textContent = `${data.run_id} · 代理连通 canary ${connectivity.canary_status || 'unknown'} · available/unique ${knownNumber(connectivity.available_slots)}/${knownNumber(connectivity.unique_egress_count)} · gate ${connectivity.gate_status || 'unknown'}:${connectivity.gate_reason || 'unknown'} · Amazon业务 requested/recorded ${knownNumber(business.requested_actions ?? data.requested_actions)}/${knownNumber(business.recorded_actions ?? data.recorded_actions)} · completed/variant/failed/blocked ${knownNumber(business.completed_actions ?? outcomes.completed)}/${knownNumber(business.variant_redirect_actions ?? outcomes.variant_redirect)}/${knownNumber(business.failed_actions ?? outcomes.failed)}/${knownNumber(business.blocked_actions ?? outcomes.blocked)} · access-control ${business.access_control_rate == null ? 'unknown' : `${(Number(business.access_control_rate) * 100).toFixed(1)}%`} · canary ${capacity.canary_operation_id || 'unknown'} · reservation ${capacity.reservation_id || 'unknown'} · reserved slots ${knownNumber(capacity.reserved_slots)} · fact expiry ${dateTime(capacity.fact_expires_at)} · proxy sessions ${number(proxySessions.length)} ${proxyPool.mode || '—'} · product ${proxyPool.product_session_scope || 'unknown'} / reviews ${proxyPool.review_session_scope || 'unknown'} · requests ${number(proxyTotals.request_count)} · session completed/variant/blocked ${number(proxyTotals.completed)}/${number(proxyTotals.variant_redirect)}/${number(proxyTotals.blocked)} · bytes ${bytes(proxyTotals.bytes)} · latency ${number(proxyTotals.latency_ms)}ms · unrequested ${knownNumber(business.unrequested_actions ?? proxyPool.unrequested_count)} · circuit ${proxyPool.circuit_open_reason || '—'} · 终态 ${data.terminal_status || '—'} · 活跃处理 ${data.worker_duration_seconds == null ? '—' : `${number(data.worker_duration_seconds)}s`}（${data.duration_source || 'unknown'}）· controller ${data.controller_duration_seconds == null ? '—' : `${number(data.controller_duration_seconds)}s`} · ${dateTime(data.started_at)} → ${dateTime(data.ended_at)} · HTTP ${trafficBytes(data.traffic?.http_compressed_response)} · Firefox主文档 ${trafficBytes(data.traffic?.firefox_main_document)} · Firefox子资源 ${trafficBytes(data.traffic?.firefox_subresources)}`;
  $('runWarning').textContent = data.context_quality_counts?.partial ? '⚠ 本run含ZIP未确认的partial商品；price、availability、buy_box/配送等位置敏感字段不可视为90001结果。' : data.inferred_actions ? '⚠ 历史网络失败没有run evidence；黄色归属为按本run时间窗口推断。新运行已永久修复。' : '全部结果均有不可变run evidence。';
  if (data.recovery_batch) $('runSummary').textContent += ` · 冻结cohort ${number(data.recovery_batch.asins?.length)} · 尝试 ${number(data.attempt_actions)} · consumer ${data.recovery_batch.status} · deadline ${dateTime(data.recovery_batch.deadline)}`;
  const body = $('runRows'); clear(body);
  for (const item of data.items || []) {
    const row = document.createElement('tr'); row.dataset.asin = item.asin;
    row.append(taskCell(item.asin, 'asin'));
    const outcome = document.createElement('td'); const outcomeLabel = item.context_quality === 'partial' && item.outcome === 'completed' ? 'completed · partial' : item.outcome; outcome.append(text('span', outcomeLabel, `status-badge ${item.context_quality === 'partial' ? 'partial' : item.outcome}`)); row.append(outcome);
    row.append(taskCell(item.title || '—', 'product-cell'), taskCell(item.price_status), taskCell(item.source_type), taskCell(item.http_status));
    const runReason = item.error_code || item.block_reason || (item.attribution === 'evidence' ? '' : item.last_error) || '—';
    row.append(taskCell(runReason, 'product-cell'));
    const attribution = document.createElement('td'); attribution.append(text('span', item.attribution === 'evidence' ? 'evidence' : '时间推断', `status-badge ${item.attribution === 'evidence' ? '' : 'inferred'}`)); row.append(attribution);
    row.append(taskCell(dateTime(item.retrieved_at || item.updated_at)));
    row.addEventListener('click', () => openDetail(item.asin)); body.append(row);
  }
}

async function loadRun(runId) {
  if (!runId) return;
  renderRun(await request(`/api/runs/${encodeURIComponent(runId)}`));
}

async function selectRun(runId) {
  state.selectedRun = runId;
  $('runSelector').value = runId;
  try { await loadRun(runId); }
  catch (error) { $('runWarning').textContent = `运行详情读取失败：${error.message}`; }
}

async function loadRuns() {
  const data = await request('/api/runs?limit=20');
  const selector = $('runSelector');
  const desired = state.selectedRun || selector.value || data.items?.[0]?.run_id || '';
  clear(selector); selector.append(new Option('选择 run_id', ''));
  for (const run of data.items || []) selector.append(new Option(`${run.run_id} · ${run.requested_actions}/${run.recorded_actions} · ${run.terminal_status}`, run.run_id));
  if (desired && (data.items || []).some((run) => run.run_id === desired)) {
    selector.value = desired; await loadRun(desired);
  }
}

function renderOperations(data) {
  state.operations = data.items || [];
  const body = $('operationRows'); clear(body);
  for (const operation of state.operations) {
    const row = document.createElement('tr');
    row.append(
      taskCell(operation.operation_id, 'asin'),
      taskCell(operation.operation_type),
      taskCell(operation.status),
      taskCell(operation.preflight_status),
      taskCell(operation.failure_stage),
      taskCell(operation.error_class),
      taskCell(operation.egress_id),
      taskCell(operation.http_status),
      taskCell(canarySummary(operation), 'product-cell'),
      taskCell(operation.duration_seconds == null ? '—' : `${number(operation.duration_seconds)}s`),
      taskCell(`${dateTime(operation.started_at)} / ${dateTime(operation.finished_at)}`),
      taskCell(operation.collection_run_id),
    );
    body.append(row);
  }
  if (!state.operations.length) {
    const row = document.createElement('tr'); const cell = taskCell('暂无操作记录', 'muted'); cell.colSpan = 12; row.append(cell); body.append(row);
  }
}

function knownNumber(value) { return value === null || value === undefined ? 'unknown' : number(value); }
function canarySummary(operation) {
  if (!['canary', 'capacity_reservation'].includes(operation.operation_type) && !operation.capacity_reservation_id) return '—';
  const p95 = operation.canary_p95_latency_ms === null || operation.canary_p95_latency_ms === undefined
    ? 'unknown' : `${number(operation.canary_p95_latency_ms)}ms`;
  return `status ${operation.canary_status || 'unknown'} · canary ${operation.authorizing_canary_operation_id || operation.operation_id || 'unknown'} · reservation ${operation.capacity_reservation_id || 'unknown'} · planned/tested/available/unique ${knownNumber(operation.planned_slots)}/${knownNumber(operation.tested_slots)}/${knownNumber(operation.available_slots)}/${knownNumber(operation.unique_egress_count)} · capacity ${knownNumber(operation.slot_capacity)}/${knownNumber(operation.requested_capacity)} · required/reserved slots ${knownNumber(operation.required_slots)}/${knownNumber(operation.reserved_slots)} · duplicates ${knownNumber(operation.duplicate_egress_count)} · fact expiry ${dateTime(operation.capacity_fact_expires_at)} · gate ${operation.capacity_gate_status || 'unknown'}:${operation.capacity_gate_reason || 'unknown'} · p95 ${p95}`;
}

async function loadOperations() {
  renderOperations(await request('/api/operations?limit=100'));
}

function taskCell(value, className = '') { const td = text('td', value ?? '—', className); return td; }
function renderItems(data) {
  state.items = data.items || [];
  const body = $('taskRows'); clear(body);
  for (const item of state.items) {
    const row = document.createElement('tr'); row.dataset.asin = item.asin;
    row.append(taskCell(item.asin, 'asin'), taskCell(item.title || '—', 'product-cell'));
    const statusCell = document.createElement('td'); statusCell.append(text('span', item.status, `status-badge ${item.status}`)); row.append(statusCell);
    row.append(taskCell(item.task_stage), taskCell(item.price_status), taskCell(item.source_type), taskCell(item.http_status));
    row.append(taskCell(item.last_error || item.evidence_error || item.block_reason || item.evidence_block || '—', 'product-cell'));
    row.append(taskCell(dateTime(item.updated_at)));
    row.addEventListener('click', () => openDetail(item.asin)); body.append(row);
  }
  if (!state.items.length) {
    const row = document.createElement('tr'); const cell = taskCell('没有符合条件的任务', 'muted'); cell.colSpan = 9; row.append(cell); body.append(row);
  }
  $('taskSummary').textContent = `显示 ${number(state.items.length)} / ${number(data.total)} 条`;
}

function detailSection(titleValue) {
  const section = text('section', '', 'detail-section'); section.append(text('h3', titleValue)); return section;
}
function fieldGrid(values) {
  const grid = text('div', '', 'detail-grid');
  for (const [key, value] of Object.entries(values)) {
    const node = text('div', '', 'detail-field'); node.append(text('span', key), text('strong', value ?? '—')); grid.append(node);
  }
  return grid;
}
function jsonBlock(value) { const pre = document.createElement('pre'); pre.textContent = JSON.stringify(value ?? {}, null, 2); return pre; }
function linkItem(url, label) {
  const item = text('div', '', 'detail-item');
  if (url && /^https:\/\//i.test(url)) { const link = text('a', label || url); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link); }
  else item.append(text('span', label || url || '—'));
  return item;
}

async function openDetail(asin) {
  const drawer = $('detailDrawer'); drawer.classList.add('open'); drawer.setAttribute('aria-hidden', 'false'); $('drawerBackdrop').hidden = false;
  $('detailTitle').textContent = asin; const root = $('detailBody'); clear(root); root.append(text('p', '正在读取详情…', 'muted'));
  try {
    const data = await request(`/api/items/${encodeURIComponent(asin)}`); clear(root);
    const task = detailSection('任务状态'); task.append(fieldGrid({ status: data.task.status, stage: data.task.task_stage, attempts: `${data.task.attempts}/${data.task.max_attempts}`, last_error: data.task.last_error, block_reason: data.task.block_reason, updated_at: dateTime(data.task.updated_at) })); root.append(task);
    const product = detailSection('商品快照');
    const partialContext = (data.evidence || []).find((value) => value.context_json?.context_quality === 'partial')?.context_json; if (partialContext) product.append(text('p', `⚠ ZIP ${partialContext.expected_postal || '目标值'} 未确认（观测 ${partialContext.observed_postal || 'unknown'}）；${(partialContext.location_sensitive_fields_unverified || []).join(', ')} 不可视为目标ZIP结果。`, 'table-summary'));
    if (data.product) product.append(fieldGrid({ title: data.product.title, brand: data.product.brand, price: data.product.price, price_status: data.product.price_status, availability: data.product.availability, rating: data.product.rating, reviews: data.product.reported_review_count }), jsonBlock({ bullets: data.product.bullets, specs: data.product.specs, buy_box: data.product.buy_box }));
    else product.append(text('p', '尚无有效商品快照', 'muted')); root.append(product);
    const media = detailSection(`媒体 URL · ${(data.media || []).length}`); const mediaList = text('div', '', 'detail-list'); for (const value of data.media || []) mediaList.append(linkItem(value.display_url || value.asset_url || value.thumbnail_url, `${value.placement || 'media'} · ${value.entry_type || ''}`)); media.append(mediaList); root.append(media);
    const topReviews = detailSection(`商品页 Top Reviews · ${(data.top_reviews || []).length}`); for (const review of data.top_reviews || []) { const item = text('div', '', 'detail-item'); item.append(text('strong', review.title || review.rating || 'Review'), text('p', review.body || review.text || JSON.stringify(review))); topReviews.append(item); } if (!(data.top_reviews || []).length) topReviews.append(text('p', '无商品页评论摘要', 'muted')); root.append(topReviews);
    const evidence = detailSection(`Evidence · ${(data.evidence || []).length}`); for (const value of data.evidence || []) { const traffic = value.context_json?.traffic || {}; const bridge = value.context_json?.cookie_bridge || {}; const proxyPool = value.context_json?.proxy_session_pool || {}; const proxySessions = proxyPool.sessions || []; const proxyTotals = proxySessions.reduce((sum, item) => { for (const key of ['request_count', 'completed', 'variant_redirect', 'failed', 'blocked', 'bytes', 'latency_ms']) sum[key] = (sum[key] || 0) + Number(item[key] || 0); return sum; }, {}); evidence.append(fieldGrid({ outcome: value.outcome, source: value.source_type, proxy_session: proxyPool.current_session_id, proxy_mode: proxyPool.mode, proxy_requests: proxyTotals.request_count, proxy_completed: proxyTotals.completed, proxy_variant: proxyTotals.variant_redirect, proxy_failed: proxyTotals.failed, proxy_blocked: proxyTotals.blocked, proxy_bytes: proxyTotals.bytes, proxy_latency_ms: proxyTotals.latency_ms, proxy_quarantines: proxySessions.filter((item) => item.quarantine_reason).map((item) => `${item.session_id}:${item.quarantine_reason}`).join(', '), proxy_circuit: proxyPool.circuit_open_reason, proxy_unrequested: proxyPool.unrequested_count, context_quality: value.context_json?.context_quality, identity_relation: value.context_json?.identity ? `${value.context_json.identity.requested_asin} → ${value.context_json.identity.observed_asin} · parent ${value.context_json.identity.parent_asin}` : null, postal_confirmed: value.context_json?.postal_confirmed, expected_postal: value.context_json?.expected_postal, observed_postal: value.context_json?.observed_postal, location_sensitive_fields_unverified: (value.context_json?.location_sensitive_fields_unverified || []).join(', '), fallback_reason: value.context_json?.fallback_reason, fallback_reasons: (value.context_json?.fallback_reasons || []).join(', '), cookie_bridge: bridge.status, cookie_bridge_error: bridge.error_code, http: value.http_status, error: value.error_code, block: value.block_reason, retrieved: dateTime(value.retrieved_at), raw_html_path: value.raw_html_path, http_compressed_bytes: traffic.http_compressed_response_bytes, firefox_main_bytes: traffic.firefox_main_document_bytes ?? 'unknown', firefox_subresource_bytes: traffic.firefox_subresource_bytes ?? 'unknown' })); } root.append(evidence);
    const content = detailSection(`内容模块 · ${(data.content_modules || []).length}`); content.append(jsonBlock(data.content_modules || [])); root.append(content);
    const reviews = detailSection('独立评论状态'); reviews.append(jsonBlock({ summary: data.review_summary, records: data.reviews || [] })); root.append(reviews);
  } catch (error) { clear(root); root.append(text('p', `详情读取失败：${error.message}`, 'error-banner')); }
}

function closeDetail() { $('detailDrawer').classList.remove('open'); $('detailDrawer').setAttribute('aria-hidden', 'true'); $('drawerBackdrop').hidden = true; }
async function loadItems() {
  const params = new URLSearchParams();
  if ($('statusFilter').value) params.set('status', $('statusFilter').value);
  if ($('stageFilter').value) params.set('stage', $('stageFilter').value);
  if ($('queryInput').value.trim()) params.set('q', $('queryInput').value.trim());
  params.set('limit', '100');
  renderItems(await request(`/api/items?${params.toString()}`));
}
function renderRecovery(data) {
  const root = $('recoverySummary');
  if (data?.availability !== 'available') { root.textContent = '持久化恢复：unknown（未部署或不可用），不得据此领取任务。'; return; }
  const gates = data.egress || [];
  const paused = gates.some((gate) => gate.manually_paused || new Date(gate.paused_until).getTime() > Date.now());
  const counts = Object.entries(data.counts || {}).map(([key,value]) => `${key}: ${value}`).join(' · ') || '尚无恢复job';
  root.textContent = `持久化恢复：${paused ? '全局暂停' : gates.some((gate) => gate.half_open) ? '半开验证' : '串行许可'} · ${counts} · 跨run预算不重置；供应商计费 unknown；这是当前tenant状态，不改写历史run。`;
}

async function refresh() {
  if (state.loading) return; state.loading = true;
  try {
    await loadBatches();
    if (!state.tenant) throw new Error('PostgreSQL中没有可见tenant');
    const [overview, recovery] = await Promise.all([request('/api/overview'), request('/api/recovery'), loadItems(), loadRuns(), loadOperations()]);
    renderRecovery(recovery);
    renderOverview(overview); $('errorBanner').hidden = true;
    $('liveBadge').classList.remove('offline', 'locked'); $('liveBadge').textContent = '● 实时';
  } catch (error) {
    const locked = error.code === 'locked';
    const offline = error.code === 'offline';
    $('errorBanner').textContent = locked
      ? '需要本地API Key；Console已锁定。自动刷新不会再次弹窗，请点击“API Key”重试。'
      : offline ? `网络离线：${error.message}。上一轮数据已保留。`
        : `Console API读取失败：${error.message}。上一轮数据已保留。`;
    $('errorBanner').hidden = false;
    $('liveBadge').classList.remove('offline', 'locked');
    $('liveBadge').classList.add(locked ? 'locked' : 'offline');
    $('liveBadge').textContent = locked ? '🔒 locked' : '● 离线';
  } finally { state.loading = false; }
}

$('refreshButton').addEventListener('click', refresh);
$('tenantSelector').addEventListener('change', (event) => selectTenant(event.target.value));
$('runSelector').addEventListener('change', (event) => selectRun(event.target.value));
$('filterForm').addEventListener('submit', (event) => { event.preventDefault(); loadItems().catch((error) => { $('errorBanner').textContent = error.message; $('errorBanner').hidden = false; }); });
$('apiKeyButton').addEventListener('click', () => {
  if (promptForApiKey(true)) refresh();
  else {
    $('errorBanner').textContent = '需要本地API Key；Console已锁定。自动刷新不会再次弹窗，请点击“API Key”重试。';
    $('errorBanner').hidden = false;
    $('liveBadge').classList.remove('offline'); $('liveBadge').classList.add('locked'); $('liveBadge').textContent = '🔒 locked';
  }
});
$('closeDrawer').addEventListener('click', closeDetail); $('drawerBackdrop').addEventListener('click', closeDetail);
refresh(); state.timer = window.setInterval(refresh, 5000);
