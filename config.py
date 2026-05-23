import json
import os
import threading

class ConfigManager:
    def __init__(self, data_dir: str, wire_fn=None):
        self._wire = wire_fn or print
        self._data_dir = data_dir
        self._config_path = os.path.join(data_dir, "plugin_config.json")
        self._lock = threading.Lock()

    def load_plugin_config(self) -> dict:
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, "r", encoding="utf-8") as f:
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
        with self._lock:
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                tmp_path = self._config_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._config_path)
                self._wire(f"[AfdianModel] 插件配置保存成功: {list(cfg.keys())}", "info")
            except Exception as e:
                self._wire(f"[AfdianModel] 插件配置保存失败: {e}", "error")
