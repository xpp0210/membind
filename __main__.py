"""Support `python -m membind.mcp_server` (convenience alias)"""
from mcp_server import main
import asyncio

asyncio.run(main())
