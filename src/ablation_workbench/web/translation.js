/* One translation action, with a third column aligned to the original diff rows. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const chineseSegmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter('zh',{granularity:'word'}) : null;
  function inlineChanges(before, after, chinese = false) {
    const tokens = text => {
      const value=String(text ?? '');
      if(chinese)return chineseSegmenter ? Array.from(chineseSegmenter.segment(value),part=>part.segment) : Array.from(value);
      return value.match(/\s+|[\p{L}\p{N}_]+|[^\s]/gu) || [];
    };
    const left=tokens(before),right=tokens(after);
    const render=(values,changed) => {
      let html='',marked=false;
      values.forEach((value,index)=>{
        const next=changed.has(index);
        if(next!==marked){html+=next?'<mark class="inline-change">':'</mark>';marked=next;}
        html+=esc(value);
      });
      return html+(marked?'</mark>':'');
    };
    const oldChanges=new Set(),newChanges=new Set();
    if(left.length*right.length>200000) {
      let start=0,end=0;
      while(start<Math.min(left.length,right.length)&&left[start]===right[start])start++;
      while(end<Math.min(left.length,right.length)-start&&left[left.length-end-1]===right[right.length-end-1])end++;
      for(let i=start;i<left.length-end;i++)oldChanges.add(i);
      for(let i=start;i<right.length-end;i++)newChanges.add(i);
    } else {
      const table=Array.from({length:left.length+1},()=>new Uint32Array(right.length+1));
      for(let i=left.length-1;i>=0;i--)for(let j=right.length-1;j>=0;j--)table[i][j]=left[i]===right[j]?table[i+1][j+1]+1:Math.max(table[i+1][j],table[i][j+1]);
      let i=0,j=0;
      while(i<left.length||j<right.length) {
        if(i<left.length&&j<right.length&&left[i]===right[j]){i++;j++;}
        else if(i<left.length&&(j===right.length||table[i+1][j]>=table[i][j+1]))oldChanges.add(i++);
        else newChanges.add(j++);
      }
    }
    return [render(left,oldChanges),render(right,newChanges)];
  }
  window.AblationInlineChanges = inlineChanges;
  async function readTranslationStream(path, body, onEvent) {
    const response = await fetch('/ablation' + path, {
      method:'POST', headers:{Accept:'text/event-stream','Content-Type':'application/json','X-Ablation-Client':'local'}, body:JSON.stringify(body),
    });
    if (!response.ok) {
      const value = await response.json();
      if (response.status === 404 && value.error?.code === 'not_found' && value.error?.message === 'Unknown experiment route' && path.endsWith('/stream')) {
        const legacy = await fetch('/ablation' + path.slice(0,-7), {
          method:'POST', headers:{Accept:'application/json','Content-Type':'application/json','X-Ablation-Client':'local'}, body:JSON.stringify(body),
        });
        const session = await legacy.json();
        if (!legacy.ok) throw new Error(session.error?.message || `翻译请求失败（HTTP ${legacy.status}）`);
        onEvent({type:'complete', session});
        return;
      }
      throw new Error(value.error?.message || `翻译请求失败（HTTP ${response.status}）`);
    }
    if (!response.body || !response.headers.get('Content-Type')?.includes('text/event-stream')) throw new Error('翻译服务未返回流式结果。');
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '', terminal = false;
    try {
      while (!terminal) {
        const {value,done} = await reader.read();
        buffer += decoder.decode(value, {stream:!done});
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0,boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (!data) continue;
          const event = JSON.parse(data);
          onEvent(event);
          if (['complete','error'].includes(event.type)) { terminal = true; break; }
        }
        if (done) break;
      }
      if (!terminal) throw new Error('翻译连接中断，已完成的段落已保存，请点击翻译继续。');
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  window.readTranslationStream = readTranslationStream;
  window.AblationTranslation = function ({api, onChange, stream = readTranslationStream}) {
    let context = null, signature = '', session = null, visible = false, loading = false, pending = false, error = '', timer = null, operation = 0;
    let drafts = new Map();
    const busy = () => pending || Boolean(session?.busy);
    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(() => fetchSession(), 1500);
    }
    async function fetchSession() {
      const current = context, expected = operation;
      if (!current) return;
      try {
        const value = await api(`${current.endpoint}?${new URLSearchParams(current.selection)}`);
        if (current !== context || expected !== operation) return;
        session = value; drafts = new Map((value.drafts || []).map(row => [row.id,row])); loading = false;
        if (value.busy || value.translations.length === value.rows.length) error = '';
        if (value.translations.length || value.busy) visible = true;
        onChange();
        if (value.busy || pending) schedule();
      } catch (failure) {
        if (current !== context || expected !== operation) return;
        loading = false; error = failure.message; onChange();
        if(busy())schedule();
      }
    }
    async function translate() {
      if (!context || loading || busy()) return;
      if (!session) { error = ''; loading = true; onChange(); await fetchSession(); if (!session) return; }
      const current = context, currentSession = session;
      visible = true; error = ''; onChange();
      if (session.translations.length === session.rows.length) return;
      operation++; pending = true; onChange({progress:true, ids:[]});
      let interrupted = false;
      try {
        await stream(current.endpoint + '/stream', {...current.selection, session_id:currentSession.id}, event => {
          if (current !== context) return;
          const ids = [];
          if (event.type === 'start') {
            session = event.session;
          } else if (event.type === 'partial') {
            for (const row of event.rows) { drafts.set(row.id,row); ids.push(row.id); }
          } else if (event.type === 'rows') {
            const completed = new Map(session.translations.map(row => [row.id,row]));
            for (const row of event.rows) { completed.set(row.id,row); drafts.delete(row.id); ids.push(row.id); }
            session.translations = [...completed.values()];
          } else if (event.type === 'task') {
            const task = session.tasks?.find(task => task.id === event.task_id);
            if (task) task.status = event.status;
          } else if (event.type === 'task_error') {
            for (const id of event.row_ids) { drafts.delete(id); ids.push(id); }
            error = event.message;
          } else if (event.type === 'complete') {
            session = event.session; drafts.clear();
          } else if (event.type === 'error') {
            error = event.message; drafts.clear();
            if (session) session.busy = false;
          }
          onChange({progress:true, ids});
        });
      } catch (failure) {
        if (current !== context) return;
        error = failure.message; interrupted = true;
      } finally {
        if (current === context) {
          operation++; pending = false; if(session)session.busy = false; drafts.clear(); clearTimeout(timer);
          onChange({progress:true, ids:(session?.rows || []).map(row => row.id)});
          if(interrupted)await fetchSession();
        }
      }
    }
    document.addEventListener('click', event => {
      const button = event.target.closest('[data-action="translate-diff"]');
      if (button && !button.disabled) translate();
    });
    return {
      sync(value) {
        const next = value ? JSON.stringify([value.endpoint, value.selection, value.diff.rows]) : '';
        if (next === signature) return;
        context = value; signature = next; session = null; drafts.clear(); visible = false; pending = false; error = ''; loading = Boolean(value); operation++; clearTimeout(timer);
        if (value) fetchSession();
      },
      toolbar() {
        if (!context) return '';
        const tasks = session?.tasks || [], completed = tasks.filter(task => task.status === 'completed').length;
        const label = busy() ? tasks.length ? `翻译中 ${completed}/${tasks.length} 段` : '翻译中…' : '翻译';
        return `<span class="translation-tools"><button class="text-btn" data-action="translate-diff" ${loading || busy() || session?.rows.length === 0 ? 'disabled' : ''}>${label}</button>${error ? `<span class="translation-error" role="alert">${esc(error)}</span>` : ''}</span>`;
      },
      columnVisible() { return visible; },
      cellMarkup(index, same) {
        if (!visible) return '';
        if (same) return '<div class="diff-translation"></div>';
        const completed = session?.translations.find(row => row.id === `row_${index}`);
        const translated = completed || drafts.get(`row_${index}`);
        if (!translated) return '<div class="diff-translation"></div>';
        const [beforeHtml,afterHtml] = completed ? inlineChanges(translated.before,translated.after,true) : [esc(translated.before),esc(translated.after)];
        return `<div class="diff-translation">${translated.before ? `<div class="translation-before"><span aria-hidden="true">−</span><span>${beforeHtml}</span></div>` : ''}${translated.after ? `<div class="translation-after"><span aria-hidden="true">+</span><span>${afterHtml}</span></div>` : ''}</div>`;
      },
    };
  };
})();
