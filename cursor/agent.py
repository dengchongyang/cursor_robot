"""
Cursor Cloud Agent API 客户端
参考文档: https://cursor.com/cn/docs/cloud-agent/api/endpoints

支持的操作:
- 创建 Agent 任务
- 添加后续问题 (followup)
- 获取 Agent 状态
"""

import httpx
from loguru import logger

from config import settings
from network import get_cursor_client, request_with_retry


class CursorAgent:
    """
    Cursor 云端 Agent 客户端
    - 创建 Agent 任务
    - 添加后续问题 (followup)
    - 获取 Agent 状态
    """

    BASE_URL = "https://api.cursor.com"

    def __init__(self):
        """初始化客户端"""
        self.api_key = settings.cursor_api_key
        self.repo = settings.cursor_github_repo
        self.ref = settings.cursor_github_ref
        self.client = get_cursor_client()

    def _get_auth(self) -> tuple[str, str]:
        """
        获取 Basic Auth 认证信息
        Cursor API 使用 API Key 作为用户名，密码为空
        
        Returns:
            tuple: (api_key, "")
        """
        return (self.api_key, "")

    def create_task(self, prompt: str, images: list[dict] | None = None) -> dict | None:
        """
        创建 Agent 任务
        
        Args:
            prompt: 完整的 prompt（包含 system prompt + 用户消息 + 上下文）
            images: 图片列表，格式 [{"data": "base64...", "dimension": {"width": w, "height": h}}]
            
        Returns:
            dict: Agent 响应，包含 id, status 等
            None: 创建失败
        """
        url = f"{self.BASE_URL}/v0/agents"

        prompt_obj = {"text": prompt}
        if images:
            prompt_obj["images"] = images[:5]  # 最多5张

        payload = {
            "prompt": prompt_obj,
            "model": settings.cursor_model,
            "source": {
                "repository": self.repo,
                "ref": self.ref,
            },
            "target": {
                "autoCreatePr": False,
            },
        }

        try:
            logger.debug(f"创建 Agent 任务 | repo={self.repo} | ref={self.ref}")

            resp = request_with_retry(
                self.client,
                "POST",
                url,
                request_name="创建 Agent 任务",
                json=payload,
                auth=self._get_auth(),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            data = resp.json()

            logger.info(f"Agent 任务创建成功 | id={data.get('id')} | status={data.get('status')}")
            return data

        except httpx.HTTPStatusError as e:
            logger.error(f"创建 Agent 任务失败 | status={e.response.status_code} | body={e.response.text}")
            return None
        except httpx.HTTPError as e:
            logger.error(f"创建 Agent 任务网络错误: {e}")
            return None

    def send_followup(self, agent_id: str, prompt: str, images: list[dict] | None = None) -> dict | None:
        """
        向已有 Agent 添加后续问题
        
        Args:
            agent_id: Agent ID (如 bc_abc123)
            prompt: 后续问题内容
            images: 图片列表，格式同 create_task
            
        Returns:
            dict: Agent 响应
            None: 发送失败（Agent 可能已完成或不存在）
        """
        url = f"{self.BASE_URL}/v0/agents/{agent_id}/followup"

        prompt_obj = {"text": prompt}
        if images:
            prompt_obj["images"] = images[:5]

        payload = {"prompt": prompt_obj}

        try:
            logger.debug(f"发送 followup | agent_id={agent_id}")

            resp = request_with_retry(
                self.client,
                "POST",
                url,
                request_name="发送 Agent followup",
                json=payload,
                auth=self._get_auth(),
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            data = resp.json()

            logger.info(f"Followup 发送成功 | agent_id={agent_id}")
            return data

        except httpx.HTTPStatusError as e:
            logger.warning(f"Followup 失败 | agent_id={agent_id} | status={e.response.status_code}")
            return None
        except httpx.HTTPError as e:
            logger.warning(f"Followup 网络错误 | agent_id={agent_id} | error={e}")
            return None

    def get_status(self, agent_id: str) -> dict | None:
        """
        获取 Agent 状态
        
        Args:
            agent_id: Agent ID (如 bc_abc123)
            
        Returns:
            dict: Agent 状态信息
            None: 查询失败
        """
        url = f"{self.BASE_URL}/v0/agents/{agent_id}"

        try:
            resp = request_with_retry(
                self.client,
                "GET",
                url,
                request_name="获取 Agent 状态",
                auth=self._get_auth(),
                timeout=settings.cursor_status_timeout_seconds,
            )
            return resp.json()

        except httpx.HTTPError as e:
            logger.error(f"获取 Agent 状态失败: {e}")
            return None
