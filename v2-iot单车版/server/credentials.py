#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备密钥加载器 —— 仓库内只有本文件，不含任何真实密钥。

设计目标：车端脚本能在本机正常连云，但公开仓库里搜不到任何可用密钥。
优先级（先命中先用）：
  1. 环境变量 IOTDA_SECRET_001 / IOTDA_SECRET_002   —— 部署/CI/不想落盘时用
  2. 本机 明文凭证/devices.local.json               —— 日常开发，已被 .gitignore 排除
  3. 空串                                           —— 调用方应打印指引并退出

换机器或重装后，把 devices.local.example.json 复制成
明文凭证/devices.local.json 并填入自己的密钥即可，不需要改任何 .py 文件。
"""
from __future__ import annotations

import json
import os

ENV_NAME = {
    "mycar_001": "IOTDA_SECRET_001",
    "mycar_002": "IOTDA_SECRET_002",
}


def load_device_secret(node_id: str):
    """返回 (secret, source)。secret 为空表示本机未配置凭证。"""
    # 1) 环境变量
    env_key = ENV_NAME.get(node_id, "")
    val = os.environ.get(env_key, "").strip()
    if val and not val.startswith("xxxx"):
        return val, f"环境变量 {env_key}"

    # 2) 本机凭证文件（明文凭证/ 整个目录被 .gitignore 排除）
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "明文凭证", "devices.local.json"),
        os.path.join(here, "..", "..", "明文凭证", "devices.local.json"),
        os.path.join(here, "明文凭证", "devices.local.json"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            entry = cfg.get(node_id) or {}
            secret = entry.get("secret", "") if isinstance(entry, dict) else ""
            if secret and not secret.startswith("xxxx"):
                return secret, os.path.normpath(path)
        except (OSError, ValueError, AttributeError):
            continue

    return "", ""


def describe(secret: str, source: str) -> str:
    """给日志用的安全描述：只显示来源，不显示密钥本身。"""
    if not secret:
        return "未配置（请在 明文凭证/devices.local.json 或环境变量里填密钥）"
    return source
