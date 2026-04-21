"""
MemBind 写入层

ContextTagger: 规则引擎提取上下文标签
EmbeddingGenerator: 调用智谱embedding-3外部API
"""

import json
import re
import hashlib
import httpx
from datetime import datetime

from config import settings
from models.context_tag import ContextTag


# ── 场景关键词表 ──
SCENE_KEYWORDS: dict[str, list[str]] = {
    "coding": ["代码", "bug", "debug", "函数", "API", "接口", "编译", "报错", "部署", "重构",
               "code", "function", "class", "method", "deploy", "build", "test"],
    "research": ["论文", "研究", "实验", "分析", "对比", "基准", "arXiv", "论文",
                 "paper", "study", "experiment", "benchmark", "analysis"],
    "writing": ["文章", "写作", "博客", "公众号", "标题", "封面", "发布",
                "blog", "article", "write", "publish", "draft"],
    "ops": ["服务器", "配置", "监控", "备份", "升级", "Cron", "定时", "OOM",
            "server", "config", "deploy", "cron", "backup", "memory", "CPU"],
}

# ── 技术实体词典（常见技术名词，用于实体提取）──
TECH_ENTITIES: set[str] = {
    "Python", "Java", "Go", "Rust", "TypeScript", "JavaScript",
    "FastAPI", "Spring", "Docker", "Kubernetes", "Redis", "MySQL",
    "PostgreSQL", "MongoDB", "Kafka", "Elasticsearch", "Nginx",
    "Git", "GitHub", "Linux", "SQLite", "Vue", "React",
    "LLM", "RAG", "Agent", "MCP", "OpenClaw", "Claude", "GPT",
    "embedding", "vector", "chunk", "token", "prompt",
}


class ContextTagger:
    """上下文标签提取器"""

    def __init__(self):
        self._llm_client: httpx.AsyncClient | None = None

    async def tag(self, content: str, hint_context: dict | None = None) -> ContextTag:
        """提取上下文标签（规则优先 + LLM兜底）"""
        # 如果调用方给了context hint，直接用
        if hint_context:
            return ContextTag(
                scene=hint_context.get("scene", "general"),
                task_type=hint_context.get("task_type", "default"),
                entities=hint_context.get("entities", self._extract_entities(content)),
                importance=hint_context.get("importance", self._calc_importance(content)),
            )

        # 第一层：规则引擎
        scene, scene_confidence = self._match_scene_with_confidence(content)
        entities = self._extract_entities(content)
        importance = self._calc_importance(content)

        # 第二层：LLM兜底（规则置信度低或场景为general时）
        if scene_confidence < 2 or scene == "general":
            llm_result = await self._llm_tag(content)
            if llm_result:
                scene = llm_result.get("scene", scene)
                # LLM实体与规则实体合并去重
                llm_entities = llm_result.get("entities", [])
                entities = list(dict.fromkeys(entities + llm_entities))[:20]
                if llm_result.get("importance"):
                    importance = llm_result["importance"]

        return ContextTag(
            scene=scene,
            task_type=self._infer_task_type(content, scene),
            entities=entities,
            importance=importance,
        )

    def tag_sync(self, content: str, hint_context: dict | None = None) -> ContextTag:
        """同步版本（不调LLM，纯规则）"""
        if hint_context:
            return ContextTag(
                scene=hint_context.get("scene", "general"),
                task_type=hint_context.get("task_type", "default"),
                entities=hint_context.get("entities", self._extract_entities(content)),
                importance=hint_context.get("importance", self._calc_importance(content)),
            )
        scene, _ = self._match_scene_with_confidence(content)
        return ContextTag(
            scene=scene,
            task_type=self._infer_task_type(content, scene),
            entities=self._extract_entities(content),
            importance=self._calc_importance(content),
        )

    def _match_scene_with_confidence(self, content: str) -> tuple[str, int]:
        """场景匹配（返回场景+置信度=命中关键词数）"""
        scores: dict[str, int] = {}
        content_lower = content.lower()
        for scene, keywords in SCENE_KEYWORDS.items():
            scores[scene] = sum(1 for kw in keywords if kw.lower() in content_lower)
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        return (best, scores[best]) if scores[best] > 0 else ("general", 0)

    async def _llm_tag(self, content: str) -> dict | None:
        """LLM兜底标签提取（glm-4-flash）"""
        api_key = settings.LLM_API_KEY
        if not api_key:
            return None

        prompt = f"""分析以下文本，提取上下文标签。严格返回JSON，不要其他内容：
{{"scene": "coding|research|writing|ops|learning|general", "entities": ["实体1","实体2"], "importance": 0.0}}

scene规则：coding=编程/代码/debug；research=研究/论文/分析；writing=写作/文章；ops=运维/部署/配置；learning=学习/笔记；general=其他
importance范围0-10，重要决策/踩坑>7，普通记录4-6，碎片信息<4
entities提取技术名词/项目名/工具名

文本：{content[:500]}"""

        try:
            payload = {
                "model": settings.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 200,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{settings.LLM_API_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"].strip()
                json_match = re.search(r'\{[^}]+\}', text)
                if json_match:
                    result = json.loads(json_match.group())
                    valid_scenes = {"coding", "research", "writing", "ops", "learning", "general"}
                    if result.get("scene") in valid_scenes:
                        return result
        except Exception as e:
            print(f"[MemBind] LLM标签提取失败: {e}")
        return None

    def _extract_entities(self, content: str) -> list[str]:
        """实体提取（技术词典匹配）"""
        found = [e for e in TECH_ENTITIES if e.lower() in content.lower()]
        # 额外匹配中文技术名词（2-6字，大写字母混合）
        pattern = r'[A-Z][a-zA-Z]+(?:-[A-Z][a-zA-Z]+)*|[A-Z]{2,}'
        extra = re.findall(pattern, content)
        found.extend(e for e in extra if e not in found and len(e) >= 2)
        return found[:20]  # 最多20个实体

    def _infer_task_type(self, content: str, scene: str) -> str:
        """推断任务类型"""
        c = content.lower()
        if any(w in c for w in ["修复", "解决", "fix", "debug", "报错", "错误"]):
            return "debug"
        if any(w in c for w in ["设计", "架构", "design", "架构"]):
            return "design"
        if any(w in c for w in ["复习", "学习", "learn", "研究", "study"]):
            return "learn"
        if any(w in c for w in ["review", "审查", "检查"]):
            return "review"
        return "default"

    def _calc_importance(self, content: str) -> float:
        """重要性评分（规则：长度+实体密度+关键词）"""
        score = 5.0
        # 长度加分
        if len(content) > 500:
            score += 1.0
        elif len(content) < 50:
            score -= 1.0
        # 实体密度加分
        entities = self._extract_entities(content)
        score += min(len(entities) * 0.3, 2.0)
        # 关键决策词加分
        decision_words = ["决定", "选择", "原因", "根因", "教训", "踩坑", "必须", "禁止",
                          "decided", "because", "lesson", "must", "never"]
        if any(w in content.lower() for w in decision_words):
            score += 1.5
        return max(0.0, min(10.0, score))


class EmbeddingGenerator:
    """调用智谱embedding-3外部API生成向量"""

    def __init__(self):
        self._api_url = settings.EMBEDDING_API_URL
        self._api_key = settings.EMBEDDING_API_KEY
        self._model = settings.EMBEDDING_API_MODEL

    async def generate(self, text: str) -> list[float]:
        """生成embedding向量"""
        if not self._api_key:
            # 无key时返回零向量（开发/测试模式）
            return [0.0] * settings.EMBEDDING_DIM

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    self._api_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]
            except httpx.HTTPStatusError as e:
                print(f"[MemBind] embedding API错误: {e.response.status_code}, 降级为零向量")
                return [0.0] * settings.EMBEDDING_DIM
            except Exception as e:
                print(f"[MemBind] embedding生成失败: {e}, 降级为零向量")
                return [0.0] * settings.EMBEDDING_DIM

    async def generate_batch(self, texts: list[str]) -> list[list[float]]:
        """批量生成embedding"""
        if not self._api_key:
            return [[0.0] * settings.EMBEDDING_DIM for _ in texts]

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self._api_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            # 按index排序确保顺序一致
            items = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in items]
