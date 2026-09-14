"""后台调度：防抖合并 → 推送 agent webhook → 超时检查；以及定期刷新后端令牌。"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .backend import XianyuBackend
from .config import Settings
from .store import Store

log = logging.getLogger("gateway.pusher")


def build_webhook_payload(event, shadow_mode: bool) -> dict:
    return {
        "event": "buyer_message",
        "event_id": event["id"],
        "account_id": event["account_id"],
        "chat_id": event["chat_id"],
        "item_id": event["item_id"] or "",
        "buyer_id": event["buyer_id"] or "",
        "buyer_name": event["buyer_name"] or "",
        "message": event["message"],
        "time": event["msg_time"] or "",
        "shadow_mode": shadow_mode,
    }


class Pusher:
    def __init__(self, settings: Settings, store: Store, backend: XianyuBackend):
        self.settings = settings
        self.store = store
        self.backend = backend
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._push_loop(), name="push-loop"),
            asyncio.create_task(self._token_loop(), name="token-loop"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.aclose()

    async def _push_loop(self) -> None:
        tick = 0
        while True:
            try:
                await self.store.promote_ready_events()
                for event in await self.store.due_pending_events():
                    await self._push(event)
                for event_id in await self.store.expire_unacked(self.settings.ack_timeout_seconds):
                    log.warning("事件 %s 推送后 %ss 内未 ack，标记 timeout", event_id, self.settings.ack_timeout_seconds)
                tick += 1
                if tick % 3600 == 0:
                    await self.store.purge_seen_hooks()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("推送循环异常")
            await asyncio.sleep(1)

    def _auth_headers(self) -> dict:
        if not self.settings.webhook_token:
            return {}
        scheme = self.settings.webhook_auth_scheme
        value = f"{scheme} {self.settings.webhook_token}" if scheme else self.settings.webhook_token
        return {self.settings.webhook_auth_header: value}

    async def _push(self, event) -> None:
        if not self.settings.webhook_url:
            await self.store.mark_push_failed(event["id"], "未配置 AGENT_WEBHOOK_URL", 1)
            log.error("未配置 AGENT_WEBHOOK_URL，事件 %s 无法推送", event["id"])
            return
        try:
            response = await self.client.post(
                self.settings.webhook_url,
                json=build_webhook_payload(event, self.settings.shadow_mode),
                headers=self._auth_headers(),
            )
            if 200 <= response.status_code < 300:
                await self.store.mark_pushed(event["id"])
                log.info("事件 %s 已推送（会话 %s）", event["id"], event["chat_id"])
                return
            error = f"HTTP {response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        attempts, give_up = await self.store.mark_push_failed(event["id"], error, self.settings.push_max_attempts)
        log.warning("事件 %s 推送失败（第 %s 次%s）: %s", event["id"], attempts, "，放弃" if give_up else "", error)

    async def _token_loop(self) -> None:
        while True:
            try:
                await self.backend.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("刷新后端令牌失败，5 分钟后重试")
                await asyncio.sleep(300)
                continue
            await asyncio.sleep(self.settings.token_refresh_hours * 3600)
