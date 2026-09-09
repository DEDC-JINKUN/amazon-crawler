const initialTenant = new URL(window.location.href).searchParams.get('tenant') || '';
const state = { overview: null, batches: [], operations: [], items: [], tenant: initialTenant, selectedRun: '', timer: null, loading: false };
// 批次模型（新架构）状态：批次列表 + 当前选中批次
const batchState = { batches: [], selectedBatch: '' };
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
  const target = new URL(path, window.location.origin);
  if (state.tenant && target.pathname !== '/api/tenants') target.searchParams.set('tenant', state.tenant);
  const response = await fetch(target, { headers, cache: 'no-store' });
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
  $('runSummary').textContent = `${data.run_id} · requested / recorded ${number(data.requested_actions)} / ${number(data.recorded_actions)} · 商品成功 ${number(outcomes.completed)} · variant redirect ${number(outcomes.variant_redirect)} · failed ${number(outcomes.failed)} · blocked ${number(outcomes.blocked)} · 终态 ${data.terminal_status || '—'} · 活跃处理 ${data.worker_duration_seconds == null ? '—' : `${number(data.worker_duration_seconds)}s`}（${data.duration_source || 'unknown'}）· controller ${data.controller_duration_seconds == null ? '—' : `${number(data.controller_duration_seconds)}s`} · ${dateTime(data.started_at)} → ${dateTime(data.ended_at)} · HTTP ${trafficBytes(data.traffic?.http_compressed_response)} · Firefox主文档 ${trafficBytes(data.traffic?.firefox_main_document)} · Firefox子资源 ${trafficBytes(data.traffic?.firefox_subresources)}`;
  $('runWarning').textContent = data.context_quality_counts?.partial ? '⚠ 本run含ZIP未确认的partial商品；price、availability、buy_box/配送等位置敏感字段不可视为90001结果。' : data.inferred_actions ? '⚠ 历史网络失败没有run evidence；黄色归属为按本run时间窗口推断。新运行已永久修复。' : '全部结果均有不可变run evidence。';
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
      taskCell(operation.duration_seconds == null ? '—' : `${number(operation.duration_seconds)}s`),
      taskCell(`${dateTime(operation.started_at)} / ${dateTime(operation.finished_at)}`),
      taskCell(operation.collection_run_id),
    );
    body.append(row);
  }
  if (!state.operations.length) {
    const row = document.createElement('tr'); const cell = taskCell('暂无操作记录', 'muted'); cell.colSpan = 11; row.append(cell); body.append(row);
  }
}

// ===== 批次模型（新架构）：采集批次列表 + 成员明细 =====

function renderCoordinatorBadge(coordinator) {
  const badge = $('coordinatorBadge');
  // 同步 worker 数输入框（后端持久化的设置值）
  if (coordinator && coordinator.workers) $('workerCount').value = coordinator.workers;
  if (coordinator) {
    const workersNote = coordinator.workers ? ` · ${coordinator.workers} worker` : '';
    badge.innerHTML = `<span class="coord-dot online"></span> 运行中${workersNote} · <button class="link-button" id="coordStopBtn" type="button">停止</button>`;
    badge.className = 'coordinator-badge online';
    $('coordStopBtn')?.addEventListener('click', async (e) => { e.stopPropagation(); await stopCoordinator(); });
  } else {
    badge.innerHTML = `<span class="coord-dot offline"></span> 离线 · <button class="link-button" id="coordStartBtn" type="button">启动采集</button>`;
    badge.className = 'coordinator-badge offline';
    $('coordStartBtn')?.addEventListener('click', async (e) => { e.stopPropagation(); await startCoordinator(); });
  }
}

async function applyWorkers() {
  const workers = parseInt($('workerCount').value, 10);
  if (!Number.isInteger(workers) || workers < 1 || workers > 8) { alert('Worker 数必须是 1-8 的整数'); return; }
  if (!confirm(`以 ${workers} 个 worker 重启协调器？每个 worker 用独立代理 IP；被拦率上升时建议回调。`)) return;
  const btn = $('applyWorkersBtn'); btn.disabled = true; btn.textContent = '重启中…';
  try {
    const tenant = new URLSearchParams(location.search).get('tenant') || '';
    const resp = await fetch(`/api/coordinator/restart?tenant=${encodeURIComponent(tenant)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workers }),
    });
    if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail || err.error || '重启失败'); }
    await loadCollectionBatches();
    alert(`已切换为 ${workers} worker，协调器重启完成，爬取将在 ~1 分钟内恢复。`);
  } catch (error) { alert('调整 worker 数失败：' + error.message); }
  finally { btn.disabled = false; btn.textContent = '应用'; }
}
$('applyWorkersBtn')?.addEventListener('click', applyWorkers);

async function startCoordinator() {
  try {
    const tenant = new URLSearchParams(location.search).get('tenant') || '';
    await fetch(`/api/coordinator/start?tenant=${encodeURIComponent(tenant)}`, { method: 'POST' });
    await loadCollectionBatches();
  } catch (error) { alert('启动协调器失败：' + error.message); }
}

async function stopCoordinator() {
  if (!confirm('确认停止协调器？正在运行的批次会被标记 stopping。')) return;
  try {
    await fetch('/api/coordinator', { method: 'DELETE' });
    await loadCollectionBatches();
  } catch (error) { alert('停止协调器失败：' + error.message); }
}

function renderCollectionBatches(data) {
  batchState.batches = data.items || [];
  renderCoordinatorBadge(data.coordinator);
  const body = $('collectionBatchRows'); clear(body);
  for (const batch of batchState.batches) {
    const row = document.createElement('tr');
    if (batch.batch_id === batchState.selectedBatch) row.classList.add('selected');
    row.append(taskCell(batch.batch_id.slice(0, 8), 'asin'));
    const statusCell = document.createElement('td');
    statusCell.append(text('span', batch.batch_status, `status-badge ${batch.batch_status}`));
    // 评论开关：批次建批时的选择（关=只采商品数据，不进评论翻页）
    if (batch.collect_reviews === false) statusCell.append(text('span', ' 不采评论', 'muted'));
    if (batch.stop_requested && !['completed', 'stopped'].includes(batch.batch_status)) statusCell.append(text('span', ' 停止请求中', 'muted'));
    row.append(statusCell);
    // 进度：已完结（成功+变体+失败+被拦+取消）/ 总数；文本显示成功数
    const done = (batch.succeeded || 0) + (batch.variant || 0) + (batch.failed_total || 0) + (batch.blocked || 0) + (batch.cancelled || 0);
    const percent = batch.requested_count ? Math.round(done / batch.requested_count * 100) : 0;
    const progressCell = document.createElement('td');
    const track = text('div', '', 'mini-track');
    const fill = text('div', '', 'mini-fill'); fill.style.width = `${Math.min(100, Math.max(percent, done ? 2 : 0))}%`;
    track.append(fill);
    progressCell.append(track, text('div', `${number(batch.succeeded)} / ${number(batch.requested_count)} 成功`, 'muted'));
    row.append(progressCell);
    row.append(
      taskCell(number(batch.blocked)),
      taskCell(number(batch.variant)),
      taskCell(number(batch.failed_fetch)),
      taskCell(number(batch.failed_system)),
      taskCell(number(batch.cancelled)),
      taskCell(number(batch.pending)),
      // 证据数应与成功数一致（交叉校验），不一致标红提示
      taskCell(batch.evidence_count === batch.succeeded ? number(batch.evidence_count) : `${number(batch.evidence_count)}（与成功数不符）`, batch.evidence_count === batch.succeeded ? '' : 'muted'),
      taskCell(bytes(batch.evidence_bytes)),
      taskCell(`${dateTime(batch.created_at)} / ${dateTime(batch.started_at)} / ${dateTime(batch.finalized_at)}`),
    );
    // 操作列：下载结果按钮（所有批次可下载当前进度）；运行中批次另显示停止按钮
    const opCell = document.createElement('td');
    const dlBtn = text('button', '下载', 'link-button');
    dlBtn.title = '下载本批次结果 CSV（含成功、失败、被拦明细）';
    dlBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      // 触发浏览器下载：带当前租户参数，与其它 API 口径一致
      const tenant = new URLSearchParams(location.search).get('tenant') || '';
      window.location.href = `/api/batches/${encodeURIComponent(batch.batch_id)}/export?tenant=${encodeURIComponent(tenant)}`;
    });
    opCell.append(dlBtn);
    const terminal = ['completed', 'failed', 'blocked', 'stopped'].includes(batch.batch_status);
    if (!terminal) {
      const stopBtn = text('button', batch.stop_requested ? '停止中…' : '停止', batch.stop_requested ? 'ghost-button muted' : 'link-button');
      stopBtn.disabled = !!batch.stop_requested;
      stopBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (confirm(`确认停止批次 ${batch.batch_id.slice(0,8)}？`)) {
          await request(`/api/batches/${encodeURIComponent(batch.batch_id)}/stop`, { method: 'POST' });
          await loadCollectionBatches();
        }
      });
      opCell.append(stopBtn);
    }
    row.append(opCell);
    row.addEventListener('click', () => selectBatch(batch.batch_id));
    body.append(row);
  }
  if (!batchState.batches.length) {
    const row = document.createElement('tr'); const cell = taskCell('暂无批次：点"上传清单"创建批次（CSV / Excel）', 'muted'); cell.colSpan = 13; row.append(cell); body.append(row);
  }
}

async function loadCollectionBatches() {
  renderCollectionBatches(await request('/api/batches?limit=20'));
}

function renderBatchItems(data) {
  const items = data.items || [];
  const body = $('batchItemRows'); clear(body);
  for (const item of items) {
    const row = document.createElement('tr'); row.dataset.asin = item.asin;
    row.append(taskCell(item.asin, 'asin'), taskCell(item.title || '—', 'product-cell'));
    const statusCell = document.createElement('td');
    statusCell.append(text('span', item.status, `status-badge ${item.status}`));
    // 变体跳转：状态旁标注实际跳转到的 ASIN（页面是变体商品，不是清单里的原 ASIN）
    if (item.status === 'variant' && item.variant_asin) statusCell.append(text('span', ` → ${item.variant_asin}`, 'muted'));
    row.append(statusCell);
    row.append(taskCell(item.task_stage));
    row.append(taskCell(`${number(item.attempts)} / ${number(item.max_attempts)}`));
    row.append(taskCell(item.error_class || (item.block_reason ? 'blocked' : '—')));
    row.append(taskCell(item.block_reason || item.last_error || '—', 'product-cell'));
    // 缓存复用提示：库内已有快照时展示采集时间——
    // 未采集成员（pending/failed）带旧快照 = 可复用（标"库内已有"）；
    // 已成功成员显示的即本批次采集时间（正常展示，不标注）
    const snapshotCell = document.createElement('td');
    if (item.snapshot_at) {
      snapshotCell.append(text('div', dateTime(item.snapshot_at)));
      if (['pending', 'failed'].includes(item.status)) snapshotCell.append(text('div', '库内已有 · 可复用', 'muted'));
    } else snapshotCell.append(text('span', '—', 'muted'));
    row.append(snapshotCell);
    row.append(taskCell(item.reported_review_count == null ? '—' : `${number(item.fetched_review_count)} / ${number(item.reported_review_count)}（${number(item.review_pages_fetched)}页）`));
    row.append(taskCell(dateTime(item.updated_at)));
    row.addEventListener('click', () => openDetail(item.asin)); body.append(row);
  }
  if (!items.length) {
    const row = document.createElement('tr'); const cell = taskCell('没有符合条件的成员', 'muted'); cell.colSpan = 10; row.append(cell); body.append(row);
  }
  $('batchItemsSummary').textContent = `批次 ${batchState.selectedBatch.slice(0, 8)} · 显示 ${number(items.length)} / ${number(data.total)} 条`;
}

async function loadBatchItems() {
  if (!batchState.selectedBatch) return;
  const params = new URLSearchParams();
  if ($('batchItemStatusFilter').value) params.set('status', $('batchItemStatusFilter').value);
  if ($('batchItemQueryInput').value.trim()) params.set('q', $('batchItemQueryInput').value.trim());
  // 5000：覆盖 5800 量级清单一次看全（后端上限 6000）
  params.set('limit', '5000');
  renderBatchItems(await request(`/api/batches/${encodeURIComponent(batchState.selectedBatch)}/items?${params.toString()}`));
}

function selectBatch(batchId) {
  batchState.selectedBatch = batchId;
  $('batchItemsPanel').hidden = false;
  loadBatchItems().catch((error) => { $('batchItemsSummary').textContent = `成员读取失败：${error.message}`; });
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
    // BSR 类目销售排名：主排名+主类目进字段区，全部条目进折叠区
    const bsrGrid = data.product?.bsr_rank ? fieldGrid({ bsr_rank: `#${data.product.bsr_rank}`, bsr_category: data.product.bsr_category, bsr_entries: `${(data.product.bsr_entries || []).length} 个类目排名` }) : null;
    if (data.product) { const grid = fieldGrid({ title: data.product.title, brand: data.product.brand, price: data.product.price, price_status: data.product.price_status, availability: data.product.availability, rating: data.product.rating, reviews: data.product.reported_review_count }); if (bsrGrid) grid.append(...bsrGrid.children); product.append(grid, jsonBlock({ bsr_entries: data.product.bsr_entries, bullets: data.product.bullets, specs: data.product.specs, buy_box: data.product.buy_box })); }
    else product.append(text('p', '尚无有效商品快照', 'muted')); root.append(product);
    const media = detailSection(`媒体 URL · ${(data.media || []).length}`); const mediaList = text('div', '', 'detail-list'); for (const value of data.media || []) mediaList.append(linkItem(value.display_url || value.asset_url || value.thumbnail_url, `${value.placement || 'media'} · ${value.entry_type || ''}`)); media.append(mediaList); root.append(media);
    const topReviews = detailSection(`商品页 Top Reviews · ${(data.top_reviews || []).length}`); for (const review of data.top_reviews || []) { const item = text('div', '', 'detail-item'); item.append(text('strong', review.title || review.rating || 'Review'), text('p', review.body || review.text || JSON.stringify(review))); topReviews.append(item); } if (!(data.top_reviews || []).length) topReviews.append(text('p', '无商品页评论摘要', 'muted')); root.append(topReviews);
    const evidence = detailSection(`Evidence · ${(data.evidence || []).length}`); for (const value of data.evidence || []) { const traffic = value.context_json?.traffic || {}; const bridge = value.context_json?.cookie_bridge || {}; evidence.append(fieldGrid({ outcome: value.outcome, source: value.source_type, context_quality: value.context_json?.context_quality, identity_relation: value.context_json?.identity ? `${value.context_json.identity.requested_asin} → ${value.context_json.identity.observed_asin} · parent ${value.context_json.identity.parent_asin}` : null, postal_confirmed: value.context_json?.postal_confirmed, expected_postal: value.context_json?.expected_postal, observed_postal: value.context_json?.observed_postal, location_sensitive_fields_unverified: (value.context_json?.location_sensitive_fields_unverified || []).join(', '), fallback_reason: value.context_json?.fallback_reason, fallback_reasons: (value.context_json?.fallback_reasons || []).join(', '), cookie_bridge: bridge.status, cookie_bridge_error: bridge.error_code, http: value.http_status, error: value.error_code, block: value.block_reason, retrieved: dateTime(value.retrieved_at), raw_html_path: value.raw_html_path, http_compressed_bytes: traffic.http_compressed_response_bytes, firefox_main_bytes: traffic.firefox_main_document_bytes ?? 'unknown', firefox_subresource_bytes: traffic.firefox_subresource_bytes ?? 'unknown' })); } root.append(evidence);
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
    await loadBatches();
    if (!state.tenant) throw new Error('PostgreSQL中没有可见tenant');
    const [overview] = await Promise.all([request('/api/overview'), loadItems(), loadRuns(), loadOperations()]);
    renderOverview(overview); $('errorBanner').hidden = true; $('liveBadge').classList.remove('offline');
  } catch (error) {
    $('errorBanner').textContent = `控制台刷新失败：${error.message}。上一轮数据已保留。`; $('errorBanner').hidden = false; $('liveBadge').classList.add('offline');
  } finally { state.loading = false; }
  // 批次模型独立刷新：批次表不存在（未迁移）不影响旧面板
  loadCollectionBatches().catch(() => renderCoordinatorBadge(null));
  if (batchState.selectedBatch) {
    loadBatchItems().catch(() => { $('batchItemsSummary').textContent = '成员读取失败'; });
  }
}

// 上传清单 → 创建批次
$('manifestUpload').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const tenant = new URLSearchParams(location.search).get('tenant') || '';
  const form = new FormData(); form.append('manifest', file);
  // 评论采集开关：显式发送 on/off（未发送字段的后端默认开，老客户端兼容）
  form.append('collect_reviews', $('collectReviews').checked ? 'on' : 'off');
  try {
    const result = await fetch(`/api/batches?tenant=${encodeURIComponent(tenant)}`, { method: 'POST', body: form });
    if (!result.ok) { const err = await result.json(); throw new Error(err.detail || err.error || '上传失败'); }
    const data = await result.json();
    // 上传成功：立即选中并展开本批次全部待采集商品（不等第一条采集完成）
    await loadCollectionBatches();
    selectBatch(data.batch_id);
    const dup = data.skipped_duplicates ? `（去重 ${data.skipped_duplicates} 条）` : '';
    const reviewNote = $('collectReviews').checked ? '' : '（不采评论）';
    alert(`批次已创建：${data.batch_id?.slice(0,8)} · ${data.requested_count} 个 ASIN${dup}${reviewNote}\n下方已显示全部待采集商品`);
  } catch (error) { alert('上传清单失败：' + error.message); }
  e.target.value = '';
});

$('refreshButton').addEventListener('click', refresh);
$('tenantSelector').addEventListener('change', (event) => selectTenant(event.target.value));
$('runSelector').addEventListener('change', (event) => selectRun(event.target.value));
$('batchItemFilterForm').addEventListener('submit', (event) => {
  event.preventDefault();
  if (batchState.selectedBatch) loadBatchItems().catch((error) => { $('batchItemsSummary').textContent = `成员读取失败：${error.message}`; });
});
$('filterForm').addEventListener('submit', (event) => { event.preventDefault(); loadItems().catch((error) => { $('errorBanner').textContent = error.message; $('errorBanner').hidden = false; }); });
$('apiKeyButton').addEventListener('click', () => { const value = window.prompt('输入新的本地API Key；留空将清除当前Key。', apiKey()); if (value === null) return; if (value) sessionStorage.setItem('amazonConsoleApiKey', value); else sessionStorage.removeItem('amazonConsoleApiKey'); refresh(); });
$('closeDrawer').addEventListener('click', closeDetail); $('drawerBackdrop').addEventListener('click', closeDetail);
refresh(); state.timer = window.setInterval(refresh, 5000);
