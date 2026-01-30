"""
飞书 SDK 长连接客户端
"""

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from loguru import logger

from config import settings
from feishu.handlers import create_message_handler


class FeishuClient:
    """
    飞书长连接客户端
    - 使用 WebSocket 接收事件
    - 无需公网域名
    """

    def __init__(self):
        """初始化飞书客户端"""
        self.app_id = settings.feishu_app_id
        self.app_secret = settings.feishu_app_secret

        # 创建事件处理器
        self.event_handler = self._create_event_handler()

        # 创建长连接客户端
        self.ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=self.event_handler,
            log_level=lark.LogLevel.INFO,
        )

    def _create_event_handler(self):
        """
        创建事件分发处理器
        
        Returns:
            EventDispatcherHandler: 事件处理器
        """
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(create_message_handler())
            .build()
        )
        return handler

    def start(self):
        """启动长连接，开始接收事件"""
        logger.info(f"启动飞书长连接客户端 | app_id={self.app_id}")
        logger.info("等待接收消息...")
        self.ws_client.start()

