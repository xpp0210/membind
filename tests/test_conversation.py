"""
MemBind ConversationParser 测试
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.conversation import ConversationParser, ParseResult, _should_skip


parser = ConversationParser()


# ── 预过滤测试 ──

class TestPreFilter:
    def test_skips_short(self):
        """短消息被过滤"""
        assert _should_skip("可以") is True
        assert _should_skip("好的") is True
        assert _should_skip("嗯") is True
        assert _should_skip("OK") is True

    def test_skips_no_reply(self):
        assert _should_skip("NO_REPLY") is True
        assert _should_skip("HEARTBEAT_OK") is True
        assert _should_skip("HEARTBEAT") is True

    def test_skips_progress(self):
        assert _should_skip("⚡ 子Agent已派出") is True
        assert _should_skip("⚡ 步骤1完成") is True
        assert _should_skip("正在处理，完成后通知") is True

    def test_keeps_valuable(self):
        assert _should_skip("Redis缓存配置：maxmemory-policy=allkeys-lru") is False
        assert _should_skip("分三步修改配置文件然后重启服务") is False
        assert _should_skip("这个bug的根因是连接池没有释放") is False

    def test_pre_filter_batch(self):
        messages = [
            {"role": "user", "content": "Redis缓存怎么配置"},
            {"role": "assistant", "content": "分三步设置maxmemory-policy"},
            {"role": "user", "content": "可以"},
            {"role": "assistant", "content": "NO_REPLY"},
            {"role": "user", "content": "好的"},
            {"role": "assistant", "content": "⚡ 子Agent已派出"},
            {"role": "user", "content": "配置完成后需要重启Redis服务才能生效"},
        ]
        filtered = parser.pre_filter(messages)
        assert len(filtered) == 3


# ── LLM提取测试 ──

def _make_mock_llm_response(response_json):
    """创建同步mock response对象（httpx.Response是同步的）"""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=response_json)
    return mock_resp


class TestExtractMemories:
    @pytest.mark.asyncio
    async def test_extract_memories(self):
        """mock LLM返回固定结果"""
        mock_response = {
            "choices": [{"message": {"content": json.dumps([
                {"content": "Redis缓存配置步骤", "importance": 7.5, "scene": "ops", "entities": ["Redis"]}
            ])}}]
        }
        mock_resp = _make_mock_llm_response(mock_response)

        with patch("core.conversation.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            with patch("core.conversation.settings") as mock_settings:
                mock_settings.LLM_API_KEY = "test-key"
                mock_settings.LLM_API_URL = "https://test.com"
                mock_settings.LLM_MODEL = "glm-4-flash"
                p = ConversationParser()
                result = await p.extract_memories([
                    {"role": "user", "content": "Redis缓存怎么配置"},
                    {"role": "assistant", "content": "设置maxmemory-policy为allkeys-lru"},
                ])

        assert len(result) == 1
        assert result[0]["content"] == "Redis缓存配置步骤"
        assert result[0]["importance"] == 7.5
        assert result[0]["scene"] == "ops"

    @pytest.mark.asyncio
    async def test_extract_memories_llm_fail(self):
        """LLM失败返回空列表"""
        with patch("core.conversation.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("API error"))
            mock_client_cls.return_value = mock_client

            with patch("core.conversation.settings") as mock_settings:
                mock_settings.LLM_API_KEY = "test-key"
                mock_settings.LLM_API_URL = "https://test.com"
                mock_settings.LLM_MODEL = "glm-4-flash"
                p = ConversationParser()
                result = await p.extract_memories([
                    {"role": "user", "content": "Redis缓存怎么配置"},
                ])

        assert result == []

    @pytest.mark.asyncio
    async def test_extract_no_api_key(self):
        """无API key返回空列表"""
        with patch("core.conversation.settings") as mock_settings:
            mock_settings.LLM_API_KEY = ""
            p = ConversationParser()
            result = await p.extract_memories([{"role": "user", "content": "test"}])

        assert result == []


# ── 去重测试 ──

class TestDeduplicate:
    @pytest.mark.asyncio
    async def test_deduplicate(self):
        """相似记忆被标记skip"""
        emb = [0.1] * 100
        memories = [
            {"content": "Redis配置allkeys-lru", "_embedding": emb},
            {"content": "Redis配置allkeys-lru模式", "_embedding": emb},
        ]
        existing = [("mem_001", emb)]

        result = await parser.deduplicate(memories, existing)
        skipped = [m for m in result if m.get("skip")]
        assert len(skipped) >= 1

    @pytest.mark.asyncio
    async def test_deduplicate_no_existing(self):
        """无已有记忆时全部保留"""
        memories = [
            {"content": "Redis配置", "_embedding": [0.1] * 100},
        ]
        result = await parser.deduplicate(memories, None)
        assert len(result) == 1
        assert result[0].get("skip") is None or result[0]["skip"] is False


# ── API测试 ──

class TestConversationAPI:
    @pytest.mark.asyncio
    async def test_conversation_api(self, client):
        """端到端API测试（无LLM key，提取为空）"""
        resp = await client.post("/api/v1/memory/conversation", json={
            "messages": [
                {"role": "user", "content": "Redis缓存怎么配置"},
                {"role": "assistant", "content": "分三步设置maxmemory-policy为allkeys-lru"},
                {"role": "user", "content": "可以"},
                {"role": "assistant", "content": "NO_REPLY"},
            ],
            "auto_store": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["filtered_count"] == 2
        assert data["stored_count"] == 0

    @pytest.mark.asyncio
    async def test_conversation_api_no_store(self, client):
        """auto_store=false不存储"""
        resp = await client.post("/api/v1/memory/conversation", json={
            "messages": [
                {"role": "user", "content": "Redis缓存怎么配置"},
                {"role": "assistant", "content": "分三步设置maxmemory-policy为allkeys-lru"},
            ],
            "auto_store": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["stored_count"] == 0
        assert data["filtered_count"] == 2

    @pytest.mark.asyncio
    async def test_conversation_api_with_mock_llm(self, client):
        """mock LLM后能提取并存储"""
        mock_llm_response = json.dumps([
            {"content": "Redis缓存配置：maxmemory-policy=allkeys-lru", "importance": 7.5, "scene": "ops", "entities": ["Redis"]}
        ])
        mock_response = {
            "choices": [{"message": {"content": mock_llm_response}}]
        }
        mock_resp = _make_mock_llm_response(mock_response)

        import api.conversation as conv_api_mod
        import core.conversation as conv_mod
        with patch("core.conversation.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            # Patch the parser instance's LLM key (set in __init__)
            with patch.object(conv_api_mod.parser, "_llm_key", "test-key"):
                resp = await client.post("/api/v1/memory/conversation", json={
                    "messages": [
                        {"role": "user", "content": "Redis缓存怎么配置"},
                        {"role": "assistant", "content": "设置maxmemory-policy为allkeys-lru，maxmemory为4gb"},
                        {"role": "user", "content": "可以"},
                    ],
                    "auto_store": True,
                })

        assert resp.status_code == 200
        data = resp.json()
        assert data["extracted_count"] == 1
        assert data["stored_count"] == 1
        assert data["memories"][0]["scene"] == "ops"
        assert "Redis" in data["memories"][0]["entities"]
