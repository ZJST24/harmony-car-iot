"""
M3 + M4 端到端自测（Flask test_client，不碰真实小车）
=====================================================================
覆盖：
  M3  /field 坐标映射、米制/GCJ-02 合规输出、geofence 校验
      /mission/presets、步骤白名单、任务状态机、逐步授权复检、取消急停
  M4  管理员鉴权、账号/绑定 CRUD、令牌轮换、严格模式、设备/场地热加载、审计

安全：会临时改写 access.json / devices.json / field.json，结束时用备份原样恢复，
      后端按 mtime 热加载，恢复后下一请求即回到原状态。任务下发用假 dispatch，
      不向 IoTDA 发任何真实命令。

运行：D:\\miniconda\\python.exe test_m3m4.py
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import app as backend                                   # noqa: E402
import field_map                                        # noqa: E402

FILES = ["access.json", "devices.json", "field.json"]
BACKUPS = {}
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  -> " + str(detail)) if detail and not cond else ""))


def backup():
    for f in FILES:
        p = os.path.join(BASE, f)
        with open(p, "rb") as fh:
            BACKUPS[f] = fh.read()


def restore():
    for f, data in BACKUPS.items():
        with open(os.path.join(BASE, f), "wb") as fh:
            fh.write(data)
    backend.sync_devices(force=True)


def body(resp):
    return resp.get_json() or {}


def main():
    backup()
    with open(os.path.join(BASE, "access.json"), encoding="utf-8") as f:
        teacher = next(u["token"] for u in json.load(f)["users"] if u["user"] == "teacher")
    with open(os.path.join(BASE, "access.json"), encoding="utf-8") as f:
        observer = next(u["token"] for u in json.load(f)["users"] if u["user"] == "observer")

    c = backend.app.test_client()
    H = {"X-Auth-Token": teacher}
    O = {"X-Auth-Token": observer}

    # ---------------------------------------------------------- M3 /field
    print("\n[M3-A] 场地坐标映射与合规底图")
    r = c.get("/field", headers=H)
    j = body(r)
    check("GET /field 200 且返回场地配置", r.status_code == 200 and j.get("ok") and
          j["field"]["name"].startswith("教学场地"), j.get("error"))
    check("未校准时抹掉示例经纬度", j["field"]["geo"]["origin_lng"] is None and
          j["field"]["geo"]["enabled"] is False)
    m = field_map.get()
    p = m.position(400, 300)
    check("逻辑坐标 → 米制 (10.0, -7.5)", p["mx"] == 10.0 and p["my"] == -7.5, p)
    check("未校准不输出 GCJ-02", p["geo"] is None)
    check("geofence 内缩判定：贴边 (5,5) 越界、中心在界内",
          m.inside(5, 5) is False and m.inside(400, 300) is True)
    check("禁行区识别", p["restricted"] is True and "禁行区" in (p["zone"] or ""))
    check("观察员看不到地理基准点", c.get("/field", headers=O).get_json()["field"]["geo"]["origin_lng"] is None)

    # ---------------------------------------------------------- M3 任务编排
    print("\n[M3-B] 任务级指令编排")
    presets = body(c.get("/mission/presets", headers=H))
    check("GET /mission/presets 返回 4 个预设", len(presets.get("presets", [])) == 4,
          presets.get("presets"))
    bad = c.post("/mission", headers=H, json={"steps": [{"cmd": "FLY", "duration_ms": 500}]})
    check("非白名单命令被拒（400 stage=validate）",
          bad.status_code == 400 and body(bad).get("stage") == "validate", body(bad))
    too_long = c.post("/mission", headers=H,
                      json={"steps": [{"cmd": "STOP", "duration_ms": 6000}]})
    check("超时长步骤被拒", too_long.status_code == 400, body(too_long))

    # 用假 dispatch / 假影子，避免向云端下发真实命令
    calls = []
    def fake_dispatch(cmd, paras, did, actor):
        calls.append((cmd, paras, actor))
        return {"command_id": "fake-" + str(len(calls)), "executed": True}, None
    backend.missions.deps["dispatch"] = fake_dispatch
    backend.missions.deps["state"] = lambda did: {"x": 200.0, "y": 300.0, "speed": 0}
    backend.missions.deps["authorize"] = lambda actor, did: (True, None)

    r = c.post("/mission", headers=H, json={
        "name": "自测任务", "steps": [
            {"cmd": "FORWARD", "speed": 30, "duration_ms": 100},
            {"cmd": "STOP", "duration_ms": 100}
        ]})
    check("POST /mission 创建任务（202）", r.status_code == 202 and body(r)["ok"], body(r))
    mid = body(r)["mission"]["id"]
    for _ in range(40):
        st = backend.missions.get(mid)
        if st["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.1)
    check("任务正常跑完 → succeeded", st["state"] == "succeeded", st.get("error"))
    check("逐步回执齐全（2 步 executed=true）",
          len(st["results"]) == 2 and all(x["executed"] for x in st["results"]), st["results"])
    check("收尾自动下发 STOP", calls[-1][0] == "STOP", calls)
    check("任务轨迹含场地映射", len(st["trail"]) >= 2 and
          st["trail"][-1]["pos"]["mx"] == 5.0, st["trail"][-1] if st["trail"] else None)
    # 逐步授权复检：第 2 步前权限被回收
    n = {"i": 0}
    def revoke_midway(actor, did):
        n["i"] += 1
        return (False, "绑定关系已变更") if n["i"] >= 2 else (True, None)
    backend.missions.deps["authorize"] = revoke_midway
    r2 = c.post("/mission", headers=H, json={"name": "中途撤权", "steps": [
        {"cmd": "LEFT", "duration_ms": 100}, {"cmd": "RIGHT", "duration_ms": 100}]})
    mid2 = body(r2)["mission"]["id"]
    for _ in range(40):
        st2 = backend.missions.get(mid2)
        if st2["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.1)
    check("运行中 revoke → 任务立刻 failed", st2["state"] == "failed" and
          "授权已失效" in (st2["error"] or ""), st2.get("error"))

    # geofence 越界拒绝启动
    backend.missions.deps["authorize"] = lambda actor, did: (True, None)
    backend.missions.deps["state"] = lambda did: {"x": 5.0, "y": 5.0}
    r3 = c.post("/mission", headers=H, json={"preset": "selfcheck"})
    check("车辆越界时拒绝启动任务（400）", r3.status_code == 400 and
          "安全边界" in (body(r3).get("error") or ""), body(r3))
    backend.missions.deps["state"] = lambda did: {"x": 200.0, "y": 300.0}

    # 取消 / 急停
    calls.clear()
    r4 = c.post("/mission", headers=H, json={"steps": [
        {"cmd": "FORWARD", "speed": 20, "duration_ms": 2000},
        {"cmd": "STOP", "duration_ms": 2000}]})
    mid4 = body(r4)["mission"]["id"]
    rdup = c.post("/mission", headers=H, json={"preset": "selfcheck"})
    check("同一台车并发任务被拒（400）",
          rdup.status_code == 400 and "进行中的任务" in (body(rdup).get("error") or ""), body(rdup))
    rc = c.post(f"/mission/{mid4}/cancel", headers=H)
    check("POST /mission/<id>/cancel 200", rc.status_code == 200 and body(rc)["ok"], body(rc))
    for _ in range(40):
        if backend.missions.get(mid4)["state"] == "cancelled" and calls and calls[-1][0] == "STOP":
            break
        time.sleep(0.1)
    check("取消后状态 cancelled 且异步下发过 STOP",
          backend.missions.get(mid4)["state"] == "cancelled" and
          any(x[0] == "STOP" for x in calls), calls[-3:])
    check("GET /missions 列表可读", c.get("/missions", headers=H).status_code == 200)
    check("一键全停需要管理员（观察员 403）",
          c.post("/mission/abort_all", headers=O).status_code == 403)

    # ---------------------------------------------------------- M4 管理台
    print("\n[M4] B/S 管理台")
    check("页面 /admin 公开可打开", c.get("/admin").status_code == 200)
    check("页面 /map 公开可打开", c.get("/map").status_code == 200)
    check("静态资源 /field.js 可加载", c.get("/field.js").status_code == 200)
    check("无令牌访问管理 API → 403", c.get("/api/admin/overview").status_code == 403)
    check("观察员（viewer）→ 403", c.get("/api/admin/overview", headers=O).status_code == 403)
    ov = body(c.get("/api/admin/overview", headers=H))
    check("管理员 overview 200 且含统计", ov.get("ok") and "counts" in ov, ov)
    us = body(c.get("/api/admin/users", headers=H))
    check("用户列表只回 token 掩码", us.get("ok") and
          all("*" in u["token_mask"] and "token" not in u for u in us["users"]), us.get("users"))

    r = c.post("/api/admin/users", headers=H,
               json={"user": "m4_test", "name": "自测账号", "role": "operator",
                     "scope": "bound", "devices": []})
    check("新建账号 201 且只回一次完整令牌", r.status_code == 201 and
          len(body(r)["user"]["token"]) > 10, body(r))
    new_token = body(r)["user"]["token"]
    did = us["device_options"][0]["device_id"]
    r = c.patch("/api/admin/users/m4_test", headers=H,
                json={"devices": [did], "enabled": True})
    check("PATCH 绑定某台车成功", r.status_code == 200 and did in body(r)["user"]["devices"], body(r))
    r = c.post("/api/admin/users/m4_test/rotate", headers=H)
    check("轮换令牌后旧令牌立即失效", r.status_code == 200 and
          body(r)["user"]["token"] != new_token and
          c.get("/whoami", headers={"X-Auth-Token": new_token}).status_code == 401)
    check("不能删除当前登录的 admin",
          c.delete("/api/admin/users/teacher", headers=H).status_code == 400)
    check("删除测试账号成功", c.delete("/api/admin/users/m4_test", headers=H).status_code == 200)

    # 设备台账热加载
    r = c.patch(f"/api/admin/devices/{did}", headers=H, json={"name": "自测改名", "enabled": True})
    check("PATCH 设备台账 200", r.status_code == 200, body(r))
    check("后端内存台账同步改名",
          backend.DEVICE_MAP[did].get("name") == "自测改名", backend.DEVICE_MAP.get(did))

    # 场地标定
    r = c.patch("/api/admin/field", headers=H, json={"meters_per_unit": 0.05})
    check("PATCH 场地比例 200", r.status_code == 200, body(r))
    time.sleep(0.05)                       # 等文件系统 mtime 变化，热加载才会触发
    check("场地热加载：1 单位 = 0.05 m", field_map.get().position(100, 0)["mx"] == 5.0)
    r = c.patch("/api/admin/field", headers=H,
                json={"enabled": True, "calibrated": False, "origin_lng": 116.0, "origin_lat": 39.0})
    check("未校准就想启用地理坐标 → 400", r.status_code == 400, body(r))
    check("审计日志可读", c.get("/api/admin/audit?limit=10", headers=H).get_json()["ok"])
    check("管理台任务列表可读", c.get("/api/admin/missions", headers=H).get_json()["ok"])

    # 严格模式：改一次、验证 401、再关掉
    c.post("/api/admin/strict", headers=H, json={"require_token": True})
    time.sleep(0.05)
    check("严格模式：不带令牌 /whoami → 401", c.get("/whoami").status_code == 401)
    check("严格模式：带管理员令牌仍可用", c.get("/whoami", headers=H).status_code == 200)
    c.post("/api/admin/strict", headers=H, json={"require_token": False})
    time.sleep(0.05)

    # ---------------------------------------------------------- 结果
    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        restore()
        print("已恢复 access.json / devices.json / field.json 原内容。")
    raise SystemExit(code)
