"""
B/S 管理台后端 API（M4，2026-11-08）
=====================================================================
把原来只能用 access_admin.py 命令行维护的「身份 ↔ 车辆绑定关系」搬到 Web：
  账号 CRUD / 令牌轮换 / 授权与回收 / 严格模式 / 设备台账启停 /
  场地坐标标定 / 审计日志 / 任务监控

安全边界（M2 的延续，不是新发明）：
  * 每个 /api/admin/* 请求都要求 role=admin 的有效身份令牌；auth.enabled=false
    时管理台整体停用（否则人人都是 guest，等于没有管理员）。
  * 页面 /admin 本身公开（令牌在页面里输入），但接口一律鉴权。
  * 用户列表默认只回 token 掩码；完整 token 只在「新建 / 轮换」的当次响应里出现一次。
  * 不能删除/停用自己、不能删掉最后一个 admin，避免把自己锁在门外。
  * 所有写操作原子替换 json 文件并写 [ADMIN] 审计，后端按 mtime 热加载立即生效。

本模块用 Flask Blueprint，依赖全部由 app.py 注入，不直接 import app.py。
"""

import json
import os
import re
import secrets
import threading

from flask import Blueprint, jsonify, request

USER_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
ROLES = ("admin", "operator", "viewer")
SCOPES = ("all", "bound")
_write_lock = threading.RLock()


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)                         # 原子替换，后端不会读到半个文件


def _mask(token):
    t = str(token or "")
    return ("*" * 8 + t[-4:]) if len(t) > 4 else "****"


def _user_public(u, token=None):
    out = {
        "user": u.get("user"),
        "name": u.get("name") or u.get("user"),
        "role": u.get("role") or "viewer",
        "scope": u.get("scope") or "all",
        "devices": list(u.get("devices") or []),
        "enabled": bool(u.get("enabled", True)),
        "note": u.get("note") or "",
        "token_mask": _mask(u.get("token")),
    }
    if token:                                     # 只在新建/轮换的成功响应里出现一次
        out["token"] = token
    return out


def create_admin_blueprint(deps):
    """deps: access_cfg / auth_enabled / get_actor / actor_public / log /
             access_path / devices_path / devices_snapshot / sync_devices /
             field_path / field / fleet_cached / missions / commands_log
    """
    bp = Blueprint("admin", __name__, url_prefix="/api/admin")
    log = deps["log"]

    def cfg():
        return deps["access_cfg"]()[0]

    def admin_guard():
        if not deps["auth_enabled"]():
            return jsonify({"ok": False, "stage": "auth",
                            "error": "鉴权未启用（access.json enabled=false），管理台停用；请先在后端开启鉴权"}), 403
        a = deps["get_actor"]()
        if a.get("role") != "admin":
            return jsonify({"ok": False, "stage": "auth",
                            "error": f"需要管理员令牌：当前身份「{a.get('name')}」角色为 {a.get('role')}"}), 403
        return None

    @bp.before_request
    def _guard():
        return admin_guard()

    def audit(action, **kw):
        a = deps["get_actor"]()
        log.info("[ADMIN] action=%s user=%s %s", action, a.get("user"),
                 " ".join(f"{k}={v}" for k, v in kw.items() if v is not None))

    def find_user(c, user):
        return next((u for u in (c.get("users") or []) if u.get("user") == user), None)

    def admin_count(c, exclude=None):
        return sum(1 for u in (c.get("users") or [])
                   if u.get("role") == "admin" and u.get("enabled", True)
                   and u.get("user") != exclude)

    def device_ids():
        return {d["device_id"] for d in deps["devices_snapshot"]()}

    def validate_devices(value):
        if not isinstance(value, list):
            return None, "devices 必须是数组"
        ids = device_ids()
        bad = [d for d in value if d != "*" and d not in ids]
        if bad:
            return None, "绑定了未登记的设备：" + ", ".join(map(str, bad[:3]))
        return [str(d) for d in value], None

    # ------------------------------------------------------------ 概览
    @bp.get("/overview")
    def overview():
        c = cfg()
        devices = deps["devices_snapshot"]()
        fleet = deps["fleet_cached"]() or {}
        online = sum(1 for d in fleet.get("devices", []) if d.get("online"))
        running = sum(1 for m in deps["missions"].list(limit=60)
                      if m["state"] in ("pending", "running", "cancelling"))
        field = deps["field"]()
        return jsonify({
            "ok": True,
            "auth_enabled": bool(c.get("enabled")),
            "require_token": bool(c.get("require_token")),
            "actor": deps["actor_public"](deps["get_actor"]()),
            "counts": {
                "users": len(c.get("users") or []),
                "devices": len(devices),
                "enabled_devices": sum(1 for d in devices if d.get("enabled", True)),
                "online": online,
                "running_missions": running,
            },
            "field": {
                "name": (field.cfg.get("name") if field.cfg else "（未配置）"),
                "width_m": round(field.width * field.meters_per_unit, 2),
                "height_m": round(field.height * field.meters_per_unit, 2),
                "meters_per_unit": field.meters_per_unit,
                "geo_ready": field.geo_enabled,
                "geofence": field.geofence_enabled,
                "statement": ((field.cfg.get("compliance") or {}).get("statement")
                              if field.cfg else ""),
            },
            "audit_recent": read_audit(5),
        })

    def read_audit(limit):
        path = deps["commands_log"]
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-max(1, min(limit, 300)):]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,\d]*)\s+(.*)$", line)
            out.append({"time": m.group(1) if m else "",
                        "message": m.group(2) if m else line})
        return out

    # ------------------------------------------------------------ 账号与绑定
    @bp.get("/users")
    def users():
        c = cfg()
        return jsonify({"ok": True, "users": [_user_public(u) for u in (c.get("users") or [])],
                        "anonymous": _user_public(c.get("anonymous") or {}),
                        "roles": list(ROLES), "scopes": list(SCOPES),
                        "device_options": deps["devices_snapshot"]()})

    @bp.post("/users")
    def create_user():
        body = request.get_json(silent=True) or {}
        user = str(body.get("user") or "").strip()
        if not USER_RE.match(user):
            return jsonify({"ok": False, "error": "账号名须为 2~32 位字母/数字/./-/_"}), 400
        c = cfg()
        if find_user(c, user):
            return jsonify({"ok": False, "error": f"账号已存在：{user}"}), 409
        role = body.get("role") or "operator"
        scope = body.get("scope") or "all"
        if role not in ROLES or scope not in SCOPES:
            return jsonify({"ok": False, "error": f"role 限 {ROLES}，scope 限 {SCOPES}"}), 400
        ds, err = validate_devices(body.get("devices") or [])
        if err:
            return jsonify({"ok": False, "error": err}), 400
        token = secrets.token_urlsafe(18)
        u = {"user": user, "name": str(body.get("name") or user)[:32],
             "token": token, "role": role, "scope": scope,
             "devices": ds, "enabled": True, "note": str(body.get("note") or "")[:80]}
        with _write_lock:
            c = cfg()                             # 重新读，避免并发覆盖别人的改动
            if find_user(c, user):
                return jsonify({"ok": False, "error": f"账号已存在：{user}"}), 409
            c.setdefault("users", []).append(u)
            _save(deps["access_path"], c)
        audit("create_user", target=user, role=role)
        return jsonify({"ok": True, "user": _user_public(u, token),
                        "hint": "完整令牌只显示这一次，请立即复制保存"}), 201

    @bp.patch("/users/<user>")
    def patch_user(user):
        body = request.get_json(silent=True) or {}
        allowed = {"name", "role", "scope", "devices", "enabled", "note"}
        if not allowed.intersection(body):
            return jsonify({"ok": False, "error": "没有可修改的字段"}), 400
        if body.get("role") and body["role"] not in ROLES:
            return jsonify({"ok": False, "error": f"role 限 {ROLES}"}), 400
        if body.get("scope") and body["scope"] not in SCOPES:
            return jsonify({"ok": False, "error": f"scope 限 {SCOPES}"}), 400
        if "devices" in body:
            ds, err = validate_devices(body["devices"])
            if err:
                return jsonify({"ok": False, "error": err}), 400
            body["devices"] = ds
        me = deps["get_actor"]()["user"]
        with _write_lock:
            c = cfg()
            u = find_user(c, user)
            if not u:
                return jsonify({"ok": False, "error": f"找不到账号：{user}"}), 404
            if user == me and (body.get("enabled") is False or
                               (body.get("role") and body["role"] != "admin")):
                return jsonify({"ok": False, "error": "不能停用或降权当前登录的管理员账号"}), 400
            last_admin = admin_count(c, exclude=user) == 0
            if last_admin and (body.get("enabled") is False or
                               (body.get("role") and body["role"] != "admin")):
                return jsonify({"ok": False, "error": "必须保留至少一个启用的 admin"}), 400
            for k in allowed.intersection(body):
                u[k] = str(body[k])[:80] if k in ("name", "note") else body[k]
            _save(deps["access_path"], c)
        audit("patch_user", target=user, fields=",".join(sorted(allowed.intersection(body))))
        return jsonify({"ok": True, "user": _user_public(u)})

    @bp.post("/users/<user>/rotate")
    def rotate_user(user):
        with _write_lock:
            c = cfg()
            u = find_user(c, user)
            if not u:
                return jsonify({"ok": False, "error": f"找不到账号：{user}"}), 404
            u["token"] = secrets.token_urlsafe(18)
            _save(deps["access_path"], c)
        audit("rotate_token", target=user)
        log.info("令牌已轮换 -> user=%s（旧令牌立即失效，不写入日志）", user)
        return jsonify({"ok": True, "user": _user_public(u, u["token"]),
                        "hint": "新令牌只显示这一次；旧令牌立即失效"})

    @bp.delete("/users/<user>")
    def delete_user(user):
        me = deps["get_actor"]()["user"]
        if user == me:
            return jsonify({"ok": False, "error": "不能删除当前登录的账号"}), 400
        with _write_lock:
            c = cfg()
            u = find_user(c, user)
            if not u:
                return jsonify({"ok": False, "error": f"找不到账号：{user}"}), 404
            if u.get("role") == "admin" and admin_count(c, exclude=user) == 0:
                return jsonify({"ok": False, "error": "必须保留至少一个启用的 admin"}), 400
            c["users"] = [x for x in c.get("users", []) if x.get("user") != user]
            _save(deps["access_path"], c)
        audit("delete_user", target=user)
        return jsonify({"ok": True, "deleted": user})

    @bp.post("/strict")
    def strict():
        body = request.get_json(silent=True) or {}
        v = bool(body.get("require_token"))
        with _write_lock:
            c = cfg()
            c["require_token"] = v
            _save(deps["access_path"], c)
        audit("strict", value="on" if v else "off")
        return jsonify({"ok": True, "require_token": v,
                        "hint": "严格模式下所有接口（含 /whoami）都必须带有效令牌；请确保 APP 与控制台已配置好令牌"})

    # ------------------------------------------------------------ 设备与场地
    @bp.get("/devices")
    def devices():
        return jsonify({"ok": True, "devices": deps["devices_snapshot"](),
                        "default": deps["default_device"]()})

    @bp.patch("/devices/<device_id>")
    def patch_device(device_id):
        body = request.get_json(silent=True) or {}
        allowed = {"name", "group", "enabled", "note", "kind"}
        if not allowed.intersection(body):
            return jsonify({"ok": False, "error": "没有可修改的字段"}), 400
        with _write_lock:
            c = deps["devices_raw"]()
            d = next((x for x in c.get("devices", []) if x.get("device_id") == device_id), None)
            if not d:
                return jsonify({"ok": False, "error": f"设备未登记：{device_id}"}), 404
            for k in allowed.intersection(body):
                d[k] = bool(body[k]) if k == "enabled" else str(body[k])[:80]
            _save(deps["devices_path"], c)
            deps["sync_devices"]()
        audit("patch_device", target=device_id,
              fields=",".join(sorted(allowed.intersection(body))))
        return jsonify({"ok": True, "device": d})

    @bp.get("/field")
    def field_get():
        f = deps["field"]()
        return jsonify({"ok": True, "field": f.public(), "raw": _clean_raw(f.cfg)})

    def _clean_raw(cfg):
        out = json.loads(json.dumps(cfg, ensure_ascii=False))
        return out

    @bp.patch("/field")
    def field_patch():
        body = request.get_json(silent=True) or {}
        with _write_lock:
            cfg = _read_json(deps["field_path"])
            sc = cfg.setdefault("scale", {})
            if "meters_per_unit" in body:
                v = float(body["meters_per_unit"])
                if not 0.001 <= v <= 10:
                    return jsonify({"ok": False, "error": "meters_per_unit 须在 0.001~10 之间"}), 400
                sc["meters_per_unit"] = v
            gf = cfg.setdefault("geofence", {})
            if "geofence_enabled" in body:
                gf["enabled"] = bool(body["geofence_enabled"])
            if "geofence_margin" in body:
                v = float(body["geofence_margin"])
                if not 0 <= v <= 200:
                    return jsonify({"ok": False, "error": "geofence_margin 须在 0~200 之间"}), 400
                gf["margin"] = v
            geo = cfg.setdefault("geo", {})
            for k in ("enabled", "calibrated", "origin_lng", "origin_lat", "rotation_deg"):
                if k in body:
                    geo[k] = bool(body[k]) if k in ("enabled", "calibrated") else float(body[k])
            if geo.get("enabled") and (not geo.get("calibrated") or not geo.get("origin_lat")):
                return jsonify({"ok": False,
                                "error": "启用地理坐标前必须先用合规服务商现场采集 GCJ-02 基准点，并把 calibrated 置 true；未校准时后端不输出经纬度"}), 400
            _save(deps["field_path"], cfg)
        audit("patch_field", fields=",".join(sorted(body)))
        return jsonify({"ok": True, "field": deps["field"]().public(),
                        "hint": "场地配置已热加载，下一次 /field 与 /fleet_status 立即生效"})

    def _read_json(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------ 审计 / 任务
    @bp.get("/audit")
    def audit_log():
        limit = request.args.get("limit", "60")
        try:
            n = max(1, min(int(limit), 300))
        except ValueError:
            n = 60
        return jsonify({"ok": True, "lines": read_audit(n)})

    @bp.get("/missions")
    def missions():
        return jsonify({"ok": True, "missions": deps["missions"].list(limit=20),
                        "presets": deps["missions"].presets()})

    @bp.post("/missions/<mid>/cancel")
    def cancel_mission(mid):
        r, err = deps["missions"].cancel(mid, deps["get_actor"]()["user"], is_admin=True,
                                         reason="管理台取消")
        if err:
            return jsonify({"ok": False, "error": err}), 400
        audit("cancel_mission", target=mid)
        return jsonify({"ok": True, "mission": r})

    @bp.post("/abort_all")
    def abort_all():
        done = deps["missions"].abort_all(deps["get_actor"]()["user"])
        audit("abort_all", count=len(done))
        return jsonify({"ok": True, "cancelled": done, "count": len(done)})

    return bp
