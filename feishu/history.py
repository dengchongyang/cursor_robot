"""
飞书聊天历史获取

支持功能：
- 获取聊天历史消息
- 解析引用回复（parent_id）
- 多种消息类型解析
"""

import time
import httpx
from loguru import logger

from feishu.token import TokenManager
from feishu.user import get_user_name, get_bot_name
from feishu.message_parser import (
    parse_text,
    parse_image,
    parse_interactive,
    parse_post,
    parse_file,
)


def get_message_by_id(message_id: str, token: str) -> str | None:
    """
    获取单条消息内容（用于解析引用消息）
    
    Args:
        message_id: 消息 ID
        token: 访问令牌
        
    Returns:
        str | None: 消息文本内容，失败返回 None
    """
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
    
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("code") != 0:
            return None
        
        item = data.get("data", {}).get("items", [{}])[0]
        msg_type = item.get("msg_type")
        body = item.get("body", {})
        content = body.get("content", "{}")
        mentions = item.get("mentions", [])
        
        # 简单解析常见类型
        if msg_type == "text":
            return parse_text(content, mentions)
        elif msg_type == "post":
            text, _ = parse_post(content, message_id, token)
            return text
        elif msg_type == "interactive":
            return parse_interactive(content)
        else:
            return f"[{msg_type}]"
            
    except Exception as e:
        logger.debug(f"获取引用消息失败: {e}")
        return None


def get_chat_history(chat_id: str, limit: int = 50) -> tuple[list[dict], list[dict]]:
    """
    获取聊天历史消息
    
    Args:
        chat_id: 会话 ID
        limit: 获取消息条数
        
    Returns:
        tuple: (消息列表, 图片列表)
    """
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    token = TokenManager.get_token()

    # 查询最近 24 小时的消息
    end_time = int(time.time())
    start_time = end_time - 86400

    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": str(start_time),
        "end_time": str(end_time),
        "page_size": limit,
        "sort_type": "ByCreateTimeDesc",
    }

    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            logger.warning(f"获取聊天历史失败: {data.get('msg')}")
            return [], []

        items = data.get("data", {}).get("items", [])
        messages = []
        images = []

        for item in reversed(items):
            msg, imgs = _parse_message(item, token)
            if msg:
                messages.append(msg)
            if imgs:
                images.extend(imgs)

        # 限制最多5张图片（Cursor API限制）
        images = images[-5:] if len(images) > 5 else images

        logger.info(f"获取聊天历史成功 | chat_id={chat_id} | msgs={len(messages)} | imgs={len(images)}")
        return messages, images

    except httpx.HTTPError as e:
        logger.error(f"获取聊天历史网络错误: {e}")
        return [], []


def _parse_message(item: dict, token: str) -> tuple[dict | None, list[dict]]:
    """
    解析单条消息，支持引用回复
    
    Returns:
        tuple: (消息dict, 图片列表)
    """
    try:
        msg_type = item.get("msg_type")
        message_id = item.get("message_id")
        parent_id = item.get("parent_id")  # 引用的消息 ID
        body = item.get("body", {})
        content = body.get("content", "{}")

        # 解析发送者和时间
        sender = item.get("sender", {})
        sender_type = sender.get("sender_type")
        sender_id = sender.get("id", "")
        
        create_time = item.get("create_time", "")
        time_str = time.strftime("%H:%M:%S", time.localtime(int(create_time) / 1000)) if create_time else "unknown"
        created_at = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(create_time) / 1000))
            if create_time
            else ""
        )
        
        # 获取发送者名字
        if sender_type == "app":
            sender_name = get_bot_name(sender_id)
        else:
            sender_name = get_user_name(sender_id) if sender_id else "未知用户"

        # 获取 @ 提及信息
        mentions = item.get("mentions", [])
        
        # 获取引用消息内容
        quoted_text = None
        if parent_id:
            quoted_text = get_message_by_id(parent_id, token)

        # 根据消息类型解析内容
        text_content = None
        images = []

        if msg_type == "text":
            text_content = parse_text(content, mentions)

        elif msg_type == "image":
            text_content, img = parse_image(content, message_id, token)
            if img:
                images.append(img)

        elif msg_type == "interactive":
            text_content = parse_interactive(content)

        elif msg_type == "post":
            text_content, images = parse_post(content, message_id, token)

        elif msg_type == "file":
            text_content = parse_file(content, message_id, token)

        elif msg_type in ("media", "audio", "sticker", "share_chat", "share_user", "system"):
            # 其他类型：显示标识
            type_labels = {
                "media": "[媒体]",
                "audio": "[语音]",
                "sticker": "[表情]",
                "share_chat": "[分享群]",
                "share_user": "[分享用户]",
                "system": "[系统消息]",
            }
            text_content = type_labels.get(msg_type, f"[{msg_type}]")

        else:
            # 未知类型，记录日志后跳过
            logger.warning(f"未处理的消息类型: {msg_type}, content: {content[:100] if content else 'empty'}")
            return None, []

        if text_content:
            # 如果有引用，添加引用前缀
            if quoted_text:
                # 截断过长的引用内容
                quote_preview = quoted_text[:50] + "..." if len(quoted_text) > 50 else quoted_text
                text_content = f"[回复: {quote_preview}] {text_content}"
            
            return {
                "message_id": message_id,
                "time": time_str,
                "sender": sender_name,
                "content": text_content,
                "created_at": created_at,
            }, images
        return None, images

    except Exception as e:
        logger.debug(f"解析消息失败: {e}")
        return None, []


def format_history(messages: list[dict]) -> str:
    """格式化历史消息为字符串"""
    if not messages:
        return "（无历史消息）"

    lines = [f"[{msg['time']}] {msg['sender']}: {msg['content']}" for msg in messages]
    return "\n".join(lines)
