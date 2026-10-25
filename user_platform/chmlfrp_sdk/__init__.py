"""ChmlFrp panel SDK — async client for cf-v2.uapis.cn.

Usage::

    import asyncio
    from chmlfrp import ChmlFrpClient

    async def main():
        async with ChmlFrpClient(access_token="<JWT>") as c:
            me = await c.fetch_userinfo()
            tunnels = await c.list_tunnels()
            for t in tunnels:
                print(t.id, t.name, t.node, t.dorp)
            ini = await c.tunnel_config(tunnels[0].node, tunnels[0].name)
            print(ini)
    asyncio.run(main())
"""
from .client import ChmlFrpClient
from .models import Node, SignInfo, TokenPair, Tunnel, User
from .exceptions import AuthError, CaptchaRequired, ChmlFrpError

__all__ = [
    "ChmlFrpClient",
    "TokenPair",
    "User",
    "SignInfo",
    "Tunnel",
    "Node",
    "ChmlFrpError",
    "AuthError",
    "CaptchaRequired",
]
__version__ = "0.1.0"
