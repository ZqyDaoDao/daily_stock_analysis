# -*- coding: utf-8 -*-
"""
===================================
数据同步相关 API 路由
===================================
"""

import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks

from api.schemas.common import (
    ApiResponse,
    DataSyncStatus,
    SyncRequest,
    DownloadRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# 全局同步状态
sync_status = {
    "is_syncing": False,
    "progress": 0,
    "message": "",
    "last_update": None,
    "error": None
}


def _get_data_sync_status_impl() -> dict:
    """获取数据同步状态（内部实现）"""
    from core.csv_manager import CsvDataManager
    from datetime import date as date_class
    from pathlib import Path

    csv_mgr = CsvDataManager.get_instance()

    # 获取股票数量
    stock_count = csv_mgr.get_stock_count()

    # 获取日期范围
    min_date, max_date = csv_mgr.get_date_range()

    # 格式化日期范围
    date_range_str = None
    if min_date and max_date:
        date_range_str = f"{min_date} ~ {max_date}"

    # 获取最后更新时间（从文件修改时间）
    last_update = None
    if max_date:
        # 使用最新日期作为最后更新时间
        last_update = datetime.combine(max_date, datetime.min.time())

    # 计算数据完整度（基于最新数据日期）
    data_completeness = 0.0
    if max_date:
        today = date_class.today()
        days_diff = (today - max_date).days

        if days_diff <= 1:  # 最新数据是今天或昨天（考虑周末/节假日）
            data_completeness = 100.0
        elif days_diff <= 3:  # 3天内
            data_completeness = 80.0
        elif days_diff <= 7:  # 一周内
            data_completeness = 60.0
        else:  # 超过一周，按比例递减
            data_completeness = max(0.0, 50.0 - days_diff * 2)

    return {
        "last_update": last_update.isoformat() if last_update else None,
        "stock_count": stock_count,
        "date_range": date_range_str,
        "data_completeness": data_completeness,
    }


@router.get("/status", response_model=ApiResponse[DataSyncStatus])
async def get_data_status():
    """
    查看数据同步状态

    返回当前数据同步的状态信息
    """
    try:
        status = _get_data_sync_status_impl()

        return ApiResponse[DataSyncStatus](
            success=True,
            data=DataSyncStatus(**status),
            message="获取状态成功"
        )
    except Exception as e:
        logger.error(f"获取数据状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync", response_model=ApiResponse[dict])
async def sync_data(
    request: SyncRequest,
    background_tasks: BackgroundTasks
):
    """
    增量同步最新K线数据

    获取最新交易日的数据并更新到数据库
    """
    global sync_status

    if sync_status["is_syncing"]:
        return ApiResponse[dict](
            success=False,
            data=None,
            message="已有同步任务正在执行"
        )

    try:
        # 标记同步开始
        sync_status["is_syncing"] = True
        sync_status["progress"] = 0
        sync_status["message"] = "正在准备同步..."
        sync_status["error"] = None

        # 添加后台任务
        background_tasks.add_task(
            _execute_sync_task,
            request.codes
        )

        return ApiResponse[dict](
            success=True,
            data={"task_started": True},
            message="同步任务已启动"
        )

    except Exception as e:
        sync_status["is_syncing"] = False
        sync_status["error"] = str(e)
        logger.error(f"启动同步任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _execute_sync_task(codes: Optional[list] = None):
    """执行同步任务（后台）- 增量同步K线数据到CSV文件"""
    global sync_status

    try:
        sync_status["message"] = "正在同步K线数据..."
        sync_status["progress"] = 10

        # 使用CSV数据管理器
        from data_provider.akshare_fetcher import AkshareFetcher
        from core.csv_manager import CsvDataManager

        fetcher = AkshareFetcher()
        csv_mgr = CsvDataManager.get_instance()

        # 获取要同步的股票代码
        if not codes:
            # 从CSV文件中获取已有数据的股票列表
            codes = csv_mgr.get_all_stocks()

            if not codes:
                # 使用默认股票列表
                codes = ['600519', '000001', '300750']

        sync_status["progress"] = 30
        total = len(codes)

        # 连续失败计数器（熔断机制）：连续N次失败后停止
        MAX_CONSECUTIVE_FAILURES = 5
        consecutive_failures = 0

        for i, code in enumerate(codes):
            try:
                # 获取最新数据（使用 get_daily_data 方法）
                df = fetcher.get_daily_data(code, days=5)  # 获取最近5天数据

                if df is not None and not df.empty:
                    csv_mgr.save_daily_data(df, code)
                    consecutive_failures = 0  # 重置连续失败计数
                else:
                    consecutive_failures += 1

                sync_status["progress"] = 30 + int(70 * (i + 1) / total)
                sync_status["message"] = f"正在同步 {code} ({i+1}/{total})..."

            except Exception as e:
                consecutive_failures += 1
                logger.warning(f"同步 {code} 失败: {e}")

                # 熔断机制：连续失败超过阈值则停止
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(f"连续 {MAX_CONSECUTIVE_FAILURES} 只股票同步失败，停止同步任务（可能网络异常或API限流）")
                    sync_status["message"] = f"同步已中止：连续{MAX_CONSECUTIVE_FAILURES}次失败（网络异常或API限流）"
                    sync_status["error"] = f"连续{MAX_CONSECUTIVE_FAILURES}次失败，已停止"
                    break

                continue

        sync_status["progress"] = 100
        sync_status["message"] = "同步完成"
        sync_status["last_update"] = datetime.now().isoformat()

    except Exception as e:
        sync_status["error"] = str(e)
        sync_status["message"] = f"同步失败: {str(e)}"
        logger.error(f"同步任务执行失败: {e}")
    finally:
        sync_status["is_syncing"] = False


@router.post("/download", response_model=ApiResponse[dict])
async def download_historical_data(
    request: DownloadRequest,
    background_tasks: BackgroundTasks
):
    """
    下载历史数据

    下载指定股票在指定日期范围内的历史K线数据
    """
    global sync_status

    if sync_status["is_syncing"]:
        return ApiResponse[dict](
            success=False,
            data=None,
            message="已有同步任务正在执行"
        )

    try:
        # 验证日期范围
        if request.end_date and request.start_date > request.end_date:
            raise HTTPException(
                status_code=400,
                detail="开始日期不能晚于结束日期"
            )

        # 标记同步开始
        sync_status["is_syncing"] = True
        sync_status["progress"] = 0
        sync_status["message"] = "正在准备下载..."
        sync_status["error"] = None

        # 添加后台任务
        background_tasks.add_task(
            _execute_download_task,
            request.codes,
            request.start_date,
            request.end_date
        )

        return ApiResponse[dict](
            success=True,
            data={"task_started": True},
            message="下载任务已启动"
        )

    except HTTPException:
        raise
    except Exception as e:
        sync_status["is_syncing"] = False
        sync_status["error"] = str(e)
        logger.error(f"启动下载任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _execute_download_task(
    codes: list,
    start_date: date,
    end_date: Optional[date] = None
):
    """执行下载任务（后台）- 下载历史K线数据到CSV文件"""
    global sync_status

    try:
        from data_provider.akshare_fetcher import AkshareFetcher
        from core.csv_manager import CsvDataManager

        fetcher = AkshareFetcher()
        csv_mgr = CsvDataManager.get_instance()

        if not end_date:
            end_date = date.today()

        sync_status["message"] = f"正在下载 {len(codes)} 只股票的历史数据..."
        sync_status["progress"] = 10

        total = len(codes)

        # 连续失败计数器（熔断机制）：连续N次失败后停止
        MAX_CONSECUTIVE_FAILURES = 5
        consecutive_failures = 0

        for i, code in enumerate(codes):
            try:
                # 使用 get_daily_data 方法获取历史数据
                df = fetcher.get_daily_data(
                    stock_code=code,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat()
                )

                if df is not None and not df.empty:
                    csv_mgr.save_daily_data(df, code)
                    consecutive_failures = 0  # 重置连续失败计数
                elif df is not None:
                    sync_status["message"] = f"跳过 {code}（无数据）"
                else:
                    logger.warning(f"下载 {code} 数据失败: 返回None")
                    consecutive_failures += 1

                sync_status["progress"] = 10 + int(90 * (i + 1) / total)
                sync_status["message"] = f"正在下载 {code} ({i+1}/{total})..."

            except Exception as e:
                consecutive_failures += 1
                logger.warning(f"下载 {code} 数据失败: {e}")

                # 熔断机制：连续失败超过阈值则停止
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(f"连续 {MAX_CONSECUTIVE_FAILURES} 只股票下载失败，停止下载任务（可能网络异常或API限流）")
                    sync_status["message"] = f"下载已中止：连续{MAX_CONSECUTIVE_FAILURES}次失败（网络异常或API限流）"
                    sync_status["error"] = f"连续{MAX_CONSECUTIVE_FAILURES}次失败，已停止"
                    break

                continue

        sync_status["progress"] = 100
        sync_status["message"] = "下载完成"
        sync_status["last_update"] = datetime.now().isoformat()

    except Exception as e:
        sync_status["error"] = str(e)
        sync_status["message"] = f"下载失败: {str(e)}"
        logger.error(f"下载任务执行失败: {e}")
    finally:
        sync_status["is_syncing"] = False


@router.get("/progress", response_model=ApiResponse[dict])
async def get_sync_progress():
    """
    获取同步进度

    返回当前正在进行的同步或下载任务的进度
    """
    return ApiResponse[dict](
        success=True,
        data=sync_status.copy(),
        message="获取进度成功"
    )


@router.post("/market-sync", response_model=ApiResponse[dict])
async def market_sync(background_tasks: BackgroundTasks):
    """
    全市场行情同步

    获取A股全市场最新行情数据并更新到数据库
    """
    global sync_status

    if sync_status["is_syncing"]:
        return ApiResponse[dict](
            success=False,
            data=None,
            message="已有同步任务正在执行"
        )

    try:
        # 标记同步开始
        sync_status["is_syncing"] = True
        sync_status["progress"] = 0
        sync_status["message"] = "正在获取全市场行情..."
        sync_status["error"] = None

        # 添加后台任务
        background_tasks.add_task(
            _execute_market_sync_task
        )

        return ApiResponse[dict](
            success=True,
            data={"task_started": True},
            message="全市场同步任务已启动"
        )

    except Exception as e:
        sync_status["is_syncing"] = False
        sync_status["error"] = str(e)
        logger.error(f"启动全市场同步失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _execute_market_sync_task():
    """执行全市场同步任务（后台）- 同步全市场股票K线数据"""
    global sync_status

    try:
        from data_provider.akshare_fetcher import AkshareFetcher
        from core.csv_manager import CsvDataManager
        import akshare as ak

        fetcher = AkshareFetcher()
        csv_mgr = CsvDataManager.get_instance()

        sync_status["message"] = "正在获取全市场股票列表..."
        sync_status["progress"] = 5

        # 获取全市场A股股票列表
        try:
            sync_status["message"] = "正在获取全市场股票列表..."
            sync_status["progress"] = 10

            # 获取A股股票列表（使用stock_zh_a_spot_em获取所有A股代码）
            spot_df = ak.stock_zh_a_spot_em()

            if spot_df is None or spot_df.empty:
                raise Exception("获取全市场股票列表失败")

            sync_status["progress"] = 20
            stock_count = len(spot_df)
            logger.info(f"获取到 {stock_count} 只股票")

        except Exception as e:
            sync_status["error"] = str(e)
            sync_status["message"] = f"获取全市场股票列表失败: {str(e)}"
            sync_status["is_syncing"] = False
            return

        # 过滤出需要的股票代码
        codes = spot_df['代码'].tolist()

        # 同步每只股票的最新K线数据
        total = len(codes)
        synced = 0
        failed = 0

        # 连续失败计数器（熔断机制）：连续N次失败后停止
        MAX_CONSECUTIVE_FAILURES = 10  # 全市场同步阈值稍高
        consecutive_failures = 0

        for i, code in enumerate(codes):
            try:
                # 获取最新几天K线数据
                df = fetcher.get_daily_data(code, days=3)

                if df is not None and not df.empty:
                    csv_mgr.save_daily_data(df, code)
                    synced += 1
                    consecutive_failures = 0  # 重置连续失败计数
                else:
                    consecutive_failures += 1

                sync_status["progress"] = 20 + int(75 * (i + 1) / total)
                sync_status["message"] = f"正在同步K线数据 {code} ({synced}/{total})..."

                # 每处理50只股票，稍作休息避免请求过快
                if (i + 1) % 50 == 0:
                    import time
                    time.sleep(1)

            except Exception as e:
                failed += 1
                consecutive_failures += 1
                logger.warning(f"同步 {code} 失败: {e}")

                # 熔断机制：连续失败超过阈值则停止
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(f"连续 {MAX_CONSECUTIVE_FAILURES} 只股票同步失败，停止全市场同步任务（可能网络异常或API限流）")
                    sync_status["message"] = f"全市场同步已中止：连续{MAX_CONSECUTIVE_FAILURES}次失败（网络异常或API限流）"
                    sync_status["error"] = f"连续{MAX_CONSECUTIVE_FAILURES}次失败，已停止"
                    break

                continue

        sync_status["progress"] = 100
        sync_status["message"] = f"全市场K线数据同步完成！成功 {synced} 只，失败 {failed} 只"
        sync_status["last_update"] = datetime.now().isoformat()

    except Exception as e:
        sync_status["error"] = str(e)
        sync_status["message"] = f"全市场同步失败: {str(e)}"
        logger.error(f"全市场同步任务执行失败: {e}")
    finally:
        sync_status["is_syncing"] = False
