# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 主调度程序
===================================

职责：
1. 协调各模块完成股票分析流程
2. 实现低并发的线程池调度
3. 全局异常处理，确保单股失败不影响整体
4. 提供命令行入口

使用方式：
    python main.py              # 正常运行
    python main.py --debug      # 调试模式
    python main.py --dry-run    # 仅获取数据不分析

交易理念（已融入分析）：
- 严进策略：不追高，乖离率 > 5% 不买入
- 趋势交易：只做 MA5>MA10>MA20 多头排列
- 效率优先：关注筹码集中度好的股票
- 买点偏好：缩量回踩 MA5/MA10 支撑
"""
import os
from src.config import setup_env
setup_env()

# 代理配置 - 通过 USE_PROXY 环境变量控制，默认关闭
# GitHub Actions 环境自动跳过代理配置
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    # 本地开发环境，启用代理（可在 .env 中配置 PROXY_HOST 和 PROXY_PORT）
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

import argparse
import logging
import sys
import time
from datetime import datetime, timezone, timedelta, date
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional
from src.feishu_doc import FeishuDocManager

from src.config import get_config, Config
from src.notification import NotificationService
from src.core.pipeline import StockAnalysisPipeline
from src.core.market_review import run_market_review
from src.search_service import SearchService
from src.analyzer import GeminiAnalyzer
from screeners.stock_screener import StockScreener, ScreeningMode

# 配置日志格式
LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def setup_logging(debug: bool = False, log_dir: str = "./logs") -> None:
    """
    配置日志系统（同时输出到控制台和文件）
    
    Args:
        debug: 是否启用调试模式
        log_dir: 日志文件目录
    """
    level = logging.DEBUG if debug else logging.INFO
    
    # 创建日志目录
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # 日志文件路径（按日期分文件）
    today_str = datetime.now().strftime('%Y%m%d')
    log_file = log_path / f"stock_analysis_{today_str}.log"
    debug_log_file = log_path / f"stock_analysis_debug_{today_str}.log"
    
    # 创建根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # 根 logger 设为 DEBUG，由 handler 控制输出级别
    
    # Handler 1: 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(console_handler)
    
    # Handler 2: 常规日志文件（INFO 级别，10MB 轮转）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(file_handler)
    
    # Handler 3: 调试日志文件（DEBUG 级别，包含所有详细信息）
    debug_handler = RotatingFileHandler(
        debug_log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=3,
        encoding='utf-8'
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(debug_handler)
    
    # 降低第三方库的日志级别
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('sqlalchemy').setLevel(logging.WARNING)
    logging.getLogger('google').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    
    logging.info(f"日志系统初始化完成，日志目录: {log_path.absolute()}")
    logging.info(f"常规日志: {log_file}")
    logging.info(f"调试日志: {debug_log_file}")


logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='A股自选股智能分析系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python main.py                    # 正常运行
  python main.py --debug            # 调试模式
  python main.py --dry-run          # 仅获取数据，不进行 AI 分析
  python main.py --stocks 600519,000001  # 指定分析特定股票
  python main.py --no-notify        # 不发送推送通知
  python main.py --single-notify    # 启用单股推送模式（每分析完一只立即推送）
  python main.py --schedule         # 启用定时任务模式
  python main.py --market-review    # 仅运行大盘复盘
        '''
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试模式，输出详细日志'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='仅获取数据，不进行 AI 分析'
    )
    
    parser.add_argument(
        '--stocks',
        type=str,
        help='指定要分析的股票代码，逗号分隔（覆盖配置文件）'
    )
    
    parser.add_argument(
        '--no-notify',
        action='store_true',
        help='不发送推送通知'
    )
    
    parser.add_argument(
        '--single-notify',
        action='store_true',
        help='启用单股推送模式：每分析完一只股票立即推送，而不是汇总推送'
    )
    
    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help='并发线程数（默认使用配置值）'
    )
    
    parser.add_argument(
        '--schedule',
        action='store_true',
        help='启用定时任务模式，每日定时执行'
    )
    
    parser.add_argument(
        '--market-review',
        action='store_true',
        help='仅运行大盘复盘分析'
    )
    
    parser.add_argument(
        '--no-market-review',
        action='store_true',
        help='跳过大盘复盘分析'
    )
    
    parser.add_argument(
        '--webui',
        action='store_true',
        help='启动本地配置 WebUI'
    )
    
    parser.add_argument(
        '--webui-only',
        action='store_true',
        help='仅启动 WebUI 服务，不自动执行分析（通过 /analysis API 手动触发）'
    )

    parser.add_argument(
        '--screen',
        action='store_true',
        help='运行全市场选股'
    )

    parser.add_argument(
        '--screen-mode',
        type=str,
        default='full',
        choices=['tech_only', 'ai_only', 'full'],
        help='选股模式：tech_only(仅技术), ai_only(仅AI), full(完整流程)'
    )

    parser.add_argument(
        '--auto-analyze',
        action='store_true',
        help='选股后自动对选中股票进行深度分析'
    )

    parser.add_argument(
        '--force-refresh',
        action='store_true',
        help='强制刷新（忽略缓存）'
    )

    parser.add_argument(
        '--strategy-screen',
        action='store_true',
        help='使用策略选股（StockTradebyZ 战法）'
    )

    parser.add_argument(
        '--strategy',
        type=str,
        help='指定运行单个策略（如：少妇战法）'
    )

    parser.add_argument(
        '--strategy-config',
        type=str,
        default='./selector_configs.json',
        help='策略配置文件路径'
    )

    parser.add_argument(
        '--data-dir',
        type=str,
        default='./data',
        help='K线数据目录'
    )

    parser.add_argument(
        '--date',
        type=str,
        default=None,
        help='指定选股日期 YYYY-MM-DD（默认今天），支持历史日期选股'
    )
    parser.add_argument(
        '--no-context-snapshot',
        action='store_true',
        default=False,
        help='不保存分析上下文快照'
    )
    
    return parser.parse_args()


def run_stock_screening(
        config: Config,
        args: argparse.Namespace,
        notifier: NotificationService,
        target_date: Optional[date] = None  # 新增参数
) -> Optional[List]:
    """
    执行全市场选股

    Args:
        config: 配置对象
        args: 命令行参数
        notifier: 通知服务
        target_date: 目标选股日期（None表示今天）

    Returns:
        选股结果列表
    """
    logger.info("=" * 60)
    logger.info("开始执行全市场选股")
    logger.info("=" * 60)

    try:
        # 创建选股器
        screener = StockScreener(
            max_workers=args.workers or config.max_workers
        )

        # 确定选股模式
        mode_map = {
            'tech_only': ScreeningMode.TECH_ONLY,
            'ai_only': ScreeningMode.AI_ONLY,
            'full': ScreeningMode.FULL,
        }
        mode = mode_map.get(args.screen_mode, ScreeningMode.FULL)

        logger.info(f"选股模式: {mode.value}")
        logger.info(f"自动分析: {'是' if args.auto_analyze else '否'}")

        # 执行选股
        results = screener.screen_market(
            mode=mode,
            force_refresh=args.force_refresh,
            target_date=target_date  # 新增参数
        )

        if not results:
            logger.info("未选出符合条件的股票")
            if notifier.is_available():
                notifier.send("🎯 全市场选股完成\n\n今日未选出符合条件的股票。")
            return []

        # 发送选股报告
        logger.info("生成选股报告...")
        # 将 target_date 转换为字符串格式
        report_date_str = target_date.strftime('%Y-%m-%d') if target_date else None
        notifier.send_screening_report(results, save_to_file=True, report_date=report_date_str)

        # 自动分析（如果启用）
        if args.auto_analyze:
            logger.info("开始对选中股票进行深度分析...")

            # 创建分析流程
            pipeline = StockAnalysisPipeline(
                config=config,
                max_workers=args.workers or config.max_workers
            )

            # 分析选中的股票
            codes_to_analyze = [r.code for r in results]
            analysis_results = pipeline.run(
                stock_codes=codes_to_analyze,
                dry_run=False,
                send_notification=not args.no_notify
            )

            logger.info(f"深度分析完成: {len(analysis_results)} 只股票")

            return results

        return results

    except Exception as e:
        logger.exception(f"选股执行失败: {e}")
        if notifier.is_available():
            notifier.send(f"🎯 全市场选股失败\n\n错误: {str(e)[:100]}")
        return None


def run_strategy_screening(
        config: Config,
        args: argparse.Namespace,
        notifier: NotificationService
) -> Optional[List]:
    """
    执行策略选股（使用 StockTradebyZ 战法）

    Args:
        config: 配置对象
        args: 命令行参数
        notifier: 通知服务

    Returns:
        选股结果列表
    """
    logger.info("=" * 60)
    logger.info("开始执行策略选股（StockTradebyZ 战法）")
    logger.info("=" * 60)

    try:
        from screeners.strategy_screener import StrategyScreener

        # 创建策略选股器
        screener = StrategyScreener(
            data_dir=args.data_dir or "./data",
            config_file=args.strategy_config
        )

        # 执行选股
        if args.strategy:
            # 运行指定策略
            selected = screener.run_strategy(args.strategy)
            strategy_results = {args.strategy: selected}
        else:
            # 运行所有策略
            strategy_results = screener.run_all_strategies()

        # 获取所有选中的股票（去重）
        all_selected = set()
        for stocks in strategy_results.values():
            all_selected.update(stocks)

        all_selected = sorted(list(all_selected))

        if not all_selected:
            logger.info("未选出符合条件的股票")
            if notifier.is_available():
                notifier.send("🎯 策略选股完成\n\n今日未选出符合条件的股票。")
            return []

        # 生成报告
        report = screener.format_report(strategy_results)
        logger.info(f"\n{report}")

        # 保存报告
        report_dir = Path("./reports")
        report_dir.mkdir(exist_ok=True)
        report_file = report_dir / f"strategy_screening_{datetime.now().strftime('%Y%m%d')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"报告已保存: {report_file}")

        # 发送通知
        if notifier.is_available():
            # 生成精简版通知
            lines = [
                "🎯 策略选股完成",
                f"📅 {datetime.now().strftime('%Y-%m-%d')}",
                "",
                f"📊 共选出 {len(all_selected)} 只股票",
                ""
            ]

            for strategy_name, stocks in strategy_results.items():
                if stocks:
                    lines.append(f"• {strategy_name}: {len(stocks)} 只")

            lines.append("")
            lines.append(f"股票代码: {', '.join(all_selected)}")

            notifier.send("\n".join(lines))

        # 自动分析（如果启用）
        if args.auto_analyze:
            logger.info("开始对选中股票进行深度分析...")

            # 创建分析流程
            pipeline = StockAnalysisPipeline(
                config=config,
                max_workers=args.workers or config.max_workers
            )

            # 分析选中的股票
            analysis_results = pipeline.run(
                stock_codes=all_selected,
                dry_run=False,
                send_notification=not args.no_notify
            )

            logger.info(f"深度分析完成: {len(analysis_results)} 只股票")

        return all_selected

    except ImportError as e:
        logger.error(f"无法导入策略选股模块: {e}")
        logger.error("请确保已将 StockTradebyZ 的相关文件复制到项目目录")
        logger.error("参考 INTEGRATION_GUIDE.md 完成整合")
        return None
    except Exception as e:
        logger.exception(f"策略选股执行失败: {e}")
        if notifier.is_available():
            notifier.send(f"🎯 策略选股失败\n\n错误: {str(e)[:100]}")
        return None


def run_full_analysis(
    config: Config,
    args: argparse.Namespace,
    stock_codes: Optional[List[str]] = None
):
    """
    执行完整的分析流程（个股 + 大盘复盘）
    
    这是定时任务调用的主函数
    """
    try:
        # 命令行参数 --single-notify 覆盖配置（#55）
        if getattr(args, 'single_notify', False):
            config.single_stock_notify = True
        
        # 创建调度器
        save_context_snapshot = None
        if getattr(args, 'no_context_snapshot', False):
            save_context_snapshot = False
        query_id = uuid.uuid4().hex
        pipeline = StockAnalysisPipeline(
            config=config,
            max_workers=args.workers,
            query_id=query_id,
            query_source="cli",
            save_context_snapshot=save_context_snapshot
        )
        
        # 1. 运行个股分析
        results = pipeline.run(
            stock_codes=stock_codes,
            dry_run=args.dry_run,
            send_notification=not args.no_notify
        )

        # Issue #128: 分析间隔 - 在个股分析和大盘分析之间添加延迟
        analysis_delay = getattr(config, 'analysis_delay', 0)
        if analysis_delay > 0 and config.market_review_enabled and not args.no_market_review:
            logger.info(f"等待 {analysis_delay} 秒后执行大盘复盘（避免API限流）...")
            time.sleep(analysis_delay)

        # 2. 运行大盘复盘（如果启用且不是仅个股模式）
        market_report = ""
        if config.market_review_enabled and not args.no_market_review:
            # 只调用一次，并获取结果
            review_result = run_market_review(
                notifier=pipeline.notifier,
                analyzer=pipeline.analyzer,
                search_service=pipeline.search_service,
                send_notification=not args.no_notify
            )
            # 如果有结果，赋值给 market_report 用于后续飞书文档生成
            if review_result:
                market_report = review_result
        
        # 输出摘要
        if results:
            logger.info("\n===== 分析结果摘要 =====")
            for r in sorted(results, key=lambda x: x.sentiment_score, reverse=True):
                emoji = r.get_emoji()
                logger.info(
                    f"{emoji} {r.name}({r.code}): {r.operation_advice} | "
                    f"评分 {r.sentiment_score} | {r.trend_prediction}"
                )
        
        logger.info("\n任务执行完成")

        # === 新增：生成飞书云文档 ===
        try:
            feishu_doc = FeishuDocManager()
            if feishu_doc.is_configured() and (results or market_report):
                logger.info("正在创建飞书云文档...")

                # 1. 准备标题 "01-01 13:01大盘复盘"
                tz_cn = timezone(timedelta(hours=8))
                now = datetime.now(tz_cn)
                doc_title = f"{now.strftime('%Y-%m-%d %H:%M')} 大盘复盘"

                # 2. 准备内容 (拼接个股分析和大盘复盘)
                full_content = ""

                # 添加大盘复盘内容（如果有）
                if market_report:
                    full_content += f"# 📈 大盘复盘\n\n{market_report}\n\n---\n\n"

                # 添加个股决策仪表盘（使用 NotificationService 生成）
                if results:
                    dashboard_content = pipeline.notifier.generate_dashboard_report(results)
                    full_content += f"# 🚀 个股决策仪表盘\n\n{dashboard_content}"

                # 3. 创建文档
                doc_url = feishu_doc.create_daily_doc(doc_title, full_content)
                if doc_url:
                    logger.info(f"飞书云文档创建成功: {doc_url}")
                    # 可选：将文档链接也推送到群里
                    if not args.no_notify:
                        pipeline.notifier.send(f"[{now.strftime('%Y-%m-%d %H:%M')}] 复盘文档创建成功: {doc_url}")

        except Exception as e:
            logger.error(f"飞书文档生成失败: {e}")
        
    except Exception as e:
        logger.exception(f"分析流程执行失败: {e}")


def start_bot_stream_clients(config: Config) -> None:
    """Start bot stream clients when enabled in config."""
    # 启动钉钉 Stream 客户端
    if config.dingtalk_stream_enabled:
        try:
            from bot.platforms import start_dingtalk_stream_background, DINGTALK_STREAM_AVAILABLE
            if DINGTALK_STREAM_AVAILABLE:
                if start_dingtalk_stream_background():
                    logger.info("[Main] Dingtalk Stream client started in background.")
                else:
                    logger.warning("[Main] Dingtalk Stream client failed to start.")
            else:
                logger.warning("[Main] Dingtalk Stream enabled but SDK is missing.")
                logger.warning("[Main] Run: pip install dingtalk-stream")
        except Exception as exc:
            logger.error(f"[Main] Failed to start Dingtalk Stream client: {exc}")

    # 启动飞书 Stream 客户端
    if getattr(config, 'feishu_stream_enabled', False):
        try:
            from bot.platforms import start_feishu_stream_background, FEISHU_SDK_AVAILABLE
            if FEISHU_SDK_AVAILABLE:
                if start_feishu_stream_background():
                    logger.info("[Main] Feishu Stream client started in background.")
                else:
                    logger.warning("[Main] Feishu Stream client failed to start.")
            else:
                logger.warning("[Main] Feishu Stream enabled but SDK is missing.")
                logger.warning("[Main] Run: pip install lark-oapi")
        except Exception as exc:
            logger.error(f"[Main] Failed to start Feishu Stream client: {exc}")


def main() -> int:
    """
    主入口函数
    
    Returns:
        退出码（0 表示成功）
    """
    # 解析命令行参数
    args = parse_arguments()
    
    # 加载配置（在设置日志前加载，以获取日志目录）
    config = get_config()
    
    # 配置日志（输出到控制台和文件）
    setup_logging(debug=args.debug, log_dir=config.log_dir)
    
    logger.info("=" * 60)
    logger.info("A股自选股智能分析系统 启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    # 验证配置
    warnings = config.validate()
    for warning in warnings:
        logger.warning(warning)
    
    # 解析股票列表
    stock_codes = None
    if args.stocks:
        stock_codes = [code.strip() for code in args.stocks.split(',') if code.strip()]
        logger.info(f"使用命令行指定的股票列表: {stock_codes}")
    
    # === 启动 WebUI (如果启用) ===
    # 优先级: 命令行参数 > 配置文件
    start_webui = (args.webui or args.webui_only or config.webui_enabled) and os.getenv("GITHUB_ACTIONS") != "true"
    
    if start_webui:
        try:
            from webui import run_server_in_thread
            run_server_in_thread(host=config.webui_host, port=config.webui_port)
            start_bot_stream_clients(config)
        except Exception as e:
            logger.error(f"启动 WebUI 失败: {e}")
    
    # === 仅 WebUI 模式：不自动执行分析 ===
    if args.webui_only:
        logger.info("模式: 仅 WebUI 服务")
        logger.info(f"WebUI 运行中: http://{config.webui_host}:{config.webui_port}")
        logger.info("通过 /analysis?code=xxx 接口手动触发分析")
        logger.info("按 Ctrl+C 退出...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\n用户中断，程序退出")
        return 0

    try:
        # 模式1: 仅大盘复盘
        if args.market_review:
            logger.info("模式: 仅大盘复盘")
            notifier = NotificationService()
            
            # 初始化搜索服务和分析器（如果有配置）
            search_service = None
            analyzer = None
            
            if config.bocha_api_keys or config.tavily_api_keys or config.serpapi_keys:
                search_service = SearchService(
                    bocha_keys=config.bocha_api_keys,
                    tavily_keys=config.tavily_api_keys,
                    serpapi_keys=config.serpapi_keys
                )
            
            if config.gemini_api_key or config.openai_api_key:
                analyzer = GeminiAnalyzer(api_key=config.gemini_api_key)
                if not analyzer.is_available():
                    logger.warning("AI 分析器初始化后不可用，请检查 API Key 配置")
                    analyzer = None
            else:
                logger.warning("未检测到 API Key (Gemini/OpenAI)，将仅使用模板生成报告")
            
            run_market_review(
                notifier=notifier, 
                analyzer=analyzer, 
                search_service=search_service,
                send_notification=not args.no_notify
            )
            return 0
        
        # 模式2: 定时任务模式
        if args.schedule or config.schedule_enabled:
            logger.info("模式: 定时任务")
            logger.info(f"每日执行时间: {config.schedule_time}")
            
            from src.scheduler import run_with_schedule
            
            def scheduled_task():
                run_full_analysis(config, args, stock_codes)
            
            run_with_schedule(
                task=scheduled_task,
                schedule_time=config.schedule_time,
                run_immediately=True  # 启动时先执行一次
            )
            return 0
        
        # 模式3: 正常单次运行
        run_full_analysis(config, args, stock_codes)
        
        logger.info("\n程序执行完成")
        
        # 如果启用了 WebUI 且是非定时任务模式，保持程序运行以便访问 WebUI
        if start_webui and not (args.schedule or config.schedule_enabled):
            logger.info("WebUI 运行中 (按 Ctrl+C 退出)...")
            try:
                # 简单的保持活跃循环
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        
        return 0
        
    except KeyboardInterrupt:
        logger.info("\n用户中断，程序退出")
        return 130
        
    except Exception as e:
        logger.exception(f"程序执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
