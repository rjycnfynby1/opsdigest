"""Пересобрать opsdigest/sample_digest.json (пример отчёта для `demo --offline`) через настоящий конвейер
и поддельную LLM из fake_llm.py. Запуск: python tests/make_sample.py"""
import copy
import json
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402  — подмена telethon, если его нет
from fake_llm import start_server  # noqa: E402

from opsdigest.config import DEFAULTS, Config, load_config  # noqa: E402
from opsdigest.demo import load_demo  # noqa: E402
from opsdigest.digest import DigestBuilder, get_tz  # noqa: E402
from opsdigest.llm import make_client  # noqa: E402
from opsdigest.storage import Store  # noqa: E402

root = Path(__file__).resolve().parent.parent
srv, url = start_server()
cfg = load_config(root / "tools" / "config.example.toml")
cfg.data["llm"]["profiles"] = {"local": {"type": "openai", "base_url": url, "model": "auto"}}
cfg.data["llm"]["provider"] = "local"
store = Store(":memory:")
since, until = load_demo(cfg, store, get_tz("local"))
d = DigestBuilder(cfg, store, make_client(*cfg.llm_profile()), progress=print).build(since, until, "day")
d["meta"]["provider"] = "пример (выдуманные данные)"
(root / "opsdigest" / "sample_digest.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
print("ok")
