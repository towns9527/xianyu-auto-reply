"""xianyu-gateway 入口。

路由：
  POST /hook/{secret}  闲鱼服务「默认回复 API」回调，立即返回 {"success": false}（不同步回复）
  POST /mcp            MCP Streamable HTTP（Bearer GATEWAY_BOT_TOKEN）
  GET  /status         事件统计与最近转人工（Bearer GATEWAY_BOT_TOKEN）
  GET  /health         健康检查
"""
from __future__ import annotations

import contextlib
import hmac
import logging

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .backend import XianyuBackend
from .config import load_settings
from .pusher import Pusher
from .store import Store
from .tools import build_mcp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")

settings = load_settings()
store = Store(settings.data_dir / "gateway.db")
backend = XianyuBackend(settings.xy_api_base, settings.data_dir / "refresh_token", settings.message_send_api_key)
pusher = Pusher(settings, store, backend)
mcp = build_mcp(settings, store, backend)

NO_REPLY = {"success": False, "message": "handled asynchronously by xianyu-gateway"}


def _bearer_ok(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.bot_token}"
    return hmac.compare_digest(header.encode(), expected.encode())


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (path == "/mcp" or path.startswith("/mcp/") or path == "/status") and not _bearer_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def hook(request: Request) -> JSONResponse:
    if not hmac.compare_digest(request.path_params["secret"].encode(), settings.hook_secret.encode()):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(NO_REPLY)
    if not isinstance(payload, dict) or not payload.get("account_id") or not payload.get("chat_id") or not str(payload.get("message") or "").strip():
        log.warning("hook 数据不完整，忽略: %s", {k: payload.get(k) for k in ("account_id", "chat_id")} if isinstance(payload, dict) else payload)
        return JSONResponse(NO_REPLY)
    event_id = await store.record_buyer_message(payload, settings.debounce_seconds)
    if event_id:
        log.info("收到买家消息 → 事件 %s（会话 %s）", event_id, payload.get("chat_id"))
    else:
        log.info("重复回调已忽略（会话 %s）", payload.get("chat_id"))
    return JSONResponse(NO_REPLY)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "shadow_mode": settings.shadow_mode})


async def status(_: Request) -> JSONResponse:
    return JSONResponse({"shadow_mode": settings.shadow_mode, **await store.stats()})


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    await store.open()
    pusher.start()
    log.info("gateway 启动：shadow_mode=%s webhook=%s", settings.shadow_mode, bool(settings.webhook_url))
    async with mcp.session_manager.run():
        yield
    await pusher.stop()
    await backend.close()
    await store.close()


app = Starlette(
    routes=[
        Route("/hook/{secret}", hook, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=lifespan,
)
