"""Feishu OpenAPI client for the Codex creation assistant.

Runtime must not depend on lark-cli profiles. This module reads only the
project .env and talks to Feishu directly.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://open.feishu.cn"

_TOKEN: str | None = None
_TOKEN_EXPIRES_AT = 0.0


def env(key: str, default: str | None = None) -> str | None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].split("#")[0].strip()
                return value or default
    return default


def require_env(key: str) -> str:
    value = env(key)
    if not value:
        raise RuntimeError(f".env 缺少 {key}")
    return value


def app_id() -> str:
    return require_env("FEISHU_APP_ID")


def app_secret() -> str:
    return require_env("FEISHU_APP_SECRET")


def default_chat_id() -> str:
    return require_env("FEISHU_CHAT_ID")


def tenant_access_token() -> str:
    global _TOKEN, _TOKEN_EXPIRES_AT
    now = time.time()
    if _TOKEN and now < _TOKEN_EXPIRES_AT:
        return _TOKEN

    resp = requests.post(
        f"{BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id(), "app_secret": app_secret()},
        timeout=20,
    )
    data = _json_response(resp)
    code = data.get("code")
    if code != 0:
        raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data}")
    _TOKEN = data["tenant_access_token"]
    _TOKEN_EXPIRES_AT = now + max(60, int(data.get("expire", 7200)) - 300)
    return _TOKEN


def api(method: str, path: str, params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.request(
        method,
        f"{BASE_URL}{path}",
        params=params,
        json=data,
        headers={"Authorization": f"Bearer {tenant_access_token()}"},
        timeout=30,
    )
    out = _json_response(resp)
    if out.get("code", 0) != 0:
        raise RuntimeError(f"飞书 OpenAPI 失败 {method} {path}: {out}")
    return out


def _json_response(resp: requests.Response) -> dict[str, Any]:
    text = resp.text
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"飞书返回非 JSON: http={resp.status_code} body={text[:300]}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"飞书 HTTP 失败: http={resp.status_code} body={data}")
    return data
