"use strict";
const csrf=document.body.dataset.csrf;
const state={preview:null,batch:null,timer:null};
const $=id=>document.getElementById(id);
const labels={requested:"请求",claimed:"处理中",completed:"完成",variant:"变体",failed:"失败",blocked:"阻断",pending:"待处理"};
const statusLabels={starting:"启动中",running:"运行中",stopping:"正在停止",cancel_pending:"取消待确认",cleanup_pending:"清理待确认",completed:"已完成",failed:"失败",stopped:"已停止"};
const subjectLabels={own:"自有",competitor:"竞品",candidate:"候选"};
const errors={unauthorized:"会话已失效，请刷新页面",csrf_rejected:"安全令牌失效，请刷新页面",unsupported_file_type:"仅支持 .xlsx 文件",invalid_rows_block_start:"存在无效非空行，不能启动",invalid_subject_type:"清单类型无效",another_batch_is_active:"已有普通批次运行中",deployment_batch_lock_busy:"已有控制器持有批次锁",selection_required:"请选择工作表和 ASIN 列",runner_failed:"控制器运行失败，请下载回执查看",runner_start_failed:"控制器未能启动，请稍后重试",status_projection_unavailable:"采集状态未能完成汇总",canary_failed:"代理容量预检失败，未开始采集",canary_setup_failed:"代理容量预检配置无效",canary_timeout:"代理容量预检超时，未开始采集",canary_cancelled:"代理容量预检已取消",canary_unique_capacity_insufficient:"代理可用容量不足，未开始采集",canary_replacement_capacity_insufficient:"代理替补容量不足，未开始采集",canary_credentials_missing:"代理凭据不可用，未开始采集",canary_database_credentials_missing:"数据库凭据不可用，未开始采集",capacity_reserved_elsewhere:"其他批次仍占用代理容量，请稍后重试",capacity_evidence_missing:"缺少可用的代理容量证据，未开始采集",capacity_evidence_stale:"代理容量证据已过期，未开始采集",capacity_config_mismatch:"代理容量配置已变化，未开始采集",capacity_release_failed:"停止批次时未能释放代理容量，请稍后重试"};

async function api(path,options={}){
  const headers=new Headers(options.headers||{});
  if(options.method==="POST")headers.set("X-CSRF-Token",csrf);
  const response=await fetch(path,{...options,headers,credentials:"same-origin"});
  if(!response.ok){let code="request_failed";try{code=(await response.json()).error.code}catch(_){ }throw new Error(errors[code]||code)}
  return response.json();
}
function notice(message=""){$("notice").textContent=message}
function metric(value,label){return `<div class="metric"><strong>${Number(value||0)}</strong><span>${label}</span></div>`}
function escapeText(value){const node=document.createElement("span");node.textContent=String(value??"");return node.innerHTML}

function renderPreview(preview){
  state.preview=preview;
  const box=$("preview");box.classList.remove("hidden");
  const previewRows=[
    ...preview.valid_rows.map(x=>({row:x.row,asin:x.asin,result:"有效"})),
    ...preview.duplicate_rows.map(x=>({row:x.row,asin:x.asin,result:`重复（首次第 ${x.first_row} 行）`})),
    ...preview.invalid_rows.map(x=>({row:x.row,asin:"—",result:"无效非空值"}))
  ].sort((a,b)=>a.row-b.row);
  const visibleRows=previewRows.slice(0,200);
  box.innerHTML=`<div class="summary-grid">${metric(preview.unique_count,"有效唯一")}${metric(preview.duplicate_rows.length,"重复")}${metric(preview.invalid_rows.length,"无效")}</div>`+
    (preview.invalid_rows.length?`<p class="bad">无效非空行：${preview.invalid_rows.map(x=>x.row).join("、")}</p>`:"")+
    (preview.duplicate_rows.length?`<p class="warn">已自动去重：${preview.duplicate_rows.map(x=>x.row).join("、")} 行</p>`:"")+
    `<p>工作表：${escapeText(preview.selected_sheet||"待选择")}；列：${escapeText(preview.selected_column||"待选择")}</p>`+
    (visibleRows.length?`<table><thead><tr><th>行</th><th>ASIN</th><th>校验结果</th></tr></thead><tbody>${visibleRows.map(x=>`<tr><td>${x.row}</td><td>${escapeText(x.asin)}</td><td>${escapeText(x.result)}</td></tr>`).join("")}</tbody></table>${previewRows.length>200?`<p class="muted">仅显示前 200 行，共 ${previewRows.length} 行。</p>`:""}`:"");
  $("start").disabled=!preview.can_start;
  const selector=$("selection");
  if(preview.selection_required){
    selector.classList.remove("hidden");
    $("sheet").innerHTML=preview.sheets.map(x=>`<option value="${escapeText(x.name)}">${escapeText(x.name)}</option>`).join("");
    updateColumns();
  }else selector.classList.add("hidden");
}
function updateColumns(){
  const sheet=(state.preview?.sheets||[]).find(x=>x.name===$("sheet").value);
  $("column").innerHTML=(sheet?.columns||[]).map(x=>`<option value="${escapeText(x.letter)}">${escapeText(x.letter)} · ${escapeText(x.header||"无表头")}</option>`).join("");
}

function renderBatch(batch){
  state.batch=batch;
  const done=Number(batch.completed||0)+Number(batch.variant||0)+Number(batch.failed||0)+Number(batch.blocked||0);
  const total=Math.max(1,Number(batch.requested||0));
  $("progress").value=Math.round(done/total*100);
  $("batch-summary").textContent=`${batch.batch_id} · ${subjectLabels[batch.subject_type]||batch.subject_type||"自有"} · ${statusLabels[batch.status]||batch.status} · ${Number(batch.elapsed_seconds||0).toFixed(1)} 秒`;
  $("metrics").innerHTML=Object.entries(labels).map(([key,label])=>metric(batch[key],label)).join("");
  $("stop").disabled=["completed","failed","stopped"].includes(batch.status);
  $("items").innerHTML=(batch.items||[]).map(item=>`<tr><td>${escapeText(item.asin)}</td><td>${escapeText(labels[item.status]||item.status)}</td><td>${escapeText(item.error||"")}</td></tr>`).join("")||'<tr><td colspan="3" class="empty">暂无数据</td></tr>';
  const names={manifest:"标准 manifest",results_csv:"结果 CSV",results_json:"结果 JSON",receipt:"回执"};
  $("downloads").innerHTML=(batch.downloads||[]).map(name=>`<a href="/api/download_artifact?batch_id=${encodeURIComponent(batch.batch_id)}&artifact=${encodeURIComponent(name)}">下载${names[name]}</a>`).join("");
  const dashboard=$("batch-dashboard");
  if(batch.dashboard_url){dashboard.href=batch.dashboard_url;dashboard.classList.remove("hidden")}else{dashboard.removeAttribute("href");dashboard.classList.add("hidden")}
  if(batch.latest_error)notice(errors[batch.latest_error]||`失败原因：${batch.latest_error}`);else notice();
  if(["completed","failed","stopped"].includes(batch.status)){clearInterval(state.timer);state.timer=null}
}
async function poll(batchId){const suffix=batchId?`?batch_id=${encodeURIComponent(batchId)}`:"";try{renderBatch(await api(`/api/get_batch${suffix}`))}catch(error){if(batchId)notice(error.message)}}

$("workbook").addEventListener("change",event=>{$("file-name").textContent=event.target.files[0]?.name||"尚未选择"});
$("sheet").addEventListener("change",updateColumns);
$("upload-form").addEventListener("submit",async event=>{event.preventDefault();notice();const file=$("workbook").files[0];if(!file)return;const body=new FormData();body.append("file",file);try{renderPreview(await api("/api/upload_preview",{method:"POST",body}))}catch(error){notice(error.message)}});
$("apply-selection").addEventListener("click",async()=>{try{renderPreview(await api("/api/upload_preview",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({import_id:state.preview.import_id,selected_sheet:$("sheet").value,selected_column:$("column").value})}))}catch(error){notice(error.message)}});
$("start").addEventListener("click",async()=>{try{const batch=await api("/api/start_batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({import_id:state.preview.import_id,worker_count:Number($("worker-count").value),subject_type:$("subject-type").value})});renderBatch(batch);if(!state.timer)state.timer=setInterval(()=>poll(batch.batch_id),1000)}catch(error){notice(error.message)}});
$("stop").addEventListener("click",async()=>{if(!state.batch)return;try{renderBatch(await api("/api/stop_batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({batch_id:state.batch.batch_id})}))}catch(error){notice(error.message)}});
poll(null).then(()=>{if(state.batch&&!state.timer&&!(["completed","failed","stopped"].includes(state.batch.status)))state.timer=setInterval(()=>poll(state.batch.batch_id),1000)}).catch(()=>{});
