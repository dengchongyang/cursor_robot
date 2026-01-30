"""
飞书 Token 管理：获取和缓存 tenant_access_token
"""

import time
import httpx
from loguru import logger

from config import settings


class TokenManager:
    """
    飞书 tenant_access_token 管理器
    - 自动缓存 token
    - 过期前自动刷新
    """

    _token: str | None = None
    _expires_at: float = 0  # 过期时间戳

    # 飞书获取 token 的 API
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

    @classmethod
    def get_token(cls) -> str:
        """
        获取 tenant_access_token
        - 如果缓存有效，直接返回
        - 否则重新获取
        
        Returns:
            str: tenant_access_token
            
        Raises:
            RuntimeError: 获取 token 失败
        """
        # 提前1200秒（20分钟）刷新，飞书只有在剩余<30分钟时才返回新token
        if cls._token and time.time() < cls._expires_at - 1200:
            return cls._token

        logger.info("正在获取新的 tenant_access_token...")
        
        try:
            resp = httpx.post(
                cls.TOKEN_URL,
                json={
                    "app_id": settings.feishu_app_id,
                    "app_secret": settings.feishu_app_secret,
                },
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                raise RuntimeError(f"获取 token 失败: {data.get('msg')}")

            cls._token = data["tenant_access_token"]
            # 飞书 token 有效期通常为 7200 秒（2小时）
            ttl = data.get("expire", 7200)
            cls._expires_at = time.time() + ttl

            logger.info(f"获取 token 成功，有效期 {ttl} 秒")
            return cls._token

        except httpx.HTTPError as e:
            logger.error(f"获取 token HTTP 错误: {e}")
            raise RuntimeError(f"获取 token 失败: {e}") from e

    @classmethod
    def clear_cache(cls):
        """清除缓存的 token"""
        cls._token = None
        cls._expires_at = 0

