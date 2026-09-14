"""网关配置：全部来自环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # 闲鱼服务 backend-web（容器内网）
    xy_api_base: str
    message_send_api_key: str
    # 数据目录：refresh_token、SQLite、rules.md
    data_dir: Path
    # 鉴权
    bot_token: str
    hook_secret: str
    # agent webhook（事件推送目标）
    webhook_url: str
    webhook_token: str
    webhook_auth_header: str
    webhook_auth_scheme: str
    # 行为
    shadow_mode: bool
    debounce_seconds: int
    ack_timeout_seconds: int
    push_max_attempts: int
    max_message_chars: int
    max_messages_per_event: int
    token_refresh_hours: int


def load_settings() -> Settings:
    settings = Settings(
        xy_api_base=os.getenv("XY_API_BASE", "http://backend-web:8089/api/v1").rstrip("/"),
        message_send_api_key=os.getenv("MESSAGE_SEND_API_KEY", "").strip(),
        data_dir=Path(os.getenv("DATA_DIR", "/data")),
        bot_token=os.getenv("GATEWAY_BOT_TOKEN", "").strip(),
        hook_secret=os.getenv("GATEWAY_HOOK_SECRET", "").strip(),
        # 兼容旧变量名 GROK_WEBHOOK_URL / GROK_WEBHOOK_TOKEN
        webhook_url=(os.getenv("AGENT_WEBHOOK_URL") or os.getenv("GROK_WEBHOOK_URL") or "").strip(),
        webhook_token=(os.getenv("AGENT_WEBHOOK_TOKEN") or os.getenv("GROK_WEBHOOK_TOKEN") or "").strip(),
        webhook_auth_header=os.getenv("AGENT_WEBHOOK_AUTH_HEADER", "Authorization").strip() or "Authorization",
        webhook_auth_scheme=os.getenv("AGENT_WEBHOOK_AUTH_SCHEME", "Bearer").strip(),
        shadow_mode=_bool("SHADOW_MODE", True),
        debounce_seconds=_int("DEBOUNCE_SECONDS", 3),
        ack_timeout_seconds=_int("ACK_TIMEOUT_SECONDS", 600),
        push_max_attempts=_int("PUSH_MAX_ATTEMPTS", 5),
        max_message_chars=_int("MAX_MESSAGE_CHARS", 200),
        max_messages_per_event=_int("MAX_MESSAGES_PER_EVENT", 5),
        token_refresh_hours=_int("TOKEN_REFRESH_HOURS", 12),
    )
    missing = [
        name
        for name, value in (
            ("GATEWAY_BOT_TOKEN", settings.bot_token),
            ("GATEWAY_HOOK_SECRET", settings.hook_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少必需环境变量: {', '.join(missing)}")
    return settings
