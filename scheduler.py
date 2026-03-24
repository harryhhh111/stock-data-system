"""
调度模块 - 使用APScheduler进行周期性数据同步
使用 SQLAlchemyJobStore 持久化到同一 SQLite 数据库
"""
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

import config
import data_fetcher
import database

logger = logging.getLogger(__name__)

# 全局调度器（使用 SQLAlchemyJobStore 持久化）
scheduler = BlockingScheduler(
    jobstores={
        "default": SQLAlchemyJobStore(url=f"sqlite:///{config.DB_PATH}"),
    },
    executors={
        "default": ThreadPoolExecutor(max_workers=config.SYNC_CONFIG["max_concurrency"]),
    },
    job_defaults={
        "coalesce": True,       # 合并错过的任务
        "max_instances": 1,     # 同一任务最多1个实例
        "misfire_grace_time": 3600,  # 错过执行时间的容忍窗口（秒）
    },
)


def job_sync_stock_list():
    """同步股票列表"""
    logger.info("=" * 40)
    logger.info("Job [sync_stock_list] started")
    try:
        data_fetcher.sync_stock_list()
        logger.info("Job [sync_stock_list] completed")
    except Exception as e:
        logger.error(f"Job [sync_stock_list] failed: {e}")


def job_sync_financial_data():
    """同步财务数据"""
    logger.info("=" * 40)
    logger.info("Job [sync_financial_data] started")
    try:
        data_fetcher.sync_financial_data(quarters=config.SYNC_CONFIG["quarters"])
        logger.info("Job [sync_financial_data] completed")
    except Exception as e:
        logger.error(f"Job [sync_financial_data] failed: {e}")


def job_sync_index_constituent():
    """同步指数成分股"""
    logger.info("=" * 40)
    logger.info("Job [sync_index_constituent] started")
    try:
        data_fetcher.sync_index_constituent()
        logger.info("Job [sync_index_constituent] completed")
    except Exception as e:
        logger.error(f"Job [sync_index_constituent] failed: {e}")


def setup_scheduler():
    """配置调度任务"""
    logger.info("Setting up scheduler...")
    
    # 股票列表同步 - 每周一凌晨2点
    scheduler.add_job(
        job_sync_stock_list,
        CronTrigger(day_of_week="mon", hour=2, minute=0),
        id="sync_stock_list",
        name="同步股票列表",
        replace_existing=True,
    )
    
    # 财务数据同步 - 每周六凌晨2点（避开交易日）
    scheduler.add_job(
        job_sync_financial_data,
        CronTrigger(day_of_week="sat", hour=2, minute=0),
        id="sync_financial_data",
        name="同步财务数据",
        replace_existing=True,
    )
    
    # 指数成分同步 - 每周一凌晨3点
    scheduler.add_job(
        job_sync_index_constituent,
        CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="sync_index_constituent",
        name="同步指数成分",
        replace_existing=True,
    )
    
    logger.info("Scheduler setup completed")
    logger.info("Scheduled jobs:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name}: {job.trigger}")


def run_scheduler():
    """运行调度器"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    # 初始化数据库
    database.init_database()
    
    # 配置调度
    setup_scheduler()
    
    # 启动调度器
    logger.info("Scheduler starting...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    run_scheduler()
