"""
Cursor Cloud Agent API 客户端
参考文档: https://cursor.com/cn/docs/cloud-agent/api/endpoints

支持的操作:
- 创建 Agent 任务
- 添加后续问题 (followup)
- 获取 Agent 状态
"""

import httpx
import subprocess
from pathlib import Path
from loguru import logger

from config import settings
from network import get_cursor_client, request_with_retry


def _normalize_model_name(model_name: str) -> str:
    """将常见别名归一化为更可能被 Cursor 接受的模型名。"""
    raw = (model_name or "").strip()
    lowered = raw.lower().replace("_", "-").replace(" ", "")

    aliases = {
        "gpt5": "gpt-5",
        "gpt-5": "gpt-5",
        "gpt5.4": "gpt-5",
        "gpt-5.4": "gpt-5",
        "gpt54": "gpt-5",
        "gpt5o": "gpt-5",
        "gpt-5o": "gpt-5",
        "gpt41": "gpt-4.1",
        "gpt-4.1": "gpt-4.1",
        "gpt4.1": "gpt-4.1",
        "gpt41mini": "gpt-4.1-mini",
        "gpt-4.1-mini": "gpt-4.1-mini",
    }
    return aliases.get(lowered, raw)


def _normalize_repository_name(repository: str) -> str:
    """将仓库地址归一化为 Cursor 更容易识别的格式。"""
    repo = (repository or "").strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_git_command(args: list[str]) -> str:
    """读取本地 git 信息，失败时静默返回空。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _detect_local_repository() -> str:
    """优先使用本地仓库 origin，避免环境变量配置过期。"""
    origin = _run_git_command(["remote", "get-url", "origin"])
    return _normalize_repository_name(origin)


def _detect_local_branch() -> str:
    """优先使用本地当前分支，避免配置分支与真实仓库不一致。"""
    branch = _run_git_command(["branch", "--show-current"])
    return branch.strip()


def _build_repository_candidates(repository: str) -> list[str]:
    """构造仓库候选格式，兼容 Cursor 对不同仓库写法的接受度。"""
    normalized = _normalize_repository_name(repository)
    candidates: list[str] = []

    def add(value: str):
        if value and value not in candidates:
            candidates.append(value)

    add(normalized)

    for prefix in ("https://github.com/", "http://github.com/"):
        if normalized.startswith(prefix):
            add(normalized[len(prefix) :])
            break
    else:
        if "/" in normalized and "://" not in normalized:
            add(f"https://github.com/{normalized}")

    return candidates


def _build_model_candidates(model_name: str) -> list[str]:
    """构造模型候选列表，用于不兼容时自动回退。"""
    normalized = _normalize_model_name(model_name)
    candidates: list[str] = []

    def add(name: str):
        if name and name not in candidates:
            candidates.append(name)

    add(normalized)

    lowered = normalized.lower()
    if lowered.startswith("gpt-5"):
        add("gpt-5")
        add("gpt-4.1")
    elif lowered.startswith("gpt"):
        add("gpt-4.1")
    else:
        add(normalized)

    return candidates


def _is_model_rejection(error: httpx.HTTPStatusError) -> bool:
    """判断是否是模型名不被支持导致的 4xx。"""
    body = error.response.text.lower()
    status = error.response.status_code
    if status not in {400, 404, 422}:
        return False
    keywords = [
        "model",
        "unsupported",
        "not found",
        "unknown model",
        "invalid model",
        "does not exist",
    ]
    return any(keyword in body for keyword in keywords)


def _is_repository_or_branch_rejection(error: httpx.HTTPStatusError) -> bool:
    """判断是否是仓库地址或分支校验失败。"""
    body = error.response.text.lower()
    status = error.response.status_code
    if status not in {400, 404, 422}:
        return False
    keywords = [
        "failed to verify existence of branch",
        "branch name is correct",
        "repository",
        "branch",
    ]
    return any(keyword in body for keyword in keywords)


def _extract_error_text(response: httpx.Response | None) -> str:
    """从 HTTP 响应中提取更适合展示的错误摘要。"""
    if response is None:
        return "请求失败"
    try:
        data = response.json()
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        if isinstance(error, dict):
            for key in ("message", "error", "detail"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("message", "msg", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass
    text = (response.text or "").strip()
    return text[:300] if text else "请求失败"


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
        self.repo = _detect_local_repository() or _normalize_repository_name(settings.cursor_github_repo)
        self.ref = _detect_local_branch() or settings.cursor_github_ref
        self.client = get_cursor_client()
        self.last_error_summary = ""
        logger.debug(f"Cursor 仓库配置 | repo={self.repo} | ref={self.ref}")

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

        model_candidates = _build_model_candidates(settings.cursor_model)
        repo_candidates = _build_repository_candidates(self.repo)
        self.last_error_summary = ""

        for repo_index, repo_name in enumerate(repo_candidates):
            for model_index, model_name in enumerate(model_candidates):
                payload = {
                    "prompt": prompt_obj,
                    "model": model_name,
                    "source": {
                        "repository": repo_name,
                        "ref": self.ref,
                    },
                    "target": {
                        "autoCreatePr": False,
                    },
                }

                try:
                    logger.debug(f"创建 Agent 任务 | repo={repo_name} | ref={self.ref} | model={model_name}")

                    resp = request_with_retry(
                        self.client,
                        "POST",
                        url,
                        request_name=f"创建 Agent 任务({model_name})",
                        json=payload,
                        auth=self._get_auth(),
                        headers={"Content-Type": "application/json"},
                        timeout=30,
                    )
                    data = resp.json()
                    if model_name != settings.cursor_model:
                        logger.warning(
                            f"请求模型 {settings.cursor_model} 不可用，已自动回退到 {model_name}"
                        )
                    if repo_name != self.repo:
                        logger.warning(f"仓库地址 {self.repo} 不可用，已自动回退到 {repo_name}")
                    logger.info(
                        f"Agent 任务创建成功 | id={data.get('id')} | status={data.get('status')} | model={model_name}"
                    )
                    self.last_error_summary = ""
                    return data

                except httpx.HTTPStatusError as e:
                    if model_index < len(model_candidates) - 1 and _is_model_rejection(e):
                        logger.warning(
                            f"模型 {model_name} 不被接受，尝试自动回退下一个候选 | status={e.response.status_code}"
                        )
                        continue
                    if repo_index < len(repo_candidates) - 1 and _is_repository_or_branch_rejection(e):
                        logger.warning(
                            f"仓库地址 {repo_name} 分支校验失败，尝试自动回退下一个仓库写法 | status={e.response.status_code}"
                        )
                        break
                    logger.error(
                        f"创建 Agent 任务失败 | repo={repo_name} | ref={self.ref} | model={model_name} | status={e.response.status_code} | body={e.response.text}"
                    )
                    self.last_error_summary = _extract_error_text(e.response)
                    return None
                except httpx.HTTPError as e:
                    logger.error(f"创建 Agent 任务网络错误 | repo={repo_name} | model={model_name} | error={e}")
                    self.last_error_summary = f"网络错误: {str(e)[:200]}"
                    return None

        self.last_error_summary = "创建 Agent 任务失败"
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
        self.last_error_summary = ""

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
            self.last_error_summary = ""
            return data

        except httpx.HTTPStatusError as e:
            logger.warning(f"Followup 失败 | agent_id={agent_id} | status={e.response.status_code}")
            self.last_error_summary = _extract_error_text(e.response)
            return None
        except httpx.HTTPError as e:
            logger.warning(f"Followup 网络错误 | agent_id={agent_id} | error={e}")
            self.last_error_summary = f"网络错误: {str(e)[:200]}"
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

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning(f"获取 Agent 状态触发限流 | agent_id={agent_id} | status=429")
                return None
            logger.error(f"获取 Agent 状态失败 | agent_id={agent_id} | status={e.response.status_code} | body={e.response.text}")
            return None
        except httpx.HTTPError as e:
            logger.error(f"获取 Agent 状态失败: {e}")
            return None
