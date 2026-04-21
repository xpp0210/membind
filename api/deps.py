"""
MemBind API公共依赖

提供namespace提取、API Key认证等公共逻辑。
"""

from fastapi import Request, HTTPException
from config import settings


def get_namespace(request: Request) -> str:
    """
    从请求中提取namespace。
    
    优先级：Header X-MemBind-Namespace > Query param ns > 默认 'default'
    """
    ns = request.headers.get("X-MemBind-Namespace")
    if not ns:
        ns = request.query_params.get("ns")
    return ns or "default"


def verify_api_key(request: Request) -> None:
    """
    API Key认证。Key为空则跳过（本地开发模式）。
    /health 端点免认证。
    """
    api_keys = settings.MEMBIND_API_KEYS.strip()
    if not api_keys:
        return  # 本地开发模式，跳过认证

    key = request.headers.get("X-MemBind-API-Key", "")
    allowed = {k.strip() for k in api_keys.split(",") if k.strip()}
    if key not in allowed:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
