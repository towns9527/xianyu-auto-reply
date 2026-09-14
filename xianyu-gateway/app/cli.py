"""初始化命令行工具（在 gateway 容器内运行）。

  python -m app.cli login [--username bot-user]
      以闲鱼后台用户登录一次，只保存 refresh token（之后 gateway 自动轮换，不保存密码）。
      后台默认开启登录滑动验证码，需先在「系统设置」临时关闭，完成后再打开。

  python -m app.cli bind-default-reply --account <闲鱼账号ID> [--hook-base http://xianyu-gateway:8788]
      把该账号的默认回复设为 API 模式并指向本网关的 hook 地址，同时检查 AI 回复、关键词是否会抢先处理消息。

  python -m app.cli check
      检查 refresh token 是否可用、当前用户可见的账号与商品数量。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

import httpx

from .backend import BackendError, XianyuBackend
from .config import load_settings


def _write_refresh_token(path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)


async def cmd_login(args, settings) -> int:
    username = args.username or input("后台用户名: ").strip()
    password = getpass.getpass("后台密码: ")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.xy_api_base}/auth/login", json={"username": username, "password": password})
    data = response.json() if response.content else {}
    if not data.get("success"):
        print(f"登录失败: {data.get('message') or response.status_code}")
        print("提示：后台默认开启「登录滑动验证码」，需在系统设置中临时关闭后再执行本命令。")
        return 1
    _write_refresh_token(settings.data_dir / "refresh_token", data["refresh_token"])
    print(f"登录成功：user={data.get('username')} is_admin={data.get('is_admin')}，refresh token 已保存到 {settings.data_dir / 'refresh_token'}")
    if data.get("is_admin"):
        print("警告：建议使用只拥有目标闲鱼账号的普通用户，而不是管理员。")
    print("完成后请重新打开「登录滑动验证码」。")
    return 0


def _backend(settings) -> XianyuBackend:
    return XianyuBackend(settings.xy_api_base, settings.data_dir / "refresh_token", settings.message_send_api_key)


async def cmd_bind_default_reply(args, settings) -> int:
    backend = _backend(settings)
    try:
        accounts = await backend.list_account_ids()
        if args.account not in accounts:
            print(f"当前用户看不到账号 {args.account}（可见：{accounts}）。请用该用户扫码添加账号，或由管理员调整账号归属。")
            return 1
        headers = {"Authorization": f"Bearer {await backend._token()}"}
        base = settings.xy_api_base
        hook_url = f"{args.hook_base.rstrip('/')}/hook/{settings.hook_secret}"
        response = await backend.client.put(
            f"{base}/default-replies/{args.account}",
            headers=headers,
            json={
                "enabled": True,
                "reply_type": "api",
                "reply_content": "",
                "reply_image": "",
                "api_url": hook_url,
                "api_timeout": args.timeout,
                "reply_once": False,
            },
        )
        result = response.json() if response.content else {}
        if response.status_code != 200 or not result.get("success", True):
            detail = str(result.get("message") or result.get("detail") or response.text).replace(settings.hook_secret, "***")
            print(f"设置默认回复失败: {detail}")
            print("提示：backend-web 与 websocket 需设置 REPLY_API_ALLOW_PRIVATE=true 才允许回调内网地址。")
            return 1
        print(f"默认回复已指向网关：{args.hook_base.rstrip('/')}/hook/***（超时 {args.timeout}s，不限只回复一次）")

        ai = (await backend.client.get(f"{base}/ai-reply-settings/{args.account}", headers=headers)).json()
        keywords = (await backend.client.get(f"{base}/keywords-with-item-id/{args.account}", headers=headers)).json()
        if isinstance(ai, dict) and ai.get("ai_enabled"):
            print("注意：该账号开启了 AI 回复，优先级高于默认回复，消息不会到达网关。")
        if isinstance(keywords, list) and keywords:
            print(f"注意：该账号有 {len(keywords)} 条关键词规则，命中的消息不会到达网关。")
        print("注意：商品级默认回复会覆盖账号级设置，请确认商品未单独配置。")
        return 0
    except BackendError as exc:
        print(f"失败: {exc}")
        return 1
    finally:
        await backend.close()


async def cmd_check(args, settings) -> int:
    backend = _backend(settings)
    try:
        accounts = await backend.list_account_ids()
        print(f"refresh token 可用；可见账号：{accounts}")
        for account in accounts:
            print(f"  {account}: 商品 {len(await backend.list_items(account))} 件")
        print(f"MESSAGE_SEND_API_KEY：{'已配置' if settings.message_send_api_key else '未配置（无法发送消息）'}")
        print(f"webhook：{'已配置' if settings.webhook_url else '未配置'}；SHADOW_MODE={settings.shadow_mode}")
        return 0
    except BackendError as exc:
        print(f"失败: {exc}（refresh token 缺失或已过期时请重新执行 login）")
        return 1
    finally:
        await backend.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="xianyu-gateway 初始化工具")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="登录后台并保存 refresh token")
    login.add_argument("--username")
    bind = sub.add_parser("bind-default-reply", help="把账号默认回复指向网关")
    bind.add_argument("--account", required=True, help="闲鱼账号ID")
    bind.add_argument("--hook-base", default="http://xianyu-gateway:8788", help="闲鱼服务访问网关的地址")
    bind.add_argument("--timeout", type=int, default=10, help="回调超时秒数")
    sub.add_parser("check", help="检查令牌与账号可见性")
    args = parser.parse_args(argv)

    settings = load_settings()
    handler = {"login": cmd_login, "bind-default-reply": cmd_bind_default_reply, "check": cmd_check}[args.command]
    return asyncio.run(handler(args, settings))


if __name__ == "__main__":
    sys.exit(main())
