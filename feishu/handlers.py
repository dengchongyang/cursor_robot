"""
飞书消息事件处理器

处理逻辑：
1. 单聊/群聊：所有消息都转发给 Agent
2. Agent 自己判断是否需要回复
3. 历史消息已包含当前消息，无需单独解析
"""

import json
import threading
import httpx
from loguru import logger
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from feishu.token import TokenManager
from feishu.history import get_chat_history, format_history
from feishu.user import get_user_name
from cursor.agent import CursorAgent
from knowledge import knowledge_retriever
from prompts.system_prompt import build_prompt
from runtime_memory import memory_store, reflect_and_store

# chat_id -> agent_id 缓存，用于 followup
_agent_cache: dict[str, str] = {}

# chat_id -> Lock，确保同一会话的消息顺序处理
_chat_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()  # 保护 _chat_locks 的锁


def _get_chat_lock(chat_id: str) -> threading.Lock:
    """获取指定会话的锁，不存在则创建"""
    with _locks_lock:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]


def send_error_reply(chat_id: str, error_msg: str = "抱歉，处理请求时出现错误，请稍后重试。"):
    """发送错误兜底回复"""
    try:
        token = TokenManager.get_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": error_msg}),
        }
        resp = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"已发送错误兜底回复 | chat_id={chat_id}")
        else:
            logger.error(f"发送兜底回复失败 | status={resp.status_code}")
    except Exception as e:
        logger.error(f"发送兜底回复异常: {e}")


def _is_bot_mentioned(mentions) -> bool:
    """检查消息中是否 @ 了机器人"""
    if not mentions:
        return False
    bot_name = settings.feishu_bot_name
    for m in mentions:
        if m.name == bot_name:
            return True
    return False


def create_message_handler():
    """创建消息接收事件处理器"""

    def handle_message(data: P2ImMessageReceiveV1) -> None:
        """
        处理接收到的消息事件
        
        根据配置决定是否转发：
        - 单聊：始终转发
        - 群聊：group_chat_mode=all 时转发所有，mention_only 时只转发 @机器人的消息
        """
        try:
            event = data.event
            message = event.message
            sender = event.sender

            message_id = message.message_id
            chat_id = message.chat_id
            chat_type = message.chat_type

            logger.info(f"收到消息 | msg_id={message_id} | chat_type={chat_type}")

            # 群聊 + mention_only 模式：检查是否 @了机器人
            if chat_type == "group" and settings.group_chat_mode == "mention_only":
                if not _is_bot_mentioned(message.mentions):
                    logger.info(f"跳过消息（未@机器人）| msg_id={message_id}")
                    return

            # 获取发送者名字
            sender_open_id = sender.sender_id.open_id if sender.sender_id else ""
            sender_name = get_user_name(sender_open_id) if sender_open_id else "未知用户"

            # 异步处理，立即返回（避免飞书重发）
            thread = threading.Thread(
                target=_process_message,
                args=(message_id, chat_id, chat_type, sender_name),
                daemon=True,
            )
            thread.start()
            logger.info(f"已启动异步处理线程 | msg_id={message_id}")

        except Exception as e:
            logger.exception(f"处理消息时发生错误: {e}")

    return handle_message


def _process_message(message_id: str, chat_id: str, chat_type: str, sender_name: str):
    """
    异步处理消息，创建 Cursor Agent 任务或发送 followup
    
    使用锁确保同一会话的消息顺序处理，避免并发创建多个 Agent
    """
    # 获取会话锁，确保同一 chat_id 的消息顺序处理
    chat_lock = _get_chat_lock(chat_id)
    
    with chat_lock:
        _do_process_message(message_id, chat_id, chat_type, sender_name)


def _do_process_message(message_id: str, chat_id: str, chat_type: str, sender_name: str):
    """实际处理消息的逻辑"""
    try:
        logger.info(f"开始处理消息 | msg_id={message_id} | chat_type={chat_type} | sender={sender_name}")

        # 获取 token
        token = TokenManager.get_token()

        # 获取聊天历史（最近20条，已包含当前消息）
        history, images = get_chat_history(chat_id, limit=20)
        if history:
            memory_store.save_messages(chat_id, chat_type, history)
        else:
            history = memory_store.get_recent_messages(chat_id, limit=20)
            if history:
                logger.info(f"聊天历史接口超时，已回退到本地持久化记忆 | chat_id={chat_id} | msgs={len(history)}")

        history_text = format_history(history)

        # 从历史消息中提取最后一条作为用户消息摘要
        user_message = history[-1]["content"] if history else "[无法获取消息内容]"
        persistent_memory = memory_store.build_memory_digest(chat_id)
        long_term_memories = memory_store.format_long_term_memories(chat_id, limit=6)
        recent_operations = memory_store.format_recent_operations(chat_id, limit=5)
        retrieval_query = "\n".join(filter(None, [user_message, history_text[-1200:]]))
        retrieved_docs = knowledge_retriever.format_for_prompt(retrieval_query, limit=4)
        memory_store.upsert_operation(
            chat_id=chat_id,
            message_id=message_id,
            sender_name=sender_name,
            user_message=user_message,
            history_excerpt=history_text,
            status="received",
        )

        # 构建 prompt
        prompt = build_prompt(
            user_message=user_message,
            chat_id=chat_id,
            tenant_access_token=token,
            chat_history=history_text,
            persistent_memory=persistent_memory,
            long_term_memories=long_term_memories,
            recent_operations=recent_operations,
            retrieved_docs=retrieved_docs,
            sender_name=sender_name,
            chat_type=chat_type,
        )

        # 创建 Agent 任务
        agent = CursorAgent()
        cached_agent_id = _agent_cache.get(chat_id)

        # 优先尝试 followup，失败则创建新 Agent
        result = None
        result_mode = "create_task"
        if cached_agent_id:
            result = agent.send_followup(cached_agent_id, prompt, images=images or None)
            result_mode = "followup"
        if not result:
            result = agent.create_task(prompt, images=images or None)
            result_mode = "create_task"

        # 更新缓存
        if result:
            resolved_agent_id = result.get("id") or cached_agent_id or ""
            _agent_cache[chat_id] = resolved_agent_id
            memory_store.complete_operation(
                chat_id=chat_id,
                message_id=message_id,
                status="succeeded",
                agent_id=resolved_agent_id,
                result_summary=f"{result_mode} 成功，agent_id={resolved_agent_id or 'unknown'}",
            )
            reflect_and_store(
                chat_id=chat_id,
                message_id=message_id,
                user_message=user_message,
                status="succeeded",
                result_summary=f"{result_mode} 成功，agent_id={resolved_agent_id or 'unknown'}",
            )
            logger.info(f"Agent 任务成功 | msg_id={message_id} | agent_id={_agent_cache[chat_id]}")
        else:
            memory_store.complete_operation(
                chat_id=chat_id,
                message_id=message_id,
                status="failed",
                agent_id=cached_agent_id or "",
                result_summary="创建或续接 Agent 失败",
            )
            reflect_and_store(
                chat_id=chat_id,
                message_id=message_id,
                user_message=user_message,
                status="failed",
                result_summary="创建或续接 Agent 失败",
            )
            logger.error(f"Agent 任务失败 | msg_id={message_id}")
            send_error_reply(chat_id, "抱歉，创建任务失败（网络错误），请稍后重试。")

    except Exception as e:
        error_summary = f"异常: {str(e)[:120]}"
        memory_store.complete_operation(
            chat_id=chat_id,
            message_id=message_id,
            status="failed",
            result_summary=error_summary,
        )
        reflect_and_store(
            chat_id=chat_id,
            message_id=message_id,
            user_message=locals().get("user_message", ""),
            status="failed",
            result_summary=error_summary,
        )
        logger.exception(f"异步处理消息失败 | msg_id={message_id} | error={e}")
        send_error_reply(chat_id, f"抱歉，处理请求时出现错误：{str(e)[:50]}")
