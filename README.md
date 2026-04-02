# 我的飞书 Cursor Agent

这是我的个人版飞书机器人项目，用来把飞书消息桥接到 Cursor Cloud Agent，让我可以直接在飞书里驱动一个可持续养成的代码助手。

当前这版已经不是单纯的消息转发器，而是一个带有本地记忆、文档检索、长期记忆沉淀和任务状态追踪的 Agent 桥接服务。

## 当前能力

- 支持飞书单聊和群聊消息接入
- 支持 Agent 会话续接 `followup`
- 支持本地持久化消息、操作日志和长期记忆
- 支持本地文档知识库检索，自动读取 `README.md`、`doc/`、`memory/`、`skills/`
- 支持长期记忆自动导出到 `memory/auto_memory.md`
- 支持 `Cursor Agent` 后台状态轮询
- 支持任务失败或超时时的飞书状态通知
- 支持性能优化后的轻量上下文构建
- 支持图片、文件、卡片、富文本等多类型消息解析

## 适合做什么

- 在飞书里直接发需求，让 Cursor Agent 帮忙分析代码、写代码、排查问题
- 持续积累个人工作流、项目约定、常见问题处理经验
- 把长期偏好、技能卡和知识文档逐步沉淀到仓库

## 核心设计

这套系统由 4 个层次组成：

1. 飞书桥接层：接收飞书消息，拉取上下文，提交 Cursor Agent 任务
2. 本地记忆层：保存消息、操作日志、长期记忆和会话状态
3. 本地知识库层：切块索引项目文档并按需检索
4. Cursor Agent 层：在云端执行任务，并最终回飞书

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入真实配置
```

建议重点配置这些变量：

| 变量 | 说明 |
|------|------|
| `FEISHU_APP_ID` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书应用密钥 |
| `FEISHU_BOT_NAME` | 机器人名称 |
| `FEISHU_MASTER_NAME` | 主人名称，可选 |
| `CURSOR_API_KEY` | Cursor API Key |
| `CURSOR_GITHUB_REPO` | Cursor Agent 绑定仓库 |
| `CURSOR_GITHUB_REF` | 默认分支，默认 `main` |
| `CURSOR_MODEL` | 模型名，默认 `gemini-3-flash` |
| `GROUP_CHAT_MODE` | `mention_only` 或 `all` |
| `MEMORY_DB_PATH` | 本地记忆数据库路径 |

### 3. 飞书开放平台配置

1. 进入 [飞书开放平台](https://open.feishu.cn) 创建应用
2. 开启机器人能力
3. 申请这些权限：

| 权限 | 权限代码 | 必需 | 说明 |
|------|----------|------|------|
| 获取与发送单聊、群组消息 | `im:message` | ✅ | 核心消息读写 |
| 读取用户发给机器人的单聊消息 | `im:message.p2p_msg:readonly` | ✅ | 接收单聊消息 |
| 接收群聊中@机器人消息事件 | `im:message.group_at_msg:readonly` | ✅ | 接收群聊@消息 |
| 以应用的身份发消息 | `im:message:send_as_bot` | ✅ | Agent 或桥接层回消息 |
| 获取通讯录基本信息 | `contact:contact.base:readonly` | ⚠️ | 开启后可获取更准确用户名 |
| 获取用户基本信息 | `contact:user.base:readonly` | ⚠️ | 开启后可获取更准确用户名 |
| 获取群组中所有消息 | `im:message.group_msg` | ⚠️ | 群聊历史上下文 |

4. 在事件配置里选择长连接模式
5. 添加事件：`im.message.receive_v1`

### 4. 启动

```bash
python main.py
```

Windows PowerShell:

```powershell
Set-Location 'E:\feishu-cursor-robot'
python main.py
```

## 使用方式

- 单聊：直接给机器人发消息
- 群聊：@机器人后发送消息
- 长任务：会先收到一条“处理中”的即时回执
- 失败或超时：桥接层会主动补发状态通知

## 目录结构

```text
├── main.py
├── config/
│   └── settings.py
├── feishu/
│   ├── client.py
│   ├── handlers.py
│   ├── history.py
│   ├── message_parser.py
│   ├── token.py
│   └── user.py
├── cursor/
│   ├── agent.py
│   └── poller.py
├── runtime_memory/
│   ├── store.py
│   └── reflection.py
├── knowledge/
│   └── retriever.py
├── memory/
│   ├── README.md
│   └── auto_memory.md
├── skills/
│   └── README.md
├── prompts/
│   └── system_prompt.py
└── doc/
    └── design.md
```

## 本地记忆与知识库

当前版本会在本地维护这些数据：

- `data/robot_memory.db`
  - 消息记录
  - 操作日志
  - 长期记忆
  - 文档切块
  - 会话状态
- `memory/auto_memory.md`
  - 自动导出的长期记忆

建议你持续维护这些目录：

- `memory/`
  - 存长期约定、偏好、项目事实、经验总结
- `skills/`
  - 存技能卡、操作 SOP、排障手册
- `doc/`
  - 存设计文档、架构说明、项目知识

## 已做的性能优化

当前默认已经做了这些优化：

- 历史消息默认只取较小窗口
- 文档知识库按间隔同步，不再每条消息都全量扫描
- 单聊先回“处理中”提升体感速度
- 历史消息默认不回源拉旧引用内容
- 历史消息默认不为旧消息逐条远程查用户名
- 会话级 `agent_id` 持久化，服务重启后优先尝试 followup
- 后台轮询 Agent 真正终态，不再把“提交成功”误当“任务完成”

## 关键配置项

以下是当前性能和行为相关的重点配置：

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `HISTORY_MESSAGE_LIMIT` | `10` | 历史消息条数 |
| `KNOWLEDGE_RETRIEVAL_LIMIT` | `2` | 文档检索片段数 |
| `RECENT_OPERATIONS_LIMIT` | `3` | 近期操作条数 |
| `LONG_TERM_MEMORY_LIMIT` | `4` | 长期记忆条数 |
| `KNOWLEDGE_SYNC_INTERVAL_SECONDS` | `60` | 知识库同步间隔 |
| `SEND_PROCESSING_REPLY_IN_P2P` | `true` | 单聊是否先发处理中 |
| `AGENT_POLL_INTERVAL_SECONDS` | `8` | Agent 轮询间隔 |
| `AGENT_POLL_TIMEOUT_SECONDS` | `600` | Agent 轮询超时 |
| `NOTIFY_ON_AGENT_COMPLETION` | `false` | 完成后是否额外通知 |
| `NOTIFY_ON_AGENT_FAILURE` | `true` | 失败后是否通知 |
| `NOTIFY_ON_AGENT_TIMEOUT` | `true` | 超时后是否通知 |
| `HISTORY_RESOLVE_REMOTE_NAMES` | `false` | 历史消息是否远程查用户名 |
| `HISTORY_RESOLVE_QUOTES` | `false` | 历史消息是否回源解析引用 |

## 日志观察

启动后建议重点关注两类日志：

1. 处理耗时日志

```text
处理耗时 | msg_id=... | token=...s | history=...s | context=...s | prompt=...s | agent_api=...s | total=...s
```

2. Agent 状态轮询日志

```text
Agent 状态更新 | agent_id=... | msg_id=... | status=RUNNING
Agent 状态更新 | agent_id=... | msg_id=... | status=COMPLETED
```

## 维护建议

- 把 `.env`、`data/robot_memory.db`、`memory/auto_memory.md` 保持在本地，不要提交到远程仓库
- 如果要长期使用，建议把 `memory/` 和 `skills/` 当成你自己的知识资产持续维护
- 如果后面要迁移到新仓库，优先保留 `memory/`、`skills/`、`doc/` 以及本地数据库

## 详细设计

完整设计说明见 `doc/design.md`。
