# -*- coding: utf-8 -*-
"""
===================================
股票选股模块 - 综合策略选股
===================================

职责：
1. 全市场数据获取（使用 akshare）
2. 技术指标筛选（第一层）：快速过滤 5000+ → 20-30
3. AI智能筛选（第二层）：深度精选 20-30 → 5-10
4. 选股结果存储和通知

核心流程：
    全市场数据获取 → 技术指标筛选（第一层） → AI深度分析（第二层） → 结果存储/通知
        (分批+限流)        (快速过滤5000→20)        (深度精选20→5)         (自动加入分析)
"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

import pandas as pd
import numpy as np

from src.config import get_config
from src.storage import get_db, DatabaseManager, ScreeningResultDB, and_, desc
from src.analyzer import GeminiAnalyzer, AnalysisResult, STOCK_NAME_MAP
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult
from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.data_cache_manager import get_cache_manager
from services.search_service import SearchService

logger = logging.getLogger(__name__)


# ==================== 数据模型 ====================

class ScreeningMode(Enum):
    """选股模式"""
    TECH_ONLY = "tech_only"       # 仅技术筛选
    AI_ONLY = "ai_only"           # 仅AI筛选（跳过技术筛选）
    FULL = "full"                 # 完整流程（技术+AI）


@dataclass
class ScreeningCriteria:
    """选股筛选条件"""
    # 技术指标条件
    min_trend_strength: int = 60      # 最小趋势强度（0-100）
    max_bias_ma5: float = 5.0         # 最大乖离率（%）
    min_volume_ratio: float = 0.8     # 最小量比
    max_volume_ratio: float = 3.0     # 最大量比
    bullish_alignment: bool = True    # 是否要求多头排列

    # 市场条件
    min_price: float = 5.0            # 最低价格（排除仙股）
    max_price: float = 1000.0         # 最高价格
    min_turnover: float = 0.5         # 最小换手率

    # 板块过滤
    exclude_st: bool = True           # 排除ST股票
    exclude_new_listed_days: int = 60 # 排除新上市天数

    # AI筛选配置
    ai_filter_enabled: bool = True    # 是否启用AI筛选
    max_candidates: int = 20          # AI筛选最大候选数
    final_selection: int = 5          # 最终选中数量

    # 并发控制
    batch_size: int = 100             # 分批大小
    request_delay: float = 2.0        # 请求延迟（秒）


@dataclass
class ScreeningResult:
    """选股结果"""
    code: str                         # 股票代码
    name: str                         # 股票名称
    tech_score: float                 # 技术评分（0-100）
    tech_reasons: List[str]           # 技术面理由
    ai_result: Optional[AnalysisResult] = None  # AI分析结果
    screen_time: datetime = None      # 选股时间

    def __post_init__(self):
        if self.screen_time is None:
            self.screen_time = datetime.now()


# ==================== 核心选股器 ====================

class StockScreener:
    """
    股票选股器 - 主控制器

    职责：
    1. 获取全市场股票列表
    2. 执行技术指标筛选（第一层）
    3. 执行AI智能筛选（第二层）
    4. 保存和推送选股结果
    """

    def __init__(
        self,
        criteria: Optional[ScreeningCriteria] = None,
        max_workers: int = 3,
        db: Optional[DatabaseManager] = None
    ):
        """
        初始化选股器

        Args:
            criteria: 筛选条件（可选，默认使用配置）
            max_workers: 最大并发线程数
            db: 数据库管理器（可选，默认使用全局实例）
        """
        self.config = get_config()
        self.criteria = criteria or self._load_criteria_from_config()
        self.max_workers = max_workers
        self.db = db or get_db()

        # 初始化各模块
        self.fetcher = AkshareFetcher(
            sleep_min=self.config.akshare_sleep_min,
            sleep_max=self.config.akshare_sleep_max
        )
        self.trend_analyzer = StockTrendAnalyzer()
        self.ai_analyzer = GeminiAnalyzer()

        # 初始化搜索服务（如果可用）
        self.search_service = None
        if self.config.tavily_api_keys or self.config.serpapi_keys:
            self.search_service = SearchService(
                tavily_keys=self.config.tavily_api_keys,
                serpapi_keys=self.config.serpapi_keys
            )

        logger.info(f"选股器初始化完成")
        logger.info(f"筛选条件: 趋势强度>={self.criteria.min_trend_strength}, "
                   f"乖离率<={self.criteria.max_bias_ma5}%, "
                   f"量比={self.criteria.min_volume_ratio}-{self.criteria.max_volume_ratio}")
        logger.info(f"价格区间: {self.criteria.min_price}-{self.criteria.max_price}元, "
                   f"换手率>={self.criteria.min_turnover}%")
        if self.criteria.ai_filter_enabled:
            logger.info(f"AI筛选: 启用, 最大候选数={self.criteria.max_candidates}, "
                       f"最终选中={self.criteria.final_selection}")
        else:
            logger.info(f"AI筛选: 禁用")

    def _load_criteria_from_config(self) -> ScreeningCriteria:
        """从配置加载筛选条件"""
        return ScreeningCriteria(
            min_trend_strength=int(os.getenv('SREENER_MIN_TRENGTH', '60')),
            max_bias_ma5=float(os.getenv('SREENER_MAX_BIAS_MA5', '5.0')),
            min_volume_ratio=float(os.getenv('SREENER_MIN_VOLUME_RATIO', '0.8')),
            max_volume_ratio=float(os.getenv('SREENER_MAX_VOLUME_RATIO', '3.0')),
            bullish_alignment=os.getenv('SREENER_BULLISH_ONLY', 'true').lower() == 'true',
            min_price=float(os.getenv('SREENER_MIN_PRICE', '5.0')),
            max_price=float(os.getenv('SREENER_MAX_PRICE', '1000.0')),
            exclude_st=os.getenv('SREENER_EXCLUDE_ST', 'true').lower() == 'true',
            exclude_new_listed_days=int(os.getenv('SREENER_EXCLUDE_NEW_LISTED_DAYS', '60')),
            ai_filter_enabled=os.getenv('SREENER_AI_ENABLED', 'true').lower() == 'true',
            max_candidates=int(os.getenv('SREENER_MAX_CANDIDATES', '20')),
            final_selection=int(os.getenv('SREENER_FINAL_SELECTION', '5')),
            batch_size=int(os.getenv('SREENER_BATCH_SIZE', '100')),
            request_delay=float(os.getenv('SREENER_REQUEST_DELAY', '2.0')),
        )

    def screen_market(
        self,
        mode: ScreeningMode = ScreeningMode.FULL,
        force_refresh: bool = False,
        target_date: Optional[date] = None  # 新增参数
    ) -> List[ScreeningResult]:
        """
        执行全市场选股（主入口）

        Args:
            mode: 选股模式
            force_refresh: 是否强制刷新（忽略缓存）
            target_date: 目标日期（None表示今天）

        Returns:
            选股结果列表
        """
        start_time = time.time()
        # 使用指定日期，默认为今天
        screen_date = target_date or date.today()

        logger.info("=" * 60)
        logger.info(f"开始全市场选股: {mode.value} 模式")
        logger.info(f"选股日期: {screen_date}")
        logger.info("=" * 60)

        # 检查是否是未来日期
        if target_date and target_date > date.today():
            logger.error(f"不能指定未来日期: {target_date}")
            raise ValueError(f"Target date cannot be in the future: {target_date}")

        # 检查是否是周末（简单判断）
        if target_date and target_date.weekday() >= 5:
            logger.warning(f"{target_date} 是周末，可能没有交易数据")

        # 检查是否已选股
        if not force_refresh and self._has_today_screening(screen_date):
            logger.info(f"{screen_date} 已执行选股，使用缓存结果")
            return self._load_screening_results_by_date(screen_date)

        # Step 1: 获取全市场股票列表
        all_stocks = self._get_all_stocks(target_date=target_date)
        logger.info(f"全市场股票数: {len(all_stocks)}")

        if not all_stocks:
            logger.warning("未获取到股票列表，选股终止")
            return []

        # Step 2: 技术指标筛选（第一层）
        tech_candidates = []
        if mode in [ScreeningMode.TECH_ONLY, ScreeningMode.FULL]:
            tech_candidates = self._technical_filter_batch(all_stocks)
            logger.info(f"技术筛选完成: {len(all_stocks)} → {len(tech_candidates)}")

            if mode == ScreeningMode.TECH_ONLY:
                # 仅技术模式，直接返回
                results = self._build_screening_results(tech_candidates)
                self._save_screening_results(results, screen_date)
                logger.info(f"选股完成（仅技术）: 共 {len(results)} 只股票")
                return results
        elif mode == ScreeningMode.AI_ONLY:
            # AI模式，跳过技术筛选
            tech_candidates = all_stocks[:self.criteria.max_candidates]

        # Step 3: AI智能筛选（第二层）
        final_results = []
        if self.criteria.ai_filter_enabled and mode in [ScreeningMode.AI_ONLY, ScreeningMode.FULL]:
            # 限制候选数量
            limited_candidates = tech_candidates[:self.criteria.max_candidates]
            logger.info(f"开始AI筛选: 候选数 {len(limited_candidates)}")

            final_results = self._ai_filter(limited_candidates)
            logger.info(f"AI筛选完成: {len(limited_candidates)} → {len(final_results)}")
        else:
            # 不启用AI筛选，返回技术筛选结果
            final_results = self._build_screening_results(tech_candidates)

        # Step 4: 保存结果
        self._save_screening_results(final_results, screen_date)

        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"选股完成! 耗时: {elapsed:.1f}秒, 最终选中: {len(final_results)} 只股票")
        logger.info("=" * 60)

        # 输出结果摘要
        for r in final_results:
            logger.info(f"  {r.name}({r.code}): 技术评分={r.tech_score:.1f}, "
                       f"AI建议={r.ai_result.operation_advice if r.ai_result else 'N/A'}")

        return final_results

    def _get_all_stocks(self, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
        """
        获取全市场股票列表（复用实时行情缓存）

        使用缓存管理器检查是否已有全市场数据，避免重复API调用

        Args:
            target_date: 目标日期（用于历史数据检查）

        Returns:
            股票列表，每个元素包含 {code, name, price, ...}
        """
        # 如果是历史日期，记录警告
        if target_date and target_date < date.today():
            logger.info(f"使用历史日期 {target_date} 进行选股")
            logger.warning("历史日期选股依赖于数据库中已有的历史数据，如数据缺失可能影响选股结果")

        try:
            import akshare as ak
            from data_provider.data_cache_manager import get_cache_manager

            cache_mgr = get_cache_manager()
            ALL_MARKET_KEY = "__all_market_spot__"

            # 检查缓存
            df = cache_mgr.get('market', ALL_MARKET_KEY)

            if df is None:
                # 缓存未命中，获取新数据
                self.fetcher._set_random_user_agent()
                self.fetcher._enforce_rate_limit()

                logger.info("调用 ak.stock_zh_a_spot_em() 获取全市场行情...")
                df = ak.stock_zh_a_spot_em()

                # 存入缓存（60秒TTL）
                cache_mgr.set('market', ALL_MARKET_KEY, df)
            else:
                logger.info("[缓存命中] 使用缓存的全市场行情数据")

            if df is None or df.empty:
                logger.error("获取全市场行情失败: 返回空数据")
                return []

            logger.info(f"获取成功: 共 {len(df)} 只股票")

            # 转换为字典列表
            stocks = []
            for _, row in df.iterrows():
                code = str(row.get('代码', ''))
                name = str(row.get('名称', ''))
                price = float(row.get('最新价', 0))

                # 基础过滤
                if self._should_exclude_stock(code, name, price):
                    continue

                stocks.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'change_pct': float(row.get('涨跌幅', 0)),
                    'volume_ratio': float(row.get('量比', 0)),
                    'turnover_rate': float(row.get('换手率', 0)),
                    'amplitude': float(row.get('振幅', 0)),
                    'pe_ratio': float(row.get('市盈率-动态', 0)),
                    'pb_ratio': float(row.get('市净率', 0)),
                    'total_mv': float(row.get('总市值', 0)),
                    'circ_mv': float(row.get('流通市值', 0)),
                })

            logger.info(f"基础过滤后: {len(stocks)} 只股票")
            return stocks

        except Exception as e:
            logger.error(f"获取全市场股票失败: {e}")
            return []

    def _should_exclude_stock(self, code: str, name: str, price: float) -> bool:
        """判断是否应该排除该股票"""
        # 排除ST股票
        if self.criteria.exclude_st and ('ST' in name or 'st' in name):
            return True

        # 价格过滤
        if price < self.criteria.min_price or price > self.criteria.max_price:
            return True

        # 排除科创板和北交所（可选，根据需要）
        if code.startswith('688') or code.startswith('8') or code.startswith('4'):
            # 可以根据需要决定是否排除
            pass

        return False

    def _technical_filter_batch(self, stocks: List[Dict]) -> List[Tuple[Dict, float, List[str]]]:
        """
        批量技术指标筛选

        Args:
            stocks: 股票列表

        Returns:
            通过筛选的股票列表，每个元素为 (stock_info, score, reasons)
        """
        candidates = []

        # 分批处理
        batch_size = self.criteria.batch_size
        total = len(stocks)

        for i in range(0, total, batch_size):
            batch = stocks[i:i + batch_size]
            logger.info(f"技术筛选进度: {i+1}-{min(i+batch_size, total)}/{total}")

            # 使用线程池并发处理
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._technical_filter, stock): stock
                    for stock in batch
                }

                for future in as_completed(futures):
                    stock = futures[future]
                    try:
                        result = future.result()
                        if result is not None:
                            score, reasons = result
                            candidates.append((stock, score, reasons))
                    except Exception as e:
                        logger.warning(f"技术筛选 {stock['code']} 失败: {e}")

            # 批次间延迟
            if i + batch_size < total:
                delay = self.criteria.request_delay + random.uniform(0, 1)
                logger.debug(f"批次间等待 {delay:.1f} 秒...")
                time.sleep(delay)

        # 按评分排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates

    def _technical_filter(self, stock: Dict) -> Optional[Tuple[float, List[str]]]:
        """
        技术指标筛选（单只股票）

        第一层筛选：基于实时行情数据快速筛选

        评分维度（0-100分）：
        1. 基础条件过滤（必须满足）
        2. 量价配合（40分）
        3. 趋势判断（60分）

        Args:
            stock: 股票信息字典

        Returns:
            (评分, 理由列表) 或 None（不满足条件）
        """
        code = stock['code']
        name = stock['name']

        try:
            # ========== 基础条件过滤 ==========
            reasons = []
            score = 0

            # 换手率过滤
            turnover = stock.get('turnover_rate', 0)
            if turnover < self.criteria.min_turnover:
                return None  # 换手率过低

            # 量比过滤
            volume_ratio = stock.get('volume_ratio', 0)
            if volume_ratio < self.criteria.min_volume_ratio or volume_ratio > self.criteria.max_volume_ratio:
                return None  # 量比不在合理区间

            # ========== 量价配合评分（40分） ==========
            # 量比评分（20分）
            if 0.8 <= volume_ratio <= 1.5:
                score += 20
                reasons.append("量比适中(0.8-1.5)")
            elif 1.5 < volume_ratio <= 2.0:
                score += 15
                reasons.append("温和放量")
            elif 0.5 <= volume_ratio < 0.8:
                score += 10
                reasons.append("量能略微萎缩")
            else:
                score += 5

            # 涨跌幅评分（20分）
            change_pct = stock.get('change_pct', 0)
            if -2 <= change_pct <= 5:
                score += 20
                reasons.append(f"涨跌幅合理({change_pct:+.1f}%)")
            elif -5 <= change_pct < -2:
                score += 15
                reasons.append("小幅回调")
            elif 5 < change_pct <= 8:
                score += 10
                reasons.append("涨幅较大")
            else:
                score += 5

            # ========== 趋势判断评分（60分）==========
            # 优先使用数据库历史数据，避免重复API调用
            try:
                df = None
                from_db = False

                # 先尝试从数据库获取历史数据
                try:
                    context = self.db.get_analysis_context(code)
                    if context and 'raw_data' in context:
                        raw_data = context['raw_data']
                        if isinstance(raw_data, list) and len(raw_data) >= 20:
                            # 数据库中有足够的历史数据
                            df = pd.DataFrame(raw_data)
                            from_db = True
                            logger.debug(f"[{code}] 使用数据库历史数据 ({len(df)}条)")
                except Exception as db_err:
                    logger.debug(f"[{code}] 数据库读取失败: {db_err}")

                # 如果数据库数据不足，从API获取
                if df is None or df.empty or len(df) < 20:
                    df = self.fetcher.get_daily_data(code, days=30)

                if df is None or df.empty or len(df) < 20:
                    # 数据不足，基于实时数据简单判断
                    if change_pct > 0:
                        score += 30
                        reasons.append("当日上涨")
                    else:
                        score += 20
                        reasons.append("当日下跌")
                else:
                    # 使用趋势分析器
                    trend_result = self.trend_analyzer.analyze(df, code)

                    # 趋势状态评分（40分）
                    if trend_result.trend_status.value in ['强势多头', '多头排列']:
                        score += 40
                        reasons.append(f"多头排列({trend_result.ma_alignment})")
                    elif trend_result.trend_status.value == '弱势多头':
                        score += 25
                        reasons.append("弱势多头")
                    elif trend_result.trend_status.value == '盘整':
                        score += 15
                        reasons.append("均线缠绕")
                    else:
                        score += 5
                        reasons.append("空头排列")

                    # 乖离率评分（20分）
                    bias_ma5 = trend_result.bias_ma5
                    if abs(bias_ma5) < 2:
                        score += 20
                        reasons.append(f"乖离率安全({bias_ma5:+.1f}%)")
                    elif abs(bias_ma5) < self.criteria.max_bias_ma5:
                        score += 15
                        reasons.append(f"乖离率可接受({bias_ma5:+.1f}%)")
                    else:
                        score += 5
                        reasons.append(f"乖离率较高({bias_ma5:+.1f}%)")

                    # 更新股票信息（添加均线数据）
                    stock['ma5'] = trend_result.ma5
                    stock['ma10'] = trend_result.ma10
                    stock['ma20'] = trend_result.ma20
                    stock['bias_ma5'] = bias_ma5

            except Exception as e:
                logger.debug(f"{code} 趋势分析失败: {e}，使用简单判断")
                # 趋势分析失败时的降级处理
                if change_pct > 0:
                    score += 30
                else:
                    score += 20

            # 检查最低评分要求
            if score < self.criteria.min_trend_strength:
                return None

            return (score, reasons)

        except Exception as e:
            logger.warning(f"技术筛选 {name}({code}) 失败: {e}")
            return None

    def _ai_filter(self, candidates: List[Tuple[Dict, float, List[str]]]) -> List[ScreeningResult]:
        """
        AI智能筛选（第二层）

        对第一层通过者进行深度分析，复用现有AI分析能力

        Args:
            candidates: 第一层筛选结果列表

        Returns:
            最终选中的股票列表
        """
        if not self.ai_analyzer.is_available():
            logger.warning("AI分析器不可用，返回技术筛选结果")
            return self._build_screening_results(candidates)

        ai_results = []

        # 限制分析数量
        to_analyze = candidates[:self.criteria.max_candidates]
        logger.info(f"开始AI分析: 待分析股票数 {len(to_analyze)}")

        for i, (stock, tech_score, tech_reasons) in enumerate(to_analyze):
            code = stock['code']
            name = stock['name']

            logger.info(f"[{i+1}/{len(to_analyze)}] AI分析 {name}({code})")

            try:
                # 获取增强数据
                enhanced_data = self.fetcher.get_enhanced_data(code, days=30)
                df = enhanced_data.get('daily_data')

                if df is None or df.empty:
                    logger.warning(f"{code} 数据不足，跳过AI分析")
                    continue

                # 构建分析上下文
                context = self._build_analysis_context(stock, df, enhanced_data)

                # 搜索新闻（如果可用）
                news_context = None
                if self.search_service and self.search_service.is_available:
                    try:
                        intel_results = self.search_service.search_comprehensive_intel(
                            stock_code=code,
                            stock_name=name,
                            max_searches=2
                        )
                        if intel_results:
                            news_context = self.search_service.format_intel_report(intel_results, name)
                            logger.debug(f"{code} 情报搜索完成")
                    except Exception as e:
                        logger.debug(f"{code} 情报搜索失败: {e}")

                # AI分析
                ai_result = self.ai_analyzer.analyze(context, news_context=news_context)

                if ai_result and ai_result.success:
                    ai_results.append((stock, tech_score, tech_reasons, ai_result))
                    logger.info(f"  → AI评分: {ai_result.sentiment_score}, 建议: {ai_result.operation_advice}")
                else:
                    logger.warning(f"  → AI分析失败")

                # 请求间延迟
                if i < len(to_analyze) - 1:
                    time.sleep(self.criteria.request_delay)

            except Exception as e:
                logger.warning(f"AI分析 {name}({code}) 失败: {e}")

        # 综合评分排序
        # 综合评分 = 技术评分 * 0.4 + AI评分 * 0.6
        scored_results = []
        for stock, tech_score, tech_reasons, ai_result in ai_results:
            combined_score = tech_score * 0.4 + ai_result.sentiment_score * 0.6
            scored_results.append((combined_score, stock, tech_score, tech_reasons, ai_result))

        # 按综合评分排序，取前N名
        scored_results.sort(key=lambda x: x[0], reverse=True)
        final_selected = scored_results[:self.criteria.final_selection]

        # 构建结果
        results = []
        for _, stock, tech_score, tech_reasons, ai_result in final_selected:
            results.append(ScreeningResult(
                code=stock['code'],
                name=stock['name'],
                tech_score=tech_score,
                tech_reasons=tech_reasons,
                ai_result=ai_result
            ))

        return results

    def _build_analysis_context(
        self,
        stock: Dict,
        df: pd.DataFrame,
        enhanced_data: Dict
    ) -> Dict[str, Any]:
        """构建AI分析上下文"""
        latest = df.iloc[-1]

        context = {
            'code': stock['code'],
            'date': date.today().isoformat(),
            'stock_name': stock['name'],
            'today': {
                'open': float(latest.get('open', 0)),
                'high': float(latest.get('high', 0)),
                'low': float(latest.get('low', 0)),
                'close': float(latest.get('close', stock.get('price', 0))),
                'volume': float(latest.get('volume', 0)),
                'amount': float(latest.get('amount', 0)),
                'pct_chg': float(stock.get('change_pct', 0)),
                'ma5': float(stock.get('ma5', 0)),
                'ma10': float(stock.get('ma10', 0)),
                'ma20': float(stock.get('ma20', 0)),
            },
            'realtime': {
                'name': stock['name'],
                'price': stock.get('price', 0),
                'volume_ratio': stock.get('volume_ratio', 0),
                'turnover_rate': stock.get('turnover_rate', 0),
                'pe_ratio': stock.get('pe_ratio', 0),
                'pb_ratio': stock.get('pb_ratio', 0),
                'total_mv': stock.get('total_mv', 0),
                'circ_mv': stock.get('circ_mv', 0),
                'change_60d': stock.get('change_60d', 0),
            },
            'ma_status': self._get_ma_status(stock),
        }

        # 添加筹码分布数据
        chip_data = enhanced_data.get('chip_distribution')
        if chip_data:
            context['chip'] = {
                'profit_ratio': chip_data.profit_ratio,
                'avg_cost': chip_data.avg_cost,
                'concentration_90': chip_data.concentration_90,
                'concentration_70': chip_data.concentration_70,
                'chip_status': chip_data.get_chip_status(stock.get('price', 0)),
            }

        return context

    def _get_ma_status(self, stock: Dict) -> str:
        """获取均线状态"""
        ma5 = stock.get('ma5', 0)
        ma10 = stock.get('ma10', 0)
        ma20 = stock.get('ma20', 0)
        price = stock.get('price', 0)

        if ma5 > ma10 > ma20 > 0:
            if price > ma5:
                return "多头排列 📈"
            else:
                return "多头排列(回踩)"
        elif ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        else:
            return "震荡整理 ↔️"

    def _build_screening_results(
        self,
        candidates: List[Tuple[Dict, float, List[str]]]
    ) -> List[ScreeningResult]:
        """构建选股结果（无AI分析）"""
        results = []
        for stock, score, reasons in candidates[:self.criteria.final_selection]:
            results.append(ScreeningResult(
                code=stock['code'],
                name=stock['name'],
                tech_score=score,
                tech_reasons=reasons,
                ai_result=None
            ))
        return results

    def _save_screening_results(self, results: List[ScreeningResult], screen_date: Optional[date] = None) -> None:
        """保存选股结果到数据库

        Args:
            results: 选股结果列表
            screen_date: 选股日期（None表示今天）
        """
        if not results:
            logger.warning("无选股结果需要保存")
            return

        try:
            save_date = screen_date or date.today()
            saved_count = 0

            with self.db.get_session() as session:
                for r in results:
                    # 检查是否已存在
                    existing = session.query(ScreeningResultDB).filter(
                        and_(
                            ScreeningResultDB.code == r.code,
                            ScreeningResultDB.screen_date == save_date
                        )
                    ).first()

                    if existing:
                        # 更新现有记录
                        existing.tech_score = r.tech_score
                        existing.tech_reasons = json.dumps(r.tech_reasons, ensure_ascii=False)
                        if r.ai_result:
                            existing.ai_sentiment_score = r.ai_result.sentiment_score
                            existing.ai_operation_advice = r.ai_result.operation_advice
                            existing.ai_trend_prediction = r.ai_result.trend_prediction
                            existing.ai_analysis_summary = r.ai_result.analysis_summary
                        existing.screen_time = r.screen_time
                    else:
                        # 创建新记录
                        record = ScreeningResultDB(
                            code=r.code,
                            name=r.name,
                            tech_score=r.tech_score,
                            tech_reasons=json.dumps(r.tech_reasons, ensure_ascii=False),
                            ai_sentiment_score=r.ai_result.sentiment_score if r.ai_result else None,
                            ai_operation_advice=r.ai_result.operation_advice if r.ai_result else None,
                            ai_trend_prediction=r.ai_result.trend_prediction if r.ai_result else None,
                            ai_analysis_summary=r.ai_result.analysis_summary if r.ai_result else None,
                            screen_date=save_date,
                            screen_time=r.screen_time
                        )
                        session.add(record)
                        saved_count += 1

                session.commit()

            logger.info(f"选股结果保存成功: 新增 {saved_count} 条")

        except Exception as e:
            logger.error(f"保存选股结果失败: {e}")

    def _has_today_screening(self, today: date) -> bool:
        """检查今日是否已执行选股"""
        try:
            with self.db.get_session() as session:
                count = session.query(ScreeningResultDB).filter(
                    ScreeningResultDB.screen_date == today
                ).count()
                return count > 0
        except Exception as e:
            logger.error(f"检查今日选股结果失败: {e}")
            return False

    def _load_screening_results_by_date(self, target_date: date) -> List[ScreeningResult]:
        """从数据库加载指定日期的选股结果

        Args:
            target_date: 目标日期

        Returns:
            选股结果列表
        """
        try:
            results = []

            with self.db.get_session() as session:
                records = session.query(ScreeningResultDB).filter(
                    ScreeningResultDB.screen_date == target_date
                ).order_by(desc(ScreeningResultDB.tech_score)).all()

                for r in records:
                    # 构建AI结果
                    ai_result = None
                    if r.ai_sentiment_score is not None:
                        ai_result = AnalysisResult(
                            code=r.code,
                            name=r.name,
                            sentiment_score=r.ai_sentiment_score,
                            operation_advice=r.ai_operation_advice or '持有',
                            trend_prediction=r.ai_trend_prediction or '震荡',
                            analysis_summary=r.ai_analysis_summary or '',
                        )

                    results.append(ScreeningResult(
                        code=r.code,
                        name=r.name,
                        tech_score=r.tech_score,
                        tech_reasons=json.loads(r.tech_reasons) if r.tech_reasons else [],
                        ai_result=ai_result,
                        screen_time=r.screen_time
                    ))

            return results

        except Exception as e:
            logger.error(f"加载 {target_date} 选股结果失败: {e}")
            return []


# ==================== 便捷函数 ====================

def get_screener() -> StockScreener:
    """获取选股器实例"""
    return StockScreener()


def screen_stocks(
    mode: ScreeningMode = ScreeningMode.FULL,
    force_refresh: bool = False
) -> List[ScreeningResult]:
    """
    执行选股的快捷方式

    Args:
        mode: 选股模式
        force_refresh: 是否强制刷新

    Returns:
        选股结果列表
    """
    screener = get_screener()
    return screener.screen_market(mode=mode, force_refresh=force_refresh)


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
    )

    # 测试技术筛选
    screener = get_screener()

    # 仅技术筛选测试
    print("\n=== 测试技术筛选 ===")
    results = screener.screen_market(mode=ScreeningMode.TECH_ONLY)

    for r in results:
        print(f"{r.name}({r.code}): 技术评分={r.tech_score:.1f}")
        print(f"  理由: {', '.join(r.tech_reasons)}")
