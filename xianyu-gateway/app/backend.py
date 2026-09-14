"""闲鱼服务 backend-web 客户端。

鉴权：只保存 refresh token（/data/refresh_token），用它轮换出 access token；
每次刷新都会拿到新的 refresh token 并原子写回，因此只要网关不连续停机超过 7 天就能一直保持登录。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("gateway.backend")


class BackendError(RuntimeError):
    pass


class XianyuBackend:
    def __init__(self, base_url: str, refresh_token_path: Path, message_send_api_key: str):
        self.base_url = base_url
        self.refresh_token_path = refresh_token_path
        self.message_send_api_key = message_send_api_key
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        self._access_token: str | None = None
        self._access_token_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    # ---------- token ----------

    def _read_refresh_token(self) -> str:
        try:
            return self.refresh_token_path.read_text().strip()
        except FileNotFoundError as exc:
            raise BackendError(f"refresh token 文件不存在: {self.refresh_token_path}") from exc

    def _write_refresh_token(self, token: str) -> None:
        tmp = self.refresh_token_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token)
        os.replace(tmp, self.refresh_token_path)

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        response = await self.client.post(
            f"{self.base_url}/auth/refresh",
            headers={"Authorization": f"Bearer {self._read_refresh_token()}"},
        )
        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get("success"):
            raise BackendError(f"刷新令牌失败: HTTP {response.status_code} {data.get('message')}")
        self._write_refresh_token(data["refresh_token"])
        self._access_token = data["token"]
        self._access_token_at = time.time()
        log.info("access token 已刷新（用户 %s）", data.get("username"))

    async def _token(self) -> str:
        # access token 有效期 24h，超过 12h 就提前换
        if not self._access_token or time.time() - self._access_token_at > 12 * 3600:
            async with self._lock:
                if not self._access_token or time.time() - self._access_token_at > 12 * 3600:
                    await self._refresh_locked()
        assert self._access_token
        return self._access_token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(2):
            response = await self.client.get(
                f"{self.base_url}{path}",
                params={k: v for k, v in (params or {}).items() if v not in (None, "")},
                headers={"Authorization": f"Bearer {await self._token()}"},
            )
            if response.status_code == 401 and attempt == 0:
                self._access_token = None
                continue
            if response.status_code != 200:
                raise BackendError(f"GET {path} 失败: HTTP {response.status_code} {response.text[:200]}")
            return response.json()
        raise BackendError(f"GET {path} 鉴权失败")

    # ---------- 业务接口 ----------

    async def list_account_ids(self) -> list[str]:
        data = await self._get("/cookies/options")
        return [str(item.get("id")) for item in data or []]

    async def get_item(self, account_id: str, item_id: str) -> dict[str, Any]:
        data = await self._get(f"/items/{account_id}/{item_id}")
        return data.get("item") or {}

    async def list_items(self, account_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/items/cookie/{account_id}")
        return data.get("items") or []

    async def list_orders(self, account_id: str, search: str | None, status: str | None, page_size: int) -> dict[str, Any]:
        return await self._get(
            "/orders",
            {"cookie_id": account_id, "search": search, "status": status, "page": 1, "page_size": page_size},
        )

    async def get_order(self, order_no: str) -> dict[str, Any]:
        return await self._get(f"/orders/{order_no}")

    async def send_message(self, account_id: str, chat_id: str, to_user_id: str, text: str) -> dict[str, Any]:
        if not self.message_send_api_key:
            raise BackendError("未配置 MESSAGE_SEND_API_KEY，无法发送消息")
        response = await self.client.post(
            f"{self.base_url}/messages/send",
            json={
                "api_key": self.message_send_api_key,
                "cookie_id": account_id,
                "chat_id": chat_id,
                "to_user_id": to_user_id,
                "message": text,
            },
        )
        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get("success"):
            raise BackendError(f"发送失败: {data.get('message') or response.status_code}")
        return data
