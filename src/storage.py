# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 存储层
===================================

职责：
1. 管理 SQLite 数据库连接（单例模式）
2. 定义 ORM 数据模型
3. 提供数据存取接口
4. 实现智能更新逻辑（断点续传）
"""
from __future__ import annotations
import json

import atexit
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from pathlib import Path
if TYPE_CHECKING:
    from typing import ForwardRef

import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Date,
    DateTime,
    Integer,
    Index,
    UniqueConstraint,
    select,
    and_,
    desc,
)
from sqlalchemy.orm import (
    declarative_base,
    sessionmaker,
    Session,
)
from sqlalchemy.exc import IntegrityError

from src.config import get_config

logger = logging.getLogger(__name__)

# SQLAlchemy ORM 基类
Base = declarative_base()


# === 数据模型定义 ===

class StockDaily(Base):
    """
    股票日线数据模型
    
    存储每日行情数据和计算的技术指标
    支持多股票、多日期的唯一约束
    """
    __tablename__ = 'stock_daily'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 股票代码（如 600519, 000001）
    code = Column(String(10), nullable=False, index=True)
    
    # 交易日期
    date = Column(Date, nullable=False, index=True)
    
    # OHLC 数据
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    
    # 成交数据
    volume = Column(Float)  # 成交量（股）
    amount = Column(Float)  # 成交额（元）
    pct_chg = Column(Float)  # 涨跌幅（%）
    
    # 技术指标
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)  # 量比
    
    # 数据来源
    data_source = Column(String(50))  # 记录数据来源（如 AkshareFetcher）
    
    # 更新时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 唯一约束：同一股票同一日期只能有一条数据
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_code_date'),
        Index('ix_code_date', 'code', 'date'),
    )
    
    def __repr__(self):
        return f"<StockDaily(code={self.code}, date={self.date}, close={self.close})>"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'amount': self.amount,
            'pct_chg': self.pct_chg,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'volume_ratio': self.volume_ratio,
            'data_source': self.data_source,
        }


class DatabaseManager:
    """
    数据库管理器 - 单例模式
    
    职责：
    1. 管理数据库连接池
    2. 提供 Session 上下文管理
    3. 封装数据存取操作
    """
    
    _instance: Optional['DatabaseManager'] = None
    
    def __new__(cls, *args, **kwargs):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_url: Optional[str] = None):
        """
        初始化数据库管理器
        
        Args:
            db_url: 数据库连接 URL（可选，默认从配置读取）
        """
        if self._initialized:
            return
        
        if db_url is None:
            config = get_config()
            db_url = config.get_db_url()
        
        # 创建数据库引擎
        self._engine = create_engine(
            db_url,
            echo=False,  # 设为 True 可查看 SQL 语句
            pool_pre_ping=True,  # 连接健康检查
        )
        
        # 创建 Session 工厂
        self._SessionLocal = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
        )
        
        # 创建所有表
        Base.metadata.create_all(self._engine)

        self._initialized = True
        logger.info(f"数据库初始化完成: {db_url}")

        # 注册退出钩子，确保程序退出时关闭数据库连接
        atexit.register(DatabaseManager._cleanup_engine, self._engine)
    
    @classmethod
    def get_instance(cls) -> 'DatabaseManager':
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（用于测试）"""
        if cls._instance is not None:
            cls._instance._engine.dispose()
            cls._instance = None

    @classmethod
    def _cleanup_engine(cls, engine) -> None:
        """
        清理数据库引擎（atexit 钩子）

        确保程序退出时关闭所有数据库连接，避免 ResourceWarning

        Args:
            engine: SQLAlchemy 引擎对象
        """
        try:
            if engine is not None:
                engine.dispose()
                logger.debug("数据库引擎已清理")
        except Exception as e:
            logger.warning(f"清理数据库引擎时出错: {e}")
    
    def get_session(self) -> Session:
        """
        获取数据库 Session
        
        使用示例:
            with db.get_session() as session:
                # 执行查询
                session.commit()  # 如果需要
        """
        session = self._SessionLocal()
        try:
            return session
        except Exception:
            session.close()
            raise
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否已有指定日期的数据
        
        用于断点续传逻辑：如果已有数据则跳过网络请求
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        if target_date is None:
            target_date = date.today()
        
        with self.get_session() as session:
            result = session.execute(
                select(StockDaily).where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date == target_date
                    )
                )
            ).scalar_one_or_none()
            
            return result is not None
    
    def get_latest_data(
        self, 
        code: str, 
        days: int = 2
    ) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        用于计算"相比昨日"的变化
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(StockDaily.code == code)
                .order_by(desc(StockDaily.date))
                .limit(days)
            ).scalars().all()
            
            return list(results)
    
    def get_data_range(
        self, 
        code: str, 
        start_date: date, 
        end_date: date
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            StockDaily 对象列表
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date >= start_date,
                        StockDaily.date <= end_date
                    )
                )
                .order_by(StockDaily.date)
            ).scalars().all()
            
            return list(results)
    
    def save_daily_data(
        self, 
        df: pd.DataFrame, 
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存日线数据到数据库
        
        策略：
        - 使用 UPSERT 逻辑（存在则更新，不存在则插入）
        - 跳过已存在的数据，避免重复
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源名称
            
        Returns:
            新增/更新的记录数
        """
        if df is None or df.empty:
            logger.warning(f"保存数据为空，跳过 {code}")
            return 0
        
        saved_count = 0
        
        with self.get_session() as session:
            try:
                for _, row in df.iterrows():
                    # 解析日期
                    row_date = row.get('date')
                    if isinstance(row_date, str):
                        row_date = datetime.strptime(row_date, '%Y-%m-%d').date()
                    elif isinstance(row_date, datetime):
                        row_date = row_date.date()
                    elif isinstance(row_date, pd.Timestamp):
                        row_date = row_date.date()
                    
                    # 检查是否已存在
                    existing = session.execute(
                        select(StockDaily).where(
                            and_(
                                StockDaily.code == code,
                                StockDaily.date == row_date
                            )
                        )
                    ).scalar_one_or_none()
                    
                    if existing:
                        # 更新现有记录
                        existing.open = row.get('open')
                        existing.high = row.get('high')
                        existing.low = row.get('low')
                        existing.close = row.get('close')
                        existing.volume = row.get('volume')
                        existing.amount = row.get('amount')
                        existing.pct_chg = row.get('pct_chg')
                        existing.ma5 = row.get('ma5')
                        existing.ma10 = row.get('ma10')
                        existing.ma20 = row.get('ma20')
                        existing.volume_ratio = row.get('volume_ratio')
                        existing.data_source = data_source
                        existing.updated_at = datetime.now()
                    else:
                        # 创建新记录
                        record = StockDaily(
                            code=code,
                            date=row_date,
                            open=row.get('open'),
                            high=row.get('high'),
                            low=row.get('low'),
                            close=row.get('close'),
                            volume=row.get('volume'),
                            amount=row.get('amount'),
                            pct_chg=row.get('pct_chg'),
                            ma5=row.get('ma5'),
                            ma10=row.get('ma10'),
                            ma20=row.get('ma20'),
                            volume_ratio=row.get('volume_ratio'),
                            data_source=data_source,
                        )
                        session.add(record)
                        saved_count += 1
                
                session.commit()
                logger.info(f"保存 {code} 数据成功，新增 {saved_count} 条")
                
            except Exception as e:
                session.rollback()
                logger.error(f"保存 {code} 数据失败: {e}")
                raise
        
        return saved_count
    
    def get_analysis_context(
        self, 
        code: str,
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析所需的上下文数据
        
        返回今日数据 + 昨日数据的对比信息
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            包含今日数据、昨日对比等信息的字典
        """
        if target_date is None:
            target_date = date.today()
        
        # 获取最近2天数据
        recent_data = self.get_latest_data(code, days=2)
        
        if not recent_data:
            logger.warning(f"未找到 {code} 的数据")
            return None
        
        today_data = recent_data[0]
        yesterday_data = recent_data[1] if len(recent_data) > 1 else None
        
        context = {
            'code': code,
            'date': today_data.date.isoformat(),
            'today': today_data.to_dict(),
        }
        
        if yesterday_data:
            context['yesterday'] = yesterday_data.to_dict()
            
            # 计算相比昨日的变化
            if yesterday_data.volume and yesterday_data.volume > 0:
                context['volume_change_ratio'] = round(
                    today_data.volume / yesterday_data.volume, 2
                )
            
            if yesterday_data.close and yesterday_data.close > 0:
                context['price_change_ratio'] = round(
                    (today_data.close - yesterday_data.close) / yesterday_data.close * 100, 2
                )
            
            # 均线形态判断
            context['ma_status'] = self._analyze_ma_status(today_data)
        
        return context

        # ==================== 选股结果相关方法 ====================

    def save_screening_result(
            self,
            code: str,
            name: str,
            tech_score: float,
            tech_reasons: List[str],
            ai_result: Optional[Any] = None,
            screen_date: Optional[date] = None
    ) -> bool:
        """
        保存单条选股结果到数据库

        Args:
            code: 股票代码
            name: 股票名称
            tech_score: 技术评分
            tech_reasons: 技术面理由列表
            ai_result: AI分析结果（可选）
            screen_date: 选股日期（默认今天）

        Returns:
            是否保存成功
        """
        if screen_date is None:
            screen_date = date.today()

        try:
            with self.get_session() as session:
                # 检查是否已存在
                existing = session.query(ScreeningResultDB).filter(
                    and_(
                        ScreeningResultDB.code == code,
                        ScreeningResultDB.screen_date == screen_date
                    )
                ).first()

                if existing:
                    # 更新现有记录
                    existing.tech_score = tech_score
                    existing.tech_reasons = json.dumps(tech_reasons, ensure_ascii=False)
                    if ai_result:
                        existing.ai_sentiment_score = ai_result.sentiment_score
                        existing.ai_operation_advice = ai_result.operation_advice
                        existing.ai_trend_prediction = ai_result.trend_prediction
                        existing.ai_analysis_summary = ai_result.analysis_summary[
                                                       :2000] if ai_result.analysis_summary else None
                    existing.screen_time = datetime.now()
                else:
                    # 创建新记录
                    record = ScreeningResultDB(
                        code=code,
                        name=name,
                        tech_score=tech_score,
                        tech_reasons=json.dumps(tech_reasons, ensure_ascii=False),
                        ai_sentiment_score=ai_result.sentiment_score if ai_result else None,
                        ai_operation_advice=ai_result.operation_advice if ai_result else None,
                        ai_trend_prediction=ai_result.trend_prediction if ai_result else None,
                        ai_analysis_summary=ai_result.analysis_summary[
                                            :2000] if ai_result and ai_result.analysis_summary else None,
                        screen_date=screen_date,
                    )
                    session.add(record)

                session.commit()
                return True

        except Exception as e:
            logger.error(f"保存选股结果失败 {code}: {e}")
            return False

    def get_screening_results(
            self,
            screen_date: Optional[date] = None,
            limit: Optional[int] = None
    ) -> List[ScreeningResultDB]:
        """
        获取指定日期的选股结果

        Args:
            screen_date: 选股日期（默认今天）
            limit: 返回结果数量限制

        Returns:
            ScreeningResultDB 对象列表（按技术评分降序）
        """
        if screen_date is None:
            screen_date = date.today()

        try:
            with self.get_session() as session:
                query = session.query(ScreeningResultDB).filter(
                    ScreeningResultDB.screen_date == screen_date
                ).order_by(desc(ScreeningResultDB.tech_score))

                if limit:
                    query = query.limit(limit)

                results = query.all()
                return list(results)

        except Exception as e:
            logger.error(f"获取选股结果失败: {e}")
            return []

    def has_today_screening(self, target_date: Optional[date] = None) -> bool:
        """
        检查指定日期是否已有选股结果

        Args:
            target_date: 目标日期（默认今天）

        Returns:
            是否存在选股结果
        """
        if target_date is None:
            target_date = date.today()

        try:
            with self.get_session() as session:
                count = session.query(ScreeningResultDB).filter(
                    ScreeningResultDB.screen_date == target_date
                ).count()
                return count > 0

        except Exception as e:
            logger.error(f"检查选股结果失败: {e}")
            return False

    def get_screening_result_by_code(
            self,
            code: str,
            screen_date: Optional[date] = None
    ) -> Optional[ScreeningResultDB]:
        """
        获取指定股票在指定日期的选股结果

        Args:
            code: 股票代码
            screen_date: 选股日期（默认今天）

        Returns:
            ScreeningResultDB 对象或 None
        """
        if screen_date is None:
            screen_date = date.today()

        try:
            with self.get_session() as session:
                result = session.query(ScreeningResultDB).filter(
                    and_(
                        ScreeningResultDB.code == code,
                        ScreeningResultDB.screen_date == screen_date
                    )
                ).first()

                return result

        except Exception as e:
            logger.error(f"获取股票选股结果失败 {code}: {e}")
            return None

    def _analyze_ma_status(self, data: StockDaily) -> str:
        """
        分析均线形态
        
        判断条件：
        - 多头排列：close > ma5 > ma10 > ma20
        - 空头排列：close < ma5 < ma10 < ma20
        - 震荡整理：其他情况
        """
        close = data.close or 0
        ma5 = data.ma5 or 0
        ma10 = data.ma10 or 0
        ma20 = data.ma20 or 0
        
        if close > ma5 > ma10 > ma20 > 0:
            return "多头排列 📈"
        elif close < ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        elif close > ma5 and ma5 > ma10:
            return "短期向好 🔼"
        elif close < ma5 and ma5 < ma10:
            return "短期走弱 🔽"
        else:
            return "震荡整理 ↔️"

class ScreeningResultDB(Base):
    """
    选股结果数据库模型

    存储每日选股结果，包含技术评分和AI分析结果
    支持多股票、多日期的唯一约束
    """
    __tablename__ = 'screening_results'

    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))

    # 技术评分
    tech_score = Column(Float)
    tech_reasons = Column(String(1000))  # JSON 字符串存储理由列表

    # AI 分析结果
    ai_sentiment_score = Column(Integer)
    ai_operation_advice = Column(String(20))
    ai_trend_prediction = Column(String(20))
    ai_analysis_summary = Column(String(2000))

    # 选股时间
    screen_date = Column(Date, nullable=False, index=True)
    screen_time = Column(DateTime, default=datetime.now)

    # 元数据
    created_at = Column(DateTime, default=datetime.now)

    # 唯一约束：同一股票同一天只能有一条选股记录
    __table_args__ = (
        UniqueConstraint('code', 'screen_date', name='uix_screening_code_date'),
        Index('ix_screening_date', 'screen_date'),
    )

    def __repr__(self):
        return f"<ScreeningResultDB(code={self.code}, name={self.name}, screen_date={self.screen_date})>"

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'tech_score': self.tech_score,
            'tech_reasons': self.tech_reasons,
            'ai_sentiment_score': self.ai_sentiment_score,
            'ai_operation_advice': self.ai_operation_advice,
            'ai_trend_prediction': self.ai_trend_prediction,
            'ai_analysis_summary': self.ai_analysis_summary,
            'screen_date': self.screen_date,
            'screen_time': self.screen_time,
        }


# 便捷函数
def get_db() -> DatabaseManager:
    """获取数据库管理器实例的快捷方式"""
    return DatabaseManager.get_instance()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    db = get_db()
    
    print("=== 数据库测试 ===")
    print(f"数据库初始化成功")
    
    # 测试检查今日数据
    has_data = db.has_today_data('600519')
    print(f"茅台今日是否有数据: {has_data}")
    
    # 测试保存数据
    test_df = pd.DataFrame({
        'date': [date.today()],
        'open': [1800.0],
        'high': [1850.0],
        'low': [1780.0],
        'close': [1820.0],
        'volume': [10000000],
        'amount': [18200000000],
        'pct_chg': [1.5],
        'ma5': [1810.0],
        'ma10': [1800.0],
        'ma20': [1790.0],
        'volume_ratio': [1.2],
    })
    
    saved = db.save_daily_data(test_df, '600519', 'TestSource')
    print(f"保存测试数据: {saved} 条")
    
    # 测试获取上下文
    context = db.get_analysis_context('600519')
    print(f"分析上下文: {context}")
