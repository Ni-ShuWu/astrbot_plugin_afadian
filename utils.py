"""公共工具模块 —— 签名、序列化、路径、日志等全项目复用的静态方法。

消除 afdian_api.py 中内联 import 和重复签名逻辑。
"""

import hashlib
import json
import os
import re
import time
from typing import Any, Callable

PLUGIN_LOG_PREFIX = "[AfdianModel]"


# ── 爱发电 API 签名 ──────────────────────────────

class AfdianSigner:
    """爱发电 API 请求签名器。统一签名逻辑，消除分散在各处的重复实现。"""

    def __init__(self, user_id: str, token: str) -> None:
        if not user_id or not token:
            raise ValueError("user_id 和 token 不能为空")
        self._user_id = user_id
        self._token = token

    def sign(self, params: dict) -> dict:
        """对参数签名，返回完整的请求体。"""
        ts = int(time.time())
        json_params = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        raw = f"{self._token}params{json_params}ts{ts}user_id{self._user_id}"
        sig = hashlib.md5(raw.encode()).hexdigest()
        return {
            "user_id": self._user_id,
            "params": json_params,
            "ts": ts,
            "sign": sig,
        }

    @staticmethod
    def normalize_base_url(raw: str) -> str:
        """规范化 API 地址：去尾部路径，去末尾斜杠。"""
        return re.sub(r"/api/open.*$", "", raw).rstrip("/")


# ── 序列化 ────────────────────────────────────────

def list_to_str(lst: list[str]) -> str:
    """列表 → 逗号分隔字符串。"""
    return ",".join(lst) if lst else ""


def str_to_list(s: str) -> list[str]:
    """逗号分隔字符串 → 去空格列表。"""
    if not s or not isinstance(s, str):
        return []
    return [item.strip() for item in s.split(",") if item.strip()]


def model_names_from_config(raw: Any) -> list[str]:
    """解析配置中的模型列表（兼容 list/str 两种格式）。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        return [m.strip() for m in raw.split(",") if m.strip()]
    return []


# ── 文件 I/O 原子写入 ──────────────────────────────

def atomic_write(path: str, content: str) -> None:
    """原子写入：先写 tmp 再 rename，防止写入中断损坏原文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def atomic_write_json(path: str, data: Any) -> None:
    """原子写入 JSON 文件。"""
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def safe_read_json(path: str, default: Any = None) -> Any:
    """安全读取 JSON 文件，不存在或损坏时返回 default。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                if content.startswith("\ufeff"):
                    content = content[1:]
                return json.loads(content)
    except (json.JSONDecodeError, OSError):
        pass
    return default


# ── 日志 ──────────────────────────────────────────

LogFn = Callable[[str, str], None]  # (msg, level)


def log_msg(wire: LogFn | None, msg: str, level: str = "info") -> None:
    """统一日志输出入口，wire 为 None 时静默。"""
    if wire:
        wire(f"{PLUGIN_LOG_PREFIX} {msg}", level)
