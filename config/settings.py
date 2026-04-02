"""
配置模块：使用 pydantic-settings 读取环境变量
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    # 飞书配置
    feishu_app_id: str
    feishu_app_secret: str
    feishu_bot_name: str = ""  # 机器人名称，用于群聊@判断
    feishu_master_name: str = ""  # 主人名字，用于Agent识别（可选）

    # Cursor配置
    cursor_api_key: str
    cursor_github_repo: str = ""  # 必填，GitHub 仓库地址
    cursor_github_ref: str = "main"
    cursor_model: str = "gemini-3-flash"  # Agent使用的模型
    memory_db_path: str = "data/robot_memory.db"

    # 群聊消息模式：all=所有消息都转发 | mention_only=只有@机器人才转发
    group_chat_mode: str = "mention_only"

    # 时区配置
    timezone: str = "Asia/Shanghai"

    # 日志级别
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# 全局配置实例
settings = Settings()

