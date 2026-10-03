"""Small newline JSON-RPC MCP stdio transport with tools and an MCP App resource."""
from __future__ import annotations

import json
import sys
from .runtime import ROOT, ensure_service, request

UI_URI = "ui://work-recap/panel.html"
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def descriptors():
    def tool(name, title, description, schema=EMPTY, read_only=True, ui=False):
        result = {"name": name, "title": title, "description": description,
                  "inputSchema": schema, "annotations": {"readOnlyHint": read_only,
                  "destructiveHint": False, "openWorldHint": False}}
        if ui:
            result["_meta"] = {"ui": {"resourceUri": UI_URI},
                               "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]}}
        return result
    return [
        tool("get_recap", "工作回顾", "查看跨本地 Codex 会话的工作回顾与来源。只读缓存，不触发模型生成。", ui=True),
        tool("refresh_recap", "更新工作回顾", "手动收集并总结最近的实质工作；使用当前 Codex 账号额度。异步返回，稍后读取 get_recap。", read_only=False),
        tool("mark_recap_seen", "标记回顾已读", "仅标记指定回顾已读。",
             {"type": "object", "properties": {"summary_id": {"type": "string"}}, "required": ["summary_id"], "additionalProperties": False}, False),
        tool("set_recap_paused", "暂停或恢复工作回顾", "控制本地后台自动摘要，不影响原会话。",
             {"type": "object", "properties": {"paused": {"type": "boolean"}}, "required": ["paused"], "additionalProperties": False}, False),
    ]


def call(name, args):
    schemas = {item["name"]: item["inputSchema"] for item in descriptors()}
    if name not in schemas:
        raise ValueError("Unknown tool")
    schema = schemas[name]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]):
        raise ValueError("Invalid tool arguments")
    if any(key not in args for key in schema.get("required", [])):
        raise ValueError("Missing tool argument")
    for key, value in args.items():
        expected = schema["properties"][key]["type"]
        if expected == "boolean" and not isinstance(value, bool):
            raise ValueError("Expected boolean")
        if expected == "string" and (not isinstance(value, str) or not value or len(value) > 200):
            raise ValueError("Expected nonempty string")
    ensure_service()
    if name == "get_recap":
        value = request("/api/state")
    elif name == "refresh_recap":
        value = request("/api/action", {"action": "refresh"})
    elif name == "mark_recap_seen":
        value = request("/api/action", {"action": "seen", "summary_id": args.get("summary_id")})
    elif name == "set_recap_paused":
        if not isinstance(args.get("paused"), bool):
            raise ValueError("paused 必须是布尔值")
        value = request("/api/action", {"action": "pause" if args["paused"] else "resume"})
    else:
        raise ValueError("Unknown tool")
    return {"structuredContent": value, "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


def dispatch(method, params):
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28"):
            version = "2025-06-18"
        return {"protocolVersion": version, "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "work-recap", "version": "0.3.13", "title": "工作回顾"},
                "instructions": "工作回顾仅提供建议。get_recap 只读，refresh_recap 异步生成；不要为了轮询而反复调用模型或给原会话发消息。"}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": descriptors()}
    if method == "tools/call":
        try:
            return call(params["name"], params.get("arguments", {}))
        except Exception as error:
            return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
    if method == "resources/list":
        return {"resources": [{"uri": UI_URI, "name": "工作回顾", "mimeType": "text/html;profile=mcp-app"}]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        if params.get("uri") != UI_URI:
            raise ValueError("Unknown resource")
        return {"contents": [{"uri": UI_URI, "mimeType": "text/html;profile=mcp-app",
                              "text": (ROOT / "ui/index.html").read_text(encoding="utf-8"),
                              "_meta": {"ui": {"prefersBorder": False,
                                        "csp": {"connectDomains": [], "resourceDomains": []}}}}]}
    raise LookupError(method)


def run():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        item = None
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or not isinstance(item.get("method"), str):
                raise RuntimeError("Invalid JSON-RPC request")
            if "params" in item and not isinstance(item["params"], dict):
                raise ValueError("Expected object params")
            if "id" in item and (isinstance(item["id"], bool) or not isinstance(item["id"], (str, int, type(None)))):
                raise RuntimeError("Invalid request id")
            if "id" not in item:
                continue
            result = dispatch(item.get("method", ""), item.get("params", {}))
            reply = {"jsonrpc": "2.0", "id": item["id"], "result": result}
        except Exception as error:
            code = -32700 if isinstance(error, json.JSONDecodeError) else -32600 if isinstance(error, RuntimeError) else -32601 if isinstance(error, LookupError) else -32602
            error_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(error_id, bool) or not isinstance(error_id, (str, int, type(None))):
                error_id = None
            reply = {"jsonrpc": "2.0", "id": error_id,
                     "error": {"code": code, "message": str(error)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
