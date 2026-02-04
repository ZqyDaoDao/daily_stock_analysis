# -*- coding: utf-8 -*-
"""
===================================
策略选股器 - 整合 StockTradebyZ 战法
===================================

职责：
1. 整合多种技术选股战法（少妇、SuperB1、填坑等）
2. 从本地数据目录读取K线数据
3. 执行选股并返回结果
4. 与 daily_stock_analysis 的分析流程无缝对接

使用方式：
    from strategy_screener import StrategyScreener
    
    screener = StrategyScreener(data_dir="./data")
    results = screener.run_all_strategies()
"""

import logging
import sys
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, date
import pandas as pd

# 导入 StockTradebyZ 的 Selector 模块
sys.path.insert(0, str(Path(__file__).parent))

try:
    from Selector import (
        BBIKDJSelector,
        SuperB1Selector,
        PeakKDJSelector,
        BBIShortLongSelector,
        MA60CrossVolumeWaveSelector,
        BigBullishVolumeSelector
    )
    from select_stock import load_data
except ImportError as e:
    logging.error(f"无法导入 Selector 模块: {e}")
    logging.error("请确保已将 StockTradebyZ 的 Selector.py 和 select_stock.py 复制到项目目录")
    raise

logger = logging.getLogger(__name__)


class StrategyScreener:
    """
    策略选股器 - 整合多种技术战法
    
    支持的战法：
    1. 少妇战法 (BBIKDJSelector)
    2. SuperB1战法 (SuperB1Selector)
    3. 填坑战法 (PeakKDJSelector)
    4. 补票战法 (BBIShortLongSelector)
    5. 上穿60放量战法 (MA60CrossVolumeWaveSelector)
    6. 暴力K战法 (BigBullishVolumeSelector)
    """
    
    # 默认策略配置
    DEFAULT_STRATEGIES = {
        "少妇战法": {
            "class": BBIKDJSelector,
            "params": {
                "j_threshold": 15,
                "bbi_min_window": 20,
                "max_window": 120,
                "price_range_pct": 1,
                "bbi_q_threshold": 0.2,
                "j_q_threshold": 0.10
            },
            "enabled": True
        },
        "SuperB1战法": {
            "class": SuperB1Selector,
            "params": {
                "lookback_n": 10,
                "close_vol_pct": 0.02,
                "price_drop_pct": 0.02,
                "j_threshold": 10,
                "j_q_threshold": 0.10,
                "B1_params": {
                    "j_threshold": 15,
                    "bbi_min_window": 20,
                    "max_window": 120,
                    "price_range_pct": 1,
                    "bbi_q_threshold": 0.3,
                    "j_q_threshold": 0.10
                }
            },
            "enabled": True
        },
        "填坑战法": {
            "class": PeakKDJSelector,
            "params": {
                "j_threshold": 10,
                "max_window": 120,
                "fluc_threshold": 0.03,
                "j_q_threshold": 0.10,
                "gap_threshold": 0.2
            },
            "enabled": True
        },
        "补票战法": {
            "class": BBIShortLongSelector,
            "params": {
                "n_short": 5,
                "n_long": 21,
                "m": 5,
                "bbi_min_window": 2,
                "max_window": 120,
                "bbi_q_threshold": 0.2,
                "upper_rsv_threshold": 75,
                "lower_rsv_threshold": 25
            },
            "enabled": True
        },
        "上穿60放量战法": {
            "class": MA60CrossVolumeWaveSelector,
            "params": {
                "lookback_n": 25,
                "vol_multiple": 1.8,
                "j_threshold": 15,
                "j_q_threshold": 0.10,
                "ma60_slope_days": 5,
                "max_window": 120
            },
            "enabled": True
        },
        "暴力K战法": {
            "class": BigBullishVolumeSelector,
            "params": {
                "up_pct_threshold": 0.06,
                "upper_wick_pct_max": 0.02,
                "require_bullish_close": True,
                "close_lt_zxdq_mult": 1.15,
                "vol_lookback_n": 20,
                "vol_multiple": 2.5
            },
            "enabled": True
        }
    }
    
    def __init__(
        self,
        data_dir: str = "./data",
        config_file: Optional[str] = None,
        strategies: Optional[Dict] = None
    ):
        """
        初始化策略选股器
        
        Args:
            data_dir: K线数据目录
            config_file: 策略配置文件路径（可选）
            strategies: 策略配置字典（可选，优先级高于配置文件）
        """
        self.data_dir = Path(data_dir)
        
        # 加载策略配置
        if strategies:
            self.strategies = strategies
        elif config_file and Path(config_file).exists():
            self.strategies = self._load_config_file(config_file)
        else:
            self.strategies = self.DEFAULT_STRATEGIES.copy()
        
        logger.info(f"策略选股器初始化完成，数据目录: {self.data_dir}")
        logger.info(f"已加载 {len(self.strategies)} 个策略")
    
    def _load_config_file(self, config_file: str) -> Dict:
        """从配置文件加载策略"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            strategies = {}
            selector_list = config.get('selectors', [])
            
            for item in selector_list:
                alias = item.get('alias', item.get('class'))
                class_name = item.get('class')
                
                # 映射类名到类对象
                class_map = {
                    'BBIKDJSelector': BBIKDJSelector,
                    'SuperB1Selector': SuperB1Selector,
                    'PeakKDJSelector': PeakKDJSelector,
                    'BBIShortLongSelector': BBIShortLongSelector,
                    'MA60CrossVolumeWaveSelector': MA60CrossVolumeWaveSelector,
                    'BigBullishVolumeSelector': BigBullishVolumeSelector
                }
                
                if class_name in class_map:
                    strategies[alias] = {
                        'class': class_map[class_name],
                        'params': item.get('params', {}),
                        'enabled': item.get('activate', True)
                    }
            
            return strategies
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}，使用默认配置")
            return self.DEFAULT_STRATEGIES.copy()
    
    def _get_stock_codes(self) -> List[str]:
        """从数据目录扫描股票代码"""
        codes = []
        for file in self.data_dir.glob("*.csv"):
            code = file.stem
            if code.isdigit() and len(code) == 6:
                codes.append(code)
        
        logger.info(f"从数据目录扫描到 {len(codes)} 只股票")
        return sorted(codes)
    
    def _load_stock_data(self, codes: List[str]) -> Dict[str, pd.DataFrame]:
        """加载股票数据"""
        try:
            data = load_data(self.data_dir, codes)
            logger.info(f"成功加载 {len(data)} 只股票的数据")
            return data
        except Exception as e:
            logger.error(f"加载股票数据失败: {e}")
            return {}
    
    def run_strategy(
        self,
        strategy_name: str,
        trade_date: Optional[date] = None
    ) -> List[str]:
        """
        运行单个策略
        
        Args:
            strategy_name: 策略名称
            trade_date: 交易日期（默认为最新日期）
        
        Returns:
            选中的股票代码列表
        """
        if strategy_name not in self.strategies:
            logger.error(f"策略 '{strategy_name}' 不存在")
            return []
        
        strategy_config = self.strategies[strategy_name]
        
        if not strategy_config.get('enabled', True):
            logger.info(f"策略 '{strategy_name}' 已禁用")
            return []
        
        logger.info(f"开始运行策略: {strategy_name}")
        
        try:
            # 获取股票代码和数据
            codes = self._get_stock_codes()
            if not codes:
                logger.warning("未找到股票数据")
                return []
            
            data = self._load_stock_data(codes)
            if not data:
                logger.warning("加载股票数据失败")
                return []
            
            # 确定交易日期
            if trade_date is None:
                trade_date = max(df['date'].max() for df in data.values())
            
            trade_date = pd.Timestamp(trade_date)
            logger.info(f"交易日期: {trade_date.date()}")
            
            # 实例化选股器
            selector_class = strategy_config['class']
            params = strategy_config['params']
            selector = selector_class(**params)
            
            # 执行选股
            selected = selector.select(trade_date, data)
            
            logger.info(f"策略 '{strategy_name}' 选出 {len(selected)} 只股票: {', '.join(selected)}")
            
            return selected
            
        except Exception as e:
            logger.exception(f"运行策略 '{strategy_name}' 失败: {e}")
            return []
    
    def run_all_strategies(
        self,
        trade_date: Optional[date] = None
    ) -> Dict[str, List[str]]:
        """
        运行所有启用的策略
        
        Args:
            trade_date: 交易日期（默认为最新日期）
        
        Returns:
            {策略名称: [股票代码列表]}
        """
        logger.info("=" * 60)
        logger.info("开始运行所有策略")
        logger.info("=" * 60)
        
        results = {}
        
        for strategy_name in self.strategies:
            selected = self.run_strategy(strategy_name, trade_date)
            if selected:
                results[strategy_name] = selected
        
        # 统计汇总
        all_stocks = set()
        for stocks in results.values():
            all_stocks.update(stocks)
        
        logger.info("=" * 60)
        logger.info(f"所有策略运行完成")
        logger.info(f"共选出 {len(all_stocks)} 只不重复股票")
        logger.info("=" * 60)
        
        # 打印详细结果
        for strategy_name, stocks in results.items():
            logger.info(f"  {strategy_name}: {len(stocks)} 只 - {', '.join(stocks)}")
        
        return results
    
    def get_union_stocks(
        self,
        trade_date: Optional[date] = None
    ) -> List[str]:
        """
        获取所有策略的并集（去重）
        
        Args:
            trade_date: 交易日期
        
        Returns:
            股票代码列表（去重）
        """
        results = self.run_all_strategies(trade_date)
        
        all_stocks = set()
        for stocks in results.values():
            all_stocks.update(stocks)
        
        return sorted(list(all_stocks))
    
    def format_report(
        self,
        results: Dict[str, List[str]]
    ) -> str:
        """
        格式化选股报告
        
        Args:
            results: 选股结果字典
        
        Returns:
            格式化的报告文本
        """
        lines = [
            "=" * 60,
            "策略选股报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            ""
        ]
        
        # 统计
        all_stocks = set()
        for stocks in results.values():
            all_stocks.update(stocks)
        
        lines.append(f"📊 总计: {len(results)} 个策略，选出 {len(all_stocks)} 只不重复股票")
        lines.append("")
        
        # 各策略详情
        for strategy_name, stocks in results.items():
            lines.append(f"🎯 {strategy_name}")
            lines.append(f"   选中: {len(stocks)} 只")
            if stocks:
                lines.append(f"   代码: {', '.join(stocks)}")
            lines.append("")
        
        # 股票出现频次统计
        stock_count = {}
        for stocks in results.values():
            for stock in stocks:
                stock_count[stock] = stock_count.get(stock, 0) + 1
        
        if stock_count:
            lines.append("📈 股票出现频次（多策略共振）")
            sorted_stocks = sorted(stock_count.items(), key=lambda x: x[1], reverse=True)
            for stock, count in sorted_stocks:
                if count > 1:
                    lines.append(f"   {stock}: {count} 个策略")
            lines.append("")
        
        lines.append("=" * 60)
        
        return "\n".join(lines)


def main():
    """测试入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="策略选股器")
    parser.add_argument("--data-dir", default="./data", help="数据目录")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--strategy", help="指定运行单个策略")
    parser.add_argument("--date", help="交易日期 YYYY-MM-DD")
    args = parser.parse_args()
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
    )
    
    # 创建选股器
    screener = StrategyScreener(
        data_dir=args.data_dir,
        config_file=args.config
    )
    
    # 解析日期
    trade_date = None
    if args.date:
        trade_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    
    # 运行策略
    if args.strategy:
        # 运行单个策略
        selected = screener.run_strategy(args.strategy, trade_date)
        print(f"\n选中股票: {', '.join(selected)}")
    else:
        # 运行所有策略
        results = screener.run_all_strategies(trade_date)
        report = screener.format_report(results)
        print(f"\n{report}")


if __name__ == "__main__":
    main()
