# MemBind

**Binding-First Agent Memory Middleware**

独立Agent记忆中间件。任何支持HTTP调用的Agent（OpenClaw、Claude Code、LangChain等）都可以通过API接入，实现**跨会话记忆持久化 + 语义检索 + 上下文感知召回**。

## 核心理念

传统记忆系统只管"存"和"取"。MemBind在此基础上引入**Binding（绑定）**机制——每次记忆被召回时，系统会评估这条记忆与当前上下文的**匹配度**，而非仅靠语义相似度。

```
传统:  query → embedding → 向量检索 → 返回Top-K
MemBind: query → embedding → 向量检索 → Binding评分(场景/实体/意图) → 返回Top-K
```

## 特性

| 能力 | 说明 |
|------|------|
| 🧠 两阶段检索 | 语义召回 + Binding评分重排，减少噪音 |
| 🏷️ 自动标签 | 规则引擎 + LLM兜底，自动提取场景/实体/重要性 |
| ⚔️ 冲突检测 | 写入预检 + 检索时检测矛盾记忆 |
| 🔄 生命周期 | 自动衰减/强化/清理，记忆库不会无限膨胀 |
| 🔍 FTS5混合检索 | 向量(0.6) + 全文(0.2) + RRF融合(0.2) |
| 📦 知识簇 | 余弦相似度>0.75自动聚类，簇扩展召回 |
| 🏢 多Agent隔离 | Namespace隔离，多Agent共享一个实例互不干扰 |
| 🔌 MCP Server | 11个工具，stdio模式，即插即用 |
| 🚀 轻量部署 | 单进程，~50MB内存，SQLite零依赖 |

## 技术栈

- **Runtime**: Python 3.11+
- **Web**: FastAPI + Uvicorn
- **Database**: SQLite + sqlite-vec（向量索引）
- **Embedding**: 硅基流动 BGE-M3 API（1024维）
- **LLM**: 智谱 glm-4-flash（标签提取 + 冲突判断）
- **全文检索**: SQLite FTS5（RRF融合）

## 快速开始

### 1. 安装

```bash
git clone git@github.com:xpp0210/membind.git
cd membind
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入API Key
```

必填配置：

| 环境变量 | 说明 | 获取 |
|---------|------|------|
| `EMBEDDING_API_KEY` | 硅基流动API Key | https://cloud.siliconflow.cn |
| `LLM_API_KEY` | 智谱API Key | https://open.bigmodel.cn |

### 3. 启动

```bash
uvicorn server:app --host 0.0.0.0 --port 8901
```

访问 http://localhost:8901/docs 查看完整API文档。

## API概览

### 核心

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/memory/write` | 写入记忆（自动标签+冲突预检） |
| POST | `/api/v1/memory/recall` | 两阶段检索（语义+Binding评分） |
| POST | `/api/v1/memory/feedback` | 反馈召回质量（强化/衰减） |

### 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/stats` | 系统统计 |
| GET | `/api/v1/memory/{id}` | 记忆详情 |
| GET | `/api/v1/conflicts` | 列出未解决冲突 |
| POST | `/api/v1/conflicts/resolve` | 手动解决冲突 |
| POST | `/api/v1/conflicts/auto-resolve` | LLM自动解决冲突 |

### 生命周期

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/lifecycle/decay` | 衰减长期未命中记忆 |
| POST | `/api/v1/lifecycle/cleanup` | 清理低重要性记忆 |
| POST | `/api/v1/lifecycle/boost` | 强化高频命中记忆 |
| POST | `/api/v1/lifecycle/restore` | 恢复被软删除的记忆 |
| GET | `/api/v1/lifecycle/candidates` | 获取衰减/清理候选列表 |

### 对话解析

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/memory/conversation` | 从对话中原子化提取记忆 |

### 示例

**写入记忆**

```bash
curl -X POST http://localhost:8901/api/v1/memory/write \
  -H "Content-Type: application/json" \
  -d '{"content":"MyBatis-Plus批量插入超过500条会OOM，用sqlSession.flushStatements()分批提交","scene":"coding","entities":["MyBatis-Plus"]}'
```

**检索记忆**

```bash
curl -X POST http://localhost:8901/api/v1/memory/recall \
  -H "Content-Type: application/json" \
  -d '{"query":"批量插入性能优化","top_k":5}'
```

**多Agent隔离**

```bash
# Agent A写入
curl -X POST http://localhost:8901/api/v1/memory/write \
  -H "X-MemBind-Namespace: agent-a" \
  -H "Content-Type: application/json" \
  -d '{"content":"Agent A的私有记忆"}'

# Agent B检索（看不到Agent A的记忆）
curl -X POST http://localhost:8901/api/v1/memory/recall \
  -H "X-MemBind-Namespace: agent-b" \
  -H "Content-Type: application/json" \
  -d '{"query":"任何内容"}'
```

## MCP Server

MemBind可作为MCP Server运行，提供11个工具：

```
memory_write, memory_recall, memory_feedback,
memory_stats, memory_decay, memory_conflict_check,
memory_merge, memory_export, memory_cluster_stats,
memory_get, memory_timeline
```

启动方式：

```bash
python mcp_server.py
```

OpenClaw配置示例：

```json
{
  "mcpServers": {
    "membind": {
      "command": "python",
      "args": ["/path/to/membind/mcp_server.py"],
      "env": {
        "EMBEDDING_API_KEY": "sk-xxx",
        "LLM_API_KEY": "xxx"
      }
    }
  }
}
```

## 检索算法

### 两阶段检索

```
Stage 1 - Recall: 向量语义检索 Top-50 → FTS5全文检索 → RRF融合 → Top-20
Stage 2 - Binding: 场景匹配(0.3) + 实体重叠(0.2) + 意图评分(0.4) + 时间(0.1) → Top-K
```

### RRF融合权重

```
最终分 = 0.6 × 向量语义 + 0.2 × FTS全文 + 0.2 × 向量RRF
```

### 知识簇

写入时自动聚类（余弦相似度>0.75加入已有簇，否则新建），检索时簇扩展召回（降权0.7）。

## 项目结构

```
membind/
├── server.py              # FastAPI入口
├── config.py              # 配置管理（环境变量）
├── mcp_server.py          # MCP Server（11工具）
├── api/                   # API路由
│   ├── write.py           # 写入
│   ├── recall.py          # 检索
│   ├── admin.py           # 统计+详情+反馈
│   ├── conflict.py        # 冲突管理
│   ├── conversation.py    # 对话解析
│   ├── lifecycle.py       # 生命周期
│   ├── chunk.py           # 记忆块管理
│   └── deps.py            # 认证+命名空间
├── core/                  # 核心算法
│   ├── writer.py          # 写入层（标签提取+embedding）
│   ├── retriever.py       # 混合检索+RRF融合
│   ├── conflict.py        # 冲突检测
│   ├── conversation.py    # 对话原子化提取
│   ├── lifecycle.py       # 生命周期管理
│   ├── cluster.py         # 知识簇
│   ├── merger.py          # 记忆合并
│   └── chunk_store.py     # 记忆块存储
├── services/              # 业务逻辑
│   └── binding_service.py # Binding评分+统计
├── db/                    # 数据库
│   ├── init.sql           # DDL + FTS5 + 触发器
│   ├── connection.py      # 连接管理+迁移
│   └── migrate.py         # MemOS数据迁移
├── tests/                 # 测试（97用例）
├── deploy/                # 部署
│   └── membind.service    # systemd服务
└── docker/                # Docker
    ├── Dockerfile
    └── docker-compose.yml
```

## 测试

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

当前状态：94/97 通过（3个flaky来自db隔离问题）。

## License

MIT
