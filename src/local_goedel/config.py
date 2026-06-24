"""Settings and configuration loading."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    model_name: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    lean_server_url: str = "http://localhost:8000"
    leandex_url: str = "https://leandex.projectnumina.ai/api/v1/search"
    leandex_api_key: str = ""
    lean_timeout_s: int = 120
    mathlib_min_interval_s: float = 2.0
    mathlib_max_retries: int = 4
    llm_max_retries: int = 5
    prover_max_tool_calls: int = 40
    prover_max_turns: int = 60
    reasoning_effort: str = "high"
    enable_thinking: bool = False
    artifacts_dir: Path = field(default_factory=lambda: Path("artifacts"))

    # Phase 2 fields
    iters_easy: int = 8
    iters_hard: int = 16
    blueprint_max_retries: int = 6
    refiner_max_retries: int = 6
    max_wall_s: int = 3600
    runs_dir: str = "runs"


_PS_LINE = re.compile(r'^\s*\$env:(\w+)\s*=\s*(.+)$')


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
        return value[1:-1]
    return value


def load_settings(env_path: str | Path | None = None) -> Settings:
    """Load settings from a PowerShell-format .env file and/or real env vars."""
    parsed: dict[str, str] = {}

    if env_path is not None:
        env_path = Path(env_path)
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    m = _PS_LINE.match(line)
                    if m:
                        key = m.group(1)
                        val = _strip_quotes(m.group(2).strip())
                        parsed[key] = val

    def _get(key: str, default: str = "") -> str:
        # env file takes priority, then real env vars
        return parsed.get(key) or os.environ.get(key, default)

    settings = Settings(
        model_name=_get("MODEL_NAME", "deepseek-chat"),
        base_url=_get("BASE_URL", "https://api.deepseek.com"),
        api_key=_get("DEEPSEEK_API_KEY", ""),
        lean_server_url=_get("LEAN_SERVER_URL", "http://localhost:8000"),
        leandex_url=_get(
            "LEANDEX_URL",
            "https://leandex.projectnumina.ai/api/v1/search",
        ),
        leandex_api_key=_get("LEANDEX_API_KEY", ""),
        artifacts_dir=Path(_get("ARTIFACTS_DIR", "artifacts")),
    )
    return settings
