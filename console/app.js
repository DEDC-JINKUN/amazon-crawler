const state = { overview: null, items: [], selectedRun: '', timer: null, loading: false };
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

function apiKey() { return sessionStorage.getItem('amazonConsoleApiKey') || ''; }
async function request(path) {
  const headers = apiKey() ? { 'X-Collection-API-Key': apiKey() } : {};
  const response = await fetch(path, { headers, cache: 'no-store' });
  if (response.status === 401) {
    const supplied = window.prompt('该控制台需要本地API Key。Key只保存在当前标签页。');
    if (supplied) {
      sessionStorage.setItem('amazonConsoleApiKey', supplied);
      return request(path);
    }
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderChips(id, values, alert = false) {
  const root = $(id); clear(root);
  const entries = Object.entries(values || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) root.append(text('span', '无', 'muted'));
  for (const [key, value] of entries) root.append(text('span', `${key} · ${number(value)}`, `chip${alert && key !== 'none' ? ' alert' : ''}`));
}

function renderOverview(data) {
  state.overview = data;
  $('tenantLabel').textContent = `Tenant · ${data.tenant_id}`;
  $('observedAt').textContent = `更新 ${dateTime(data.observed_at)}`;
  $('progressMetric').textContent = `${data.progress.percent}%`;
  $('progressSub').textContent = `${number(data.progress.touched)} / ${number(data.progress.total)} ASIN`;
  $('productsMetric').textContent = number(data.progress.successful_products);
  $('partialMetric').textContent = number(data.context_quality_counts?.partial);
  $('blockedMetric').textContent = number(data.status_counts.blocked);
  $('failedMetric').textContent = number(data.status_counts.failed);
  $('actionsMetric').textContent = number(data.four_scale_metrics.page_actions);
  $('rowsMetric').textContent = number(data.four_scale_metrics.database_rows);
  $('rawMetric').textContent = bytes(data.traffic.saved_raw_html_bytes);
  $('rawSub').textContent = `${number(data.traffic.raw_html_files)} files`;
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
  $('runSummary').textContent = `${data.run_id} · ${number(data.items.length)}项（evidence ${number(data.recorded_actions)}，历史推断 ${number(data.inferred_actions)}）· context full ${number(data.context_quality_counts?.full)} / partial ${number(data.context_quality_counts?.partial)} / invalid ${number(data.context_quality_counts?.invalid)} · ${dateTime(data.started_at)} → ${dateTime(data.ended_at)} · HTTP ${trafficBytes(data.traffic?.http_compressed_response)} · Firefox主文档 ${trafficBytes(data.traffic?.firefox_main_document)} · Firefox子资源 ${trafficBytes(data.traffic?.firefox_subresources)}`;
  $('runWarning').textContent = data.context_quality_counts?.partial ? '⚠ 本run含ZIP未确认的partial商品；price、availability、buy_box/配送等位置敏感字段不可视为90001结果。' : data.inferred_actions ? '⚠ 历史网络失败没有run evidence；黄色归属为按本run时间窗口推断。新运行已永久修复。' : '全部结果均有不可变run evidence。';
  const body = $('runRows'); clear(body);
  for (const item of data.items || []) {
    const row = document.createElement('tr'); row.dataset.asin = item.asin;
    row.append(taskCell(item.asin, 'asin'));
    const outcome = document.createElement('td'); const outcomeLabel = item.context_quality === 'partial' && item.outcome === 'completed' ? 'completed · partial' : item.outcome; outcome.append(text('span', outcomeLabel, `status-badge ${item.context_quality === 'partial' ? 'partial' : item.outcome}`)); row.append(outcome);
    row.append(taskCell(item.title || '—', 'product-cell'), taskCell(item.source_type), taskCell(item.http_status));
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
  for (const run of data.items || []) selector.append(new Option(`${run.run_id} · ${run.evidence_actions} evidence`, run.run_id));
  if (desired && (data.items || []).some((run) => run.run_id === desired)) {
    selector.value = desired; await loadRun(desired);
  }
}

function taskCell(value, className = '') { const td = text('td', value ?? '—', className); return td; }
function renderItems(data) {
  state.items = data.items || [];
  const body = $('taskRows'); clear(body);
  for (const item of state.items) {
    const row = document.createElement('tr'); row.dataset.asin = item.asin;
    row.append(taskCell(item.asin, 'asin'), taskCell(item.title || '—', 'product-cell'));
    const statusCell = document.createElement('td'); statusCell.append(text('span', item.status, `status-badge ${item.status}`)); row.append(statusCell);
    row.append(taskCell(item.task_stage), taskCell(item.source_type), taskCell(item.http_status));
    row.append(taskCell(item.last_error || item.evidence_error || item.block_reason || item.evidence_block || '—', 'product-cell'));
    row.append(taskCell(dateTime(item.updated_at)));
    row.addEventListener('click', () => openDetail(item.asin)); body.append(row);
  }
  if (!state.items.length) {
    const row = document.createElement('tr'); const cell = taskCell('没有符合条件的任务', 'muted'); cell.colSpan = 8; row.append(cell); body.append(row);
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
    if (data.product) product.append(fieldGrid({ title: data.product.title, brand: data.product.brand, price: data.product.price, availability: data.product.availability, rating: data.product.rating, reviews: data.product.reported_review_count }), jsonBlock({ bullets: data.product.bullets, specs: data.product.specs, buy_box: data.product.buy_box }));
    else product.append(text('p', '尚无有效商品快照', 'muted')); root.append(product);
    const media = detailSection(`媒体 URL · ${(data.media || []).length}`); const mediaList = text('div', '', 'detail-list'); for (const value of data.media || []) mediaList.append(linkItem(value.display_url || value.asset_url || value.thumbnail_url, `${value.placement || 'media'} · ${value.entry_type || ''}`)); media.append(mediaList); root.append(media);
    const topReviews = detailSection(`商品页 Top Reviews · ${(data.top_reviews || []).length}`); for (const review of data.top_reviews || []) { const item = text('div', '', 'detail-item'); item.append(text('strong', review.title || review.rating || 'Review'), text('p', review.body || review.text || JSON.stringify(review))); topReviews.append(item); } if (!(data.top_reviews || []).length) topReviews.append(text('p', '无商品页评论摘要', 'muted')); root.append(topReviews);
    const evidence = detailSection(`Evidence · ${(data.evidence || []).length}`); for (const value of data.evidence || []) { const traffic = value.context_json?.traffic || {}; const bridge = value.context_json?.cookie_bridge || {}; evidence.append(fieldGrid({ source: value.source_type, context_quality: value.context_json?.context_quality, postal_confirmed: value.context_json?.postal_confirmed, expected_postal: value.context_json?.expected_postal, observed_postal: value.context_json?.observed_postal, location_sensitive_fields_unverified: (value.context_json?.location_sensitive_fields_unverified || []).join(', '), fallback_reason: value.context_json?.fallback_reason, fallback_reasons: (value.context_json?.fallback_reasons || []).join(', '), cookie_bridge: bridge.status, cookie_bridge_error: bridge.error_code, http: value.http_status, error: value.error_code, block: value.block_reason, retrieved: dateTime(value.retrieved_at), raw_html_path: value.raw_html_path, http_compressed_bytes: traffic.http_compressed_response_bytes, firefox_main_bytes: traffic.firefox_main_document_bytes ?? 'unknown', firefox_subresource_bytes: traffic.firefox_subresource_bytes ?? 'unknown' })); } root.append(evidence);
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
async function refresh() {
  if (state.loading) return; state.loading = true;
  try {
    const [overview] = await Promise.all([request('/api/overview'), loadItems(), loadRuns()]);
    renderOverview(overview); $('errorBanner').hidden = true; $('liveBadge').classList.remove('offline');
  } catch (error) {
    $('errorBanner').textContent = `控制台刷新失败：${error.message}。上一轮数据已保留。`; $('errorBanner').hidden = false; $('liveBadge').classList.add('offline');
  } finally { state.loading = false; }
}

$('refreshButton').addEventListener('click', refresh);
$('runSelector').addEventListener('change', (event) => selectRun(event.target.value));
$('filterForm').addEventListener('submit', (event) => { event.preventDefault(); loadItems().catch((error) => { $('errorBanner').textContent = error.message; $('errorBanner').hidden = false; }); });
$('apiKeyButton').addEventListener('click', () => { const value = window.prompt('输入新的本地API Key；留空将清除当前Key。', apiKey()); if (value === null) return; if (value) sessionStorage.setItem('amazonConsoleApiKey', value); else sessionStorage.removeItem('amazonConsoleApiKey'); refresh(); });
$('closeDrawer').addEventListener('click', closeDetail); $('drawerBackdrop').addEventListener('click', closeDetail);
refresh(); state.timer = window.setInterval(refresh, 5000);
