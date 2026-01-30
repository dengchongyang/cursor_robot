"""
飞书用户/机器人信息获取
"""

import httpx
from loguru import logger

from feishu.token import TokenManager
from config import settings

# 用户名缓存：open_id -> name
_user_cache: dict[str, str] = {}


def get_user_name(open_id: str) -> str:
    """
    根据 open_id 获取用户姓名
    
    Args:
        open_id: 用户的 open_id
        
    Returns:
        str: 用户姓名，获取失败返回简写
    """
    if open_id in _user_cache:
        return _user_cache[open_id]

    url = f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}"
    token = TokenManager.get_token()

    try:
        resp = httpx.get(
            url,
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") == 0:
            name = data.get("data", {}).get("user", {}).get("name", "")
            if name:
                _user_cache[open_id] = name
                return name

    except Exception as e:
        logger.debug(f"获取用户名失败: {e}")

    fallback = f"用户_{open_id[-4:]}"
    _user_cache[open_id] = fallback
    return fallback


def get_bot_name(app_id: str) -> str:
    """
    获取机器人名字
    
    飞书没有专门的 API 获取机器人名字，采用以下策略：
    1. 如果是自己的机器人（app_id 匹配），返回配置的名字
    2. 否则返回 app_id 后4位标识
    
    Args:
        app_id: 机器人的 app_id（如 cli_xxxxxxxxxx）
        
    Returns:
        str: 机器人名字
    """
    # 检查是否是自己的机器人
    my_app_id = settings.feishu_app_id
    if app_id == my_app_id:
        return settings.feishu_bot_name
    
    # 其他机器人，返回简写标识
    return f"机器人_{app_id[-4:]}" if app_id else "机器人"
