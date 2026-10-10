/* The UI renders durable experiments; model output and source data are always text. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const json = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const id = () => globalThis.crypto?.randomUUID?.() || `review-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const artifacts = {prompt:'prompt.txt', input:'scenario-input.json', request:'request.json', response:'response.json', checks:'checks.json', source:'source.json'};
  const artifactLabels = {prompt:'提示词',input:'测试输入',request:'完整请求',response:'输出结果',checks:'判断标准',source:'原始数据'};
  const STORE = 'ablation.workbench.drafts.v1';
  const state = {experiment:null, experiments:[], config:{}, jobs:[], update:null, base:'baseline', candidate:'baseline', scenario:'original', artifact:'prompt', layout:'split', tab:'diff', diff:null, diffKey:'', groups:[], replyDrafts:{}, replyOpen:null, editor:null, modal:null, groupId:null, busy:false, requestSequence:0, diffSequence:0, lastJobs:'', deferred:false, whatsNew:false, expanded:false, focusBeforeModal:null, toastTimer:null};
  let pollRunning = false;

  function readStorage(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } }
  function persist() {
    try { localStorage.setItem(STORE, JSON.stringify({groups:state.groups, replyDrafts:state.replyDrafts, editor:state.editor, layout:state.layout})); }
    catch { $('#draft-storage-status').textContent = '浏览器存储不可用；请在关闭前提交或复制草稿'; }
    updateDraftCount();
  }
  const saved = readStorage(STORE);
  if (saved && Array.isArray(saved.groups)) state.groups = saved.groups.filter(g => g && typeof g.id === 'string' && typeof g.experiment === 'string' && Array.isArray(g.comments));
  if (saved?.replyDrafts && typeof saved.replyDrafts === 'object') state.replyDrafts=saved.replyDrafts;
  if (saved?.editor?.anchor && typeof saved.editor.text === 'string') state.editor = saved.editor;
  if (['split','unified'].includes(saved?.layout)) state.layout = saved.layout;
  const theme = readStorage('ablation.workbench.theme');
  if (['light','dark'].includes(theme)) document.documentElement.dataset.theme = theme;

  async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
    const options = {method, headers:{Accept:'application/json'}};
    if (body !== undefined) { options.headers['Content-Type']='application/json'; options.headers['X-Ablation-Client']='local'; options.body=JSON.stringify(body); }
    const response = await fetch(path, options);
    let value;
    try { value = await response.json(); } catch { throw new Error(`服务返回了无法读取的内容（HTTP ${response.status}）`); }
    if (!response.ok) throw new Error(value.error?.message || value.error || `请求失败（HTTP ${response.status}）`);
    return value;
  }
  const endpoint = suffix => `/api/experiments/${encodeURIComponent(state.experiment.id)}${suffix || ''}`;
  const formatTime = value => { if (!value) return ''; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); };
  const label = (collection, value) => (collection || []).find(item => item.id === value)?.label || value;
  const objectMessage = value => typeof value === 'string' ? value : value?.message || (value ? json(value) : '');
  const currentScenario = () => state.experiment?.scenarios?.find(s => s.id === state.scenario);
  const matchesContext = a => a.experiment === state.experiment?.id && a.base === state.base && a.candidate === state.candidate && a.scenario === state.scenario;
  const currentGroups = () => state.groups.filter(g => g.experiment === state.experiment?.id);
  const activeStatus = status => ['queued','pending','running','starting','cancelling'].includes(status);
  const experimentJobs = () => state.jobs.filter(j => [j.experiment_id,j.experiment,j.experimentId].includes(state.experiment?.id));
  const profiles = () => Array.isArray(state.config.profiles) ? state.config.profiles.map(p => typeof p === 'string' ? p : p.name).filter(Boolean) : Object.keys(state.config.profiles || {});
  const agentConfigured = () => Boolean(state.config.agent?.backend || state.config.agent_configured || state.config.agent_backend || state.config.agent?.configured);
  const profileOptions = () => '<option value="">默认连接</option>' + profiles().map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  function toast(message) { clearTimeout(state.toastTimer); $('#toast').textContent=message; $('#toast').hidden=false; state.toastTimer=setTimeout(() => $('#toast').hidden=true, 6500); }
  function globalStatus(message) { $('#global-status').textContent=message || ''; $('#global-status').hidden=!message; }
  function errorInModal(error) { const el=$('#form-error'); if(el) {el.textContent=objectMessage(error);el.hidden=false;} else toast(objectMessage(error)); }
  function updateDraftCount() { const count=currentGroups().reduce((n,g) => n+g.comments.length, 0);$('#draft-count').textContent=count;$('#draft-count').hidden=count===0; }
  function setOptions(select, items, selected) {
    const markup = items.map(item => `<option value="${esc(item.id)}">${esc(item.label || item.id)}${item.withdrawn || item.status === 'withdrawn' ? '（已撤回）' : ''}</option>`).join('');
    if (select.innerHTML !== markup) select.innerHTML=markup;
    select.value=selected;
  }
  function editing() { return Boolean(document.activeElement?.matches('textarea,input,select')) || Boolean($('#modal').open); }

  async function refreshList() {
    const result=await api('/api/experiments');
    state.experiments=result.experiments || [];
    return state.experiments;
  }
  async function selectExperiment(experimentId, updateURL=true) {
    const sequence=++state.requestSequence;
    globalStatus('');
    try {
      const experiment=await api(`/api/experiments/${encodeURIComponent(experimentId)}`);
      if (sequence!==state.requestSequence) return;
      state.experiment=experiment;
      state.base='baseline'; state.candidate=experiment.candidates?.at(-1)?.id || 'baseline'; state.scenario='original'; state.diff=null; state.diffKey=''; state.expanded=false;
      if (updateURL) { const u=new URL(location.href);u.searchParams.set('experiment',experiment.id);history.pushState({},'',u); }
      $('#empty-state').hidden=true; $('#experiment').hidden=false;
      renderExperiment(); await loadDiff();
    } catch (error) { globalStatus(error.message); if(!state.experiment) $('#empty-state').hidden=false; }
  }
  function renderExperiment() {
    const e=state.experiment; if(!e) return;
    const candidates=e.candidates || [], scenarios=e.scenarios || [];
    if(!candidates.some(c => c.id===state.base)) state.base=candidates[0]?.id || 'baseline';
    if(!candidates.some(c => c.id===state.candidate)) state.candidate=candidates.at(-1)?.id || state.base;
    if(!scenarios.some(s => s.id===state.scenario)) state.scenario=scenarios[0]?.id || 'original';
    document.title=`${e.title || e.id} · Ablation`;
    $('#experiment-title').textContent=e.title || '未命名实验';
    setOptions($('#base-select'),candidates,state.base);setOptions($('#candidate-select'),candidates,state.candidate);setOptions($('#scenario-select'),scenarios.map(s => ({...s,label:`${s.label || s.id}${s.origin==='synthetic'?' · 人工构造':''}`})),state.scenario);
    $('#discussion-count').textContent=e.reviews?.length || 0; $('#discussion-count').hidden=!e.reviews?.length; $('#runs-count').textContent=e.runs?.length || 0;$('#runs-count').hidden=!e.runs?.length;
    updateDraftCount(); renderScenario(); renderJobs(); renderDiscussion(); renderRuns();
    $$('[data-artifact]').forEach(el => el.setAttribute('aria-pressed',String(el.dataset.artifact===state.artifact)));
    $$('[data-layout]').forEach(el => el.setAttribute('aria-pressed',String(el.dataset.layout===state.layout)));
    $('#selected-evidence').textContent=['prompt','response'].includes(state.artifact)?'':` · ${artifactLabels[state.artifact]}`;
    $('#more-evidence').classList.toggle('selected',!['prompt','response'].includes(state.artifact));
    switchTab(state.tab);
  }
  function renderScenario() {
    const scenario=currentScenario();
    const originLabel={synthetic:'人工构造',derived:'派生场景',real:'真实记录',source:'导入原场景'}[scenario?.origin] || '来源未标注';
    $('#scenario-context').innerHTML=`<span class="badge ${scenario?.origin==='synthetic'?'synthetic':''}">${originLabel}</span><span>${esc(scenario?.description || '未提供场景说明。')}</span>`;
  }
  function effectSummary() {
    const candidates=[...new Set([state.base,state.candidate])];
    const runs=(state.experiment.runs || []).filter(r=>candidates.includes(r.candidate)&&r.scenario===state.scenario);
    const counts={'通过':0,'未通过':0,'未评分':0,'执行错误':0,'运行中':0,'已取消':0,'已中断':0};
    for(const run of runs) {
      const result=activeStatus(run.status)?'运行中':run.error||['error','failed'].includes(run.status)?'执行错误':['cancelled','canceled'].includes(run.status)?'已取消':run.status!=='completed'?'已中断':run.passed===true?'通过':run.passed===false?'未通过':'未评分';
      counts[result]++;
    }
    let text='尚未运行，修改效果待验证。';
    if(runs.length)text=`当前比较的历史回放 ${runs.length} 次：${Object.entries(counts).filter(([,n])=>n).map(([label,n])=>`${n} ${label}`).join(' · ')}。${candidates.some(candidate=>!runs.some(r=>r.candidate===candidate))?'部分版本尚未回放。':''}`;
    return `<div class="effect-summary"><span class="effect-dot" aria-hidden="true"></span><span>${esc(text)}</span>${state.artifact==='response'?'':'<button class="text-btn" data-artifact="response">查看输出</button>'}</div>`;
  }
  function experimentDetails() {
    const e=state.experiment,source=e.source || {};
    const synthetic=e.synthetic || source.kind==='demo' || source.fidelity==='synthetic';
    openModal('experiment-details','实验详情',`<dl class="config-list"><dt>数据来源</dt><dd>${synthetic?'合成示例':esc(source.kind || '未标注')}<br><span class="muted">${esc(source.ref || '')}</span></dd><dt>来源完整度</dt><dd>${esc(source.fidelity || '未标注')}</dd><dt>实验编号</dt><dd><code>${esc(e.id)}</code></dd><dt>记录版本</dt><dd>r${esc(e.revision)}</dd><dt>创建时间</dt><dd>${esc(formatTime(e.created_at))}</dd><dt>原始请求指纹</dt><dd><code>${esc(e.baseline?.hash || '未提供')}</code></dd></dl>${source.notes?.length?`<details class="preview-details"><summary>来源说明</summary><p class="dialog-explanation">${esc(Array.isArray(source.notes)?source.notes.join(' '):source.notes)}</p></details>`:''}<div class="form-actions"><a class="btn" id="export-link" href="${endpoint('/export')}" download>导出实验</a><button class="btn" data-action="close-modal">关闭</button></div>`);
  }
  function switchTab(tab) {
    state.tab=tab;
    for(const name of ['diff','discussion','runs']) { $(`#${name}-view`).hidden=name!==tab; $(`[data-tab="${name}"]`).setAttribute('aria-current',name===tab?'page':'false'); }
  }
  async function loadDiff() {
    if(!state.experiment) return;
    const seq=++state.diffSequence;
    const key=[state.experiment.id,state.base,state.candidate,state.scenario,state.artifact].join('|');
    $('#diff-file').setAttribute('aria-busy','true');
    if(state.diffKey!==key) $('#diff-file').innerHTML='<div class="file-empty">正在读取比较…</div>';
    try {
      const query=new URLSearchParams({base:state.base,candidate:state.candidate,scenario:state.scenario,artifact:state.artifact});
      const diff=await api(endpoint(`/diff?${query}`));
      if(seq!==state.diffSequence) return;
      state.diff=diff;state.diffKey=key;
      renderDiff();
    } catch(error) { if(seq===state.diffSequence) $('#diff-file').innerHTML=`<div class="file-empty danger-text">${esc(error.message)} <button class="text-btn" data-action="retry-diff">重试</button></div>`; }
    finally { if(seq===state.diffSequence) $('#diff-file').setAttribute('aria-busy','false'); }
  }
  function normalizedRows() {
    return (state.diff?.rows || []).map((row,index) => {
      const oldLine=row.old_line ?? null,newLine=row.new_line ?? null;
      const before=oldLine===null && row.before==null ? null : row.before;
      const after=newLine===null && row.after==null ? null : row.after;
      const same=['same','equal','context','unchanged'].includes(row.kind) || (before===after && oldLine!==null && newLine!==null);
      return {...row,before,after,old_line:oldLine,new_line:newLine,same,index};
    });
  }
  function makeAnchor(row) { return {experiment:state.experiment.id,revision:state.experiment.revision,base:state.base,candidate:state.candidate,scenario:state.scenario,artifact:state.artifact,old_line:row.old_line,new_line:row.new_line,before:row.before,after:row.after}; }
  const sameAnchor = (a,b) => a.artifact===b.artifact && a.old_line===b.old_line && a.new_line===b.new_line && (a.before ?? '')===(b.before ?? '') && (a.after ?? '')===(b.after ?? '');
  const locationText = c => `${artifactLabels[c.artifact] || c.artifact} · ${c.new_line!=null?'改动后第 '+c.new_line+' 行':'改动前第 '+c.old_line+' 行'}`;
  function rowMarkup(row) {
    const plus=`<button type="button" class="line-plus" data-plus="${row.index}" aria-label="在${esc(row.new_line!=null?'新版第 '+row.new_line+' 行':'旧版第 '+row.old_line+' 行')}添加评论">+</button>`;
    const left=row.before!=null,right=row.after!=null;
    return `<div class="diff-row"><div class="diff-cell ${row.same?'':left?'del':'empty'}">${left&&(!right||row.same)?plus:'<span></span>'}<span class="line-number">${esc(row.old_line)}</span><span class="line-sign">${!row.same&&left?'−':''}</span><code>${esc(row.before)}</code></div><div class="diff-cell ${row.same?'same-right':right?'add':'empty'}">${right?plus:'<span></span>'}<span class="line-number">${esc(row.new_line)}</span><span class="line-sign">${!row.same&&right?'+':''}</span><code>${esc(row.after)}</code></div></div>`;
  }
  function rationaleMarkup() {
    const explanation=state.artifact==='source' ? (state.experiment.source?.notes || []).join(' ') : state.artifact==='input' || state.artifact==='checks' ? currentScenario()?.description : state.diff?.rationale;
    if(!explanation) return '';
    return `<div class="thread rationale"><div class="note"><div class="avatar">A</div><div class="bubble"><p class="comment-body"><strong>${state.artifact==='input'||state.artifact==='checks'?'场景说明':'修改原因'}：</strong>${esc(explanation)}</p></div></div></div>`;
  }
  function pendingMarkup(group,comment) {
    return `<div class="thread"><div class="note"><div class="avatar user">你</div><div class="bubble"><div class="comment-head"><strong>你</strong><span class="badge pending">待提交</span><span class="location">${esc(locationText(comment))}</span></div><p class="comment-body">${esc(comment.text)}</p><div class="comment-actions"><button class="text-btn" data-edit-comment="${esc(comment.id)}" data-group="${esc(group.id)}">编辑</button><button class="text-btn" data-delete-comment="${esc(comment.id)}" data-group="${esc(group.id)}">删除</button></div></div></div></div>`;
  }
  function repliesMarkup(review,index=null) {
    return (review.replies || []).filter(reply => index===null ? reply.comment_index==null : reply.comment_index===index).map(reply => `<div class="reply"><span class="reply-meta">${esc(reply.author || 'Agent')} · ${esc(formatTime(reply.created_at))}</span><p>${esc(reply.text)}</p></div>`).join('');
  }
  function savedCommentMarkup(review,comment,index) {
    return `<div class="thread"><div class="note"><div class="avatar user">你</div><div class="bubble"><div class="comment-head"><strong>你</strong><span class="badge">${review.intent==='changes'?'请求修改':'追问'}</span><span class="location">${esc(locationText(comment))}</span></div><p class="comment-body">${esc(comment.text)}</p><div class="comment-actions"><button class="text-btn" data-followup-review="${esc(review.id)}" data-comment-index="${index}">继续追问</button></div></div></div>${repliesMarkup(review,index)}</div>`;
  }
  function editorMarkup() {
    const editor=state.editor;if(!editor)return '';
    return `<div class="thread"><div class="comment-editor"><div class="editor-head"><strong>添加评论</strong><span class="muted">${esc(locationText(editor.anchor))}</span></div><div class="editor-body"><textarea id="inline-comment" aria-label="行内评论" maxlength="10000" placeholder="写一句意见，或问问为什么这样修改。">${esc(editor.text)}</textarea></div><p id="inline-error" class="editor-error" role="status"></p><div class="editor-footer"><button class="btn" data-action="cancel-comment">取消</button><button class="btn primary" data-action="add-comment">${editor.commentId?'更新评论':'添加评论'}</button></div></div></div>`;
  }
  function renderDiff() {
    if(!state.diff) return;
    const rows=normalizedRows(),diff=state.diff;
    const contextLines=new Set();
    const anchorComments=currentGroups().filter(matchesContext).flatMap(g=>g.comments);
    for(const review of state.experiment.reviews || []) if(review.base===state.base&&review.candidate===state.candidate&&review.scenario===state.scenario) anchorComments.push(...(review.comments || []));
    if(state.editor&&matchesContext(state.editor.anchor))anchorComments.push(state.editor.anchor);
    for(let i=0;i<rows.length;i++)if(!rows[i].same||anchorComments.some(c=>sameAnchor(c,rowWithArtifact(rows[i]))))for(let j=Math.max(0,i-3);j<=Math.min(rows.length-1,i+3);j++)contextLines.add(j);
    if(!contextLines.size)for(let i=0;i<Math.min(rows.length,20);i++)contextLines.add(i);
    const display=state.expanded?rows:rows.filter((_,i)=>contextLines.has(i)).slice(0,500);
    let body='',groupOpen=false,rationaleShown=false,lastIndex=-1;
    for(let n=0;n<display.length;n++) {
      const row=display[n];if(row.index>lastIndex+1)body+=`<div class="diff-more"><button class="text-btn" data-action="expand-diff">显示 ${row.index-lastIndex-1} 行上下文</button></div>`;body+=rowMarkup(row);lastIndex=row.index;
      if(!row.same) groupOpen=true;
      if(groupOpen && (n===display.length-1 || display[n+1].same)) { if(!rationaleShown){body+=rationaleMarkup();rationaleShown=true;}groupOpen=false; }
      for(const group of currentGroups().filter(matchesContext)) for(const comment of group.comments) if(sameAnchor(comment,rowWithArtifact(row))) body+=pendingMarkup(group,comment);
      for(const review of state.experiment.reviews || []) if(review.base===state.base&&review.candidate===state.candidate&&review.scenario===state.scenario) (review.comments || []).forEach((comment,index) => {if(sameAnchor(comment,rowWithArtifact(row)))body+=savedCommentMarkup(review,comment,index);});
      if(state.editor && matchesContext(state.editor.anchor) && sameAnchor(state.editor.anchor,rowWithArtifact(row))) body+=editorMarkup();
    }
    if(!rows.length) body='<div class="file-empty">没有可显示的内容。响应需要先运行，未评分的结果不会标记为通过。</div>';
    if(lastIndex<rows.length-1) body+=`<div class="diff-more"><button class="text-btn" data-action="expand-diff">显示剩余 ${rows.length-lastIndex-1} 行</button></div>`;
    for(const group of currentGroups().filter(g=>matchesContext(g)&&g.parent_review)) for(const comment of group.comments) if(comment.artifact===state.artifact&&!display.some(row=>sameAnchor(comment,rowWithArtifact(row)))) body+=`<details class="preview-details"><summary>追问引用的历史原文</summary><pre class="context-snippet">${esc('改动前：\n'+(comment.before||'（无内容）')+'\n\n改动后：\n'+(comment.after||'（无内容）'))}</pre></details>${pendingMarkup(group,comment)}`;
    if(state.editor && matchesContext(state.editor.anchor) && state.editor.anchor.artifact===state.artifact && !display.some(row => sameAnchor(state.editor.anchor,rowWithArtifact(row)))) body+=`<div class="notice warning">${state.groups.find(g=>g.id===state.editor.groupId)?.parent_review?'追问已保存的历史评论，将保留当时的内容。':'原内容已变化，请核对新内容后重新添加评论。'}</div>${editorMarkup()}`;
    const plus=rows.filter(r => !r.same&&r.after!=null).length,minus=rows.filter(r => !r.same&&r.before!=null).length;
    $('#diff-file').classList.toggle('unified',state.layout==='unified');
    $('#diff-file').innerHTML=`<div class="file-top"><div class="file-path"><span aria-hidden="true">▤</span><strong class="artifact-title">${esc(artifactLabels[state.artifact])}</strong></div><div class="file-actions"><span class="stat-add">+${plus}</span><span class="stat-del">−${minus}</span><button class="text-btn" data-action="copy-artifact">复制右侧内容</button></div></div><div class="diff-headrow"><span title="${esc(diff.base_label || label(state.experiment.candidates,state.base))}">改动前 <span class="version-caption">${esc(diff.base_label || label(state.experiment.candidates,state.base))}</span></span><span title="${esc(diff.candidate_label || label(state.experiment.candidates,state.candidate))}">改动后 <span class="version-caption">${esc(diff.candidate_label || label(state.experiment.candidates,state.candidate))}</span></span></div>${body}`;
    const notes=(Array.isArray(diff.notes)?diff.notes:diff.notes?[diff.notes]:[]).filter(n=>!/^Scenario:/.test(objectMessage(n)));
    let status=effectSummary();
    if(state.artifact==='input'||state.artifact==='checks') {
      const scenario=currentScenario();
      status=`<div class="effect-summary"><span class="badge ${scenario?.origin==='synthetic'?'synthetic':''}">${scenario?.origin==='synthetic'?'人工构造':'导入原场景'}</span><span>${esc(scenario?.label || state.scenario)}</span>${rows.length&&rows.every(row=>row.same)?'<span class="muted">与原始内容一致</span>':''}</div>`;
    }
    if(notes.length)status+=`<details class="evidence-notes"><summary>查看比较说明</summary>${notes.map(n=>`<p>${esc(objectMessage(n))}</p>`).join('')}</details>`;
    if(state.artifact==='response'&&state.experiment.baseline?.response!=null)status+=`<details class="evidence-notes"><summary>查看历史响应</summary><p class="small muted">导入时记录的响应，不作为本次比较的回放结果。</p><pre class="context-snippet">${esc(json(state.experiment.baseline.response))}</pre></details>`;
    $('#comparison-status').innerHTML=status;renderScenario();
  }
  function rowWithArtifact(row){return{...row,artifact:state.artifact};}
  function runBadge(run) {
    if(run.status==='failed'||run.status==='error'||run.error)return '<span class="badge error">执行错误</span>';
    if(activeStatus(run.status))return '<span class="badge">运行中</span>';
    if(run.status==='cancelled'||run.status==='canceled')return '<span class="badge">已取消</span>';
    if(run.status!=='completed')return '<span class="badge">已中断</span>';
    if(run.passed===true)return '<span class="badge success">断言通过</span>';
    if(run.passed===false)return '<span class="badge error">断言未通过</span>';
    return '<span class="badge">已返回 · 未评分</span>';
  }
  function renderJobs() {
    const jobs=experimentJobs().filter(j=>activeStatus(j.status));
    $('#active-jobs').innerHTML=jobs.map(job=>`<div class="jobs-bar"><span class="inline-progress" aria-hidden="true"></span><strong>${job.kind==='agent'||job.review_id?'Agent 任务':'回放'}</strong><span>${esc(job.status==='cancelling'?'正在停止…':job.status==='queued'?'等待开始':'正在执行')}</span><span>${esc(job.progress?.message || job.message || '')}</span><div class="button-row"><button class="btn" data-cancel-job="${esc(job.id)}" ${job.status==='cancelling'?'disabled':''}>停止</button><button class="text-btn" data-tab="runs">查看记录</button></div></div>`).join('');
  }
  function renderDiscussion() {
    const reviews=state.experiment.reviews || [];
    $('#discussion-view').innerHTML=`<div class="section-head"><h2>评论与解释</h2><button class="btn" data-action="review">提交评论</button></div><p class="section-description">评论保留审阅时的内容。回答问题只新增回复，不代表生成了新候选或重新运行。</p>${reviews.length?[...reviews].reverse().map(review=>`<article class="review-card"><div class="review-card-header"><strong>Review ${esc(review.id.slice(0,8))}</strong><span class="badge">${review.intent==='changes'?'请求修改':'追问'}</span><span class="muted">${esc(formatTime(review.created_at))}</span><span class="muted">${esc(label(state.experiment.candidates,review.base))} → ${esc(label(state.experiment.candidates,review.candidate))} · ${esc(label(state.experiment.scenarios,review.scenario))} · r${esc(review.revision)}</span></div><div class="review-card-content">${review.summary?`<p class="comment-body">${esc(review.summary)}</p>`:''}${(review.comments||[]).map((c,index)=>`<div class="discussion-item"><div class="review-location"><code>${esc(locationText(c))}</code><button class="text-btn" data-jump-review="${esc(review.id)}" data-comment-index="${index}">查看位置</button></div><div class="bubble"><div class="comment-head"><strong>你</strong></div><p class="comment-body">${esc(c.text)}</p><div class="comment-actions"><button class="text-btn" data-followup-review="${esc(review.id)}" data-comment-index="${index}">继续追问</button></div></div><details class="preview-details"><summary>审阅时的原文</summary><pre class="context-snippet">${esc('改动前：\n'+(c.before||'（无内容）')+'\n\n改动后：\n'+(c.after||'（无内容）'))}</pre></details>${repliesMarkup(review,index)}</div>`).join('')}${repliesMarkup(review)}</div><div class="review-card-footer"><button class="btn" data-copy-review="${esc(review.id)}">复制评论</button><button class="btn" data-dispatch-review="${esc(review.id)}" ${agentConfigured()?'':'disabled'}>交给 Agent</button><button class="text-btn" data-reply-review="${esc(review.id)}">追加回复</button></div><form class="thread-reply-form" data-reply-form="${esc(review.id)}" ${state.replyOpen===review.id?'':'hidden'}><textarea aria-label="回复评论" name="text" required placeholder="补充说明。保存回复不会自动运行 Agent。">${esc(state.replyDrafts[state.experiment.id+'/'+review.id] || '')}</textarea><div class="button-row"><button type="button" class="btn" data-reply-review="${esc(review.id)}">取消</button><button type="submit" class="btn primary">保存回复</button></div></form></article>`).join(''):'<div class="muted-empty">还没有已提交的 review。点击变更行旁的 ＋，先添加一条评论。</div>'}`;
  }
  function jobStatusLabel(job) { return ({queued:'已排队',pending:'待开始',starting:'启动中',running:'运行中',cancelling:'停止中',cancelled:'已取消',canceled:'已取消',completed:'执行结束',succeeded:'执行结束',failed:'执行失败',error:'执行失败',timed_out:'执行超时',interrupted:'执行中断'})[job.status] || job.status || '状态未知'; }
  function renderRuns() {
    const runs=[...(state.experiment.runs || [])].reverse(),jobs=[...experimentJobs()].reverse();
    $('#runs-view').innerHTML=`<div class="section-head"><h2>实际运行记录</h2><button class="btn primary" data-action="run">运行比较</button></div><p class="section-description">返回结果、断言是否通过、工具是否实际执行是不同的事情。这里仅回放模型请求，不执行模型返回的工具调用。</p>${runs.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>候选 / 场景</th><th>结果</th><th>请求指纹</th><th>耗时</th><th>时间</th><th>外部证据</th><th></th></tr></thead><tbody>${runs.map(run=>`<tr><td><strong>${esc(label(state.experiment.candidates,run.candidate))}</strong><br><span class="muted">${esc(label(state.experiment.scenarios,run.scenario))} · 第 ${esc(run.repeat ?? 1)} 次</span></td><td>${runBadge(run)}${run.error?`<p class="run-error">${esc(objectMessage(run.error))}</p>`:''}</td><td><code title="${esc(run.request_hash)}">${esc(run.request_hash?.slice(0,12) || '—')}</code></td><td>${run.elapsed_ms!=null?esc(Math.round(run.elapsed_ms))+' ms':'—'}</td><td>${esc(formatTime(run.created_at))}</td><td>${evidenceLinks(run) || '—'}</td><td><button class="text-btn" data-run-detail="${esc(run.id)}">详情</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="muted-empty">当前没有 replay 记录。导入日志、创建候选和发表评论均不会自动调用模型。</div>'}<div class="job-history"><h3>任务记录</h3><p class="small muted">Agent 执行结束只说明进程已结束；是否写回解释、候选或运行结果，以实验内容为准。</p>${jobs.length?jobs.map(job=>`<details class="notice"><summary>${esc(job.kind==='agent'||job.review_id?'Agent':'Replay')} · ${esc(jobStatusLabel(job))} · ${esc(formatTime(job.created_at))}</summary><div class="small muted">${esc(job.id)}${job.review_id?' · Review '+esc(job.review_id):''}</div>${job.error?`<p class="run-error">${esc(objectMessage(job.error))}</p>`:''}${job.message?`<p>${esc(job.message)}</p>`:''}${job.output||job.result||job.log_tail?`<pre class="job-log">${esc(json(job.output || job.log_tail || job.result))}</pre>`:''}${activeStatus(job.status)?`<div class="form-actions"><button class="btn danger" data-cancel-job="${esc(job.id)}">停止任务</button></div>`:''}</details>`).join(''):'<p class="small muted">暂无后台任务。</p>'}</div>`;
  }
  function ensureGroup(anchor, forceNew=false) {
    let group=!forceNew && state.groups.find(g => g.experiment===anchor.experiment && g.base===anchor.base && g.candidate===anchor.candidate && g.scenario===anchor.scenario && g.revision===anchor.revision && !g.saved && !g.parent_review);
    if(!group) { group={id:id(),experiment:anchor.experiment,revision:anchor.revision,base:anchor.base,candidate:anchor.candidate,scenario:anchor.scenario,intent:'comment',summary:'',comments:[]};state.groups.push(group); }
    return group;
  }
  function openComment(rowIndex) {
    const row=normalizedRows().find(r=>r.index===rowIndex);if(!row)return;
    const anchor=makeAnchor(row);
    if(state.editor&&matchesContext(state.editor.anchor)&&sameAnchor(state.editor.anchor,anchor)){renderDiff();$('#inline-comment')?.focus();return;}
    if(state.editor?.text?.trim() && (!matchesContext(state.editor.anchor) || !sameAnchor(state.editor.anchor,anchor))) { toast('请先对上一条草稿点击“添加评论”；它仍保留在原位置。'); return; }
    const group=ensureGroup(anchor);
    state.editor={anchor,groupId:group.id,text:'',commentId:null}; persist();renderDiff();$('#inline-comment')?.focus();
  }
  function stashEditor() {
    const editor=state.editor;if(!editor?.text?.trim())return;
    const group=state.groups.find(g=>g.id===editor.groupId);if(!group)return;
    const comment={...editor.anchor,id:editor.commentId||id(),text:editor.text};
    const at=group.comments.findIndex(c=>c.id===comment.id);if(at>=0)group.comments[at]=comment;else group.comments.push(comment);
  }
  function addComment() {
    const editor=state.editor;if(!editor)return;
    if(!editor.text.trim()) { $('#inline-error').textContent='写一句评论后再添加。';$('#inline-comment').focus();return; }
    stashEditor();state.editor=null;persist();renderDiff();toast('评论已加入待提交评论。');
  }
  function editComment(groupId,commentId) {
    const group=state.groups.find(g=>g.id===groupId),comment=group?.comments.find(c=>c.id===commentId);if(!comment)return;
    if(state.editor?.text?.trim()&&state.editor.commentId!==commentId){toast('请先完成原位置的草稿，再编辑另一条评论。');return;}
    state.base=group.base;state.candidate=group.candidate;state.scenario=group.scenario;state.artifact=comment.artifact;
    state.editor={anchor:{...comment,experiment:group.experiment,revision:group.revision,base:group.base,candidate:group.candidate,scenario:group.scenario},groupId,text:comment.text,commentId};
    closeModal();renderExperiment();persist();loadDiff().then(()=>$('#inline-comment')?.focus());
  }
  function deleteComment(groupId,commentId) {
    const group=state.groups.find(g=>g.id===groupId);if(!group)return;
    group.comments=group.comments.filter(c=>c.id!==commentId);if(state.editor?.commentId===commentId)state.editor=null;
    persist();renderDiff();if(state.modal==='review')renderReviewModal();
  }
  function openModal(kind,title,content,wide=false) {
    state.modal=kind;state.focusBeforeModal=document.activeElement;
    $('#modal-title').textContent=title;$('#modal-body').innerHTML=content;$('#modal').classList.toggle('wide',wide);
    if(!$('#modal').open)$('#modal').showModal();
  }
  function closeModal() { if(state.busy)return;$('#modal').close();state.modal=null;state.focusBeforeModal?.focus?.(); }
  const formError=()=>'<p id="form-error" class="form-error" role="alert" hidden></p>';
  const formStatus=()=>'<p id="form-status" class="form-status" role="status" aria-live="polite"></p>';
  function setFormBusy(busy) {state.busy=busy;$$('#modal button,#modal input,#modal select,#modal textarea').forEach(el=>{if(busy){el.dataset.wasDisabled=String(el.disabled);el.disabled=true;}else if(el.dataset.wasDisabled!==undefined){el.disabled=el.dataset.wasDisabled==='true';delete el.dataset.wasDisabled;}});}
  function parseArray(text,name) {let value;try{value=JSON.parse(text);}catch{throw new Error(`${name}不是有效的 JSON。`);}if(!Array.isArray(value))throw new Error(`${name}必须是 JSON 数组。`);return value;}
  async function experimentsModal() {
    openModal('experiments','实验','<p class="muted">正在读取工作区…</p>');
    try {await refreshList();if(state.modal!=='experiments')return;$('#modal-body').innerHTML=`<div class="button-row"><button class="btn primary" data-action="import">导入数据</button><button class="btn" data-action="demo">打开合成示例</button></div>${state.experiments.length?`<ul class="experiment-list">${state.experiments.map(e=>`<li><button data-select-experiment="${esc(e.id)}"><strong>${esc(e.title || e.id)}</strong><span class="small">${esc(e.id)}　${esc(formatTime(e.created_at))}</span></button></li>`).join('')}</ul>`:'<p class="dialog-explanation" style="margin-top:20px">还没有实验。导入请求或日志 JSON，也可以先打开合成示例。</p>'}`;}catch(error){errorInModal(error);}
  }
  function importModal() {
    openModal('import','导入实验数据',`<p class="dialog-explanation">导入模型请求、日志记录或 HTTP 录制 JSON。原始数据和还原后的请求会分别保留。</p><form id="import-form"><label class="field"><span class="field-label">实验名称（可选）</span><input name="title" placeholder="例如：工具完成状态判断" maxlength="200"></label><div class="form-columns"><label class="field"><span class="field-label">数据格式</span><select name="kind" id="import-kind"><option value="request">模型请求 JSON</option><option value="log">日志记录 JSON</option><option value="capture">HTTP 录制 JSON</option><option value="bundle">Ablation 实验包</option></select></label><label class="field" id="import-profile-field"><span class="field-label">数据连接（可选）</span><select name="profile">${profileOptions()}</select><span class="field-help">用于远程取数或读取已配置的字段映射。</span></label></div><label class="field" id="import-url-field"><span class="field-label">JSON 地址 / 记录 ID（与 JSON 内容二选一）</span><input name="url" placeholder="粘贴 JSON 地址，或连接中配置的记录 ID" autocomplete="off"></label><label class="field"><span class="field-label">选择 JSON 文件</span><input type="file" id="import-file" accept=".json,application/json"><span class="field-help" id="import-file-status">也可以直接粘贴到下方。</span></label><label class="field"><span class="field-label">JSON 内容</span><textarea class="code-input" name="data" id="import-json" spellcheck="false" placeholder='{"model": "…", "messages": […]}'></textarea></label><label class="field" id="import-response-field"><span class="field-label">原始响应 JSON（可选）</span><textarea class="code-input" name="response" spellcheck="false" placeholder="保留历史响应；它不会被标记为一次新回放。"></textarea></label>${formError()}${formStatus()}<div class="form-actions"><button type="button" class="btn" data-action="close-modal">取消</button><button type="submit" class="btn primary">导入并查看</button></div></form>`);
  }
  function candidateModal() {
    if(!state.experiment)return;
    openModal('candidate','新建提示词候选',`<p class="dialog-explanation">以指定候选为父版本应用 JSON Patch，保留原始请求。完整工具列表不会因为修改一处说明而被替换。</p><form id="candidate-form"><div class="form-columns"><label class="field"><span class="field-label">候选名称</span><input name="label" required maxlength="150" placeholder="例如：通用完成条件"></label><label class="field"><span class="field-label">父版本</span><select name="parent">${state.experiment.candidates.map(c=>`<option value="${esc(c.id)}" ${c.id===state.candidate?'selected':''}>${esc(c.label || c.id)}</option>`).join('')}</select></label></div><label class="field"><span class="field-label">为什么修改</span><textarea name="rationale" required maxlength="10000" placeholder="用一句话说明要修复的问题和修改的原因。"></textarea><span class="field-help">这段说明会显示在 diff 下方。</span></label><label class="field"><span class="field-label">JSON Patch</span><textarea class="code-input" name="patch" required spellcheck="false">[]</textarea><span class="field-help">例如 [{"op":"replace","path":"/tools/0/function/description","value":"新的工具说明"}]。路径以完整请求 为准。</span></label>${formError()}<div class="form-actions"><button type="button" class="btn" data-action="close-modal">取消</button><button type="submit" class="btn primary">创建候选</button></div></form>`);
  }
  function scenarioModal() {
    if(!state.experiment)return;
    openModal('scenario','新建测试场景',`<p class="dialog-explanation">从原始输入构造独立场景。请描述改变的条件及其含义；这里创建的场景会明确标记为“人工构造”。</p><form id="scenario-form"><label class="field"><span class="field-label">场景名称</span><input name="label" required maxlength="150" placeholder="例如：执行结果未得到确认"></label><label class="field"><span class="field-label">条件与目的</span><textarea name="description" required placeholder="修改了哪些字段？它们表示什么？这个场景验证什么？"></textarea></label><label class="field"><span class="field-label">输入 JSON Patch</span><textarea name="patch" class="code-input" spellcheck="false">[]</textarea><span class="field-help">输入从固定的原始请求构造。修改提示词请使用“新建候选”。</span></label><label class="field"><span class="field-label">响应断言</span><textarea name="checks" class="code-input" spellcheck="false">[]</textarea><span class="field-help">支持 json_equals、text_contains、tool_called、tool_argument_equals。空数组表示未评分；例如 [{"kind":"tool_argument_equals","name":"report_result","pointer":"/status","value":"unknown"}]。</span></label>${formError()}<div class="form-actions"><button type="button" class="btn" data-action="close-modal">取消</button><button type="submit" class="btn primary">创建场景</button></div></form>`,true);
  }
  function runModal() {
    if(!state.experiment)return;
    const candidates=[...new Set([state.base,state.candidate])];
    openModal('run','运行比较',`<p class="dialog-explanation">向配置的模型接口发送请求，并保存真实响应。模型返回的工具调用不会执行。</p><form id="run-form"><div class="notice"><strong>${esc(candidates.map(c=>label(state.experiment.candidates,c)).join(' 与 '))}</strong><br>场景：${esc(label(state.experiment.scenarios,state.scenario))}<br><span class="muted">同一场景、相同次数，分别运行所选候选。</span></div><div class="form-columns"><label class="field"><span class="field-label">推理连接</span><select name="profile">${profileOptions()}</select></label><label class="field"><span class="field-label">每个候选的运行次数</span><input name="repeats" id="run-repeats" type="number" min="1" max="10" value="1" required></label></div><p id="run-estimate" class="small muted">共 ${candidates.length} 次模型请求。重复运行不等于统计上的稳定性证明。</p>${profiles().length?'':'<div class="notice warning">尚未发现推理连接。请先通过 CLI 配置连接地址和凭证的环境变量名称。</div>'}${formError()}<div class="form-actions"><button type="button" class="btn" data-action="close-modal">取消</button><button type="submit" class="btn primary">开始 replay</button></div></form>`);
  }
  function openReview(groupId=null) {
    if(!state.experiment)return;
    if(state.editor?.text?.trim()) toast('尚未点击“添加评论”的内容仍是草稿，不会提交。');
    let group=state.groups.find(g=>g.id===groupId);
    if(!group)group=currentGroups().find(g=>g.comments.length)||ensureGroup({experiment:state.experiment.id,revision:state.experiment.revision,base:state.base,candidate:state.candidate,scenario:state.scenario});
    state.groupId=group.id;
    openModal('review','提交评论','');renderReviewModal();persist();
  }
  function renderReviewModal() {
    const groups=currentGroups(),group=groups.find(g=>g.id===state.groupId);if(!group)return;
    const e=state.experiment,configured=agentConfigured();
    $('#modal-body').innerHTML=`${groups.length>1?`<label class="field"><span class="field-label">选择待提交的评论</span><select id="review-group" class="review-group-select">${groups.map(g=>`<option value="${esc(g.id)}" ${g.id===group.id?'selected':''}>${esc(label(e.candidates,g.candidate))} · ${esc(label(e.scenarios,g.scenario))} · ${g.comments.length} 条</option>`).join('')}</select></label>`:''}${group.comments.map(c=>`<div class="pending-item"><div class="small"><span>${esc(locationText(c))}</span><button class="text-btn" data-edit-comment="${esc(c.id)}" data-group="${esc(group.id)}">编辑</button><button class="text-btn" data-delete-comment="${esc(c.id)}" data-group="${esc(group.id)}">删除</button></div><p>${esc(c.text)}</p></div>`).join('')}<details class="review-summary-details" ${!group.comments.length||group.summary?'open':''}><summary>${group.comments.length?'补充总体意见（可选）':'写下你的问题或意见'}</summary><textarea id="review-summary" maxlength="20000" aria-label="总体意见" placeholder="想进一步了解什么？">${esc(group.summary)}</textarea></details><div class="radio-row review-intent"><label><input type="radio" name="review-intent" value="comment" ${group.intent==='comment'?'checked':''}>追问解释</label><label><input type="radio" name="review-intent" value="changes" ${group.intent==='changes'?'checked':''}>请求修改</label></div><p id="intent-description" class="small muted">${group.intent==='changes'?'按评论创建下一版，并运行相关验证。':'只解释原因与依据，不修改或重跑。'}</p><details class="review-context"><summary>评论位置已附带</summary><p>${esc(label(e.candidates,group.base))} → ${esc(label(e.candidates,group.candidate))}<br>${esc(label(e.scenarios,group.scenario))} · 记录 r${esc(group.revision)}</p></details>${formError()}${formStatus()}<div class="form-actions">${configured?'<button class="btn" data-action="copy-pending">复制到对话</button><button class="btn primary" data-action="dispatch-pending">交给 Agent</button>':'<button class="text-btn configure-agent" data-action="settings">设置自动 Agent</button><button class="btn primary" data-action="copy-pending">复制到对话</button>'}</div>`;
  }
  async function savePending(dispatch) {
    if(state.busy)return;
    const group=state.groups.find(g=>g.id===state.groupId);if(!group)return;
    if(!group.comments.length&&!group.summary.trim())return errorInModal(new Error('至少添加一条评论或总体意见。'));
    const expId=group.experiment;
    const body={id:group.id,revision:group.revision,base:group.base,candidate:group.candidate,scenario:group.scenario,intent:group.intent,comments:group.comments.map(c=>({artifact:c.artifact,old_line:c.old_line,new_line:c.new_line,before:c.before,after:c.after,text:c.text})),summary:group.summary};
    if(group.parent_review)body.parent_review=group.parent_review;
    setFormBusy(true);
    let savedReview=null;
    try {
      const review=await api(`/api/experiments/${encodeURIComponent(expId)}/reviews`,body);
      savedReview=review;state.groups=state.groups.filter(g=>g.id!==group.id);if(state.editor?.groupId===group.id)state.editor=null;persist();
      const result=await api(`/api/experiments/${encodeURIComponent(expId)}/reviews/${encodeURIComponent(review.id)}/text`);
      let job=null,error=null;
      if(dispatch) {try{job=await api(`/api/experiments/${encodeURIComponent(expId)}/reviews/${encodeURIComponent(review.id)}/dispatch`,{});}catch(e){error=e;}}
      state.groups=state.groups.filter(g=>g.id!==group.id);if(state.editor?.groupId===group.id)state.editor=null;persist();
      if(dispatch) {
        $('#modal-body').innerHTML=`<div class="notice ${error?'warning':''}">${error?'评论已保存，但 Agent 未确认开始。':'评论已保存，已创建 Agent 任务。'}</div>${error?`<p class="form-error">${esc(error.message)}</p>`:`<p class="small muted">状态：${esc(jobStatusLabel(job))}。执行结果以任务记录和写回内容为准。</p>`}<textarea class="copy-preview" id="portable-review" readonly aria-label="可复制的 review">${esc(result.text)}</textarea><div class="form-actions"><button class="btn" data-action="copy-portable">复制到对话</button><button class="btn primary" data-action="close-modal">完成</button></div>`;
      } else {
        let copied=false;try{await navigator.clipboard.writeText(result.text);copied=true;}catch{}
        $('#modal-body').innerHTML=`<div class="notice">${copied?'评论已保存并复制，可以粘贴到对话。':'评论已保存。浏览器未允许自动复制，请选择下方内容复制。'}</div><textarea class="copy-preview" id="portable-review" readonly aria-label="可复制的 review">${esc(result.text)}</textarea><p class="small muted" style="margin-top:10px">内容包含实验位置、版本、原文和评论。让当前 Agent 按其中的写回命令保存解释或继续实验。</p><div class="form-actions"><button class="btn" data-action="copy-portable">再次复制</button><button class="btn primary" data-action="close-modal">完成</button></div>`;
        if(!copied){$('#portable-review').focus();$('#portable-review').select();}
      }
      state.modal='review-saved';await refreshSelected();
    } catch(error) {
      if(savedReview){state.modal='review-saved';$('#modal-body').innerHTML=`<div class="notice warning">评论已保存，但交接尚未完成。</div><p class="form-error">${esc(error.message)}</p><div class="form-actions"><button class="btn" data-copy-review="${esc(savedReview.id)}">重新复制评论</button><button class="btn primary" data-action="close-modal">完成</button></div>`;await refreshSelected();}
      else errorInModal(error);
    }
    finally {setFormBusy(false);}
  }
  async function copyPersistedReview(reviewId) {
    const {text}=await api(endpoint(`/reviews/${encodeURIComponent(reviewId)}/text`));
    try{await navigator.clipboard.writeText(text);toast('已复制评论 及完整位置，可以粘贴到对话。');}
    catch{openModal('copy','复制评论',`<p class="dialog-explanation">浏览器未允许自动复制，请选择下方内容复制。</p><textarea class="copy-preview" id="portable-review" readonly>${esc(text)}</textarea><div class="form-actions"><button class="btn" data-action="copy-portable">复制</button></div>`);$('#portable-review').focus();$('#portable-review').select();}
  }
  function followupReview(reviewId,index) {
    const review=state.experiment.reviews.find(r=>r.id===reviewId),comment=review?.comments?.[index];if(!comment)return;
    if(state.editor?.text?.trim()){toast('请先完成原位置的草稿，再继续追问；草稿仍保留在原位置。');return;}
    const anchor={...comment,experiment:state.experiment.id,revision:review.revision,base:review.base,candidate:review.candidate,scenario:review.scenario};
    const group=ensureGroup(anchor,true);group.parent_review={id:review.id,comment_index:index};group.summary=`继续讨论 Review ${review.id}：\n`;
    const fresh={...anchor,id:id(),text:''};
    state.base=review.base;state.candidate=review.candidate;state.scenario=review.scenario;state.artifact=comment.artifact;
    state.editor={anchor,groupId:group.id,text:'',commentId:fresh.id};state.tab='diff';persist();renderExperiment();loadDiff().then(()=>{$('#inline-comment')?.focus();toast('继续追问将创建新的评论记录，原讨论和位置会保留。');});
  }
  async function dispatchReview(reviewId,button) {
    if(button)button.disabled=true;
    try{const job=await api(endpoint(`/reviews/${encodeURIComponent(reviewId)}/dispatch`),{});toast(`Agent 任务：${jobStatusLabel(job)}。同一 review 不会重复启动。`);await refreshJobs();renderJobs();renderRuns();}
    catch(error){toast(error.message);}finally{if(button)button.disabled=false;}
  }
  async function settingsModal() {
    openModal('settings','设置与版本','<p class="muted">正在读取配置…</p>');
    try {
      state.config=await api('/api/config');if(state.modal!=='settings')return;
      const agent=state.config.agent?.backend || state.config.agent_backend;
      $('#modal-body').innerHTML=`<p class="dialog-explanation">连接地址、凭证的环境变量名称及 Agent 通过 CLI 配置。凭证不写入实验，也不在这里显示。</p><dl class="config-list"><dt>工作区</dt><dd><code>${esc(state.config.workspace || '当前工作区')}</code></dd><dt>连接</dt><dd>${profiles().map(p=>`<span class="badge">${esc(p)}</span>`).join(' ') || '尚未配置'}</dd><dt>自动 Agent</dt><dd>${agentConfigured()?esc(agent || '已配置'):'尚未配置；可以复制评论 交接'}</dd></dl><div class="notice"><code>ablation config --help</code><br><span class="muted">查看当前版本支持的连接与 Agent 配置命令。配置后点击“刷新配置”。</span></div><label class="field"><span class="field-label">外观</span><select id="theme-select"><option value="system" ${!document.documentElement.dataset.theme?'selected':''}>跟随系统</option><option value="light" ${document.documentElement.dataset.theme==='light'?'selected':''}>浅色</option><option value="dark" ${document.documentElement.dataset.theme==='dark'?'selected':''}>深色</option></select></label><p class="small muted">自动更新检查可通过 ABLATION_UPDATE_CHECK=0 关闭。更新工具不修改实验历史，也不安装或同步 skills。</p><div class="button-row"><button class="btn" data-action="settings">刷新配置</button><button class="btn" data-action="updates">版本与 What's New</button></div>${formError()}`;
    }catch(error){errorInModal(error);}
  }
  function updateValues() {
    const u=state.update || {};
    return {current:u.current_version || u.installed_version || u.current || u.version || '未知',latest:u.latest_version || u.latest || '',available:Boolean(u.available || u.update_available || u.status==='available'),message:u.message || u.reason || '',status:u.status || ''};
  }
  function updateIndicator() {
    const u=updateValues();$('#update-indicator').hidden=!u.available;$('#update-indicator').textContent=u.available?`更新 ${u.latest || ''}`:'版本信息';
    const seen=readStorage('ablation.workbench.seenVersion');
    if(!u.available && u.current!=='未知' && seen && seen!==u.current){$('#update-indicator').hidden=false;$('#update-indicator').textContent="What's New";state.whatsNew=true;}
    if(!seen && u.current!=='未知')try{localStorage.setItem('ablation.workbench.seenVersion',JSON.stringify(u.current));}catch{}
  }
  async function updatesModal() {
    openModal('updates',"版本与 What's New",'<p class="muted">正在读取版本信息…</p>');
    const results=await Promise.allSettled([api('/api/update'),api('/api/changelog')]);
    if(state.modal!=='updates')return;
    if(results[0].status==='fulfilled')state.update=results[0].value;
    const u=updateValues(),changes=results[1].status==='fulfilled'?results[1].value:null;
    const changelog=typeof changes==='string'?changes:changes?.changelog || changes?.text || changes?.content || '当前安装包未提供更新说明。';
    $('#modal-body').innerHTML=`<dl class="config-list"><dt>当前版本</dt><dd><strong>${esc(u.current)}</strong></dd><dt>可用版本</dt><dd>${esc(u.latest || '尚未确认')}</dd></dl><p class="dialog-explanation">${esc(u.message || (u.available?'有新版可安装。升级前需结束正在运行的实验和 Agent 任务。':u.status==='up_to_date'?'当前版本已是最新。':'暂未确认有可用更新；本地实验可继续使用。'))}</p>${results[0].status==='rejected'?`<p class="form-error">${esc(results[0].reason.message)}</p>`:''}<h3>What's New</h3><pre class="changelog">${esc(changelog)}</pre>${formError()}${formStatus()}<div class="form-actions"><button class="btn" data-action="updates">重新读取</button><button class="btn primary" data-action="upgrade" ${u.available?'':'disabled'}>升级工具</button></div><p class="small muted" style="margin-top:12px">升级完成后需重新启动 GUI。实验与评论保存在工作区。</p>`;
    if(u.current!=='未知')try{localStorage.setItem('ablation.workbench.seenVersion',JSON.stringify(u.current));}catch{}
    state.whatsNew=false;updateIndicator();
  }
  function runDetails(runId) {
    const run=state.experiment.runs.find(r=>r.id===runId);if(!run)return;
    const checks=run.checks || [];
    openModal('run-detail','运行详情',`<div class="metadata">${runBadge(run)}<span>${esc(label(state.experiment.candidates,run.candidate))} / ${esc(label(state.experiment.scenarios,run.scenario))}</span></div><p class="small muted" style="margin-top:12px">${esc(run.id)} · ${esc(run.request_hash || '')}</p>${run.error?`<div class="notice error">${esc(objectMessage(run.error))}</div>`:''}<h3>外部证据</h3>${evidenceLinks(run) || '<p class="small muted">未配置外部日志关联。</p>'}<p class="small muted">批次：${esc(run.batch_id || '—')}</p><h3>断言结果</h3>${checks.length?`<ul class="check-list">${checks.map(c=>`<li><span class="badge ${c.passed===true?'success':c.passed===false?'error':''}">${c.passed===true?'通过':c.passed===false?'未通过':'未评分'}</span> <code>${esc(json(c.check || c.assertion || c))}</code>${c.message||c.error?`<p class="small muted">${esc(c.message || objectMessage(c.error))}</p>`:''}</li>`).join('')}</ul>`:'<p class="muted small">没有断言；不能据此标记为通过。</p>'}<details class="preview-details"><summary>追踪标识与请求 header</summary><pre class="context-snippet">${esc(json({tracking:run.tracking,headers:run.request_headers,response_headers:run.response_headers}))}</pre></details><details class="preview-details"><summary>实际发送的完整请求</summary><pre class="context-snippet">${esc(json(run.request))}</pre></details><details class="preview-details" open><summary>实际响应</summary><pre class="context-snippet">${esc(run.response==null?'尚无响应':json(run.response))}</pre></details>`,true);
  }
  async function refreshJobs() { const result=await api('/api/jobs');state.jobs=result.jobs || []; }
  async function refreshSelected() {
    if(!state.experiment)return;
    const expId=state.experiment.id;
    const results=await Promise.allSettled([api(`/api/experiments/${encodeURIComponent(expId)}`),api('/api/jobs')]);
    if(expId!==state.experiment?.id)return;
    if(results[1].status==='fulfilled')state.jobs=results[1].value.jobs || [];
    if(results[0].status==='fulfilled')state.experiment=results[0].value;
    renderExperiment();await loadDiff();
  }
  async function poll() {
    if(pollRunning||document.hidden||state.busy)return;
    pollRunning=true;
    try {
      const expId=state.experiment?.id;
      const results=await Promise.allSettled([api('/api/jobs'),expId?api(`/api/experiments/${encodeURIComponent(expId)}`):Promise.resolve(null)]);
      if(expId!==state.experiment?.id)return;
      if(results[0].status==='fulfilled') {
        const jobs=results[0].value.jobs || [],signature=JSON.stringify(jobs);
        if(signature!==state.lastJobs){state.jobs=jobs;state.lastJobs=signature;if(state.experiment){renderJobs();if(!editing())renderRuns();else state.deferred=true;}}
      }
      if(results[1].status==='fulfilled'&&results[1].value&&results[1].value.revision!==state.experiment?.revision){state.experiment=results[1].value;if(editing())state.deferred=true;else{renderExperiment();await loadDiff();}}
    } finally {pollRunning=false;}
  }
  async function createDemo(button) {
    if(button)button.disabled=true;
    try{const e=await api('/api/demo',{});closeModal();await selectExperiment(e.id);toast('已创建合成示例；未调用模型，响应页会明确显示未运行。');}
    catch(error){if(state.modal)errorInModal(error);else globalStatus(error.message);}finally{if(button)button.disabled=false;}
  }

  document.addEventListener('click', async event => {
    const button=event.target.closest('button');if(!button||button.disabled)return;
    const action=button.dataset.action;
    try {
      if(button.dataset.selectExperiment){closeModal();await selectExperiment(button.dataset.selectExperiment);return;}
      if(button.dataset.tab){switchTab(button.dataset.tab);return;}
      if(button.dataset.artifact){$('#more-evidence').open=false;state.artifact=button.dataset.artifact;state.expanded=false;renderExperiment();await loadDiff();return;}
      if(button.dataset.layout){state.layout=button.dataset.layout;persist();$$('[data-layout]').forEach(el=>el.setAttribute('aria-pressed',String(el===button)));renderDiff();return;}
      if(button.hasAttribute('data-plus')){openComment(Number(button.dataset.plus));return;}
      if(button.dataset.editComment){editComment(button.dataset.group,button.dataset.editComment);return;}
      if(button.dataset.deleteComment){deleteComment(button.dataset.group,button.dataset.deleteComment);return;}
      if(button.dataset.copyReview){await copyPersistedReview(button.dataset.copyReview);return;}
      if(button.dataset.dispatchReview){await dispatchReview(button.dataset.dispatchReview,button);return;}
      if(button.dataset.followupReview){followupReview(button.dataset.followupReview,Number(button.dataset.commentIndex));return;}
      if(button.dataset.jumpReview){const review=state.experiment.reviews.find(r=>r.id===button.dataset.jumpReview),comment=review?.comments?.[Number(button.dataset.commentIndex)];if(review&&comment){state.base=review.base;state.candidate=review.candidate;state.scenario=review.scenario;state.artifact=comment.artifact;state.tab='diff';renderExperiment();await loadDiff();toast('已打开该比较；审阅时的精确原文保留在 Discussion 中。');}return;}
      if(button.dataset.replyReview){const form=$$('[data-reply-form]').find(f=>f.dataset.replyForm===button.dataset.replyReview);form.hidden=!form.hidden;state.replyOpen=form.hidden?null:button.dataset.replyReview;if(!form.hidden)$('textarea',form).focus();return;}
      if(button.dataset.cancelJob){button.disabled=true;const job=await api(`/api/jobs/${encodeURIComponent(button.dataset.cancelJob)}/cancel`,{});toast(`停止请求已提交：${jobStatusLabel(job)}。正在进行的网络请求可能需等待返回。`);await refreshJobs();renderJobs();renderRuns();return;}
      if(button.dataset.runDetail){runDetails(button.dataset.runDetail);return;}
      switch(action) {
        case 'experiments': await experimentsModal();break;
        case 'import':importModal();break;
        case 'demo':await createDemo(button);break;
        case 'candidate':candidateModal();break;
        case 'scenario':scenarioModal();break;
        case 'run':runModal();break;
        case 'review':openReview();break;
        case 'settings':await settingsModal();break;
        case 'experiment-details':experimentDetails();break;
        case 'toggle-comparison':{const panel=$('#comparison-settings');panel.hidden=!panel.hidden;button.setAttribute('aria-expanded',String(!panel.hidden));break;}
        case 'updates':await updatesModal();break;
        case 'close-modal':closeModal();break;
        case 'retry-diff':await loadDiff();break;
        case 'expand-diff':state.expanded=true;renderDiff();break;
        case 'add-comment':addComment();break;
        case 'cancel-comment':state.editor=null;persist();renderDiff();break;
        case 'copy-pending':await savePending(false);break;
        case 'dispatch-pending':await savePending(true);break;
        case 'copy-portable': {const area=$('#portable-review');try{await navigator.clipboard.writeText(area.value);toast('已复制，可以粘贴到对话。');}catch{area.focus();area.select();toast('请使用系统复制快捷键复制已选内容。');}break;}
        case 'copy-artifact': {try{await navigator.clipboard.writeText(json(state.diff?.after) || '');toast('已复制右侧内容。');}catch{openModal('copy','复制右侧内容',`<textarea id="portable-review" class="copy-preview" readonly>${esc(json(state.diff?.after) || '')}</textarea><div class="form-actions"><button class="btn" data-action="copy-portable">复制</button></div>`);}break;}
        case 'upgrade': {setFormBusy(true);try{const result=await api('/api/upgrade',{});$('#form-status').textContent=objectMessage(result.message || result.output || result.status || result);if(result.success||result.status==='completed')$('#form-status').textContent+='\n请重新启动 GUI，再核对安装版本。';}catch(error){errorInModal(error);}finally{setFormBusy(false);}break;}
      }
    } catch(error) {if(state.modal)errorInModal(error);else toast(error.message);}
  });
  document.addEventListener('input', event => {
    const target=event.target;
    const replyForm=target.closest('[data-reply-form]');if(replyForm&&target.matches('textarea')){state.replyDrafts[state.experiment.id+'/'+replyForm.dataset.replyForm]=target.value;persist();}
    if(target.id==='inline-comment'&&state.editor){state.editor.text=target.value;persist();}
    if(target.id==='review-summary'){const group=state.groups.find(g=>g.id===state.groupId);if(group){group.summary=target.value;persist();}}
    if(target.id==='run-repeats')$('#run-estimate').textContent=`共 ${[...new Set([state.base,state.candidate])].length*Math.max(1,Number(target.value)||1)} 次模型请求。重复运行不等于统计上的稳定性证明。`;
  });
  document.addEventListener('change', async event => {
    const target=event.target;
    if(['base-select','candidate-select','scenario-select'].includes(target.id)){state[target.id==='base-select'?'base':target.id==='candidate-select'?'candidate':'scenario']=target.value;state.expanded=false;renderExperiment();await loadDiff();}
    if(target.id==='review-group'){state.groupId=target.value;renderReviewModal();}
    if(target.name==='review-intent'){const group=state.groups.find(g=>g.id===state.groupId);if(group){group.intent=target.value;persist();$('#intent-description').textContent=group.intent==='changes'?'按评论创建下一版，并运行相关验证。':'只解释原因与依据，不修改或重跑。';}}
    if(target.id==='theme-select'){if(target.value==='system')delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=target.value;try{localStorage.setItem('ablation.workbench.theme',JSON.stringify(target.value));}catch{}}
    if(target.id==='import-kind'){const kind=target.value;$('#import-url-field').hidden=kind==='bundle';$('#import-profile-field').hidden=kind==='bundle';$('#import-response-field').hidden=kind!=='request';}
    if(target.id==='import-file'&&target.files[0]){try{const file=target.files[0];if(file.size>32*1024*1024)throw new Error('文件超过 32 MB，请使用 CLI 导入。');const text=await file.text();JSON.parse(text);$('#import-json').value=text;$('#import-file-status').textContent=`已读取 ${file.name}（${Math.ceil(file.size/1024)} KB）`;}catch(error){errorInModal(error);}}
  });
  document.addEventListener('submit', async event => {
    const form=event.target;if(!form.matches('form'))return;event.preventDefault();if(state.busy)return;
    const data=new FormData(form);
    if(form.dataset.replyForm){const textarea=$('textarea',form),button=$('[type=submit]',form);button.disabled=true;try{await api(endpoint(`/reviews/${encodeURIComponent(form.dataset.replyForm)}/replies`),{text:data.get('text'),author:'user'});textarea.value='';delete state.replyDrafts[state.experiment.id+'/'+form.dataset.replyForm];state.replyOpen=null;persist();await refreshSelected();toast('回复已保存；未启动 Agent 或 replay。');}catch(error){toast(error.message);}finally{button.disabled=false;}return;}
    setFormBusy(true);
    try {
      let experiment;
      if(form.id==='import-form') {
        const kind=data.get('kind'),raw=data.get('data').trim(),url=data.get('url')?.trim();
        if(raw&&url&&kind!=='bundle')throw new Error('JSON 地址或记录 ID 与 JSON 内容请只填写一项。');
        if(kind==='bundle'){if(!raw)throw new Error('请选择或粘贴实验包 JSON。');experiment=await api('/api/import-bundle',JSON.parse(raw));}
        else {
          const source={kind};if(raw)source.data=JSON.parse(raw);else if(url)source[/^https?:\/\//i.test(url)?'url':'ref']=url;else throw new Error('请提供 JSON 数据、JSON 地址或记录 ID。');
          if(data.get('profile'))source.profile=data.get('profile');
          if(kind==='request'&&data.get('response')?.trim())source.response=JSON.parse(data.get('response'));
          experiment=await api('/api/import',{source,title:data.get('title')});
        }
        state.busy=false;closeModal();await selectExperiment(experiment.id);toast('数据已导入。可以在“更多证据”中检查原始数据和完整请求。');
      } else if(form.id==='candidate-form') {
        experiment=await api(endpoint('/candidates'),{label:data.get('label'),parent:data.get('parent'),rationale:data.get('rationale'),patch:parseArray(data.get('patch'),'JSON Patch')});
        state.experiment=experiment;state.candidate=experiment.candidates.at(-1).id;state.artifact='prompt';state.busy=false;closeModal();renderExperiment();await loadDiff();toast('候选已保存；尚未运行模型。');
      } else if(form.id==='scenario-form') {
        experiment=await api(endpoint('/scenarios'),{label:data.get('label'),description:data.get('description'),origin:'synthetic',patch:parseArray(data.get('patch'),'输入 JSON Patch'),checks:parseArray(data.get('checks'),'响应断言')});
        state.experiment=experiment;state.scenario=experiment.scenarios.at(-1).id;state.artifact='input';state.busy=false;closeModal();renderExperiment();await loadDiff();toast('人工构造场景已保存；尚未运行模型。');
      } else if(form.id==='run-form') {
        const repeats=Number(data.get('repeats'));if(!Number.isInteger(repeats)||repeats<1||repeats>10)throw new Error('运行次数需为 1 到 10 的整数。');
        const job=await api(endpoint('/run'),{candidates:[...new Set([state.base,state.candidate])],scenarios:[state.scenario],repeats,profile:data.get('profile')});
        state.busy=false;closeModal();await refreshJobs();renderJobs();renderRuns();toast(`Replay 任务已创建：${jobStatusLabel(job)}。`);
      }
    } catch(error){errorInModal(error instanceof SyntaxError?new Error('JSON 格式不正确，请检查内容后重试。'):error);}
    finally{setFormBusy(false);}
  });
  $('#modal').addEventListener('cancel',event=>{if(state.busy){event.preventDefault();return;}state.modal=null;});
  $('#modal').addEventListener('close',()=>{state.modal=null;if(state.deferred){state.deferred=false;renderExperiment();loadDiff();}});
  document.addEventListener('focusout',()=>setTimeout(()=>{if(state.deferred&&!editing()){state.deferred=false;renderExperiment();loadDiff();}},0));
  window.addEventListener('popstate',()=>{const exp=new URL(location.href).searchParams.get('experiment');if(exp)selectExperiment(exp,false);});
  window.addEventListener('beforeunload',()=>persist());
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});

  async function boot() {
    const results=await Promise.allSettled([refreshList(),api('/api/config'),api('/api/jobs'),api('/api/update')]);
    if(results[1].status==='fulfilled')state.config=results[1].value;
    if(results[2].status==='fulfilled')state.jobs=results[2].value.jobs || [];
    if(results[3].status==='fulfilled'){state.update=results[3].value;updateIndicator();}
    const requested=new URL(location.href).searchParams.get('experiment');
    if(requested)await selectExperiment(requested,false);
    else if(state.experiments.length)await selectExperiment(state.experiments[0].id);
    else $('#empty-state').hidden=false;
    if(results[0].status==='rejected')globalStatus(`无法读取工作区：${results[0].reason.message}`);
    if(state.whatsNew)await updatesModal();
    setInterval(poll,2500);
  }

function evidenceLinks(run) {
  const items=run.external_evidence || [];
  const link=(url,label)=>{
    try {
      const parsed=new URL(url);
      if (!['http:','https:'].includes(parsed.protocol) || parsed.username || parsed.password) return '';
      return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
    } catch { return ''; }
  };
  return items.map(item=>`<div class="small">${item.url?link(item.url,item.platform+' 详情'):esc(item.platform)}${item.batch_url?' · '+link(item.batch_url,'日志列表'):''}<span class="muted"> · ${esc(item.status==='available'?(item.url?'已关联':'已取得记录 ID，缺少详情链接'):item.status==='linked'?'查看入口，记录未核实':'待关联')}</span>${item.error?`<p class="muted">${esc(item.error)}</p>`:''}</div>`).join('');
}

  boot().catch(error=>globalStatus(error.message));
})();
