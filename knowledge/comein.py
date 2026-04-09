"""
ComeIn 系统集成模块
实现对 ComeIn 监控日志接口的调用
"""

import time
import json
from typing import Any, Dict, List, Optional
import httpx
from loguru import logger

from config import settings

class ComeInClient:
    """
    ComeIn 系统客户端
    """
    
    BASE_URL = "https://server.comein.cn"
    TOKEN_CACHE_KEY = "comein_token"
    
    def __init__(self):
        self.login_name = settings.comein_login_name
        self.password = settings.comein_password
        self._token = None
        self._load_token()

    def _load_token(self):
        """从缓存加载 token"""
        try:
            from runtime_memory import memory_store
            session = memory_store.get_chat_session("comein_global")
            if session:
                self._token = session.get("token")
        except Exception as e:
            logger.debug(f"加载 ComeIn token 失败: {e}")

    def _save_token(self, token: str):
        """保存 token 到缓存"""
        self._token = token
        try:
            from runtime_memory import memory_store
            memory_store.set_chat_session("comein_global", token=token)
        except Exception as e:
            logger.debug(f"保存 ComeIn token 失败: {e}")

    def login(self, code: str) -> bool:
        """
        使用验证码登录并获取 token
        """
        url = f"{self.BASE_URL}/comein/auth/login" # 假设的登录接口
        payload = {
            "loginName": self.login_name,
            "password": self.password,
            "code": code
        }
        try:
            resp = httpx.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                token = data.get("data", {}).get("token")
                if token:
                    self._save_token(token)
                    return True
            logger.error(f"ComeIn 登录失败: {data.get('msg')}")
            return False
        except Exception as e:
            logger.error(f"ComeIn 登录异常: {e}")
            return False

    def get_user_events(self, phone: str, start_time: int = None, end_time: int = None) -> List[Dict[str, Any]]:
        """
        调用 user-events-page 接口查询日志
        """
        if not self._token:
            logger.warning("ComeIn token 缺失，无法查询日志")
            return []

        url = f"{self.BASE_URL}/comein/meeting/monitor/eventlog/user-events-page"
        
        # 自动取当天时间窗（毫秒）
        if not start_time or not end_time:
            now = time.time()
            local_now = time.localtime(now)
            start_of_day = time.mktime((local_now.tm_year, local_now.tm_mon, local_now.tm_mday, 0, 0, 0, 0, 0, local_now.tm_isdst))
            start_time = int(start_of_day * 1000)
            end_time = int((start_of_day + 86400) * 1000) - 1

        params = {
            "phone": phone,
            "startTime": start_time,
            "endTime": end_time,
            "pageSize": 100,
            "pageNo": 1
        }
        
        headers = {"Authorization": f"Bearer {self._token}"}
        
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=15)
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("list", [])
            elif data.get("code") == 401: # Token 过期
                logger.warning("ComeIn token 已过期")
                self._token = None
                return []
            else:
                logger.error(f"查询 user-events-page 失败: {data.get('msg')}")
                return []
        except Exception as e:
            logger.error(f"查询 user-events-page 异常: {e}")
            return []

comein_client = ComeInClient()
