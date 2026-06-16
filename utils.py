"""公共工具模块 —— 签名、序列化、日志等全项目复用的静态方法。"""

import hashlib
import json
import re
import time
from typing import Any, Callable

PLUGIN_LOG_PREFIX = "[AfdianModel]"

# 模型编号前缀映射
LEVEL_ID_PREFIX = {"0": "zero", "1": "one", "2": "two"}


# ── 爱发电 API 签名 ──────────────────────────────

class AfdianSigner:
    """爱发电 API 请求签名器。"""

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
        return {"user_id": self._user_id, "params": json_params, "ts": ts, "sign": sig}

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


# ── 输入清洗 ──────────────────────────────────────

def clean_arg(value: str) -> str:
    """清洗用户输入参数，去除误输入的尖括号及首尾空白。

    防止用户将帮助文本中的 <占位符> 当作字面语法输入，
    例如 afdian_bind<abcd1234> 或 afdian_bind <abcd1234>。
    """
    return value.strip().strip("<>").strip()


# ── 日志 ──────────────────────────────────────────

LogFn = Callable[[str, str], None]  # (msg, level)


def log_msg(wire: LogFn | None, msg: str, level: str = "info") -> None:
    """统一日志输出入口，wire 为 None 时静默。"""
    if wire:
        wire(f"{PLUGIN_LOG_PREFIX} {msg}", level)
