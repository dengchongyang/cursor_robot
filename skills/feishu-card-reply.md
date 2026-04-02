# 飞书卡片消息发送技能

## 适用场景
当需要向用户发送格式化、美观且包含交互元素（如按钮、Markdown）的消息时。

## 操作步骤

### 1. 构造卡片 JSON
参考飞书开放平台 [卡片搭建工具](https://open.feishu.cn/tool/card-builder)。

### 2. 使用 Python 发送
```python
import json
import requests

def send_feishu_card(receive_id, card_content, token):
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card_content, ensure_ascii=False)
    }
    return requests.post(url, headers=headers, json=payload)
```

### 3. 使用 Curl 发送
注意 `content` 字段必须是转义后的 JSON 字符串。

## 输入输出约定
- **输入**：`receive_id` (chat_id), `card_json`, `access_token`
- **输出**：飞书 API 响应结果

## 常见异常
- **230001**: `content is not a string in json format`。通常是因为 `content` 字段没有进行二次 JSON 序列化（即它应该是一个字符串，内容是 JSON）。
- **401**: Token 过期或无效。
