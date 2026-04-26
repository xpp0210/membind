"""
MemBind 共享 HTTP 客户端

统一 httpx.AsyncClient 连接池，避免每个模块各自创建短连接。
"""
import httpx

_client: httpx.AsyncClient | None = None
_client_loop = None  # track which event loop the client was created on

def get_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """获取全局 httpx 客户端（懒初始化，连接池复用）"""
    global _client, _client_loop
    import asyncio
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _client is None or _client.is_closed or (current_loop is not None and _client_loop is not current_loop):
        # Discard stale client (don't await aclose — may be on a dead loop)
        _client = None
        _client_loop = None
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        _client_loop = current_loop
    return _client

def close_client():
    """关闭全局客户端（应用关闭时调用）"""
    global _client, _client_loop
    if _client and not _client.is_closed:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_client.aclose())
        except RuntimeError:
            # No running loop — try sync close
            pass
        _client = None
        _client_loop = None
