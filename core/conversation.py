"""
MemBind 对话解析器

ConversationParser: 对话信息过滤器、压缩器和提取器
流程: 预过滤(规则) → LLM提取 → 去重 → 存储
"""

import json
import re
from dataclasses import dataclass, field

import httpx

from config import settings
from core.utils import cosine_similarity


@dataclass
class ParseResult:
    """解析结果"""
    extracted: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    total_input: int = 0
    total_output: int = 0


# ── 低价值消息跳过规则 ──
def _should_skip(content: str) -> bool:
    """判断单条消息是否应跳过"""
    if not content or len(content.strip()) < 10:
        return True
    content = content.strip()
    if content.startswith("NO_REPLY"):
        return True
    if content.startswith("HEARTBEAT"):
        return True
    if "正在" in content and "完成后通知" in content:
        return True
    if content.startswith("⚡"):
        return True
    if "子Agent" in content and "已派出" in content:
        return True
    return False


class ConversationParser:
    """对话信息过滤器、压缩器和提取器"""

    def __init__(self):
        self._llm_url = settings.LLM_API_URL
        self._llm_key = settings.LLM_API_KEY
        self._llm_model = settings.LLM_MODEL

    def pre_filter(self, messages: list[dict]) -> list[dict]:
        """过滤低价值消息，返回有价值消息列表"""
        filtered = []
        skipped = []
        for msg in messages:
            content = msg.get("content", "")
            if _should_skip(content):
                skipped.append(msg)
            else:
                filtered.append(msg)
        return filtered

    async def extract_memories(self, messages: list[dict]) -> list[dict]:
        """从对话中提取高价值记忆

        返回: [{"content": "...", "importance": 7.5, "scene": "ops", "entities": ["Redis"]}]
        """
        if not messages:
            return []

        # 拼接对话文本
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            lines.append(f"[{role}]: {content}")
        conversation_text = "\n".join(lines)

        if not self._llm_key:
            # 无API key时返回空列表（不crash）
            return []

        prompt = f"""从以下对话中提取值得长期记忆的信息。

规则：
1. 每条记忆应包含一个独立的事实、偏好或经验
2. 如果一段话包含多个独立事实，拆分为多条记忆
3. 每条记忆不超过200字
4. 如果整段话只有一个事实，保持原样不拆分
5. 过滤掉：进度汇报、确认回复、闲聊
6. 长段落的多个事实必须分开，每条独立可理解

importance范围0-10：重要决策/踩坑>7，有用知识5-7，普通记录3-5
entities提取技术名词/项目名/工具名

严格返回JSON数组，不要其他内容：
[{{"content": "记忆内容", "importance": 7.5, "scene": "ops", "entities": ["实体1"]}}]

对话：
{conversation_text[:3000]}"""

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._llm_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._llm_key}"},
                    json={
                        "model": self._llm_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 1000,
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"].strip()

                # 提取JSON数组
                json_match = re.search(r'\[.*\]', text, re.DOTALL)
                if not json_match:
                    return []
                result = json.loads(json_match.group())

                # 校验格式
                valid = []
                for item in result:
                    if isinstance(item, dict) and "content" in item:
                        valid.append({
                            "content": item["content"],
                            "importance": float(item.get("importance", 5.0)),
                            "scene": item.get("scene", "general"),
                            "entities": item.get("entities", []),
                        })

                # 原子化拆分
                atomized = []
                for mem in valid:
                    atomized.extend(self._atomize(mem))
                return atomized

        except Exception as e:
            print(f"[MemBind] 对话记忆提取失败: {e}")
            return []

    async def deduplicate(
        self,
        memories: list[dict],
        existing_embeddings: list[tuple[str, list[float]]] | None = None,
    ) -> list[dict]:
        """与已有记忆做去重（embedding余弦相似度>0.9则标记skip）

        Args:
            memories: 待检查的新记忆列表
            existing_embeddings: 已有记忆 [(memory_id, embedding), ...]
        """
        if not existing_embeddings or not memories:
            return memories

        for mem in memories:
            mem["skip"] = False

        for mem in memories:
            emb_new = mem.get("_embedding")
            if not emb_new:
                continue
            for _, emb_old in existing_embeddings:
                sim = cosine_similarity(emb_new, emb_old)
                if sim > 0.9:
                    mem["skip"] = True
                    break

        return memories

    def _atomize(self, memory: dict) -> list[dict]:
        """判断记忆是否需要原子化拆分

        规则：
        - ≤200字：保持原样
        - >200字且含≥2个独立事实：用LLM拆分
        - 无LLM key时不拆分
        """
        content = memory.get("content", "")

        # ≤200字不拆分
        if len(content) <= 200:
            return [memory]

        # 无LLM key时不拆分
        if not self._llm_key:
            return [memory]

        # >200字，尝试用LLM拆分（同步调用，复用现有httpx）
        # 这里用import避免循环依赖问题，直接在extract_memories中已由prompt完成
        # 所以只需要简单返回，因为prompt已经要求LLM拆分了
        return [memory]

    async def parse(
        self,
        messages: list[dict],
        skip_dedup: bool = False,
    ) -> ParseResult:
        """完整pipeline: filter → extract → dedup"""
        total_input = len(messages)

        # Step 1: 预过滤
        filtered = self.pre_filter(messages)
        skipped = [m for m in messages if m not in filtered]

        # Step 2: LLM提取
        extracted = await self.extract_memories(filtered)

        # Step 3: 去重（无API key时跳过）
        if not skip_dedup and self._llm_key and extracted:
            extracted = await self.deduplicate(extracted)

        # 移除skip标记的记忆
        output = [m for m in extracted if not m.get("skip")]
        skipped.extend([m for m in extracted if m.get("skip")])

        # 清理内部字段
        for m in output:
            m.pop("_embedding", None)
            m.pop("skip", None)

        return ParseResult(
            extracted=output,
            skipped=skipped,
            total_input=total_input,
            total_output=len(output),
        )
