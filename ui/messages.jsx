import {createRoot} from 'react-dom/client';
import {useCallback, useLayoutEffect, useRef} from 'react';
import {ResponsesItemRow} from './vendor/bifrost/responsesItemRow';
import {fromRun, runInfo} from './messages-data.cjs';
import './vendor/bifrost/messages.css';

const roots = new Map();

function Messages({run, onReady, initialScrollTop, targetTool}) {
  const rows = fromRun(run);
  const outputStart = rows.findIndex(row => row.source === 'output');
  const resultIndex = rows.findIndex(row => row.source === 'output' && row.msg.type !== 'reasoning');
  const keyIndex = resultIndex < 0 ? outputStart : resultIndex;
  const viewport = useRef(null), keyMessage = useRef(null);
  const followingOutput = useRef(true);
  const scrollToOutput = useCallback(() => {
    const area = viewport.current, target = keyMessage.current;
    if (!area || !target) return;
    const top = area.scrollTop + target.getBoundingClientRect().top - area.getBoundingClientRect().top - 16;
    area.scrollTo({top, behavior:'auto'});
  }, []);
  const locateOutput = useCallback(() => {
    followingOutput.current = true;
    scrollToOutput();
  }, [scrollToOutput]);
  const keepReadingPosition = () => { followingOutput.current = false; };
  useLayoutEffect(() => {
    onReady(locateOutput);
    const target = targetTool && [...viewport.current.querySelectorAll('[data-message-source="output"]')].find(row => row.dataset.messageTool === targetTool);
    if (target) {
      followingOutput.current = false;
      viewport.current.scrollTop = target.offsetTop - viewport.current.offsetTop - 16;
    } else if (Number.isFinite(initialScrollTop)) {
      followingOutput.current = false;
      viewport.current.scrollTop = initialScrollTop;
    } else locateOutput();
    const observer = new ResizeObserver(() => {
      if (followingOutput.current) scrollToOutput();
    });
    observer.observe(viewport.current.firstElementChild);
    return () => { observer.disconnect(); onReady(null); };
  }, [locateOutput, scrollToOutput, onReady, initialScrollTop, targetTool]);
  return <div className="bifrost-messages" aria-label="Messages 消息时间线">
    <div className="bf-section-label"><span>请求上下文与本次输出 · 回放不执行工具</span>{keyIndex >= 0 && <button className="bf-locate-output" onClick={locateOutput}>定位本次输出</button>}</div>
    <div ref={viewport} className="bf-message-scroll" role="region" aria-label="回放消息内容" tabIndex={0} onWheel={keepReadingPosition} onPointerDown={keepReadingPosition} onTouchStart={keepReadingPosition} onKeyDown={keepReadingPosition}>
      <div className="bf-message-content">
        {rows.length ? rows.map((row, index) => <div key={row.key} ref={index === keyIndex ? keyMessage : null} className={index === keyIndex ? 'bf-key-message' : undefined} data-message-source={row.source} data-message-tool={row.msg.name || row.callName}>
          {index === outputStart && <div className="bf-output-label">本次模型返回</div>}
          <ResponsesItemRow msg={row.msg} callName={row.callName} last={index === rows.length - 1} onImageOpen={src => globalThis.dispatchEvent(new CustomEvent('ablation-image-preview', {detail:{src}}))} />
        </div>) : <p className="muted">尚无已保存的消息。</p>}
      </div>
    </div>
  </div>;
}

globalThis.AblationMessages = {
  runInfo,
  mount(host, run, {focus = false, scrollTop, targetTool} = {}) {
    const existing = roots.get(host);
    if (existing) { if (focus) existing.locate?.(); return; }
    const root = createRoot(host);
    const entry = {root, locate:null};
    roots.set(host, entry);
    root.render(<Messages run={run} onReady={locate => { entry.locate = locate; }} initialScrollTop={scrollTop} targetTool={targetTool} />);
  },
  clear() {
    for (const {root} of roots.values()) root.unmount();
    roots.clear();
  },
};
