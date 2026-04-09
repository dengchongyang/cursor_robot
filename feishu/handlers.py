"""
飞书消息事件处理器

处理逻辑：
1. 单聊/群聊：所有消息都转发给 Agent
2. Agent 自己判断是否需要回复
3. 历史消息已包含当前消息，无需单独解析
"""

import json
import threading
import time
import re
import httpx
from loguru import logger
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from cursor import CursorAgent, start_agent_polling
from feishu.message_parser import parse_interactive, parse_text
from feishu.token import TokenManager
from feishu.history import get_chat_history, format_history
from feishu.user import get_user_name
from knowledge import knowledge_retriever, comein_client
from network import get_feishu_client, request_with_retry
from prompts.system_prompt import build_prompt
from quick_reply import generate_quick_reply
from runtime_memory import memory_store, reflect_and_store

# 手机号提取正则
PHONE_REGEX = re.compile(r"1[3-9]\d{9}")
CODE_REGEX = re.compile(r"\b\d{6}\b")

# chat_id -> agent_id 缓存，用于 followup
_agent_cache: dict[str, str] = {}

# chat_id -> Lock，确保同一会话的消息顺序处理
_chat_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()  # 保护 _chat_locks 的锁
_feishu_client = get_feishu_client()


def _get_chat_lock(chat_id: str) -> threading.Lock:
    """获取指定会话的锁，不存在则创建"""
    with _locks_lock:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]


def send_error_reply(chat_id: str, error_msg: str = "抱歉，处理请求时出现错误，请稍后重试。"):
    """发送错误兜底回复"""
    send_text_reply(chat_id, error_msg)


def _build_user_facing_error(error_summary: str, fallback: str) -> str:
    """将底层错误转换为更适合发给飞书用户的文本。"""
    summary = (error_summary or "").strip()
    if not summary:
        return fallback
    summary = summary.replace("\n", " ").strip()
    return f"{fallback}\n原因：{summary[:220]}"


def send_text_reply(chat_id: str, text: str):
    """发送文本消息。"""
    try:
        token = TokenManager.get_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        resp = request_with_retry(
            _feishu_client,
            "POST",
            url,
            request_name="发送飞书文本回复",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"已发送文本回复 | chat_id={chat_id}")
        else:
            logger.error(f"发送文本回复失败 | status={resp.status_code}")
    except Exception as e:
        logger.error(f"发送文本回复异常: {e}")


def _is_bot_mentioned(mentions) -> bool:
    """检查消息中是否 @ 了机器人"""
    if not mentions:
        return False
    bot_name = settings.feishu_bot_name
    for m in mentions:
        if m.name == bot_name:
            return True
    return False


def _extract_current_message_preview(message) -> str:
    """直接从飞书事件体提取当前消息预览，避免误拿历史中的上一条消息。"""
    try:
        msg_type = getattr(message, "message_type", "") or ""
        content = getattr(message, "content", "") or ""
        mentions = getattr(message, "mentions", None)

        if msg_type == "text":
            return parse_text(content, mentions) or ""
        if msg_type == "interactive":
            return parse_interactive(content) or "[卡片消息]"
        if msg_type == "post":
            try:
                data = json.loads(content)
                return json.dumps(data, ensure_ascii=False)[:200]
            except Exception:
                return "[富文本消息]"
        if msg_type == "image":
            return "[图片]"
        if msg_type == "file":
            try:
                file_info = json.loads(content)
                file_name = file_info.get("file_name", "文件")
                return f"[文件: {file_name}]"
            except Exception:
                return "[文件]"
        return ""
    except Exception:
        return ""


def _sanitize_quick_reply_source(text: str) -> str:
    """过滤明显属于旧错误提示的内容，避免首答串味。"""
    normalized = (text or "").strip()
    if not normalized:
        return ""

    blocked_markers = [
        "任务处理结束，但状态为",
        "可在 Cursor 查看详情",
        "已收到，你现在想处理的是：已收到，你现在想处理的是：",
        "抱歉，创建或续接任务失败",
    ]
    if any(marker in normalized for marker in blocked_markers):
        return ""
    return normalized


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
            current_message_preview = _extract_current_message_preview(message)

            # 异步处理，立即返回（避免飞书重发）
            thread = threading.Thread(
                target=_process_message,
                args=(message_id, chat_id, chat_type, sender_name, current_message_preview),
                daemon=True,
            )
            thread.start()
            logger.info(f"已启动异步处理线程 | msg_id={message_id}")

        except Exception as e:
            logger.exception(f"处理消息时发生错误: {e}")

    return handle_message


def _process_message(
    message_id: str,
    chat_id: str,
    chat_type: str,
    sender_name: str,
    current_message_preview: str = "",
):
    """
    异步处理消息，创建 Cursor Agent 任务或发送 followup
    
    使用锁确保同一会话的消息顺序处理，避免并发创建多个 Agent
    """
    # 获取会话锁，确保同一 chat_id 的消息顺序处理
    chat_lock = _get_chat_lock(chat_id)
    
    with chat_lock:
        _do_process_message(message_id, chat_id, chat_type, sender_name, current_message_preview)


def _do_process_message(
    message_id: str,
    chat_id: str,
    chat_type: str,
    sender_name: str,
    current_message_preview: str = "",
):
    """实际处理消息的逻辑"""
    started_at = time.perf_counter()
    user_message = ""
    try:
        logger.info(f"开始处理消息 | msg_id={message_id} | chat_type={chat_type} | sender={sender_name}")

        # 获取 token
        token = TokenManager.get_token()
        token_ready_at = time.perf_counter()

        # 获取聊天历史（最近若干条，已包含当前消息）
        history, images = get_chat_history(chat_id, limit=settings.history_message_limit)
        if history:
            memory_store.save_messages(chat_id, chat_type, history)
        else:
            history = memory_store.get_recent_messages(chat_id, limit=settings.history_message_limit)
            if history:
                logger.info(f"聊天历史接口超时，已回退到本地持久化记忆 | chat_id={chat_id} | msgs={len(history)}")
        history_ready_at = time.perf_counter()

        history_text = format_history(history)

        # 优先使用当前事件体中的消息，避免历史接口尚未包含当前消息时误拿上一条内容
        fallback_history_message = history[-1]["content"] if history else "[无法获取消息内容]"
        user_message = _sanitize_quick_reply_source(current_message_preview) or _sanitize_quick_reply_source(
            fallback_history_message
        ) or "[无法获取当前消息内容]"

        # ComeIn 逻辑集成
        comein_info = ""
        if settings.comein_enabled:
            phone_match = PHONE_REGEX.search(user_message)
            code_match = CODE_REGEX.search(user_message)
            
            if phone_match:
                phone = phone_match.group()
                # 检查是否需要验证码（每日首次或 Token 失效）
                needs_code = memory_store.is_first_query_today("comein") or not comein_client._token
                
                if code_match:
                    code = code_match.group()
                    if comein_client.login(code):
                        send_text_reply(chat_id, f"验证码 {code} 已收到。我正在调用 ComeIn 系统深度调取 {phone} 今日的参会质量数据，请稍候。")
                    else:
                        send_text_reply(chat_id, "验证码登录失败，请重新发送 6 位验证码。")
                        return
                elif needs_code:
                    send_text_reply(chat_id, f"检测到你提到了手机号 {phone}，请把今天的 6 位验证码发给我，我才能继续查询外部系统。")
                    return
                
                # 调用接口查询日志
                events = comein_client.get_user_events(phone)
                if events:
                    comein_info = f"\n\n# ComeIn 外部查询结果\n手机号 {phone} 今日 user-events-page 日志：\n" + json.dumps(events, ensure_ascii=False, indent=2)
                else:
                    comein_info = f"\n\n# ComeIn 外部查询结果\n手机号 {phone} 今日未查询到 user-events-page 日志。"

        quick_reply_text = generate_quick_reply(user_message, chat_type, sender_name)
        if quick_reply_text:
            send_text_reply(chat_id, quick_reply_text)
        elif chat_type == "p2p" and settings.send_processing_reply_in_p2p:
            send_text_reply(chat_id, settings.processing_reply_text)
        persistent_memory = memory_store.build_memory_digest(chat_id)
        long_term_memories = memory_store.format_long_term_memories(
            chat_id, limit=settings.long_term_memory_limit
        )
        recent_operations = memory_store.format_recent_operations(
            chat_id, limit=settings.recent_operations_limit
        )
        retrieval_query = "\n".join(filter(None, [user_message, history_text[-600:]]))
        retrieved_docs = knowledge_retriever.format_for_prompt(
            retrieval_query, limit=settings.knowledge_retrieval_limit
        )
        prompt_context_ready_at = time.perf_counter()
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
            user_message=user_message + comein_info,
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
        prompt_ready_at = time.perf_counter()

        # 创建 Agent 任务
        agent = CursorAgent()
        persisted_session = memory_store.get_chat_session(chat_id)
        cached_agent_id = _agent_cache.get(chat_id) or (persisted_session or {}).get("agent_id", "")

        # 优先尝试 followup，失败则创建新 Agent
        result = None
        result_mode = "create_task"
        if cached_agent_id:
            result = agent.send_followup(cached_agent_id, prompt, images=images or None)
            result_mode = "followup"
        if not result:
            result = agent.create_task(prompt, images=images or None)
            result_mode = "create_task"
        agent_request_ready_at = time.perf_counter()

        # 更新缓存
        if result:
            resolved_agent_id = result.get("id") or cached_agent_id or ""
            target = result.get("target") or {}
            cursor_url = target.get("url", "") or ""
            _agent_cache[chat_id] = resolved_agent_id
            memory_store.set_chat_session(
                chat_id=chat_id,
                agent_id=resolved_agent_id,
                status=result.get("status") or "SUBMITTED",
                cursor_url=cursor_url,
            )
            memory_store.complete_operation(
                chat_id=chat_id,
                message_id=message_id,
                status="submitted",
                agent_id=resolved_agent_id,
                result_summary=f"{result_mode} 已提交，agent_id={resolved_agent_id or 'unknown'}",
                cursor_url=cursor_url,
                polled_status=result.get("status") or "SUBMITTED",
            )
            start_agent_polling(
                chat_id=chat_id,
                message_id=message_id,
                agent_id=resolved_agent_id,
                notify=lambda text: send_text_reply(chat_id, text),
            )
            logger.info(f"Agent 任务成功 | msg_id={message_id} | agent_id={_agent_cache[chat_id]}")
        else:
            if cached_agent_id:
                memory_store.set_chat_session(chat_id=chat_id, agent_id=cached_agent_id, status="FOLLOWUP_FAILED")
            failure_summary = agent.last_error_summary or "创建或续接 Agent 失败"
            memory_store.complete_operation(
                chat_id=chat_id,
                message_id=message_id,
                status="failed",
                agent_id=cached_agent_id or "",
                result_summary=failure_summary,
            )
            reflect_and_store(
                chat_id=chat_id,
                message_id=message_id,
                user_message=user_message,
                status="failed",
                result_summary=failure_summary,
            )
            logger.error(f"Agent 任务失败 | msg_id={message_id}")
            send_error_reply(
                chat_id,
                _build_user_facing_error(
                    failure_summary,
                    "抱歉，创建或续接任务失败，请检查配置或稍后重试。",
                ),
            )

        logger.info(
            "处理耗时 | msg_id={} | token={:.2f}s | history={:.2f}s | context={:.2f}s | prompt={:.2f}s | agent_api={:.2f}s | total={:.2f}s".format(
                message_id,
                token_ready_at - started_at,
                history_ready_at - token_ready_at,
                prompt_context_ready_at - history_ready_at,
                prompt_ready_at - prompt_context_ready_at,
                agent_request_ready_at - prompt_ready_at,
                agent_request_ready_at - started_at,
            )
        )

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
