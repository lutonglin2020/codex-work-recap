"""Opt-in model check using synthetic records; never writes the desktop cache."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recap.adapter import CodexAdapter, source_from_turn
from recap.engine import Engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow one real Codex model call.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("This check requires --live and uses the signed-in Codex model.")
    now = time.time()
    milestones = [
        "订单筛选条件接通", "导出字段统一", "日期格式修正", "空结果提示补齐",
        "文件名加入日期", "导出按钮接通", "重复点击处理", "中文内容显示正常",
        "大量订单导出检查通过", "取消导出处理完成", "演示包生成", "同事已收到演示包并能导出文件",
    ]
    sources = []
    for index, result in enumerate(milestones):
        ended = now - (12-index)*240
        sources.append(source_from_turn({"id": "active-a", "name": "订单导出功能"}, {
            "id": f"a-{index}", "status": "completed", "startedAt": ended-90, "completedAt": ended,
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "继续把订单导出功能做完，让同事能用起来。"}]},
                {"type": "agentMessage", "phase": "final_answer", "text": f"{result}。本轮相关实现与检查已经完成。" + "字段、交互和文件保存的修改均已落实。"*70 + f"最终结果：{result}。"},
            ]}, now))
    ended = now-3000
    sources.append(source_from_turn({"id": "quiet-b", "name": "Python字典用法咨询"}, {
        "id": "b-1", "status": "completed", "startedAt": ended-60, "completedAt": ended,
        "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "顺便问一下，Python字典的get方法怎么用？"}]},
            {"type": "agentMessage", "phase": "final_answer", "text": "只是解释语法，没有修改文件或完成项目工作。" + "get方法可以提供默认返回值。"*250 + "这次仅为普通用法咨询，没有实际交付成果。"},
        ]}, now))
    with tempfile.TemporaryDirectory(prefix="recap-focus-eval-") as temp:
        adapter = CodexAdapter(temp)
        engine = Engine(temp, adapter=adapter, clock=lambda: now)
        try:
            selected, truncated = engine._input(sources, sources, window_start=now-3600, window_end=now, kind="recent")
            context = engine._model_context("recent", now-3600, now, sources, sources, sources, now, "synthetic_validation")
            result = adapter.summarize(selected, "最近60分钟", context=context)
            amounts = {thread: sum(len(s["text"]) for s in selected if s["thread_id"] == thread)
                       for thread in {s["thread_id"] for s in selected}}
            report = {"synthetic": True, "production_data_modified": False,
                      "scenario": "Sustained completed work in A; one older, verbose ordinary question in B.",
                      "selected_text_characters": amounts, "selected_count": len(selected), "truncated": truncated,
                      "activity": [{"thread": s["thread_id"], "activity": s.get("activity")} for s in selected],
                      "result": result, "diagnostics": adapter.last_run_diagnostics}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            print(json.dumps({"synthetic": True, "selected_text_characters": amounts,
                              "overview": result["overview"], "should_surface": result["should_surface"],
                              "refs": [r for i in result["work_items"] for r in i["source_ids"]]}, ensure_ascii=False))
        finally:
            engine.close()


if __name__ == "__main__":
    main()
