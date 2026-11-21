"""
任务级指令编排（M3，2026-11-08）
=====================================================================
把「一次点一个方向键」升级成「一个可编排、可观测、可急停的任务」：
  预设任务 / 自定义步骤 → 逐步白名单校验 → 逐步下发并等回执 → 记录轨迹 → 收尾 STOP

安全设计（教学上比功能本身更重要）：
  1. **单任务单设备**：同一台车同时只允许一个 running 任务，避免两个任务抢方向盘。
  2. **逐步复检授权**：access.json 是热加载的，任务跑一半被 revoke 也要立刻停；
     每一步下发前重新校验 (身份, device_id)，不沿用创建时的一次性结论。
  3. **AI 不进这条链**：任务步骤只认白名单里的规范指令，绝不解析自然语言。
  4. **急停优先**：取消 / 一键全停会立即异步下发 STOP，且不受 geofence 限制；
     物理急停与直连入口依旧保留，不依赖本模块。
  5. **边界约束**：启动前车辆必须在 geofence 内；步骤数、单步时长、总时长都设上限。

本模块不 import Flask / IoTDA，全部依赖由 app.py 注入，方便单独跑单测。
"""

import json
import os
import threading
import time
import uuid

MAX_STEPS = 20
MIN_STEP_MS = 100
MAX_STEP_MS = 5000
MAX_TOTAL_MS = 60000
HISTORY_LIMIT = 60                       # 内存里保留最近 N 个任务

PRESETS = {
    "square": {
        "name": "矩形巡检",
        "desc": "前进 → 右转 ×4 走一个方框，适合演示连续编排与轨迹回放",
        "steps": [
            {"cmd": "FORWARD", "speed": 50, "duration_ms": 1200, "note": "第一条边"},
            {"cmd": "RIGHT", "duration_ms": 700, "note": "转 90°"},
            {"cmd": "FORWARD", "speed": 50, "duration_ms": 1200, "note": "第二条边"},
            {"cmd": "RIGHT", "duration_ms": 700},
            {"cmd": "FORWARD", "speed": 50, "duration_ms": 1200, "note": "第三条边"},
            {"cmd": "RIGHT", "duration_ms": 700},
            {"cmd": "FORWARD", "speed": 50, "duration_ms": 1200, "note": "第四条边"},
            {"cmd": "STOP", "duration_ms": 300, "note": "收尾停车"}
        ]
    },
    "out_back": {
        "name": "直线往返",
        "desc": "前进 → 后退 → 停车，最短的可观测任务，适合首次验证链路",
        "steps": [
            {"cmd": "FORWARD", "speed": 40, "duration_ms": 1800, "note": "前进"},
            {"cmd": "BACK", "speed": 40, "duration_ms": 1800, "note": "原路退回"},
            {"cmd": "STOP", "duration_ms": 300}
        ]
    },
    "patrol": {
        "name": "通道巡检",
        "desc": "沿主通道前进两次、中途转向，配合场地图上的巡检点讲解",
        "steps": [
            {"cmd": "FORWARD", "speed": 45, "duration_ms": 2000, "note": "驶向 P1/P2"},
            {"cmd": "STOP", "duration_ms": 500, "note": "采样停留"},
            {"cmd": "LEFT", "duration_ms": 600},
            {"cmd": "FORWARD", "speed": 40, "duration_ms": 1500},
            {"cmd": "STOP", "duration_ms": 300}
        ]
    },
    "selfcheck": {
        "name": "原地自检",
        "desc": "左转 → 右转 → 停车，不产生位移，设备离线演示时最安全",
        "steps": [
            {"cmd": "LEFT", "duration_ms": 800},
            {"cmd": "RIGHT", "duration_ms": 800},
            {"cmd": "STOP", "duration_ms": 300}
        ]
    }
}


class MissionManager:
    """deps 需要提供的回调：
        build_paras(cmd, step)      -> (paras, err)
        dispatch(cmd, paras, did, actor) -> (result|None, err|None)
        authorize(actor, did)       -> (ok, err)   逐步复检授权
        state(did)                  -> dict|None   取影子坐标（可返回 None）
        field                       -> FieldMap    场地映射与 geofence
        log                         -> logging.Logger
    """

    def __init__(self, deps, log_path=None):
        self.deps = deps
        self.log = deps["log"]
        self.log_path = log_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "missions.log")
        self._lock = threading.RLock()
        self._missions = {}
        self._events = {}                      # mission_id -> threading.Event
        self._order = []
        self._io_lock = threading.Lock()

    # ------------------------------------------------------------ 校验
    @staticmethod
    def presets():
        out = []
        for key, p in PRESETS.items():
            out.append({
                "key": key, "name": p["name"], "desc": p["desc"],
                "steps": p["steps"],
                "est_ms": sum(s.get("duration_ms", 0) for s in p["steps"]),
            })
        return out

    def normalize_steps(self, steps):
        """返回 (规范步骤, 错误)。拒绝任何非白名单字段、越界速度与时长。"""
        if not isinstance(steps, list) or not steps:
            return None, "steps 必须是非空数组"
        if len(steps) > MAX_STEPS:
            return None, f"步骤过多：最多 {MAX_STEPS} 步（收到 {len(steps)}）"
        out, total = [], 0
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                return None, f"第 {i + 1} 步必须是对象"
            cmd = str(raw.get("cmd") or "").upper()
            if cmd not in ("FORWARD", "BACK", "LEFT", "RIGHT", "STOP"):
                return None, f"第 {i + 1} 步命令不合法：{cmd or '(空)'}（只认白名单五指令）"
            dur = raw.get("duration_ms", 500)
            if not isinstance(dur, int) or isinstance(dur, bool) or not MIN_STEP_MS <= dur <= MAX_STEP_MS:
                return None, f"第 {i + 1} 步 duration_ms 必须是 {MIN_STEP_MS}~{MAX_STEP_MS} 的整数"
            total += dur
            step = {"cmd": cmd, "duration_ms": dur}
            if cmd in ("FORWARD", "BACK"):
                speed = raw.get("speed")
                if not isinstance(speed, int) or isinstance(speed, bool) or not 0 <= speed <= 100:
                    return None, f"第 {i + 1} 步 speed 必须是 0~100 的整数"
                step["speed"] = speed
            if raw.get("note"):
                step["note"] = str(raw["note"])[:60]
            paras, err = self.deps["build_paras"](cmd, step)
            if err:
                return None, f"第 {i + 1} 步参数校验失败：{err}"
            out.append(step)
        if total > MAX_TOTAL_MS:
            return None, f"任务总时长 {total} ms 超过上限 {MAX_TOTAL_MS} ms"
        return out, None

    def _running_on(self, did):
        with self._lock:
            for m in self._missions.values():
                if m["device_id"] == did and m["state"] in ("pending", "running"):
                    return m
        return None

    def _append_event(self, ev):
        """任务事件追加写盘（JSONL），供 /admin 审计与课堂回放。"""
        line = json.dumps(ev, ensure_ascii=False)
        with self._io_lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                self.log.warning("写 missions.log 失败：%s", e)

    # ------------------------------------------------------------ 创建 / 查询
    def create(self, name, device_id, actor, steps, source="custom", preset=None):
        norm, err = self.normalize_steps(steps)
        if err:
            return None, err
        ok, err = self.deps["authorize"](actor, device_id)
        if not ok:
            return None, err
        busy = self._running_on(device_id)
        if busy:
            return None, (f"该车辆已有进行中的任务 {busy['id']}（{busy['name']}）；"
                          "请先取消或等待结束，避免两个任务抢方向盘")

        field = self.deps["field"]()
        st = self.deps["state"](device_id) or {}
        pos = field.position(st.get("x"), st.get("y"))
        if st.get("x") is not None and not pos["inside"]:
            return None, ("车辆当前在安全边界外或贴边过近，拒绝启动任务；"
                          "先用 /command 把它开回场内，或到管理台调整 field.json")

        mid = "M" + uuid.uuid4().hex[:10]
        now = time.time()
        m = {
            "id": mid,
            "name": (name or preset or "自定义任务")[:40],
            "preset": preset,
            "source": source,
            "device_id": device_id,
            "actor": actor,
            "state": "pending",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "total_steps": len(norm),
            "index": 0,
            "est_ms": sum(s["duration_ms"] for s in norm),
            "steps": norm,
            "results": [],
            "trail": [{"at": now, "x": st.get("x"), "y": st.get("y"),
                       "pos": pos, "label": "起点"}],
            "error": None,
            "cancel_reason": None,
        }
        with self._lock:
            self._missions[mid] = m
            self._events[mid] = threading.Event()
            self._order.append(mid)
            while len(self._order) > HISTORY_LIMIT:
                old = self._order.pop(0)
                self._missions.pop(old, None)
                self._events.pop(old, None)
        self._append_event({"event": "created", "id": mid, "name": m["name"],
                            "device_id": device_id, "actor": actor,
                            "preset": preset, "steps": len(norm), "at": now})
        self.log.info("任务创建 -> id=%s user=%s [%s] %s %d 步",
                      mid, actor, device_id, m["name"], len(norm))
        t = threading.Thread(target=self._run, args=(mid,), daemon=True,
                             name="mission-" + mid)
        t.start()
        return self.public(mid), None

    def public(self, mid):
        with self._lock:
            m = self._missions.get(mid)
            if not m:
                return None
            return {k: v for k, v in m.items() if k != "steps"} | {
                "steps": [dict(s) for s in m["steps"]],
                "progress": (round(m["index"] / m["total_steps"] * 100)
                             if m["total_steps"] else 0),
            }

    def list(self, limit=20):
        with self._lock:
            ids = list(reversed(self._order))[:max(1, min(limit, HISTORY_LIMIT))]
            return [self.public(i) for i in ids]

    def get(self, mid):
        return self.public(mid)

    # ------------------------------------------------------------ 执行
    def _set_state(self, m, state, error=None):
        with self._lock:
            m["state"] = state
            m["error"] = error
            m["finished_at"] = time.time()
        self._append_event({"event": state, "id": m["id"], "error": error,
                            "at": m["finished_at"], "index": m["index"]})

    def _snapshot(self, m, label):
        """记录一个轨迹点：影子位置 + 场地映射结果。取不到就跳过，不阻断任务。"""
        try:
            st = self.deps["state"](m["device_id"]) or {}
            if st.get("x") is None:
                return
            field = self.deps["field"]()
            pt = {"at": time.time(), "x": st.get("x"), "y": st.get("y"),
                  "pos": field.position(st.get("x"), st.get("y")), "label": label}
            with self._lock:
                m["trail"].append(pt)
                if len(m["trail"]) > 80:
                    m["trail"] = m["trail"][-80:]
        except Exception as e:                       # noqa: BLE001 - 轨迹只是观测数据
            self.log.warning("任务轨迹采样失败 [%s]: %s", m["id"], e)

    def _run(self, mid):
        with self._lock:
            m = self._missions.get(mid)
            ev = self._events.get(mid)
        if not m or not ev:
            return
        with self._lock:
            m["state"] = "running"
            m["started_at"] = time.time()
        self._append_event({"event": "running", "id": mid, "at": m["started_at"]})
        actor, did = m["actor"], m["device_id"]
        final = None

        for i, step in enumerate(m["steps"]):
            if ev.is_set():
                final = ("cancelled", m.get("cancel_reason") or "已取消")
                break
            # 逐步复检：任务创建后被 revoke / 停用账号，这里必须立刻停下来
            ok, why = self.deps["authorize"](actor, did)
            if not ok:
                final = ("failed", "步骤 " + str(i + 1) + " 前授权已失效：" + why)
                break
            paras, err = self.deps["build_paras"](step["cmd"], step)
            if err:
                final = ("failed", "步骤 " + str(i + 1) + " 白名单校验失败：" + err)
                break
            t0 = time.time()
            try:
                result, err = self.deps["dispatch"](step["cmd"], paras, did, actor)
            except Exception as e:                   # noqa: BLE001
                result, err = None, str(e)
            executed = bool(result and result.get("executed"))
            rec = {
                "index": i + 1, "cmd": step["cmd"], "paras": paras,
                "command_id": (result or {}).get("command_id"),
                "executed": executed,
                "error": err or (None if executed else "命令已送达，但车端未回执（executed=false）"),
                "at": time.time(), "elapsed_ms": int((time.time() - t0) * 1000),
            }
            with self._lock:
                m["results"].append(rec)
                m["index"] = i + 1
            self._append_event({"event": "step", "id": mid, **rec})
            self.log.info("任务步骤 -> id=%s %d/%d user=%s [%s] %s %s executed=%s",
                          mid, i + 1, m["total_steps"], actor, did,
                          step["cmd"], paras, executed)
            if err or not executed:
                final = ("failed", "步骤 " + str(i + 1) + " 下发失败：" + str(rec["error"]))
                break
            self._snapshot(m, step["cmd"])
            if ev.wait(step["duration_ms"] / 1000.0):
                final = ("cancelled", m.get("cancel_reason") or "已取消")
                break
        else:
            final = ("succeeded", None)

        # 收尾停车：成功 / 失败都发 STOP；取消由 cancel() 立即发过一次，这里不再重复
        if final[0] != "cancelled":
            try:
                self.deps["dispatch"]("STOP", {}, did, actor)
            except Exception as e:                   # noqa: BLE001
                self.log.warning("任务收尾 STOP 失败 [%s]: %s", mid, e)
        self._snapshot(m, "终点")
        self._set_state(m, *final)
        self.log.info("任务结束 -> id=%s state=%s error=%s", mid, final[0], final[1])
        with self._lock:
            self._events.pop(mid, None)

    # ------------------------------------------------------------ 取消 / 急停
    def _emergency_stop(self, did, actor, mid):
        """异步下发 STOP：不让取消请求被 IoTDA 的 20 秒同步超时拖住。"""
        def work():
            try:
                self.deps["dispatch"]("STOP", {}, did, actor)
                self.log.info("急停已下发 -> id=%s user=%s [%s]", mid, actor, did)
            except Exception as e:                   # noqa: BLE001
                self.log.warning("急停下发失败 [%s]: %s", mid, e)
        threading.Thread(target=work, daemon=True, name="estop-" + mid).start()

    def cancel(self, mid, actor, is_admin=False, reason="人工取消"):
        with self._lock:
            m = self._missions.get(mid)
            ev = self._events.get(mid)
        if not m:
            return None, f"找不到任务 {mid}"
        if m["state"] not in ("pending", "running"):
            return None, f"任务已结束（{m['state']}），无需取消"
        if not is_admin and m["actor"] != actor:
            return None, "只能取消自己创建的任务（管理员可取消全部）"
        with self._lock:
            m["cancel_reason"] = reason
            m["state"] = "cancelling"
        if ev:
            ev.set()
        self._emergency_stop(m["device_id"], actor, mid)
        self._append_event({"event": "cancel_requested", "id": mid, "by": actor,
                            "reason": reason, "at": time.time()})
        return self.public(mid), None

    def abort_all(self, actor):
        with self._lock:
            running = [m["id"] for m in self._missions.values()
                       if m["state"] in ("pending", "running")]
        done = []
        for mid in running:
            r, err = self.cancel(mid, actor, is_admin=True, reason="一键全停")
            if not err:
                done.append(mid)
        self.log.warning("一键全停 -> user=%s 取消 %d 个任务", actor, len(done))
        return done
