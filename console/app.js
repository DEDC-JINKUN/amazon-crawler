const initialTenant = new URL(window.location.href).searchParams.get('tenant') || '';
const state = { overview: null, batches: [], operations: [], items: [], tenant: initialTenant, selectedRun: '', runRequest: 0, runSelectionCleared: false, timer: null, loading: false, apiKey: '', authCancelled: false };
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

function statusLabel(value) {
  return ({running:'Running',starting:'Starting',completed:'Complete',succeeded:'Collected',
    failed:'Failed',blocked:'Needs attention',quality_failed:'Completed with issues',
    interrupted:'Interrupted',pending:'Pending',reviews_pending:'Reviews pending',
    product_done:'Collected',partial:'Partial',full:'Qualified',invalid:'Invalid',unknown:'Unknown',
    legacy_complete:'Complete',legacy_blocked:'Needs attention',queued:'Queued',claimed:'Running',
    not_started:'Not started',skipped:'Skipped'})[value] || 'Unknown';
}
function issueLabel(value) {
  const code = String(value || '');
  if (!code || code === 'none' || code === '—') return '—';
  if (/captcha|robot_check/i.test(code)) return 'CAPTCHA';
  if (/429|too_many_requests|rate_limit/i.test(code)) return 'Rate limited';
  if (/403|access_denied|waf/i.test(code)) return 'Access denied';
  if (/login|sign_in/i.test(code)) return 'Login required';
  if (/asin_mismatch|identity_terminal/i.test(code)) return 'Identity mismatch';
  if (/variant/i.test(code)) return 'Variant';
  if (/currency/i.test(code)) return 'Currency not verified';
  if (/postal|delivery_context/i.test(code)) return 'Region not verified';
  if (/country/i.test(code)) return 'Marketplace not verified';
  if (/missing_core/i.test(code)) return 'Missing product data';
  if (/job_budget_exhausted|browser_budget|http_body_budget/i.test(code)) return 'Product request limit';
  if (/budget_or_lease/i.test(code)) return 'Request budget or lease';
  if (/timeout|browser_navigation/i.test(code)) return 'Navigation timeout';
  if (/transport|fetch_error|network/i.test(code)) return 'Network error';
  if (/deadline|expired/i.test(code)) return 'Deadline reached';
  return 'See details';
}
function resultLabel(item) {
  const outcome = item.outcome || item.status;
  if (outcome === 'variant_redirect') return 'Variant';
  if (['completed','succeeded','product_done'].includes(outcome))
    return 'Collected';
  const reason = item.error_code || item.block_reason || item.last_error || item.evidence_error || item.evidence_block;
  return ['failed','blocked'].includes(outcome) && reason ? issueLabel(reason) : statusLabel(outcome);
}
function resultIssue(item) {
  if (item.outcome === 'variant_redirect') return '—';
  const reason = item.error_code || item.block_reason || (item.attribution === 'evidence' ? '' : item.last_error) || '—';
  return reason === '—' && item.context_quality === 'partial' ? 'Region not verified' : issueLabel(reason);
}
function runName(run) {
  const start = run.started_at || run.ended_at;
  const when = start && Number.isFinite(new Date(start).getTime())
    ? new Date(start).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})
    : '时间未记录';
  return `${when} · ${run.command === 'reviews' ? 'Review crawl' : 'Product crawl'}`;
}
function runOption(run) {
  return `${runName(run)} · ${knownNumber(run.recorded_actions)}/${knownNumber(run.requested_actions)} · ${statusLabel(run.terminal_status)}`;
}
function durationLabel(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const seconds = Math.max(0, Math.round(Number(value)));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}
function sourceLabel(value) { return ({http_html:'HTTP',selenium_dom:'Browser',selenium:'Browser'})[value] || 'Unknown'; }
function priceLabel(value) { return ({available:'Available',unavailable:'Unavailable',missing:'—',unknown:'—'})[value] || '—'; }
function priceValue(item) { return String(item.price || '').trim() || priceLabel(item.price_status); }
function resultClass(item) { return ['blocked','failed'].includes(item.outcome || item.status) ? 'result-error' : (item.context_quality === 'partial' ? 'result-warning' : 'result-ok'); }
function renderIssues(items) {
  const root = $('runIssues'); clear(root);
  const counts = {};
  for (const item of items || []) {
    if (['completed','variant_redirect'].includes(item.outcome)) continue;
    const label = resultLabel(item); counts[label] = (counts[label] || 0) + 1;
  }
  for (const [label,count] of Object.entries(counts)) root.append(text('span',`${label} ${count}`,'chip alert'));
}

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
  state.runRequest += 1;
  state.runSelectionCleared = false;
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
      taskCell(`${number(batch.recorded)} / ${number(batch.requested)}`),
      taskCell(number(batch.product_succeeded)),
      taskCell(number(batch.variant_redirect)),
      taskCell(number(batch.failed)),
      taskCell(number(batch.blocked)),
      taskCell(`${number(batch.pending)} / ${number(batch.running)}`),
      taskCell(`${bytes(batch.known_transfer_bytes)}${batch.unknown_transfer_records ? ` · ${number(batch.unknown_transfer_records)} unknown` : ''}`),
      taskCell(batch.active_duration_seconds === null ? '—' : `${number(batch.active_duration_seconds)}s`),
      taskCell(batch.wall_span_seconds === null ? '—' : `${number(batch.wall_span_seconds)}s（含等待）`),
      taskCell(statusLabel(batch.terminal_status)),
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
  for (const [key, value] of entries) root.append(text('span', `${id === 'sourceList' ? sourceLabel(key) : issueLabel(key)} · ${number(value)}`, `chip${alert && key !== 'none' ? ' alert' : ''}`));
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
    row.append(text('span', statusLabel(key)));
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
    node.title = run.run_id;
    node.append(text('strong', runName(run)), text('small', `已记录 ${number(run.actions)} 条`));
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
  const processed = business.recorded_actions ?? data.recorded_actions;
  const requested = business.requested_actions ?? data.requested_actions;
  const issues = Number(outcomes.failed || 0) + Number(outcomes.blocked || 0);
  $('runSummary').textContent = `${statusLabel(data.terminal_status)} · ${knownNumber(processed)} / ${knownNumber(requested)} processed · ${knownNumber(outcomes.completed)} collected · ${knownNumber(outcomes.variant_redirect)} variants · ${number(issues)} issues · ${durationLabel(data.worker_duration_seconds)} elapsed`;
  // Replace the selected option using this same response, not the older list query.
  for (const option of Array.from($('runSelector').options || []))
    if (option.value === data.run_id) option.text = runOption({...data,recorded_actions:processed,requested_actions:requested});
  const technical = $('runTechnical'); clear(technical);
  technical.append(jsonBlock({run_id:data.run_id,started_at:data.started_at,ended_at:data.ended_at,
    duration_source:data.duration_source,controller_duration_seconds:data.controller_duration_seconds,
    capacity_authorization:capacity,proxy_connectivity:connectivity,proxy_session_pool:proxyPool,
    proxy_totals:proxyTotals,traffic:data.traffic,recovery_batch:data.recovery_batch,attempt_actions:data.attempt_actions}));
  renderIssues(data.items);
  $('runWarning').textContent = data.context_quality_counts?.partial ? '部分结果未确认任务指定的配送地区，价格与库存需结合该标记使用。' : data.inferred_actions ? '部分历史记录按时间推断归属，详情可查看依据。' : '';
  $('runWarning').hidden = !$('runWarning').textContent;
  const body = $('runRows'); clear(body);
  for (const item of data.items || []) {
    const row = document.createElement('tr'); row.dataset.asin = item.asin;
    row.append(taskCell(item.asin, 'asin'));
    row.append(taskCell(item.title || '—', 'product-cell'), taskCell(priceValue(item)));
    const outcome = document.createElement('td'); outcome.append(text('span', resultLabel(item), `result-label ${resultClass(item)}`)); row.append(outcome);
    row.append(taskCell(resultIssue(item), 'product-cell'), taskCell(dateTime(item.retrieved_at || item.updated_at)));
    row.addEventListener('click', () => openDetail(item.asin)); body.append(row);
  }
}

async function loadRun(runId) {
  if (!runId) return;
  const requestId = ++state.runRequest;
  const data = await request(`/api/runs/${encodeURIComponent(runId)}`);
  if (requestId !== state.runRequest || state.selectedRun !== runId) return;
  renderRun(data);
}

async function selectRun(runId) {
  state.selectedRun = runId;
  state.runSelectionCleared = !runId;
  $('runSelector').value = runId;
  if (!runId) {
    state.runRequest += 1;
    $('runSummary').textContent = '选择任务查看本次采集结果。';
    for (const id of ['runRows','runIssues','runTechnical']) clear($(id));
    $('runWarning').textContent = ''; $('runWarning').hidden = true;
    return;
  }
  try { await loadRun(runId); }
  catch (error) { if (state.selectedRun === runId) { $('runWarning').textContent = `运行详情读取失败：${error.message}`; $('runWarning').hidden = false; } }
}

async function loadRuns() {
  const data = await request('/api/runs?limit=20');
  const selector = $('runSelector');
  const desired = state.runSelectionCleared ? '' : state.selectedRun || selector.value || data.items?.[0]?.run_id || '';
  clear(selector); selector.append(new Option('选择采集任务', ''));
  for (const run of data.items || []) selector.append(new Option(runOption(run), run.run_id));
  if (desired && (data.items || []).some((run) => run.run_id === desired)) {
    state.selectedRun = desired; selector.value = desired; await loadRun(desired);
  }
}

function renderOperations(data) {
  state.operations = data.items || [];
  $('operationCount').textContent = `最近 ${number(state.operations.length)} 条`;
  const body = $('operationRows'); clear(body);
  for (const operation of state.operations) {
    const row = document.createElement('tr');
    row.append(
      taskCell(dateTime(operation.started_at)),
      taskCell(({egress:'出口检查',canary:'容量检查',probe:'小批采集',run:'商品采集',reviews:'评论采集',capacity_reservation:'容量预约'})[operation.operation_type] || '操作'),
      taskCell(statusLabel(operation.status)),
      taskCell(operation.preflight_status),
      taskCell(operation.failure_stage),
      taskCell(issueLabel(operation.error_class)),
      taskCell(operation.egress_id),
      taskCell(operation.http_status),
      taskCell(canarySummary(operation), 'product-cell'),
      taskCell(operation.duration_seconds == null ? '—' : `${number(operation.duration_seconds)}s`),
      taskCell(`${dateTime(operation.started_at)} / ${dateTime(operation.finished_at)}`),
      taskCell(operation.collection_run_id ? '关联采集任务' : '—'),
    );
    row.title = `操作ID：${operation.operation_id}；采集ID：${operation.collection_run_id || '—'}`;
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
    const statusCell = document.createElement('td'); statusCell.append(text('span', resultLabel(item), `status-badge ${item.status}`)); row.append(statusCell);
    row.append(taskCell(({product:'商品',reviews:'评论',complete:'完成'})[item.task_stage] || '—'), taskCell(priceLabel(item.price_status)), taskCell(sourceLabel(item.source_type)), taskCell(item.http_status));
    row.append(taskCell(issueLabel(item.last_error || item.evidence_error || item.block_reason || item.evidence_block), 'product-cell'));
    row.append(taskCell(dateTime(item.updated_at)));
    row.addEventListener('click', () => openDetail(item.asin)); body.append(row);
  }
  if (!state.items.length) {
    const row = document.createElement('tr'); const cell = taskCell('没有符合条件的任务', 'muted'); cell.colSpan = 9; row.append(cell); body.append(row);
  }
  $('taskSummary').textContent = `显示 ${number(state.items.length)} / ${number(data.total)} 条`;
}

function detailSection(titleValue, collapsed = false) {
  const section = text(collapsed ? 'details' : 'section', '', 'detail-section');
  section.append(text(collapsed ? 'summary' : 'h3', titleValue)); return section;
}
function fieldGrid(values) {
  const grid = text('div', '', 'detail-grid');
  for (const [key, value] of Object.entries(values)) {
    const label = ({title:'商品名称',brand:'品牌',price:'价格',price_status:'价格状态',availability:'库存状态',
      rating:'评分',reviews:'评论数',status:'状态',stage:'阶段',attempts:'尝试次数',last_error:'具体问题',
      block_reason:'访问问题',updated_at:'更新时间'})[key] || key;
    const node = text('div', '', 'detail-field'); node.append(text('span', label), text('strong', value ?? '—')); grid.append(node);
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
    const task = detailSection('任务状态'); task.append(fieldGrid({ status: resultLabel(data.task), stage: ({product:'商品',reviews:'评论',complete:'完成'})[data.task.task_stage] || '—', attempts: `${data.task.attempts}/${data.task.max_attempts}`, last_error: issueLabel(data.task.last_error), block_reason: issueLabel(data.task.block_reason), updated_at: dateTime(data.task.updated_at) })); root.append(task);
    const product = detailSection('商品快照');
    const productContext = (data.evidence || []).find((value) => value.outcome === 'completed' && value.context_json?.context_quality)?.context_json;
    if (productContext?.context_quality === 'partial') product.append(text('p', `配送地区待确认：当前 ${productContext.observed_postal || '未记录'}，目标 ${productContext.expected_postal || '未记录'}。价格、库存和配送信息不可用于目标地区分析。`, 'table-summary'));
    else if (productContext?.observed_postal) product.append(text('p', `当次配送地区：${productContext.observed_postal}。价格与库存代表当次页面观察值。`, 'muted'));
    if (data.product) product.append(fieldGrid({ title: data.product.title, brand: data.product.brand, price: data.product.price, price_status: priceLabel(data.product.price_status), availability: data.product.availability, rating: data.product.rating, reviews: data.product.reported_review_count }), jsonBlock({ bullets: data.product.bullets, specs: data.product.specs, buy_box: data.product.buy_box }));
    else product.append(text('p', '尚无有效商品快照', 'muted')); root.append(product);
    const media = detailSection(`媒体 URL · ${(data.media || []).length}`); const mediaList = text('div', '', 'detail-list'); for (const value of data.media || []) mediaList.append(linkItem(value.display_url || value.asset_url || value.thumbnail_url, `${value.placement || 'media'} · ${value.entry_type || ''}`)); media.append(mediaList); root.append(media);
    const topReviews = detailSection(`商品页 Top Reviews · ${(data.top_reviews || []).length}`); for (const review of data.top_reviews || []) { const item = text('div', '', 'detail-item'); item.append(text('strong', review.title || review.rating || 'Review'), text('p', review.body || review.text || JSON.stringify(review))); topReviews.append(item); } if (!(data.top_reviews || []).length) topReviews.append(text('p', '无商品页评论摘要', 'muted')); root.append(topReviews);
    const evidence = detailSection(`采集记录与技术详情 · ${(data.evidence || []).length}`, true); for (const value of data.evidence || []) { const traffic = value.context_json?.traffic || {}; const bridge = value.context_json?.cookie_bridge || {}; const proxyPool = value.context_json?.proxy_session_pool || {}; const proxySessions = proxyPool.sessions || []; const proxyTotals = proxySessions.reduce((sum, item) => { for (const key of ['request_count', 'completed', 'variant_redirect', 'failed', 'blocked', 'bytes', 'latency_ms']) sum[key] = (sum[key] || 0) + Number(item[key] || 0); return sum; }, {}); evidence.append(fieldGrid({ outcome: value.outcome, source: value.source_type, proxy_session: proxyPool.current_session_id, proxy_mode: proxyPool.mode, proxy_requests: proxyTotals.request_count, proxy_completed: proxyTotals.completed, proxy_variant: proxyTotals.variant_redirect, proxy_failed: proxyTotals.failed, proxy_blocked: proxyTotals.blocked, proxy_bytes: proxyTotals.bytes, proxy_latency_ms: proxyTotals.latency_ms, proxy_quarantines: proxySessions.filter((item) => item.quarantine_reason).map((item) => `${item.session_id}:${item.quarantine_reason}`).join(', '), proxy_circuit: proxyPool.circuit_open_reason, proxy_unrequested: proxyPool.unrequested_count, context_quality: value.context_json?.context_quality, identity_relation: value.context_json?.identity ? `${value.context_json.identity.requested_asin} → ${value.context_json.identity.observed_asin} · parent ${value.context_json.identity.parent_asin}` : null, postal_confirmed: value.context_json?.postal_confirmed, expected_postal: value.context_json?.expected_postal, observed_postal: value.context_json?.observed_postal, location_sensitive_fields_unverified: (value.context_json?.location_sensitive_fields_unverified || []).join(', '), fallback_reason: value.context_json?.fallback_reason, fallback_reasons: (value.context_json?.fallback_reasons || []).join(', '), cookie_bridge: bridge.status, cookie_bridge_error: bridge.error_code, http: value.http_status, error: value.error_code, block: value.block_reason, retrieved: dateTime(value.retrieved_at), raw_html_path: value.raw_html_path, http_compressed_bytes: traffic.http_compressed_response_bytes, firefox_main_bytes: traffic.firefox_main_document_bytes ?? 'unknown', firefox_subresource_bytes: traffic.firefox_subresource_bytes ?? 'unknown' })); } root.append(evidence);
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
  if (data?.availability !== 'available') { root.hidden = false; root.textContent = '调度状态暂不可用（unknown），不能视为可执行。'; return; }
  const gates = data.egress || [];
  const paused = gates.some((gate) => gate.manually_paused || new Date(gate.paused_until).getTime() > Date.now());
  root.hidden = !paused && !gates.some((gate) => gate.half_open);
  const due = (data.jobs || []).filter((job) => !job.terminal).length;
  root.textContent = paused ? '请求暂停：正在冷却或等待人工恢复，任务保留。' : gates.some((gate) => gate.half_open) ? '正在少量试探恢复。' : '调度正常：单商品失败不会直接结束整个任务。';
  root.title = `当前批次待恢复任务（最多显示100条）：${due}；具体时间与额度见技术详情。`;
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
