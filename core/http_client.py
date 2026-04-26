"""
MemBind 共享 HTTP 客户端

统一 httpx.AsyncClient 连接池，避免每个模块各自创建短连接。
"""
import httpx

_client: httpx.AsyncClient | None = None

def get_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """获取全局 httpx 客户端（懒初始化，连接池复用）"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _client

def close_client():
    """关闭全局客户端（应用关闭时调用）"""
    global _client
    if _client and not _client.is_closed:
        _client.aclose()
        _client = None
