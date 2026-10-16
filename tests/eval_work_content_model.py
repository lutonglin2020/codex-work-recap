"""Opt-in recap policy checks using synthetic sources, outside the desktop cache."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recap.adapter import CodexAdapter, source_from_turn


SCENARIOS = {
    "learning": {
        "title": "学习加工流程并讨论重构方案",
        "request": "继续讲孔预处理，再看看是否可以抽成独立的只读查询模块。",
        "answer": "今天继续学习孔预处理：加工说明提供孔的要求，几何提供位置与实际深度，两者匹配后才形成工序。随后分析了现有提取器与启动器的依赖，提出直接封装和独立抽取两种方案；仍在比较取舍，尚未决定或实施。没有修改代码。",
        "surface": True,
        "in_progress": True,
    },
    "diagnosis": {
        "title": "排查导出失败",
        "request": "先查清导出为什么失败，不要改代码。",
        "answer": "日志与导出输入对照确认：部分记录缺少日期字段，格式化时访问该字段导致失败。问题已经定位，本轮没有实施修复，导出故障仍未解决。",
        "surface": True,
        "status": "in_progress",
    },
    "repeat": {
        "title": "重复确认加工流程",
        "request": "所以仍然是加工说明给要求、几何给实际对象，对吧？",
        "answer": "对，和上一轮结论相同：加工说明提供孔的要求，几何提供位置与实际深度，两者匹配后才形成工序。本轮没有新增判断、发现或方案。",
        "previous": "正在学习孔预处理，已讲清加工说明与孔几何如何共同决定工序。",
        "surface": False,
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow real calls with the signed-in Codex model.")
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("This check requires --live and uses the signed-in Codex model.")
    now = time.time()
    reports = []
    with tempfile.TemporaryDirectory(prefix="recap-work-content-eval-") as temp:
        adapter = CodexAdapter(temp)
        try:
            for name in args.scenario or SCENARIOS:
                scenario = SCENARIOS[name]
                source = source_from_turn({"id": f"synthetic-{name}", "name": scenario["title"]}, {
                    "id": "turn-1", "status": "completed", "startedAt": now-480, "completedAt": now-120,
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": scenario["request"]}]},
                        {"type": "agentMessage", "phase": "final_answer", "text": scenario["answer"]},
                    ],
                }, now)
                source["activity"] = {"thread_share": 1.0, "thread_turn_count": 1, "is_new": True}
                context = {"window_kind": "recent", "evaluation_reason": "synthetic_validation",
                           "work_span_minutes": 6, "user_idle_seconds": 0}
                if scenario.get("previous"):
                    context["recent_overviews"] = [{"text": scenario["previous"], "not_source": True}]
                result = adapter.summarize([source], "最近60分钟", context=context)
                checks = {"surface": result["should_surface"] is scenario["surface"]}
                if scenario.get("in_progress"):
                    checks["unfinished_work_preserved"] = any(
                        item["status"] == "in_progress" for item in result["work_items"])
                if scenario.get("status"):
                    checks["accurate_status"] = bool(result["work_items"]) and all(
                        item["status"] == scenario["status"] for item in result["work_items"])
                report = {"scenario": name, "checks": checks, "passed": all(checks.values()),
                          "result": result, "diagnostics": dict(adapter.last_run_diagnostics)}
                reports.append(report)
                print(json.dumps({"scenario": name, "passed": report["passed"],
                                  "overview": result["overview"], "should_surface": result["should_surface"],
                                  "statuses": [item["status"] for item in result["work_items"]]}, ensure_ascii=False), flush=True)
        finally:
            adapter.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"synthetic": True, "production_data_modified": False,
                                     "passed": all(report["passed"] for report in reports),
                                     "scenarios": reports}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    if not all(report["passed"] for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
