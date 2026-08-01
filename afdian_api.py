"""爱发电 API 客户端 —— 使用统一的 AfdianSigner 签名，复用连接会话。"""

import asyncio

import aiohttp

from .utils import AfdianSigner, LogFn, log_msg


class AfdianAPI:
    """爱发电开放 API 异步客户端。"""

    def __init__(self, user_id: str, token: str, api_base: str, wire: LogFn | None = None) -> None:
        self._signer = AfdianSigner(user_id, token)
        self._api_base = AfdianSigner.normalize_base_url(api_base)
        self._wire = wire
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        """关闭复用的连接会话。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def query_order(self, page: int = 1) -> dict:
        """查询订单列表（分页）。失败返回 {"ec": -1, "em": ...} 并记录日志。"""
        body = self._signer.sign({"page": page})
        url = f"{self._api_base}/api/open/query-order"

        log_msg(self._wire, f"API请求: query-order page={page}")
        try:
            session = await self._ensure_session()
            async with session.post(url, json=body) as resp:
                if resp.status != 200:
                    log_msg(self._wire, f"API HTTP异常: page={page} status={resp.status}", "warning")
                    return {"ec": -1, "em": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log_msg(self._wire, f"API请求失败: query-order page={page} - {e}", "error")
            return {"ec": -1, "em": str(e)}

        ec = data.get("ec", -1)
        if ec == 200:
            count = len(data.get("data", {}).get("list", []))
            log_msg(self._wire, f"API响应: query-order page={page} ec=200 orders={count}")
        else:
            log_msg(self._wire, f"API响应异常: page={page} ec={ec} em={data.get('em', '')}", "warning")
        return data
