"""A small JSON CLI usable by people, scripts, and AI tools."""
import argparse
import json
import sys

from .store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="知识采集：网页转为含图片和来源的 Markdown")
    parser.add_argument("--data", default="data", help="个人知识目录，默认 ./data")
    parser.add_argument("--providers", help="用户配置的 API/MCP 服务、字段映射与采集路由 JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("capture", help="采集一个公开网页")
    ingest.add_argument("url")
    ingest.add_argument("--note", default="", help="收藏意图")
    ingest.add_argument("--origin", choices=["user", "discovery"], default="user")
    commands.add_parser("list", help="列出已采集来源")
    commands.add_parser("history", help="查看成功、部分完成和失败任务")
    commands.add_parser("mcp-serve", help="通过标准输入输出为 AI 提供只读知识库工具")
    read = commands.add_parser("read", help="读取来源正文与元数据")
    read.add_argument("source_id")
    read.add_argument("--version")
    search = commands.add_parser("search", help="按关键词查找；尚未接入语义检索")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    export = commands.add_parser("export", help="导出最新版本 Markdown 与图片为 ZIP")
    export.add_argument("source_id")
    export.add_argument("output")
    library_export = commands.add_parser("export-library", help="导出完整知识库、图片、Wiki与背景，不含服务令牌")
    library_export.add_argument("output")
    library_restore = commands.add_parser("restore-library", help="将完整备份恢复到新目录，恢复后定时检索默认关闭")
    library_restore.add_argument("archive")
    library_restore.add_argument("destination")
    vault = commands.add_parser("vault-sync", help="同步关联Markdown阅读镜像，可直接作为Obsidian知识库打开")
    vault.add_argument("--output", help="输出目录，默认主库内obsidian；保留用户编辑并报告冲突")
    analyze = commands.add_parser("analyze", help="使用配置的云端模型整理已采集资料")
    analyze.add_argument("source_id")
    commands.add_parser("analysis-history", help="查看 AI 整理任务及失败原因")
    analysis_read = commands.add_parser("analysis-read", help="读取 AI 整理结果与证据")
    analysis_read.add_argument("analysis_id")
    analysis_export = commands.add_parser("analysis-export", help="导出整理、原文、证据与图片")
    analysis_export.add_argument("analysis_id")
    analysis_export.add_argument("output")
    commands.add_parser("interests", help="列出关注候选和支持资料")
    feedback = commands.add_parser("interest-set", help="关注、暂停或关闭一个主题")
    feedback.add_argument("topic_id")
    feedback.add_argument("state", choices=["watching", "followed", "paused", "closed"])
    web_search = commands.add_parser("search-web", help="使用配置的 API/MCP 搜索公开资料")
    web_search.add_argument("query")
    wiki_build = commands.add_parser("wiki-build", help="跨来源综合一个主题并保留引用")
    wiki_build.add_argument("topic_id")
    wiki_read = commands.add_parser("wiki-read", help="读取 Wiki 并检查是否过时")
    wiki_read.add_argument("topic_id")
    commands.add_parser("wiki-list", help="列出已有 Wiki")
    discover = commands.add_parser("discover", help="为已关注主题检索新增资料、整理并更新 Wiki")
    discover.add_argument("topic_id")
    commands.add_parser("discovery-history", help="查看主动检索状态与失败原因")
    discovery_read = commands.add_parser("discovery-read", help="查看候选筛选与入库记录")
    discovery_read.add_argument("run_id")
    schedule = commands.add_parser("schedule-set", help="为已关注主题启用或暂停定时检索")
    schedule.add_argument("topic_id")
    schedule.add_argument("--hours", type=int, default=24)
    schedule.add_argument("--disabled", action="store_true")
    commands.add_parser("schedule-list", help="查看持久化检索计划")
    commands.add_parser("schedule-history", help="查看定时任务实际结果")
    commands.add_parser("schedule-tick", help="执行本轮到期检索（最多5个主题）")
    commands.add_parser("schedule-work", help="持续执行到期检索，Ctrl+C停止")
    fact = commands.add_parser("context-set", help="显式记录用户背景，供关联提醒引用")
    fact.add_argument("subject")
    fact.add_argument("field")
    fact.add_argument("value", nargs="?", default="")
    fact.add_argument("--unknown", action="store_true")
    fact.add_argument("--valid-days", type=int, default=30)
    fact.add_argument("--confirmed-at")
    commands.add_parser("context-list", help="查看已确认、未知或过期背景")
    alerts = commands.add_parser("alerts-check", help="从知识证据生成背景关联提醒候选")
    alerts.add_argument("source_ids", nargs="*")
    commands.add_parser("alerts-list", help="查看提醒及其当前有效性")
    alert_read = commands.add_parser("alerts-read", help="读取提醒原文与背景依据")
    alert_read.add_argument("run_id")
    alert_feedback = commands.add_parser("alerts-set", help="记录提醒已读、忽略或重新打开")
    alert_feedback.add_argument("alert_id")
    alert_feedback.add_argument("state", choices=["open", "acknowledged", "dismissed"])
    serve = commands.add_parser("serve", help="启动本机插件与微信桥收件接口")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--auto-process", action="store_true", help="采集后调用配置模型整理、更新Wiki和检查背景关联")
    args = parser.parse_args()
    try:
        if args.command == "restore-library":
            from .portable import restore_library
            print(json.dumps({"path": str(restore_library(args.archive, args.destination))}, ensure_ascii=False))
            return 0
        store = Store(args.data)
        if args.command == "vault-sync":
            from .vault_export import sync_vault
            result = sync_vault(store, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result['status'] == 'needs_review' else 0
        if args.command == "mcp-serve":
            from .knowledge_mcp import run
            run(store)
            return 0
        from .settings import Settings
        configuration = Settings(store).provider_configuration(args.providers)
        if args.command in {"analyze", "analysis-history", "analysis-read", "analysis-export", "interests", "interest-set"}:
            from .processing import Processor
            processor = Processor(store)
        match args.command:
            case "capture":
                from .providers import CaptureRouter
                result = store.ingest(args.url, args.note, args.origin,
                                      capture_fn=CaptureRouter(configuration, store=store).capture if configuration else None)
            case "list": result = store.list_sources()
            case "history": result = store.captures()
            case "read": result = store.read(args.source_id, args.version)
            case "search": result = store.search(args.query, args.limit)
            case "export": result = {"path": str(store.export(args.source_id, args.output))}
            case "export-library":
                from .portable import export_library
                result = {"path": str(export_library(store, args.output))}
            case "analyze": result = processor.analyze(args.source_id)
            case "analysis-history": result = processor.history()
            case "analysis-read": result = processor.read(args.analysis_id)
            case "analysis-export": result = {"path": str(processor.export(args.analysis_id, args.output))}
            case "interests": result = processor.interests()
            case "interest-set": result = processor.feedback(args.topic_id, args.state)
            case "search-web":
                from .providers import ConfiguredSearch, ProviderError
                if configuration is None:
                    raise ProviderError("configuration_missing", "请使用 --providers 指定搜索 API/MCP 配置")
                result = ConfiguredSearch(configuration, store=store).search(args.query)
            case "wiki-build" | "wiki-read" | "wiki-list":
                from .wiki import Wiki
                wiki = Wiki(store)
                result = wiki.build(args.topic_id) if args.command == "wiki-build" else wiki.read(args.topic_id) if args.command == "wiki-read" else wiki.list_pages()
            case "discover" | "discovery-history" | "discovery-read":
                from .discovery import Discovery
                discovery = Discovery(store, configuration)
                result = discovery.run(args.topic_id) if args.command == "discover" else discovery.read(args.run_id) if args.command == "discovery-read" else discovery.history()
            case "serve":
                from .gateway import create_server
                server = create_server(store, port=args.port, configuration=configuration if args.providers else None, auto_process=args.auto_process)
                print(json.dumps({"status": "listening", "url": f"http://127.0.0.1:{server.server_port}",
                                  "token_file": str(store.root / ".api-token")}, ensure_ascii=False), flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
                return 0
            case "schedule-set" | "schedule-list" | "schedule-history" | "schedule-tick" | "schedule-work":
                from .scheduler import Scheduler
                scheduler = Scheduler(store, configuration, configuration_loader=(
                    None if args.providers else lambda: Settings(store).provider_configuration()))
                if args.command == "schedule-work":
                    try:
                        scheduler.work(emit=lambda value: print(value, flush=True))
                    except KeyboardInterrupt:
                        pass
                    return 0
                result = (scheduler.set(args.topic_id, args.hours, not args.disabled) if args.command == "schedule-set"
                          else scheduler.list() if args.command == "schedule-list"
                          else scheduler.history() if args.command == "schedule-history"
                          else scheduler.tick())
            case "context-set" | "context-list" | "alerts-check" | "alerts-list" | "alerts-read" | "alerts-set":
                from .context_alerts import ContextAlerts
                context = ContextAlerts(store)
                if args.command == "context-set":
                    result = context.set_fact(args.subject, args.field, args.value,
                                              status="unknown" if args.unknown else "confirmed",
                                              confirmed_at=args.confirmed_at, valid_days=args.valid_days)
                elif args.command == "context-list": result = context.list_facts()
                elif args.command == "alerts-check": result = context.analyze(args.source_ids or None)
                elif args.command == "alerts-list": result = context.list_alerts()
                elif args.command == "alerts-set": result = context.feedback(args.alert_id, args.state)
                else: result = context.read(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("status") in {"partial", "needs_review"} else 0
    except Exception as exc:
        from .capture import CaptureError
        from .llm import LLMError
        from .processing import AnalysisError
        from .wiki import WikiError
        from .connectors import ConnectorError
        from .context_alerts import ContextError
        message = str(exc) if isinstance(exc, (ValueError, CaptureError, LLMError, AnalysisError, WikiError, ConnectorError, ContextError, FileExistsError)) else "操作失败，请检查网络、磁盘或依赖配置"
        print(json.dumps({"status": "failed", "code": getattr(exc, "code", "operation_failed"), "error": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
