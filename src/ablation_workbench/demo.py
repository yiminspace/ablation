"""Synthetic examples that require no accounts or model calls."""
from __future__ import annotations


def create_demo(workbench):
    request = {
        "model": "example-model",
        "instructions": "Report the result of the originally requested export format.",
        "input": [
            {"role": "user", "content": "Export the project as CSV."},
            {"role": "assistant", "content": "CSV is unavailable. Would PDF work?"},
            {"role": "user", "content": "Yes, PDF is fine."},
            {"role": "user", "content": "Export report: format=PDF; file_created=true; verification=confirmed."},
        ],
        "tools": [{"type": "function", "name": "report_result", "description": "Report whether the requested task was completed.", "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["success", "failed", "unknown"]}, "summary": {"type": "string"}}, "required": ["status", "summary"], "additionalProperties": False}}],
    }
    exp = workbench.import_source({"kind": "request", "data": request}, title="导出结果判断 · 人工构造演示")
    with workbench.store.lock(exp["id"]):
        exp["source"].update(synthetic=True, fidelity="synthetic")
        exp["source"]["notes"].append("全部为人工构造示例，没有引用真实用户日志，也没有预设模型运行结果。")
        exp["scenarios"][0].update(label="用户同意 PDF，文件生成已确认", origin="synthetic", description="演示中的原始输入：用户从 CSV 改为接受 PDF，随后收到了文件生成成功的确认。没有调用模型。")
        workbench.store.save(exp)
    exp = workbench.add_candidate(exp["id"], label="旧候选：针对 PDF 的写法（已放弃）", patch=[{"op": "replace", "path": "/instructions", "value": "When a PDF export exists, call report_result with status success."}], rationale="这版把 PDF 和成功工具调用写死，可能只碰巧通过当前例子，因此保留为过拟合反例。")
    exp = workbench.add_candidate(exp["id"], label="按用户确认后的任务和证据判断", patch=[{"op": "replace", "path": "/instructions", "value": "Evaluate completion against the task as resolved through authorized user choices. Apply each choice only within its stated scope, retain other requirements, and distinguish confirmed effects from unresolved outcomes."}], rationale="用户同意 PDF 后应据此判断完成情况，同时保留其他要求，并在缺少执行证据时说明结果尚未确认。")
    exp = workbench.add_scenario(exp["id"], label="文件生成结果未确认", patch=[{"op": "replace", "path": "/input/3/content", "value": "Export report: format=PDF; file_created=unknown; verification=unavailable. No file result was received."}], description="人工构造边界场景：只把最后一条报告的 file_created 从 true 改为 unknown，verification 从 confirmed 改为 unavailable；用户请求和同意 PDF 的内容不变。它表示没有收到文件结果，不能据此说失败。原版和候选都需在此输入下运行，当前尚未验证。", origin="synthetic", checks=[{"kind": "tool_argument_equals", "name": "report_result", "pointer": "/status", "value": "unknown"}])
    return exp
