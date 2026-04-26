"""
MemBind 共享工具函数

向量相似度计算、BLOB编解码等跨模块复用的工具。
"""

import math
import struct


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度，维度不匹配时返回 0.0"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def blob_to_floats(data: bytes, dim: int) -> list[float]:
    """将 SQLite BLOB（packed little-endian float32）解码为 float 列表"""
    if not data:
        return []
    try:
        return list(struct.unpack(f"{dim}f", data[: dim * 4]))
    except Exception:
        return []


def cosine_similarity_blob(blob: bytes, vec: list[float]) -> float:
    """BLOB 字节直接与 float 列表计算余弦相似度"""
    dim = len(blob) // 4
    return cosine_similarity(blob_to_floats(blob, dim), vec)
