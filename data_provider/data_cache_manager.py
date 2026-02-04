# -*- coding: utf-8 -*-
"""
===================================
数据缓存管理器 - 统一缓存管理
===================================

职责：
1. 提供线程安全的缓存读写接口
2. 支持TTL（过期时间）策略
3. 分层管理不同类型数据的缓存

缓存策略：
- 实时行情：60秒TTL
- 筹码分布：300秒TTL（5分钟，更新频率低）
- 全市场行情：60秒TTL
"""

import logging
from threading import Lock
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """缓存条目"""
    data: Any
    timestamp: datetime
    ttl: int  # 秒

    def is_expired(self) -> bool:
        """检查缓存是否过期"""
        return datetime.now() - self.timestamp > timedelta(seconds=self.ttl)


class DataCacheManager:
    """
    数据缓存管理器（单例模式，线程安全）

    使用线程安全的单例模式，确保全局只有一个缓存管理器实例。
    所有缓存操作都通过锁保护，确保并发安全。

    缓存策略：
    1. 实时行情：60秒TTL
    2. 筹码分布：300秒TTL（5分钟，更新频率低）
    3. 增强数据：组合缓存，基于子项最小TTL
    """

    _instance = None
    _lock = Lock()

    def __new__(cls):
        """单例模式实现（线程安全）"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """初始化缓存管理器"""
        if self._initialized:
            return

        self._cache: Dict[str, CacheEntry] = {}
        self._cache_lock = Lock()
        self._initialized = True

        # 配置TTL（秒）
        self.TTL_REALTIME = 60      # 实时行情60秒
        self.TTL_CHIP = 300         # 筹码分布5分钟
        self.TTL_ENHANCED = 60      # 增强数据60秒
        self.TTL_MARKET = 60        # 全市场行情60秒

        logger.info("数据缓存管理器初始化完成")

    def _generate_key(self, category: str, stock_code: str) -> str:
        """生成缓存键"""
        return f"{category}:{stock_code}"

    def get(self, category: str, stock_code: str) -> Optional[Any]:
        """
        获取缓存（线程安全）

        Args:
            category: 缓存类别（realtime, chip, enhanced, market）
            stock_code: 股票代码

        Returns:
            缓存数据，如果不存在或已过期返回 None
        """
        key = self._generate_key(category, stock_code)

        with self._cache_lock:
            if key not in self._cache:
                return None

            entry = self._cache[key]
            if entry.is_expired():
                # 缓存过期，删除并返回None
                del self._cache[key]
                logger.debug(f"[缓存过期] {category}:{stock_code}")
                return None

            logger.debug(f"[缓存命中] {category}:{stock_code}")
            return entry.data

    def set(self, category: str, stock_code: str, data: Any, ttl: Optional[int] = None) -> None:
        """
        设置缓存（线程安全）

        Args:
            category: 缓存类别
            stock_code: 股票代码
            data: 要缓存的数据
            ttl: 过期时间（秒），None则使用默认TTL
        """
        key = self._generate_key(category, stock_code)

        # 使用默认TTL或自定义TTL
        if ttl is None:
            ttl_map = {
                'realtime': self.TTL_REALTIME,
                'chip': self.TTL_CHIP,
                'enhanced': self.TTL_ENHANCED,
                'market': self.TTL_MARKET,
            }
            ttl = ttl_map.get(category, 60)

        entry = CacheEntry(
            data=data,
            timestamp=datetime.now(),
            ttl=ttl
        )

        with self._cache_lock:
            self._cache[key] = entry
            logger.debug(f"[缓存存储] {category}:{stock_code}, TTL={ttl}s")

    def invalidate(self, category: str, stock_code: str) -> None:
        """
        使指定缓存失效

        Args:
            category: 缓存类别
            stock_code: 股票代码
        """
        key = self._generate_key(category, stock_code)

        with self._cache_lock:
            if key in self._cache:
                del self._cache[key]
                logger.debug(f"[缓存失效] {category}:{stock_code}")

    def clear_all(self) -> None:
        """清空所有缓存"""
        with self._cache_lock:
            count = len(self._cache)
            self._cache.clear()
            logger.info(f"[缓存清空] 清除了 {count} 条缓存")

    def get_stats(self) -> Dict[str, int]:
        """
        获取缓存统计

        Returns:
            包含缓存条目数量的字典
        """
        with self._cache_lock:
            return {
                'total_entries': len(self._cache),
                'realtime_count': sum(1 for k in self._cache if k.startswith('realtime:')),
                'chip_count': sum(1 for k in self._cache if k.startswith('chip:')),
                'enhanced_count': sum(1 for k in self._cache if k.startswith('enhanced:')),
                'market_count': sum(1 for k in self._cache if k.startswith('market:')),
            }

    def cleanup_expired(self) -> int:
        """
        清理过期的缓存条目

        Returns:
            清理的条目数量
        """
        with self._cache_lock:
            expired_keys = [
                key for key, entry in self._cache.items()
                if entry.is_expired()
            ]

            for key in expired_keys:
                del self._cache[key]

            if expired_keys:
                logger.info(f"[缓存清理] 清除了 {len(expired_keys)} 条过期缓存")
            return len(expired_keys)


# ==================== 便捷函数 ====================

def get_cache_manager() -> DataCacheManager:
    """获取缓存管理器实例（单例）"""
    return DataCacheManager()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s | %(levelname)-8s | %(message)s'
    )

    cache = get_cache_manager()

    # 测试基本读写
    print("\n=== 测试基本读写 ===")
    cache.set('realtime', '600519', {'price': 100.0, 'name': '贵州茅台'})
    data = cache.get('realtime', '600519')
    print(f"读取数据: {data}")

    # 测试缓存过期
    print("\n=== 测试缓存过期（短TTL） ===")
    cache.set('test', 'expire', 'data', ttl=1)
    print(f"立即读取: {cache.get('test', 'expire')}")
    import time
    time.sleep(2)
    print(f"2秒后读取: {cache.get('test', 'expire')}")

    # 测试统计
    print("\n=== 测试统计 ===")
    cache.set('realtime', '000001', {'price': 10.0})
    cache.set('chip', '000001', {'profit_ratio': 0.5})
    stats = cache.get_stats()
    print(f"缓存统计: {stats}")
