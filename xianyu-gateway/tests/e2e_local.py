"""本地端到端测试：假 backend-web + 假 agent webhook + 真 gateway + 真 MCP 客户端。

运行：.venv/bin/python tests/e2e_local.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[1]
BACKEND_PORT, WEBHOOK_PORT, GATEWAY_PORT = 18089, 18090, 18788
BOT_TOKEN, HOOK_SECRET = "test-bot-token", "test-hook-secret"
GATEWAY_ENV: dict[str, str] = {}
received_webhooks: list[dict] = []
sent_messages: list[dict] = []
refresh_calls = {"n": 0}


# ---------------- 假 backend-web ----------------
async def refresh(request: Request):
    refresh_calls["n"] += 1
    assert request.headers["authorization"].startswith("Bearer rt-")
    return JSONResponse({"success": True, "token": "access-1", "refresh_token": f"rt-{refresh_calls['n']}", "username": "bot-user"})


def _auth(request: Request):
    return request.headers.get("authorization") == "Bearer access-1"


async def item(request: Request):
    if not _auth(request):
        return JSONResponse({}, status_code=401)
    return JSONResponse({"item": {"item_id": "700000001", "item_title": "示例商品", "item_price": "9",
                                  "item_quantity": "1", "item_status_desc": "在售", "item_description": "封面干净",
                                  "ai_prompt": "", "item_sku_list": [], "item_detail": "{}"}})


async def orders(request: Request):
    return JSONResponse({"success": True, "data": [
        {"order_id": "O1", "buyer_id": "B1", "item_id": "700000001", "status": "paid", "amount": "9"},
        {"order_id": "O2", "buyer_id": "OTHER", "item_id": "x", "status": "paid", "amount": "99"},
    ], "total": 2})


async def order_detail(request: Request):
    no = request.path_params["no"]
    buyer = "B1" if no == "O1" else "OTHER"
    return JSONResponse({"success": True, "data": {"order_id": no, "cookie_id": "100000001", "buyer_id": buyer, "status": "paid"}})


async def account_options(request: Request):
    return JSONResponse([{"pk": 1, "id": "100000001"}] if _auth(request) else {}, status_code=200 if _auth(request) else 401)


async def items_of_account(request: Request):
    return JSONResponse({"items": [{"item_id": "700000001", "title": "示例商品"}]})


default_reply_calls: list[dict] = []


async def default_reply(request: Request):
    default_reply_calls.append(await request.json())
    return JSONResponse({"success": True, "message": "默认回复更新成功"})


async def ai_settings(request: Request):
    return JSONResponse({"ai_enabled": False})


async def keywords(request: Request):
    return JSONResponse([])


async def send(request: Request):
    body = await request.json()
    assert body["api_key"] == "send-key"
    sent_messages.append(body)
    return JSONResponse({"success": True, "message": "ok"})


backend_app = Starlette(routes=[
    Route("/api/v1/auth/refresh", refresh, methods=["POST"]),
    Route("/api/v1/items/{acc}/{item}", item),
    Route("/api/v1/orders", orders),
    Route("/api/v1/orders/{no}", order_detail),
    Route("/api/v1/messages/send", send, methods=["POST"]),
    Route("/api/v1/cookies/options", account_options),
    Route("/api/v1/items/cookie/{acc}", items_of_account),
    Route("/api/v1/default-replies/{acc}", default_reply, methods=["PUT"]),
    Route("/api/v1/ai-reply-settings/{acc}", ai_settings),
    Route("/api/v1/keywords-with-item-id/{acc}", keywords),
])


# ---------------- 假 agent webhook ----------------
async def webhook(request: Request):
    assert request.headers["authorization"] == "Bearer agent-webhook-token"
    received_webhooks.append(await request.json())
    return JSONResponse({"success": True, "runUuid": "r1"})


webhook_app = Starlette(routes=[Route("/wh", webhook, methods=["POST"])])


async def wait_port(port: int, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"port {port} not ready")


async def call(session: ClientSession, name: str, **args):
    result = await session.call_tool(name, args)
    assert not result.isError, f"{name} 报错: {result.content}"
    sc = result.structuredContent
    if sc is not None:
        return sc["result"] if set(sc) == {"result"} else sc
    text = result.content[0].text if result.content else ""
    try:
        return json.loads(text)
    except ValueError:
        return text


async def run_checks(data_dir: Path, shadow: bool) -> None:
    base = f"http://127.0.0.1:{GATEWAY_PORT}"
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{base}/health")).json()["ok"]
        assert (await client.get(f"{base}/status")).status_code == 401, "status 未鉴权"
        assert (await client.post(f"{base}/mcp", json={})).status_code == 401, "mcp 未鉴权"
        assert (await client.post(f"{base}/hook/wrong", json={})).status_code == 404, "hook secret 未校验"

        hook = {"account_id": "100000001", "chat_id": "C1", "item_id": "700000001", "send_user_id": "B1", "send_user_name": "示例买家"}
        r = await client.post(f"{base}/hook/{HOOK_SECRET}", json={**hook, "message": "在吗", "msg_time": "t1"})
        assert r.json()["success"] is False
        await client.post(f"{base}/hook/{HOOK_SECRET}", json={**hook, "message": "在吗", "msg_time": "t1"})  # 重复
        await client.post(f"{base}/hook/{HOOK_SECRET}", json={**hook, "message": "能便宜点吗", "msg_time": "t2"})

    for _ in range(40):
        if received_webhooks:
            break
        await asyncio.sleep(0.25)
    assert len(received_webhooks) == 1, f"webhook 次数异常: {received_webhooks}"
    evt = received_webhooks[0]
    assert evt["message"] == "在吗\n能便宜点吗", f"防抖合并/去重失败: {evt['message']!r}"
    assert evt["shadow_mode"] is shadow
    print("  ✓ hook 去重 + 防抖合并 + webhook 推送", evt["event_id"])

    headers = {"Authorization": f"Bearer {BOT_TOKEN}"}
    async with streamablehttp_client(f"http://127.0.0.1:{GATEWAY_PORT}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            print("  ✓ MCP 工具:", tools)
            ev = await call(session, "get_event", event_id=evt["event_id"])
            assert ev["buyer_id"] == "B1" and len(ev["history"]) == 2, ev
            it = await call(session, "get_item", event_id=evt["event_id"])
            assert it["price"] == "9", it
            od = await call(session, "search_buyer_orders", event_id=evt["event_id"])
            assert [o["order_no"] for o in od] == ["O1"], f"订单未按买家过滤: {od}"
            assert "error" in await call(session, "get_order", event_id=evt["event_id"], order_no="O2"), "他人订单泄露"
            assert (await call(session, "get_order", event_id=evt["event_id"], order_no="O1"))["order_id"] == "O1"
            blocked = await call(session, "send_message", event_id=evt["event_id"], text="加我微信聊")
            assert blocked["sent"] is False and "拦截" in blocked["error"], blocked
            ok = await call(session, "send_message", event_id=evt["event_id"], text="在的，9块包邮哈")
            if shadow:
                assert ok.get("shadow_mode") and not sent_messages, ok
            else:
                assert ok["sent"] and sent_messages[-1]["to_user_id"] == "B1" and sent_messages[-1]["chat_id"] == "C1", ok
            assert (await call(session, "ack_event", event_id=evt["event_id"], result="replied"))["acked"]
            late = await call(session, "send_message", event_id=evt["event_id"], text="再发一条")
            assert late["sent"] is False, "ack 后仍可发送"
            print("  ✓ 查询 / 买家订单隔离 / 内容拦截 / 发送(shadow=%s) / ack 后禁止发送" % shadow)

    async with httpx.AsyncClient() as client:
        st = (await client.get(f"http://127.0.0.1:{GATEWAY_PORT}/status", headers=headers)).json()
        assert st["events"].get("acked") == 1, st
    rt = (data_dir / "refresh_token").read_text()
    assert rt.startswith("rt-") and rt != "rt-0", f"refresh token 未轮换: {rt}"
    print("  ✓ /status 统计 + refresh token 轮换:", rt)

    env = {**os.environ, **GATEWAY_ENV}
    check = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "app.cli", "check"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert check.returncode == 0 and "100000001" in check.stdout, check.stdout + check.stderr
    bind = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "app.cli", "bind-default-reply", "--account", "100000001"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert bind.returncode == 0, bind.stdout + bind.stderr
    assert HOOK_SECRET not in bind.stdout, "CLI 输出泄露 hook 密钥"
    dr_call = default_reply_calls[-1]
    assert dr_call["reply_type"] == "api" and dr_call["api_url"].endswith(f"/hook/{HOOK_SECRET}") and dr_call["reply_once"] is False, dr_call
    print("  ✓ CLI check / bind-default-reply（输出不含密钥）")


async def main(shadow: bool) -> None:
    received_webhooks.clear()
    sent_messages.clear()
    servers = [
        uvicorn.Server(uvicorn.Config(backend_app, port=BACKEND_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(webhook_app, port=WEBHOOK_PORT, log_level="warning")),
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "refresh_token").write_text("rt-0")
        env = {
            "XY_API_BASE": f"http://127.0.0.1:{BACKEND_PORT}/api/v1",
            "MESSAGE_SEND_API_KEY": "send-key",
            "DATA_DIR": str(data_dir),
            "GATEWAY_BOT_TOKEN": BOT_TOKEN,
            "GATEWAY_HOOK_SECRET": HOOK_SECRET,
            "AGENT_WEBHOOK_URL": f"http://127.0.0.1:{WEBHOOK_PORT}/wh",
            "AGENT_WEBHOOK_TOKEN": "agent-webhook-token",
            "SHADOW_MODE": "true" if shadow else "false",
            "DEBOUNCE_SECONDS": "2",
        }
        GATEWAY_ENV.clear()
        GATEWAY_ENV.update(env)
        env = {**os.environ, **env}
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(GATEWAY_PORT), "--log-level", "warning"],
            cwd=ROOT, env=env,
        )
        try:
            await wait_port(BACKEND_PORT)
            await wait_port(WEBHOOK_PORT)
            await wait_port(GATEWAY_PORT)
            await run_checks(data_dir, shadow)
        finally:
            proc.terminate()
            proc.wait(10)
            for s in servers:
                s.should_exit = True
            await asyncio.gather(*tasks)


if __name__ == "__main__":
    for shadow_mode in (True, False):
        print(f"== shadow_mode={shadow_mode}")
        asyncio.run(main(shadow_mode))
    print("ALL PASSED")
