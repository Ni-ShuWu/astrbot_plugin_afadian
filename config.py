import json
import os

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
PLUGIN_CONFIG_PATH = os.path.join(DATA_DIR, "plugin_config.json")


class ConfigManager:
    def __init__(self, wire_fn=None):
        self._wire = wire_fn or print

    def load_plugin_config(self) -> dict:
        try:
            if os.path.exists(PLUGIN_CONFIG_PATH):
                with open(PLUGIN_CONFIG_PATH, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.startswith('\ufeff'):
                        content = content[1:]
                    cfg = json.loads(content)
                    self._wire(f"[AfdianModel] 插件配置加载成功: {list(cfg.keys())}", "info")
                    return cfg
        except Exception as e:
            self._wire(f"[AfdianModel] 插件配置加载失败: {e}", "error")
        return {}

    def save_plugin_config(self, cfg: dict):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp_path = PLUGIN_CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, PLUGIN_CONFIG_PATH)
            self._wire(f"[AfdianModel] 插件配置保存成功: {list(cfg.keys())}", "info")
        except Exception as e:
            self._wire(f"[AfdianModel] 插件配置保存失败: {e}", "error")
