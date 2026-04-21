"""
MemBind 配置管理

从环境变量读取配置，提供合理默认值。
支持 .env 文件（python-dotenv）。
"""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    """MemBind 全局配置"""

    # ── 数据库 ──
    MEMBIND_DB_PATH: str = "data/membind.db"
    MEMBIND_PORT: int = 8901

    # ── Embedding ──
    EMBEDDING_DIM: int = 1024
    EMBEDDING_API_URL: str = "https://api.siliconflow.cn/v1/embeddings"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_API_MODEL: str = "BAAI/bge-m3"

    # ── LLM（标签提取 + binding评分）──
    LLM_API_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "glm-4-flash"

    # ── 检索权重（三者和=1.0）──
    SEMANTIC_WEIGHT: float = 0.4       # 语义相似度权重
    TIME_DECAY_WEIGHT: float = 0.3     # 时间衰减权重
    IMPORTANCE_WEIGHT: float = 0.3     # 重要性权重

    # ── Binding评分权重（四者和=1.0）──
    BINDING_INTENT_WEIGHT: float = 0.4   # 查询意图匹配
    BINDING_SCENE_WEIGHT: float = 0.3    # 场景匹配
    BINDING_ENTITY_WEIGHT: float = 0.2   # 实体匹配
    BINDING_TIME_WEIGHT: float = 0.1     # 时间新鲜度

    # ── API认证 ──
    MEMBIND_API_KEYS: str = ""  # 逗号分隔的API Key列表，为空则跳过认证

    # ── 冲突检测 ──
    CONFLICT_THRESHOLD: float = 0.85    # 相似度超过此值视为冲突

    # ── 生命周期 ──
    IMPORTANCE_DECAY: float = 0.5       # 衰减步长
    IMPORTANCE_BOOST: float = 1.0       # 强化步长
    DECAY_DAYS: int = 30               # 衰减阈值（天）
    DELETE_DAYS: int = 90              # 软删除阈值（天）
    BOOST_RECENT_BINDINGS: int = 5     # 强化所需最近绑定次数
    BOOST_RELEVANT_RATE: float = 0.8   # 强化所需相关率
    CLEANUP_THRESHOLD: float = 1.0      # importance低于此值标记删除

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
