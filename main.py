"""
飞书机器人 + Cursor云端Agent 桥接服务
入口文件
"""

import sys
from loguru import logger

from config import settings
from feishu import FeishuClient
from knowledge import knowledge_retriever
from runtime_memory import memory_store


def setup_logging():
    """配置日志"""
    logger.remove()  # 移除默认处理器
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )


def main():
    """主函数"""
    setup_logging()
    memory_store.init_db()
    memory_store.sync_memories_to_markdown()
    knowledge_retriever.sync()

    logger.info("=" * 50)
    logger.info("飞书机器人 + Cursor Agent 桥接服务")
    logger.info("=" * 50)
    logger.info(f"飞书 App ID: {settings.feishu_app_id}")
    logger.info(f"GitHub 仓库: {settings.cursor_github_repo}")
    logger.info(f"GitHub 分支: {settings.cursor_github_ref}")
    logger.info("=" * 50)

    # 创建并启动飞书客户端
    client = FeishuClient()
    client.start()


if __name__ == "__main__":
    main()

