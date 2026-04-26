"""
MemBind pytest fixtures

所有测试共享的fixture：临时数据库 + httpx AsyncClient
"""

import os
import sys
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# 确保项目根目录在path中
sys.path.insert(0, str(__file__).rsplit("/tests", 1)[0])

from config import settings


@pytest_asyncio.fixture
async def client(tmp_path):
    """创建测试用的async client，使用临时数据库"""
    db_path = str(tmp_path / "test_membind.db")

    # 临时覆盖数据库路径
    original_db = settings.MEMBIND_DB_PATH
    settings.MEMBIND_DB_PATH = db_path

    # 确保无API key（使用零向量模式）
    os.environ.pop("EMBEDDING_API_KEY", None)
    os.environ.pop("LLM_API_KEY", None)
    settings.EMBEDDING_API_KEY = ""
    settings.LLM_API_KEY = ""

    # 禁用速率限制（测试环境）
    original_rate = settings.RATE_LIMIT_PER_MINUTE
    settings.RATE_LIMIT_PER_MINUTE = 0

    # 延迟导入server（确保settings已更新）
    from db.connection import init_db
    init_db(db_path)

    from server import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # 恢复
    settings.MEMBIND_DB_PATH = original_db
    settings.RATE_LIMIT_PER_MINUTE = original_rate
