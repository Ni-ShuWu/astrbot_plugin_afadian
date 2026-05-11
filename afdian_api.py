import asyncio
import hashlib
import json
import aiohttp


class AfdianAPI:
    def __init__(self, user_id: str, token: str, api_base: str, log_fn=None):
        self._user_id = user_id
        self._token = token
        import re
        self._api_base = re.sub(r"/api/open.*$", "", api_base).rstrip("/")
        self._wire = log_fn or print

    async def query_order(self, page: int = 1) -> dict:
        params = {"page": page}
        ts = int(__import__("time").time())
        json_params = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        raw = f"{self._token}params{json_params}ts{ts}user_id{self._user_id}"
        sig = hashlib.md5(raw.encode()).hexdigest()
        body = {
            "user_id": self._user_id,
            "params": json_params,
            "ts": ts,
            "sign": sig,
        }
        url = f"{self._api_base}/api/open/query-order"
        try:
            self._wire(f"[AfdianModel] API请求: query-order page={page} url={url}")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.json(content_type=None)
                    ec = data.get("ec", -1)
                    if ec == 200:
                        order_count = len(data.get("data", {}).get("list", []))
                        self._wire(f"[AfdianModel] API响应: query-order page={page} ec=200 orders={order_count}")
                    else:
                        self._wire(f"[AfdianModel] API响应异常: query-order page={page} ec={ec} em={data.get('em', '')}", "warning")
                    return data
        except Exception as e:
            self._wire(f"[AfdianModel] API请求失败: query-order page={page} - {e}", "error")
            return {"ec": -1, "em": str(e)}
