const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const list = value => Array.isArray(value) ? value : [];
const pretty = value => {
  if (typeof value !== 'string') return JSON.stringify(value, null, 2);
  try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
};

function safeImage(source) {
  if (typeof source !== 'string') return null;
  if (/^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z\d+/=\s]+$/i.test(source)) return source;
  // Remote images remain references; opening a replay must not contact their hosts.
  if (/^https?:\/\//i.test(source)) return { reference: source };
  return null;
}

function contentBlocks(value) {
  if (typeof value === 'string') return [{type:'input_text', text:pretty(value)}];
  if (!Array.isArray(value)) return value == null ? [] : [{type:'input_text', text:pretty(value)}];
  return value.filter(isRecord).flatMap(block => {
    if (['image_url','input_image','image'].includes(block.type)) {
      const source = block.image_url?.url ?? block.image_url ?? block.url ??
        (block.source?.type === 'base64' ? `data:${block.source.media_type};base64,${block.source.data}` : block.source?.url);
      const image = safeImage(source);
      return typeof image === 'string' ? [{type:'input_image', image_url:image}] :
        image?.reference ? [{type:'input_text', text:`图片引用：${image.reference}`}]:
        [{type:'input_text', text:'图片格式无法预览；原始记录保留在完整详情中。'}];
    }
    if (typeof block.text === 'string') return [{...block, type:block.type === 'text' ? 'input_text' : block.type, text:pretty(block.text)}];
    if (typeof block.refusal === 'string') return [{type:'refusal', refusal:block.refusal}];
    return [{type:'input_text', text:pretty(block)}];
  });
}

function normalizeItem(item) {
  if (typeof item === 'string') item = {role:'user', content:item};
  if (!isRecord(item)) return [];
  const msg = {...item};
  if (msg.type === 'function_call_output' || msg.role === 'tool') {
    msg.type = 'function_call_output';
    msg.call_id ??= msg.tool_call_id;
    const value = msg.output ?? msg.content;
    msg.content = contentBlocks(value);
    delete msg.output;
    delete msg.tool_call_id;
  } else if (msg.type === 'reasoning') {
    msg.summary = list(msg.summary).filter(isRecord);
  } else if ('content' in msg) {
    msg.content = contentBlocks(msg.content);
  }
  if (msg.arguments !== undefined) msg.arguments = pretty(msg.arguments);
  const calls = list(msg.tool_calls).filter(isRecord).map(call => ({
    type:'function_call', call_id:call.id, name:call.function?.name ?? call.name,
    arguments:pretty(call.function?.arguments ?? call.arguments ?? {}),
  }));
  delete msg.tool_calls;
  const rows = [];
  if (msg.reasoning || msg.reasoning_content) rows.push({type:'reasoning', content:String(msg.reasoning ?? msg.reasoning_content), summary:[]});
  delete msg.reasoning;
  delete msg.reasoning_content;
  if (msg.type || !calls.length || msg.content?.length) rows.push(msg);
  return [...rows, ...calls];
}

function fromRun(run) {
  const request = run.request ?? {}, response = run.response ?? {};
  const input = [];
  if (request.instructions) input.push({role:'system', content:String(request.instructions)});
  const rawInput = request.input ?? request.messages ?? [];
  input.push(...(Array.isArray(rawInput) ? rawInput : [rawInput]));
  const output = Array.isArray(response.output) ? response.output :
    list(response.choices).flatMap(choice => choice.message ? [{role:'assistant', ...choice.message}] : []) ;
  const rows = [
    ...input.flatMap(normalizeItem).map(msg => ({msg, source:'input'})),
    ...output.flatMap(normalizeItem).map(msg => ({msg, source:'output'})),
  ];
  const calls = new Map(rows.filter(row => row.msg.type === 'function_call' && row.msg.call_id).map(row => [row.msg.call_id, row.msg.name]));
  return rows.map((row, index) => ({...row, key:`${row.source}-${index}`, callName:calls.get(row.msg.call_id)}));
}

function runInfo(run) {
  const response = run.response ?? {}, usage = response.usage ?? {};
  const input = usage.input_tokens ?? usage.prompt_tokens;
  const output = usage.output_tokens ?? usage.completion_tokens;
  const total = usage.total_tokens ?? (Number.isFinite(input) && Number.isFinite(output) ? input + output : null);
  return {model:response.model ?? run.request?.model, tokens:Number.isFinite(total) ? total : null};
}

module.exports = {fromRun, runInfo, safeImage};
