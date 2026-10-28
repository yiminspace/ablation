/* Describe saved assertion evidence without re-evaluating or changing it. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AblationRunResults = api;
})(globalThis, function () {
  'use strict';
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const format = value => value === undefined ? '未记录' : JSON.stringify(value);
  const message = value => typeof value === 'string' ? value : value?.message || (value == null ? '' : format(value));
  const checkData = value => {
    const record = value && typeof value === 'object' ? value : {};
    const definition = record.check || record.assertion;
    return definition && typeof definition === 'object' ? {...definition, ...record} : record;
  };
  function checkSummary(record) {
    const check = checkData(record), passed = typeof check.passed === 'boolean' ? check.passed : null;
    const errors = [check.error, ...(Array.isArray(check.errors) ? check.errors : [])].map(message).filter(Boolean);
    const result = {
      title: check.label || check.title || '断言', passed,
      expected: own(check, 'value') ? format(check.value) : '见断言定义',
      actual: own(check, 'actual') ? format(check.actual) : '未记录',
      reason: passed === true ? '符合预期。' : passed === null ? '尚未评分。' : message(check.message) || errors.join('；') || '实际结果不符合断言要求。',
      toolName: typeof check.name === 'string' ? check.name : '',
      pointer: typeof check.pointer === 'string' ? check.pointer : '',
      kind: check.kind || '', raw: record,
    };
    if (check.kind === 'tool_argument_equals') {
      const values = Array.isArray(check.actual) ? check.actual : own(check, 'actual') ? [check.actual] : null;
      const releaseAction = check.name === 'cu_release_app' && check.pointer === '/action';
      const actionLabel = value => releaseAction && typeof value === 'string' && own({park:1,handoff:1,discard:1}, value)
        ? `${value}（${{park:'后台保留结果',handoff:'主动切到前台',discard:'清理应用会话'}[value]}）`
        : format(value);
      result.title = releaseAction ? '收尾动作' : `${check.name || '工具'} 的参数 ${check.pointer || '（根节点）'}`;
      result.expected = own(check, 'value') ? actionLabel(check.value) : '未记录';
      result.actual = values === null ? '未记录' : values.length ? values.map(actionLabel).join('、') : errors.length ? '未取得可比较的参数值' : '未调用目标工具';
      if (passed === false) {
        if (values === null) result.reason = errors.length ? errors.join('；') : '记录未提供实际参数值，无法说明具体差异。';
        else if (!values.length && !errors.length) result.reason = `模型没有调用 ${check.name || '目标工具'}。`;
        else if (!values.length) result.reason = '工具参数不是有效 JSON，或不包含指定路径，无法取得待检查的值。';
        else result.reason = `期望 ${result.expected}，模型实际返回 ${result.actual}。${errors.length ? '另有工具参数不是有效 JSON 或缺少指定路径。' : ''}`;
      }
    } else if (check.kind === 'tool_called') {
      const values = Array.isArray(check.actual) ? check.actual : null;
      result.title = '工具调用';
      result.expected = `调用 ${check.name || '指定工具'}`;
      result.actual = values === null ? '未记录' : values.length ? values.join('、') : '没有工具调用';
      if (passed === false) result.reason = `模型没有调用 ${check.name || '指定工具'}。`;
    } else if (check.kind === 'text_contains') {
      result.title = '回复包含指定文本';
      result.expected = `包含 ${format(check.value)}`;
      if (passed === false) result.reason = own(check, 'actual') ? '模型回复未包含期望文本。' : '记录未提供实际回复文本。';
    } else if (check.kind === 'json_equals') {
      result.title = `响应字段 ${check.pointer || '（根节点）'}`;
      if (passed === false) {
        result.reason = own(check, 'actual') ? `响应字段 ${check.pointer || '（根节点）'} 的实际值与期望值不同。` : errors.length ? '响应中不存在指定的 JSON 路径。' : '记录未提供该响应字段的实际值。';
        if (!own(check, 'actual') && errors.length) result.actual = '路径不存在';
      }
    }
    return result;
  }
  function runFailures(run = {}) {
    if (run.error || ['error', 'failed', 'timed_out', 'interrupted'].includes(run.status)) {
      return [{source:'execution', title:'回放执行未完成', passed:null, expected:'取得完整的模型响应',
        actual:message(run.error) || ({timed_out:'请求超时',interrupted:'回放已中断'}[run.status]) || '请求执行失败',
        reason:message(run.error) || ({timed_out:'请求超时，未取得确认完成的模型响应。',interrupted:'回放已中断，未取得确认完成的模型响应。'}[run.status]) || '请求执行失败，无法判断模型行为是否符合预期。', toolName:'', pointer:'', index:null}];
    }
    const failed = (Array.isArray(run.checks) ? run.checks : []).map((check,index) => ({...checkSummary(check), source:'check', index})).filter(check => check.passed === false);
    if (!failed.length && run.passed === false) return [{source:'check', title:'断言未通过', passed:false, expected:'满足本次断言', actual:'未记录具体差异', reason:'记录标记为未通过，但没有保存具体失败断言。', toolName:'', pointer:'', index:null}];
    return failed;
  }
  function failureSummary(run) {
    const failed = runFailures(run);
    return failed.length ? `${failed[0].reason}${failed.length > 1 ? `另有 ${failed.length - 1} 项未通过。` : ''}` : '';
  }
  function renderCheck(check, index) {
    const summary = checkSummary(check), status = summary.passed === true ? '通过' : summary.passed === false ? '未通过' : '未评分';
    const tone = summary.passed === true ? 'success' : summary.passed === false ? 'error' : '';
    return `<details class="run-check-result"${summary.passed === false ? ' open' : ''}><summary><span class="vg-state badge neu ${tone}">${status}</span><strong>${esc(summary.title)}</strong></summary><div class="run-check-detail"><p class="run-check-reason">${esc(summary.reason)}</p><dl class="run-check-values"><dt>期望</dt><dd><pre>${esc(summary.expected)}</pre></dd><dt>实际</dt><dd><pre>${esc(summary.actual)}</pre></dd></dl><button class="text-btn" data-run-check-output="${index}" data-check-tool="${esc(summary.toolName)}">查看对应模型输出</button><details class="preview-details"><summary>原始断言记录</summary><pre>${esc(JSON.stringify(check, null, 2))}</pre></details></div></details>`;
  }
  function renderChecks(run = {}) {
    const checks = Array.isArray(run.checks) ? run.checks : [];
    const failures = runFailures(run), execution = failures.find(item => item.source === 'execution');
    const notice = execution ? `<div class="notice error"><strong>回放执行错误</strong><p>${esc(execution.reason)}</p><p class="small">未完成的请求不计为行为断言失败。</p></div>` : '';
    return `${notice}<h3>断言结果</h3>${checks.length ? `<div class="run-check-list">${checks.map((check,index) => ({check,index,order:checkSummary(check).passed === false ? 0 : 1})).sort((a,b)=>a.order-b.order || a.index-b.index).map(({check,index})=>renderCheck(check,index)).join('')}</div>` : `<p class="muted small">${execution ? '本次未产生断言结果。' : run.passed === false ? '记录标记为未通过，但没有保存具体失败断言。' : '没有断言；此回放未评分。'}</p>`}`;
  }
  return {checkSummary, runFailures, failureSummary, renderChecks};
});
