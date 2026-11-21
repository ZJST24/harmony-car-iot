"""
绑定关系维护工具（M2）
=====================================================================
access.json 的命令行管理端 —— 将来 B/S 管理台的「用户↔车绑定」页面就是把它包一层 UI。

后端按文件 mtime 热加载，**改完立刻生效，不用重启 app.py**（下一请求即生效）。

用法（在 server 目录下）：
  D:\\miniconda\\python.exe access_admin.py list
  D:\\miniconda\\python.exe access_admin.py grant group1 <device_id>   # 授权控制某台车
  D:\\miniconda\\python.exe access_admin.py grant teacher '*'          # 授权全部
  D:\\miniconda\\python.exe access_admin.py revoke group1 <device_id>
  D:\\miniconda\\python.exe access_admin.py rotate group1               # 轮换 token
  D:\\miniconda\\python.exe access_admin.py enable group1 / disable group1
  D:\\miniconda\\python.exe access_admin.py scope group1 bound          # bound=只看自己绑定的车
  D:\\miniconda\\python.exe access_admin.py strict on / off              # 严格模式：必须带 token

课堂演示建议：先用 list 看当前绑定 → 用第二组 token 登录控制台 →
  grant group2 002 后立刻能控 2 号车，revoke 后立刻被 403 拦下。
"""

import argparse
import json
import os
import secrets
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE_DIR, "access.json")


def load():
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)


def save(cfg):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, PATH)                     # 原子替换，避免后端读到半个文件


def find(cfg, user):
    for u in cfg.get("users") or []:
        if u.get("user") == user:
            return u
    raise SystemExit(f"找不到账号：{user}（用 list 查看现有账号）")


def show(cfg):
    print(f"鉴权 enabled={cfg.get('enabled')}  require_token={cfg.get('require_token', False)}")
    anon = cfg.get("anonymous") or {}
    print(f"匿名访客  user={anon.get('user')}  role={anon.get('role')}  "
          f"scope={anon.get('scope')}  devices={anon.get('devices')}")
    print("-" * 88)
    print(f"{'账号':<12}{'名称':<10}{'角色':<10}{'状态':<8}{'可见':<8}{'可控制设备':<28}token")
    for u in cfg.get("users") or []:
        ds = u.get("devices") or []
        print(f"{u.get('user', ''):<12}{u.get('name', ''):<10}{u.get('role', ''):<10}"
              f"{('启用' if u.get('enabled', True) else '停用'):<8}"
              f"{u.get('scope', 'all'):<8}{','.join(d[-9:] if len(d) > 9 else d for d in ds) or '(无)':<28}"
              f"{u.get('token', '')}")
    print("-" * 88)
    print("device_id 只显示末 9 位；完整 ID 见 devices.json。token 请勿发给学生或提交仓库。")


def main():
    p = argparse.ArgumentParser(description="M2 身份与绑定台账维护")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出账号与绑定关系")
    for name in ("grant", "revoke", "rotate", "enable", "disable"):
        q = sub.add_parser(name)
        q.add_argument("user")
        if name in ("grant", "revoke"):
            q.add_argument("device", help="完整 device_id，或 * 表示全部车辆")
    q = sub.add_parser("scope")
    q.add_argument("user")
    q.add_argument("value", choices=["all", "bound"])
    q = sub.add_parser("strict")
    q.add_argument("value", choices=["on", "off"])

    a = p.parse_args()
    cfg = load()

    if a.cmd == "list":
        show(cfg)
        return

    if a.cmd == "strict":
        cfg["require_token"] = (a.value == "on")
        save(cfg)
        print(f"require_token = {cfg['require_token']}（后端热加载，立即生效）")
        return

    u = find(cfg, a.user)
    if a.cmd in ("grant", "revoke"):
        ds = list(u.get("devices") or [])
        if a.cmd == "grant":
            if a.device not in ds:
                ds.append(a.device)
            print(f"已授权 {a.user} 控制 {a.device}")
        else:
            ds = [d for d in ds if d != a.device and d != "*"]
            print(f"已解除 {a.user} 对 {a.device} 的控制权")
        u["devices"] = ds
    elif a.cmd == "rotate":
        u["token"] = secrets.token_urlsafe(18)
        print(f"已轮换 {a.user} 的 token：{u['token']}")
        print("请把新 token 告知使用者；旧 token 立即失效。")
    elif a.cmd in ("enable", "disable"):
        u["enabled"] = (a.cmd == "enable")
        print(f"账号 {a.user} 已{'启用' if u['enabled'] else '停用'}")
    elif a.cmd == "scope":
        u["scope"] = a.value
        print(f"账号 {a.user} 可见范围 = {a.value}")
    save(cfg)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as e:
        print(f"读写 access.json 失败：{e}", file=sys.stderr)
        raise SystemExit(1)
