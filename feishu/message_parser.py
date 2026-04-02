"""
飞书消息内容解析模块

支持的消息类型：
- text: 文本消息
- image: 图片消息（下载 base64）
- interactive: 卡片消息（提取全部文本）
- post: 富文本消息（提取全部文本）
- file: 文件消息（文档类下载解析，其他只提取文件名）
"""

import json
import base64
import io
import httpx
from loguru import logger

from network import get_feishu_client, request_with_retry

_feishu_client = get_feishu_client()


def parse_text(content: str, mentions: list[dict] | None = None) -> str:
    """
    解析文本消息，并替换 @ 提及的占位符
    
    Args:
        content: 消息内容 JSON 字符串
        mentions: @ 提及列表，格式 [{"key": "@_user_1", "name": "张三"}, ...]
        
    Returns:
        str: 解析后的文本，@ 占位符已替换为实际名字
    """
    try:
        text = json.loads(content).get("text", "")
        
        # 替换 @ 提及的占位符
        if mentions and text:
            for mention in mentions:
                key = mention.get("key", "")
                name = mention.get("name", "")
                if key and name:
                    text = text.replace(key, f"@{name}")
        
        return text
    except:
        return ""


def parse_interactive(content: str) -> str:
    """
    解析卡片消息，提取全部文本内容
    
    支持两种结构：
    1. title + elements（二维数组）
    2. header.title.content + elements（一维数组）
    """
    try:
        card = json.loads(content)
        parts = []

        # 提取标题
        title = card.get("title", "") or card.get("header", {}).get("title", {}).get("content", "")
        if title:
            parts.append(title)

        # 遍历所有 elements 提取文本
        for row in card.get("elements", []):
            if isinstance(row, list):
                # 二维数组格式
                for elem in row:
                    tag = elem.get("tag")
                    if tag == "text":
                        parts.append(elem.get("text", ""))
                    elif tag == "a":
                        parts.append(elem.get("text", ""))
            elif isinstance(row, dict):
                # 一维数组格式
                tag = row.get("tag")
                if tag == "markdown":
                    parts.append(row.get("content", ""))
                elif tag == "div":
                    text_obj = row.get("text", {})
                    if isinstance(text_obj, dict):
                        parts.append(text_obj.get("content", ""))
                    elif isinstance(text_obj, str):
                        parts.append(text_obj)

        return " ".join(filter(None, parts)) or "[卡片消息]"
    except Exception as e:
        logger.debug(f"解析卡片消息失败: {e}")
        return "[卡片消息]"


def parse_post(content: str, message_id: str, token: str) -> tuple[str, list[dict]]:
    """
    解析富文本消息，递归提取所有文本和图片
    
    Args:
        content: 消息内容 JSON 字符串
        message_id: 消息 ID（用于下载图片）
        token: 访问令牌
        
    Returns:
        tuple: (文本内容, 图片列表)
    """
    try:
        data = json.loads(content)
        texts = []
        images = []
        
        def extract(obj):
            """递归提取所有文本内容"""
            if isinstance(obj, str):
                return
            if isinstance(obj, list):
                for item in obj:
                    extract(item)
                return
            if isinstance(obj, dict):
                # 提取文本字段
                for key in ("text", "title", "content", "user_name"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        texts.append(val.strip())
                    elif val is not None:
                        extract(val)
                # 处理图片
                if obj.get("tag") == "img":
                    image_key = obj.get("image_key", "")
                    if image_key:
                        img_data = _download_image(message_id, image_key, token)
                        if img_data:
                            images.append(img_data)
                        texts.append("[图片]")
                # 递归其他字段
                for key, val in obj.items():
                    if key not in ("text", "title", "content", "user_name"):
                        extract(val)
        
        extract(data)
        text = " ".join(texts) if texts else "[富文本]"
        return text, images
    except Exception as e:
        logger.warning(f"解析富文本失败: {e}")
        return "[富文本]", []


def parse_file(content: str, message_id: str, token: str) -> str:
    """
    解析文件消息
    
    - 文档类（.txt, .md, .docx, .doc）：下载并提取内容
    - PDF：下载并提取内容
    - 其他：只返回文件名
    """
    try:
        file_info = json.loads(content)
        file_key = file_info.get("file_key", "")
        file_name = file_info.get("file_name", "未知文件")

        # 获取文件扩展名
        ext = file_name.lower().split(".")[-1] if "." in file_name else ""

        # 文本类文件
        if ext in ("txt", "md", "markdown"):
            file_content = _download_file(message_id, file_key, token)
            if file_content:
                try:
                    text = file_content.decode("utf-8")
                    return f"[文件: {file_name}]\n{text}"
                except:
                    return f"[文件: {file_name}] (无法解码)"

        # Word 文档
        if ext in ("docx", "doc"):
            file_content = _download_file(message_id, file_key, token)
            if file_content:
                text = _extract_docx(file_content)
                if text:
                    return f"[文件: {file_name}]\n{text}"
            return f"[文件: {file_name}] (无法解析)"

        # PDF 文件
        if ext == "pdf":
            file_content = _download_file(message_id, file_key, token)
            if file_content:
                text = _extract_pdf(file_content)
                if text:
                    return f"[文件: {file_name}]\n{text}"
            return f"[文件: {file_name}] (无法解析)"

        # 其他文件：只返回文件名
        return f"[文件: {file_name}]"

    except Exception as e:
        logger.debug(f"解析文件消息失败: {e}")
        return "[文件]"


def parse_image(content: str, message_id: str, token: str) -> tuple[str, dict | None]:
    """
    解析图片消息，下载并转为 base64
    
    Returns:
        tuple: (显示文本, 图片数据dict)
    """
    try:
        image_info = json.loads(content)
        image_key = image_info.get("image_key", "")

        if image_key and message_id:
            img_data = _download_image(message_id, image_key, token)
            if img_data:
                return "[图片]", img_data
        return "[图片-无法下载]", None
    except Exception as e:
        logger.debug(f"解析图片失败: {e}")
        return "[图片]", None


def _download_file(message_id: str, file_key: str, token: str) -> bytes | None:
    """下载文件内容"""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
    try:
        resp = request_with_retry(
            _feishu_client,
            "GET",
            url,
            request_name="下载飞书文件",
            params={"type": "file"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.content
    except Exception as e:
        logger.debug(f"下载文件失败: {e}")
        return None


def _download_image(message_id: str, image_key: str, token: str) -> dict | None:
    """下载图片并转为 base64"""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}"
    try:
        resp = request_with_retry(
            _feishu_client,
            "GET",
            url,
            request_name="下载飞书图片",
            params={"type": "image"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        img_base64 = base64.b64encode(resp.content).decode("utf-8")
        return {
            "data": img_base64,
            "dimension": {"width": 800, "height": 600}  # 默认尺寸
        }
    except Exception as e:
        logger.debug(f"下载图片失败: {e}")
        return None


def _extract_docx(content: bytes) -> str:
    """从 docx 文件提取文本"""
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        logger.warning("python-docx 未安装，无法解析 docx 文件")
        return ""
    except Exception as e:
        logger.debug(f"解析 docx 失败: {e}")
        return ""


def _extract_pdf(content: bytes) -> str:
    """从 PDF 文件提取文本"""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text_parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
        return "\n".join(text_parts)
    except ImportError:
        logger.warning("PyPDF2 未安装，无法解析 PDF 文件")
        return ""
    except Exception as e:
        logger.debug(f"解析 PDF 失败: {e}")
        return ""

