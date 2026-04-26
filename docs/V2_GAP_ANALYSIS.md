# MemBind V2 差距分析

> 日期: 2026-04-26 | 基于 Obsidian 知识库中记忆系统和自进化研究

---

## 研究来源

| 文件 | 类型 | 核心贡献 |
|------|------|---------|
| raw/Binding-First记忆系统-架构设计.md | 竞品分析 | Mem0/Letta/安宝系统的binding盲区，SuperMemory/llm-wiki/MemPalace竞品差距 |
| raw/Binding-First记忆系统-详细设计.md | 架构设计 | MemBind当前实现完整API/DDL/算法 |
| raw/Mengram项目架构解析-综述.md | 竞品分析 | 三层记忆+程序性记忆自动进化机制 |
| raw/长期记忆的注意力实现vs-RAG对比-对比分析.md | 理论研究 | RAG vs MSA多维度对比，多跳推理短板 |
| raw/知识图谱长期记忆-学术.md | 学术 | 知识图谱=结构化语义记忆，多跳推理原生支持 |
| wiki/记忆巩固.md | 认知科学 | 突触巩固→系统巩固，睡眠重放机制 |
| wiki/遗忘曲线.md | 认知科学 | Ebbinghaus指数衰减 R=e^(-t/S)，节省效应 |
| wiki/间隔效应.md | 认知科学 | 最优间隔≈目标保持时间10-20%，扩展间隔优于等距 |
| wiki/自进化.md | 方法论 | 五大进化方法论，记忆演化四阶段(L0-L3) |
| wiki/安宝自进化系统v5.5-架构与设计文档.md | 实践 | 六步闭环进化循环，Skills=程序性记忆 |
| raw/奥一-自我进化的第二大脑.md | 实践 | Token一鱼多吃，提示词驱动进化 |

---

## 差距1：缺少程序性记忆（Procedural Memory）

### 理论依据

- **Mengram**：三层记忆（语义+情景+程序性），程序性记忆支持失败自动进化（失败→LLM分析缺失步骤→版本升级）
- **安宝自进化v5.5**：Skills系统本质就是程序性记忆，通过reflect→归因→进化闭环
- **自进化wiki**：记忆演化L2阶段=跨会话持久化经验，L3=共享知识库

### 当前状态

MemBind只有语义记忆（facts/preferences/knowledge），没有工作流记忆。无法存储"部署五步法v3"这类步骤序列。

### 建议实现

```sql
CREATE TABLE procedures (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,              -- "Spring Boot部署流程"
    steps TEXT NOT NULL,             -- JSON数组: [{"step":1,"action":"mvn clean","note":"..."}, ...]
    version INTEGER DEFAULT 1,
    trigger_conditions TEXT,         -- JSON: [{"scene":"ops","task":"deploy"}]
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    last_failed_at TEXT,
    namespace TEXT DEFAULT 'default',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

- 写入时LLM识别"步骤型"内容 → 自动归档为procedure
- 失败feedback → LLM分析哪步出了问题 → 自动补充步骤 → version+1
- MCP新增 `procedure_recall` / `procedure_feedback` 工具

### 工作量：~3h | 影响：高（从"事实库"升级为"经验库"）

---

## 差距2：衰减算法不符合认知科学

### 理论依据

- **遗忘曲线**：Ebbinghaus指数衰减 R = e^(-t/S)，S=记忆强度
- **间隔效应**：最优复习间隔 ≈ 目标保持时间的10-20%
- **记忆巩固**：每次成功提取（recall）增强记忆痕迹

### 当前状态

`core/lifecycle.py` 衰减是线性衰减（每次调用 `decay_all()` 固定减少importance 0.1），不符合认知科学。没有memory_strength概念，衰减与记忆被使用的频率无关。

### 建议实现

```python
# 认知科学衰减模型
def cognitive_decay(importance: float, strength: float, days_since_last_hit: float) -> float:
    """
    R = importance * e^(-days / (strength * STRENGTH_SCALE))
    - importance: 原始重要性(0-10)
    - strength: 记忆强度，每次成功hit增加
    - days_since_last_hit: 距上次被成功检索的天数
    """
    STRENGTH_SCALE = 30.0  # strength=1时，30天衰减到37%
    decay_factor = math.exp(-days_since_last_hit / (strength * STRENGTH_SCALE))
    return importance * decay_factor
```

- memories表新增 `memory_strength REAL DEFAULT 1.0`（每次成功binding +0.5，上限10）
- 衰减基于 `exp(-days / strength)` 而非固定值
- 强记忆衰减慢，弱记忆衰减快

### 工作量：~1.5h | 影响：中（衰减更准确，遗忘更自然）

---

## 差距3：缺少记忆巩固机制

### 理论依据

- **记忆巩固**：短期→长期需要"重放"过程（海马体→新皮层转移）
- **SWS重放**：睡眠期间海马体压缩重放白天经历
- **间隔效应**：多次提取=多次巩固窗口

### 当前状态

MemBind写入即长期存储，没有"试用期"或"巩固周期"。新写入的低质量记忆和老的高质量记忆享有同等待遇。

### 建议实现

```sql
-- memories表新增字段
ALTER TABLE memories ADD COLUMN consolidation_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN consolidated INTEGER DEFAULT 0;  -- 0=未巩固, 1=已巩固
```

- 新记忆标记 `consolidated=0`
- 每次成功recall算一次巩固（`consolidation_count += 1`）
- `consolidation_count >= 3` → `consolidated = 1`
- 未巩固记忆的衰减系数额外 ×0.7（更容易被遗忘）
- Cron每日检查：创建>30天且consolidation_count=0 → 候选清理

### 工作量：~2h | 影响：中（新记忆有"试用期"，减少噪音）

---

## 差距4：检索缺少多跳推理能力

### 理论依据

- **知识图谱 vs 向量数据库**：图谱原生支持多跳推理（路径查询），向量只能近似
- **RAG vs MSA**：RAG多跳推理需多轮检索，误差累积；MSA单次注意力可完成
- **知识图谱长期记忆**：节点=概念，边=关系，激活扩散=检索

### 当前状态

- 有FTS5+向量混合检索+知识簇（向量聚类）
- 没有实体-关系图
- 知识簇基于向量相似度，不是语义关系
- 无法回答多跳问题

### 建议实现（轻量方案：实体共现图）

不做完整知识图谱（ROI低），而是基于已有 `context_tags.entities` 建立共现图：

```sql
CREATE TABLE entity_cooccurrence (
    entity_a TEXT NOT NULL,
    entity_b TEXT NOT NULL,
    co_count INTEGER DEFAULT 1,  -- 共现次数
    PRIMARY KEY (entity_a, entity_b),
    CHECK(entity_a < entity_b)
);
```

- 写入时提取entities，两两共现 +1
- 检索时：先向量召回 → 提取命中记忆的entities → 查共现图找关联entity → 用关联entity做第二轮检索（1跳扩展）
- 成本极低（纯SQL，无LLM调用）

### 工作量：~3h | 影响：中（支持1跳扩展检索）

---

## 差距5：反馈驱动自动记忆进化

### 理论依据

- **Mengram**：失败feedback → 自动进化procedure
- **达尔文Skill**：选择→变异→评估→保留
- **自进化v5.5**：reflect→归因→修复→验证闭环

### 当前状态

`binding_history.was_relevant` 记录了反馈，但只是被动存储。没有基于feedback自动触发记忆的合并、拆分、重写、归档。

### 建议实现

```
触发条件：同一条记忆连续3次 was_relevant=0
自动执行：
  1. LLM分析原因（过时？太泛？冲突？上下文错位？）
  2. 根据原因执行操作：
     - 过时 → 标记 is_deleted=1
     - 太泛 → 拆分为多条精确记忆
     - 冲突 → 触发 conflict_detector 合并
     - 上下文错位 → 补充 context_tags
  3. 记录进化日志到 conflict_log
```

- Cron每日执行一次feedback扫描
- 新增 `api/evolution.py` 端点：手动触发/查看进化候选

### 工作量：~2h | 影响：中（记忆自净化，减少人工维护）

---

## 优先级与依赖

```
V2-1 程序性记忆 (3h)     ← 最高ROI，定义MemBind核心差异化
V2-2 认知科学衰减 (1.5h)  ← 独立，可先做
V2-3 记忆巩固机制 (2h)    ← 依赖V2-2的strength字段
V2-4 实体共现图 (3h)      ← 独立
V2-5 反馈驱动进化 (2h)    ← 依赖V2-1（procedure进化需要V2-1）

建议顺序：V2-2 → V2-3 → V2-1 → V2-4 → V2-5
总工作量：~11.5h
```

## 与P5路线图的关系

P5（PG/OAuth2/WebUI）是**基础设施**，V2是**能力升级**。两者独立，可并行。
V2完成后MemBind的核心差异点：Binding-First + 认知科学衰减 + 程序性记忆进化。
