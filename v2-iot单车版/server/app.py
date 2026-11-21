"""
iot-stage - 业务后端（轻量中转服务）
=====================================
架构角色：APP（控制端）→ 业务后端（本服务）→ 华为云 IoTDA → 小车端

职责（对应"路线B"的三层管控思想）：
  1. 凭证隔离：AK/SK 与 AI 服务密钥只存在本机，客户端零持有
  2. 管控点：命令白名单 + 设备台账校验 + 审计日志（commands.log）
  3. 协议简化：把平台 API 包成极简接口给 APP

多车（M1，2026-11-08）：
  - 设备清单在 devices.json，服务端只认台账里登记且 enabled 的设备
  - 所有接口接受显式 device_id；不传时回落 default，旧 APP / curl 零改动
  - 审计日志每条命令带 device_id，可区分是哪台车

绑定授权（M2，2026-11-08）：
  - 身份台账在 access.json：user / token / role / 可控制设备列表 / 可见范围
  - 每个请求解析身份，服务端逐次校验 (身份, device_id)；前端隐藏按钮不算授权
  - 「可见」与「可控」分开：观察端仍能看到全部车，但只有绑定该车的身份能下发
  - 越权返回 403 并写 commands.log；审计日志每条带 [user] 与 [device_id]
  - 兼容模式（require_token=false）：不带 token 视为 anonymous，旧 APP / curl 零改动

场地映射与任务编排（M3，2026-11-08）：
  - field.json 定义本地矢量场地、米制换算、安全边界 geofence、可选 GCJ-02 基准点
  - /status 与 /fleet_status 每台车多带 pos（米制坐标 / 区域 / 是否越界 / 可选经纬度）
  - /mission 把多步规范指令编成一个任务：逐步授权复检 + 逐步回执 + 轨迹 + 收尾 STOP
  - 默认不加载任何在线地图瓦片；真实底图只在 /map 用合规服务商

B/S 管理台（M4，2026-11-08）：
  - /admin 页面 + /api/admin/*，仅 role=admin 的令牌可用
  - 账号/绑定关系/严格模式/设备台账/场地标定/审计/任务都可在 Web 维护
  - devices.json 与 field.json 均按 mtime 热加载，管理台改完下一请求即生效

HTTP 接口：
  GET  /  /console               远程控制台页面（web/car_console.html）
  GET  /view                     云俯视图只读页面（web/car_cloud_view.html）
  GET  /map                      合规地理底图页（腾讯位置服务代理模式，可选）
  GET  /admin                    B/S 管理台页面（令牌在页内输入）
  GET  /assets/<file>            web/ 下的静态资源（field.js 等）
  GET  /whoami                   当前身份与可见/可控制车辆清单（M2）
  GET  /devices                  设备台账（按身份过滤，不含任何凭证）
  GET  /field                    场地坐标系 / 合规底图配置（M3）
  GET  /fleet_status             批量状态（一次拿全部车辆，供多车同屏；带短缓存）
  GET  /health                   探活 + 设备在线状态（可带 ?device_id=）
  GET  /status                   小车最新状态（可带 ?device_id=）
  POST /command                  下发控制命令（同步：等车执行完才返回）
       body: {"cmd": "FORWARD", "speed": 80, "device_id": "..."}
       cmd   ∈ FORWARD / BACK / LEFT / RIGHT / STOP
       speed 仅 FORWARD/BACK 需要，0~100
  GET  /mission/presets          预设任务清单（M3）
  POST /mission                  创建任务 {"device_id":"...", "preset":"square"} 或 {"steps":[...]}
  GET  /missions  /mission/<id>  任务列表 / 进度
  POST /mission/<id>/cancel      取消并急停；POST /mission/abort_all 一键全停（管理员）
  POST /voice_command            自然语言控车（ASR → DeepSeek → 规则裁决）
       multipart: audio=<16kHz 单声道 16bit WAV> [+ device_id]
       或 JSON:  {"text": "往前开慢一点", "execute": false, "device_id": "..."}
       默认只解析、不执行；execute=true 才真正下发（也可配 voice.auto_execute）
  /api/admin/*                   管理台接口（见 admin_api.py）

运行：python app.py           （读取同目录 config.json + devices.json，默认端口 8000）
依赖：flask、huaweicloudsdkcore、huaweicloudsdkiotda（v5）、requests、dashscope
"""

import hmac
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

try:
    from flask import Flask, g, jsonify, request, send_from_directory

    from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
    from huaweicloudsdkcore.exceptions import exceptions as hc_exceptions
    from huaweicloudsdkcore.region.region import Region
    from huaweicloudsdkiotda.v5 import IoTDAClient
    from huaweicloudsdkiotda.v5.model import (
        CreateCommandRequest,
        DeviceCommandRequest,
        ShowDeviceRequest,
        ShowDeviceShadowRequest,
    )
    from voice_chain import VoiceChain
    from field_map import get as load_field_map
    from mission import MissionManager
    from admin_api import create_admin_blueprint
except ModuleNotFoundError as _e:      # 多半是用错了 Python 解释器（裸 python 里没这些包）
    raise SystemExit(
        f"缺少依赖 {_e.name}。请用装了依赖的 Python 3.10 解释器运行：\n"
        "  D:\\miniconda\\python.exe app.py\n"
        "安装依赖：D:\\miniconda\\python.exe -m pip install flask huaweicloudsdkiotda requests dashscope\n"
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 配置加载
with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

# 设备台账只放元数据：GET /devices 会把它发给前端，绝不能出现任何密钥。
# 台账丢了也要能起服务 —— 退化成 config.json 里那台原单车，避免课堂现场全崩。
# M4：管理台会改 devices.json，因此改成按 mtime 热加载；三个容器原地更新，
#     所有旧代码继续引用这几个名字，不需要逐个改调用点。
DEVICES_PATH = os.path.join(BASE_DIR, "devices.json")
FIELD_PATH = os.path.join(BASE_DIR, "field.json")
COMMANDS_LOG_PATH = os.path.join(BASE_DIR, "commands.log")
_devices_lock = threading.Lock()
_devices_mtime = None
DEVICE_LIST = []
DEVICE_MAP = {}
DEFAULT_DEVICE = CFG["device_id"]


def devices_raw():
    """读一份完整台账配置（管理台写文件前用）。"""
    with open(DEVICES_PATH, encoding="utf-8") as f:
        return json.load(f)


def sync_devices(force=False):
    """文件变了就原地刷新 DEVICE_LIST / DEVICE_MAP / DEFAULT_DEVICE。"""
    global _devices_mtime, DEFAULT_DEVICE
    try:
        mt = os.path.getmtime(DEVICES_PATH)
    except OSError:
        mt = None
    with _devices_lock:
        if not force and mt == _devices_mtime and DEVICE_LIST:
            return
        try:
            cfg = devices_raw()
        except (OSError, ValueError) as e:
            print(f"[警告] devices.json 读取失败（{e}），保持上一份台账", flush=True)
            return
        items = [d for d in (cfg.get("devices") or []) if d.get("device_id")]
        DEVICE_LIST[:] = items
        DEVICE_MAP.clear()
        DEVICE_MAP.update({d["device_id"]: d for d in items})
        DEFAULT_DEVICE = cfg.get("default") or (items[0]["device_id"] if items else CFG["device_id"])
        _devices_mtime = mt


sync_devices(force=True)
if not DEVICE_LIST:
    DEVICE_LIST.append({"device_id": CFG["device_id"], "name": "默认车", "enabled": True})
    DEVICE_MAP[CFG["device_id"]] = DEVICE_LIST[0]


def devices_snapshot():
    sync_devices()
    return [dict(d) for d in DEVICE_LIST]


def default_device():
    sync_devices()
    return DEFAULT_DEVICE

# ---------------------------------------------------------------- 身份与绑定台账（M2）
# access.json 与 config.json 分开：AK/SK 是云凭证，绑定关系是业务数据（将来给 B/S 管理台维护）。
# 按文件 mtime 热加载 —— 课堂上用 access_admin.py 改一次绑定，下一次请求立即生效，不用重启服务。
ACCESS_PATH = os.path.join(BASE_DIR, "access.json")
_access_lock = threading.Lock()
_access_cache = {"mtime": None, "cfg": None, "by_token": {}}
_FALLBACK_ANON = {
    "user": "guest", "name": "访客（未登录）", "role": "operator",
    "scope": "all", "devices": [], "enabled": True,
}


def access_cfg():
    """返回 (配置, token→用户 映射)。文件改动自动热加载；文件缺失/损坏时降级为「不鉴权」。"""
    try:
        mt = os.path.getmtime(ACCESS_PATH)
    except OSError:
        return {"enabled": False, "require_token": False,
                "anonymous": _FALLBACK_ANON, "users": []}, {}
    with _access_lock:
        if _access_cache["cfg"] is None or mt != _access_cache["mtime"]:
            try:
                with open(ACCESS_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[警告] access.json 读取失败（{e}），本次按关闭鉴权处理", flush=True)
                cfg = {"enabled": False, "require_token": False,
                       "anonymous": _FALLBACK_ANON, "users": []}
            by_token = {}
            for u in (cfg.get("users") or []):
                t = (u.get("token") or "").strip()
                if t:
                    by_token[t] = u
            _access_cache.update(mtime=mt, cfg=cfg, by_token=by_token)
        return _access_cache["cfg"], _access_cache["by_token"]


def normalize_actor(u, fallback="guest"):
    return {
        "user": u.get("user") or fallback,
        "name": u.get("name") or u.get("user") or fallback,
        "role": u.get("role") or "viewer",
        "scope": u.get("scope") or "all",
        "devices": list(u.get("devices") or []),
        "enabled": bool(u.get("enabled", True)),
    }


def current_actor():
    return getattr(g, "actor", None) or normalize_actor(_FALLBACK_ANON)


def auth_enabled():
    return bool(access_cfg()[0].get("enabled", False))


def enabled_device_ids():
    ids = [d["device_id"] for d in DEVICE_LIST if d.get("enabled", True)]
    return ids or [DEFAULT_DEVICE]


def can_control(actor, did):
    """绑定授权的核心：只有 (身份, device_id) 命中才允许下发。前端隐藏按钮不算授权。"""
    if not auth_enabled():
        return True
    if actor["role"] not in ("operator", "admin"):
        return False
    ds = actor["devices"]
    return "*" in ds or did in ds


def can_view(actor, did):
    """可见范围与可控制范围刻意分开：观察端仍能看到全部车，多车同屏不退化。"""
    if not auth_enabled():
        return True
    if actor["scope"] == "all":
        return True
    ds = actor["devices"]
    return "*" in ds or did in ds


def actor_public(actor):
    """给前端的身份摘要，绝不包含 token。"""
    ids = enabled_device_ids()
    return {
        "user": actor["user"], "name": actor["name"], "role": actor["role"],
        "scope": actor["scope"], "auth_enabled": auth_enabled(),
        "can_control": [d for d in ids if can_control(actor, d)],
        "visible": [d for d in ids if can_view(actor, d)],
    }

# ---------------------------------------------------------------- 审计日志
# 管控层可见的证据：谁在何时对哪台车下发了什么、结果如何。教学点：命令审计日志。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, "commands.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("backend")

# ---------------------------------------------------------------- 云客户端（全局一份）
# 官方指南用法（标准版实例，见 sdkreference-iothub/iot_10_10003.html）：
#   1. BasicCredentials + with_derived_predicate：专属端点自动启用 V11 衍生签名
#   2. 自建 Region(region_id, 应用侧专属端点) + with_region()
#   3. 用实例专属端点时，请求一律不带 instance_id（域名已隐含实例）
credentials = BasicCredentials(CFG["ak"], CFG["sk"], CFG["project_id"])
credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
iot_client = (
    IoTDAClient.new_builder()
    .with_credentials(credentials)
    .with_region(Region(CFG["region"], CFG["endpoint"]))
    .build()
)

COMMAND_PARAS = {"FORWARD": ("speed",), "BACK": ("speed",), "LEFT": (), "RIGHT": (), "STOP": ()}

# 语音链路：ASR → DeepSeek → 规则裁决。密钥由 config.json 注入，不进前端。
voice_chain = VoiceChain(CFG, log)

app = Flask(__name__)


# ---------------------------------------------------------------- CORS
# 页面向本服务发请求时浏览器做跨源校验：
#   - 直接用 file:// 双击打开网页 -> 需要 ACAO（简单请求只需这一条）
#   - POST /command 带 application/json -> 浏览器先发 OPTIONS 预检，
#     必须额外放行 Allow-Headers，否则预检失败、请求根本发不出去
# 教学环境放行所有来源。
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    # M2：自定义身份头必须显式放行，否则浏览器预检失败、请求根本发不出去
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token, Authorization"
    # M4：管理台会发 PATCH / DELETE，预检必须一起放行
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


# ---------------------------------------------------------------- 身份解析与越权闸门（M2）
# 静态页面本身公开（身份在页面里输入），接口才鉴权；OPTIONS 预检也要放行。
PUBLIC_PATHS = {"/", "/console", "/view", "/map", "/admin"}


@app.before_request
def authenticate():
    if request.path.startswith("/assets/") or request.path == "/field.js":
        return None                               # 静态资源在严格模式下也要能加载
    if request.method == "OPTIONS" or request.path in PUBLIC_PATHS:
        return None
    sync_devices()                                # M4：管理台改完台账，下一请求立即生效
    cfg, by_token = access_cfg()
    if not cfg.get("enabled", False):
        g.actor = normalize_actor(_FALLBACK_ANON)
        return None

    token = (request.headers.get("X-Auth-Token") or "").strip()
    authz = request.headers.get("Authorization", "")
    if not token and authz.lower().startswith("bearer "):
        token = authz[7:].strip()
    if not token:
        token = (request.args.get("token") or "").strip()   # 便于 curl / 旧 APP 演示，正式环境应关掉

    if token:
        # 常量时间比较，避免按字符逐个猜 token；token 本身绝不写进日志
        matched = next((u for t, u in by_token.items()
                        if hmac.compare_digest(t, token)), None)
        if matched is None:
            log.warning("鉴权失败 -> 无效 token（来源 %s %s）", request.method, request.path)
            return jsonify({"ok": False, "stage": "auth",
                            "error": "身份令牌无效，请重新输入或清除后回到访客"}), 401
        if not matched.get("enabled", True):
            return jsonify({"ok": False, "stage": "auth",
                            "error": f"账号已停用：{matched.get('user')}"}), 403
        g.actor = normalize_actor(matched)
        return None

    if cfg.get("require_token", False):
        return jsonify({"ok": False, "stage": "auth",
                        "error": "需要身份令牌：请在请求头带 X-Auth-Token"}), 401
    # 兼容模式：不带 token 的旧 APP / curl 视为访客，仍能控制其绑定的默认车
    g.actor = normalize_actor(cfg.get("anonymous") or _FALLBACK_ANON)
    return None


def guard_control(did, item, source=""):
    """写接口前置闸门：返回 (响应, 状态码) 表示拒绝，返回 None 表示放行。"""
    a = current_actor()
    if not can_control(a, did):
        why = (f"身份「{a['name']}」无权控制 {item.get('name') or did}："
               f"该账号只绑定了 {len(a['devices'])} 台车。服务端已拒绝下发（403）。")
        log.warning("越权拒绝 -> user=%s device=%s source=%s", a["user"], did, source)
        return jsonify({"ok": False, "stage": "auth", "error": why,
                        "device_id": did, "actor": a["user"]}), 403
    return None


def guard_view(did):
    a = current_actor()
    if not can_view(a, did):
        log.warning("越权查看 -> user=%s device=%s", a["user"], did)
        return jsonify({"ok": False, "stage": "auth",
                        "error": f"身份「{a['name']}」无权查看该车辆",
                        "device_id": did, "actor": a["user"]}), 403
    return None


# ---------------------------------------------------------------- 辅助
def resolve_device(raw):
    """把请求里的 device_id 解析成可信目标。返回 (device_id, 台账条目, 错误信息)

    多车第一道闸门：不传 -> 默认车（兼容旧 APP）；传了但没登记 / 已停用 -> 拒绝。
    客户端不能凭空指定任意设备 ID，避免绕过台账直接操作云上其它设备。
    """
    did = (raw or "").strip() or DEFAULT_DEVICE
    item = DEVICE_MAP.get(did)
    if item is None:
        if did == CFG["device_id"]:
            # 台账漏登记时仍放行原来那台车，保证服务可用
            return did, {"device_id": did, "name": "默认车", "enabled": True}, None
        return None, None, f"设备未登记：{did}；可选：{list(DEVICE_MAP)}"
    if not item.get("enabled", True):
        return None, None, f"设备已停用：{did}（devices.json enabled=false）"
    return did, item, None


def build_paras(cmd: str, body: dict):
    """按白名单校验命令并抽出参数。返回 (paras, 错误信息)"""
    if cmd not in COMMAND_PARAS:
        return None, f"未知命令 {cmd}，可选：{list(COMMAND_PARAS)}"
    needed = COMMAND_PARAS[cmd]
    if not needed:
        return {}, None
    speed = body.get("speed")
    if not isinstance(speed, int) or not 0 <= speed <= 100:
        return None, "speed 必须是 0~100 的整数"
    return {"speed": speed}, None


def shadow_to_state(shadow_resp) -> dict:
    """把设备影子响应压平成 APP 好用的状态字典"""
    state = {"x": None, "y": None, "heading": None,
             "speed": None, "battery": None, "time": None}
    try:
        for s in shadow_resp.shadow:
            if s.reported and s.reported.properties:
                for k in state:
                    if k in s.reported.properties:
                        v = s.reported.properties[k]
                        # battery 等数值可能返回 Decimal，jsonify 不能序列化 Decimal
                        state[k] = float(v) if isinstance(v, Decimal) else v
                state["time"] = s.reported.event_time
    except (AttributeError, TypeError):
        pass
    return state


def device_online(device_id: str) -> bool:
    """设备是否在线（设备详情的 status 字段：ONLINE/OFFLINE/INACTIVE）"""
    try:
        resp = iot_client.show_device(ShowDeviceRequest(device_id=device_id))
        return resp.status == "ONLINE"
    except hc_exceptions.SdkException as e:
        log.error("查设备状态失败 [%s]: %s", device_id, e)
        return False


# ---------------------------------------------------------------- 静态页面
# 由后端自己托管网页 -> 与接口同源，浏览器完全不需要 CORS；
# 手机连同一热点时也能直接访问 http://<本机IP>:8000/ 操作。
WEB_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir, "web"))


@app.get("/")
@app.get("/console")
def console_page():
    """远程控制台（可看可控）"""
    return send_from_directory(WEB_DIR, "car_console.html")


@app.get("/view")
def view_page():
    """云俯视图（只读观察端，原 web/car_cloud_view.html）"""
    return send_from_directory(WEB_DIR, "car_cloud_view.html")


@app.get("/map")
def geo_map_page():
    """M3：合规地理底图（腾讯位置服务官方代理模式，页面内不写任何 key）"""
    return send_from_directory(WEB_DIR, "car_geo_map.html")


@app.get("/admin")
def admin_page():
    """M4：B/S 管理台（页面公开，接口 /api/admin/* 一律要求 admin 令牌）"""
    return send_from_directory(WEB_DIR, "car_admin.html")


@app.get("/assets/<path:name>")
def web_assets(name):
    """web/ 下的静态资源（field.js 等）。send_from_directory 自带路径穿越防护。"""
    return send_from_directory(WEB_DIR, name)


@app.get("/field.js")
def field_js():
    """同目录相对路径：页面用 <script src="field.js">，file:// 双击打开也能加载。"""
    return send_from_directory(WEB_DIR, "field.js")


# ---------------------------------------------------------------- 接口
@app.get("/field")
def field_config():
    """M3：场地坐标系与合规矢量底图配置，前端据此画网格/分区/比例尺、换算米制坐标。"""
    pub = load_field_map().public()
    a = current_actor()
    if a["role"] == "viewer" and isinstance(pub.get("geo"), dict):
        # 只读观察员不给基准点：未校准时本来就是 null，校准后这里也抹掉
        pub["geo"]["origin_lng"] = None
        pub["geo"]["origin_lat"] = None
    return jsonify({"ok": True, "field": pub, "actor": actor_public(a)})


@app.get("/whoami")
def whoami():
    """M2：当前身份与授权范围。前端据此显示「我是谁、能控哪几台车」，绝不返回 token。"""
    a = current_actor()
    return jsonify({"ok": True, "actor": actor_public(a),
                    "require_token": bool(access_cfg()[0].get("require_token", False))})


@app.get("/devices")
def devices():
    """设备台账（M1）：供前端选车下拉。只回元数据，不含任何凭证。

    M2：按身份的可见范围过滤，并给每台车标注 can_control —— 前端可提示"只读"，
    但真正能否下发仍由服务端 guard_control() 裁决。
    刻意不在这里查在线状态：N 台车 = N 次云 API 同步调用，2 秒轮询会被拖垮。
    """
    a = current_actor()
    items = [{
        "device_id": d["device_id"],
        "name": d.get("name") or d["device_id"],
        "kind": d.get("kind", "simulator"),
        "group": d.get("group", ""),
        "enabled": bool(d.get("enabled", True)),
        "default": d["device_id"] == DEFAULT_DEVICE,
        "can_control": can_control(a, d["device_id"]),
    } for d in DEVICE_LIST if can_view(a, d["device_id"])]
    return jsonify({"ok": True, "default": DEFAULT_DEVICE,
                    "devices": items, "actor": actor_public(a)})


@app.get("/health")
def health():
    did, item, err = resolve_device(request.args.get("device_id"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    denied = guard_view(did)
    if denied:
        return denied
    return jsonify({"ok": True, "device_id": did, "device_online": device_online(did)})


@app.get("/status")
def status():
    """APP 轮询此接口拿小车最新状态（数据源：设备影子）"""
    did, item, err = resolve_device(request.args.get("device_id"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    denied = guard_view(did)
    if denied:
        return denied
    try:
        resp = iot_client.show_device_shadow(ShowDeviceShadowRequest(device_id=did))
    except hc_exceptions.SdkException as e:
        return jsonify({"ok": False, "error": f"查影子失败: {e}", "device_id": did}), 502
    state = shadow_to_state(resp)
    # M3：把逻辑坐标映射成场地米制坐标 / 区域 / 越界标记 / 可选 GCJ-02
    return jsonify({"ok": True, "device_id": did,
                    "device_name": item.get("name") or did,
                    "online": device_online(did),
                    "pos": load_field_map().position(state["x"], state["y"]),
                    **state})


# ---------------------------------------------------------------- 批量状态（多车同屏）
# 多车同屏时，前端若对每台车各发一次 /status，N 台车就是 N 个 HTTP + 2N 次云 API，
# 2 秒轮询很快把请求打满。这里一次返回全部车辆：并发查询 + 短缓存，
# 让「多开几个页面 / 多次轮询」共用同一份快照。单台查询失败不影响其它车。
FLEET_CACHE_TTL = 1.0            # 秒；比前端 2 秒轮询短，既能削峰又不会读到太旧的数据
FLEET_MAX_WORKERS = 8
_fleet_lock = threading.Lock()
_fleet_cache = {"at": 0.0, "data": None}


def fleet_for_actor(data, cached):
    """缓存的是全量快照，返回前按当前身份裁剪可见车辆，避免不同账号互相看到不该看的车。"""
    a = current_actor()
    ctrl = set(actor_public(a)["can_control"])
    visible = []
    for d in data["devices"]:
        if not can_view(a, d["device_id"]):
            continue
        one = dict(d)                       # 浅拷贝：不要污染共享缓存
        one["can_control"] = one["device_id"] in ctrl
        visible.append(one)
    return {**data, "devices": visible, "cached": cached, "actor": actor_public(a)}


def _one_device_state(item: dict) -> dict:
    """查一台车的影子与在线状态；任何异常都收敛成该车的 error 字段。"""
    did = item["device_id"]
    out = {
        "device_id": did,
        "name": item.get("name") or did,
        "kind": item.get("kind", "simulator"),
        "group": item.get("group", ""),
        "default": did == DEFAULT_DEVICE,
        "ok": False, "online": False,
        "x": None, "y": None, "heading": None,
        "speed": None, "battery": None, "time": None,
        "pos": None,
    }
    try:
        resp = iot_client.show_device_shadow(ShowDeviceShadowRequest(device_id=did))
        st = shadow_to_state(resp)
        out.update(st)
        # M3：前端画合规底图、显示米制坐标与安全边界都靠这个字段
        out["pos"] = load_field_map().position(st["x"], st["y"])
        out["online"] = device_online(did)
        out["ok"] = True
    except Exception as e:                       # noqa: BLE001 - 单台失败不能拖垮整批
        out["error"] = f"查状态失败: {e}"
        log.error("批量状态 -> [%s] %s", did, e)
    return out


@app.get("/fleet_status")
def fleet_status():
    """所有 enabled 车辆的最新状态，一次 HTTP 拿全（前端多车同屏用）。

    返回 devices 里每台车都带自己的 ok / online / 坐标 / 速度 / 电量 / time；
    time 仍是影子原始上报时间（UTC），前端据此显示「几分钟前」而不是「0 秒前」。
    """
    with _fleet_lock:                            # 持锁查询：并发请求共用一次云端快照
        cached = _fleet_cache["data"]
        if cached and (time.monotonic() - _fleet_cache["at"]) < FLEET_CACHE_TTL:
            return jsonify(fleet_for_actor(cached, True))

        enabled = [d for d in DEVICE_LIST if d.get("enabled", True)]
        if not enabled:
            enabled = [{"device_id": DEFAULT_DEVICE, "name": "默认车", "kind": "simulator"}]
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(len(enabled), FLEET_MAX_WORKERS)) as pool:
            states = list(pool.map(_one_device_state, enabled))
        data = {
            "ok": True,
            "default": DEFAULT_DEVICE,
            "devices": states,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "cached": False,
        }
        _fleet_cache["data"] = data
        _fleet_cache["at"] = time.monotonic()
        return jsonify(fleet_for_actor(data, False))


def dispatch(cmd: str, paras: dict, device_id: str, actor: str = ""):
    """真正下发到 IoTDA 并等待回执。/command 与语音自动执行共用这一份逻辑。"""
    req = CreateCommandRequest(
        device_id=device_id,
        # 云端校验要求 paras 恒非空：无参命令也传 {}（传 None 报 IOTDA.000006）
        body=DeviceCommandRequest(service_id="car", command_name=cmd, paras=paras or {}),
    )
    log.info("命令下发 -> user=%s [%s] %s %s", actor, device_id, cmd, paras or "")
    try:
        resp = iot_client.create_command(req)
    except hc_exceptions.SdkException as e:
        log.error("命令失败 -> user=%s [%s] %s %s err=%s",
                  actor, device_id, cmd, paras or "", e)
        return None, str(e)

    # 同步命令：resp.response 有值（response_name=命令名）说明车已执行并回执
    # 注意：SDK 反序列化后 response 可能是 dict 也可能是模型对象，统一压成 dict 处理
    resp_info = getattr(resp, "response", None)
    if isinstance(resp_info, dict):
        info = resp_info
    elif resp_info is not None:
        info = {k: getattr(resp_info, k, None) for k in ("response_name", "result", "paras", "error_code")}
    else:
        info = None
    result = {
        "command_id": getattr(resp, "command_id", None),
        "executed": bool(info and info.get("response_name")),
        "response_name": info.get("response_name") if info else None,
    }
    log.info("命令回执 -> user=%s [%s] %s %s result=%s",
             actor, device_id, cmd, paras or "", result)
    return result, None


# ---------------------------------------------------------------- 任务编排（M3）
# dispatch / build_paras / 授权复检 / 影子位置全部由本文件注入，mission.py 不碰 Flask 与云 SDK。
def recheck_authority(actor_user, did):
    """任务每一步下发前重新读 access.json：中途 revoke / 停用账号必须立刻停。"""
    target, item, err = resolve_device(did)
    if err:
        return False, err
    if not auth_enabled():
        return True, None                         # 兼容模式：未开鉴权时维持原行为
    cfg, _ = access_cfg()
    u = next((x for x in (cfg.get("users") or []) if x.get("user") == actor_user), None)
    if u is None:
        anon = cfg.get("anonymous") or _FALLBACK_ANON
        if (anon.get("user") or "guest") != actor_user:
            return False, "账号已不存在或已被删除"
        u = anon
    if not u.get("enabled", True):
        return False, "账号已停用"
    if not can_control(normalize_actor(u), target):
        return False, "绑定关系已变更（该车控制权已被回收）"
    return True, None


def mission_shadow(did):
    """给任务轨迹采样：只取影子坐标，失败返回 None（观测数据不能阻断任务）。"""
    try:
        resp = iot_client.show_device_shadow(ShowDeviceShadowRequest(device_id=did))
        return shadow_to_state(resp)
    except Exception:                             # noqa: BLE001
        return None


missions = MissionManager({
    "build_paras": build_paras,
    "dispatch": dispatch,
    "authorize": recheck_authority,
    "state": mission_shadow,
    "field": load_field_map,
    "log": log,
})


@app.get("/mission/presets")
def mission_presets():
    return jsonify({"ok": True, "presets": missions.presets(),
                    "limits": {"max_steps": 20, "step_ms": [100, 5000], "total_ms": 60000}})


@app.get("/missions")
def mission_list():
    return jsonify({"ok": True, "missions": missions.list(limit=20)})


@app.post("/mission")
def mission_create():
    """创建任务：预设 preset 或自定义 steps，二者选一。"""
    body = request.get_json(silent=True) or {}
    did, item, err = resolve_device(body.get("device_id"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    denied = guard_control(did, item, "/mission")
    if denied:
        return denied
    preset = (body.get("preset") or "").strip() or None
    if preset:
        found = next((p for p in missions.presets() if p["key"] == preset), None)
        if not found:
            return jsonify({"ok": False,
                            "error": f"未知预设任务：{preset}；可选 {[p['key'] for p in missions.presets()]}"}), 400
        steps, default_name = found["steps"], found["name"]
    else:
        steps, default_name = body.get("steps"), "自定义任务"
    m, err = missions.create(str(body.get("name") or default_name)[:40],
                             did, current_actor()["user"], steps,
                             source="web" if preset else "api", preset=preset)
    if err:
        return jsonify({"ok": False, "stage": "validate", "error": err}), 400
    return jsonify({"ok": True, "mission": m}), 202


@app.get("/mission/<mid>")
def mission_detail(mid):
    m = missions.get(mid)
    if not m:
        return jsonify({"ok": False, "error": f"找不到任务 {mid}"}), 404
    denied = guard_view(m["device_id"])
    if denied:
        return denied
    return jsonify({"ok": True, "mission": m})


@app.post("/mission/<mid>/cancel")
def mission_cancel(mid):
    a = current_actor()
    m = missions.get(mid)
    if not m:
        return jsonify({"ok": False, "error": f"找不到任务 {mid}"}), 404
    is_admin = a["role"] == "admin"
    if not is_admin and m["actor"] != a["user"]:
        return jsonify({"ok": False, "error": "只能取消自己创建的任务"}), 403
    out, err = missions.cancel(mid, a["user"], is_admin=is_admin, reason="人工取消")
    if err:
        return jsonify({"ok": False, "error": err}), 400
    log.warning("任务取消 -> id=%s by=%s", mid, a["user"])
    return jsonify({"ok": True, "mission": out,
                    "hint": "已异步下发 STOP 急停；急停不受安全边界限制"})


@app.post("/mission/abort_all")
def mission_abort_all():
    """一键全停：课堂上出状况时最快的兜底，仅管理员（或未开鉴权时）可用。"""
    a = current_actor()
    if auth_enabled() and a["role"] != "admin":
        return jsonify({"ok": False, "stage": "auth",
                        "error": "一键全停需要管理员令牌"}), 403
    done = missions.abort_all(a["user"])
    return jsonify({"ok": True, "cancelled": done, "count": len(done)})


# ---------------------------------------------------------------- 管理台蓝图（M4）
admin_bp = create_admin_blueprint({
    "access_cfg": access_cfg,
    "auth_enabled": auth_enabled,
    "get_actor": current_actor,
    "actor_public": actor_public,
    "log": log,
    "access_path": ACCESS_PATH,
    "devices_path": DEVICES_PATH,
    "devices_raw": devices_raw,
    "devices_snapshot": devices_snapshot,
    "sync_devices": lambda: sync_devices(force=True),
    "default_device": default_device,
    "field_path": FIELD_PATH,
    "field": load_field_map,
    "fleet_cached": lambda: _fleet_cache["data"],
    "missions": missions,
    "commands_log": COMMANDS_LOG_PATH,
})
app.register_blueprint(admin_bp)


@app.post("/command")
def command():
    """下发命令（同步命令：等小车执行回执后才返回，超时约 20 秒）"""
    body = request.get_json(silent=True) or {}
    did, item, err = resolve_device(body.get("device_id"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    # M2 第二道闸门：台账校验之后、真正下发之前，逐次校验 (身份, device_id)
    denied = guard_control(did, item, "/command")
    if denied:
        return denied
    cmd = (body.get("cmd") or "").upper()
    paras, err = build_paras(cmd, body)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    result, err = dispatch(cmd, paras, did, current_actor()["user"])
    if err:
        return jsonify({"ok": False, "error": err, "device_id": did}), 502
    return jsonify({"ok": True, "device_id": did, **result})


@app.post("/voice_command")
def voice_command():
    """自然语言控车：音频或文本 → ASR → DeepSeek → 规则裁决 →（可选）下发

    返回体刻意保留每一层的中间结果，方便课堂上逐层讲解与排错：
      text      识别/输入的原文
      parsed    大模型原始 JSON
      command   裁决后允许执行的规范指令
      stage     异常发生在哪一段（asr / llm / validate / dispatch）
      executed  是否真的拿到了车端回执
      device_id 本次指令归属哪台车
    """
    voice_cfg = CFG.get("voice") or {}
    if not voice_cfg.get("enabled", False):
        return jsonify({"ok": False, "error": "语音链路未启用：config.json voice.enabled=false"}), 503

    wav_path = None
    try:
        execute = None
        raw_device_id = None
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            audio = request.files.get("audio")
            if audio is None or not audio.filename:
                return jsonify({"ok": False, "error": "缺少 audio 文件字段"}), 400
            fd, wav_path = tempfile.mkstemp(prefix="voice_", suffix=".wav")
            os.close(fd)
            audio.save(wav_path)
            size = os.path.getsize(wav_path)
            with open(wav_path, "rb") as fh:
                head = fh.read(12)
            if not 44 <= size <= 5 * 1024 * 1024:
                return jsonify({"ok": False, "error": f"音频大小异常：{size} 字节"}), 400
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return jsonify({"ok": False,
                                "error": "只接受 WAV（16kHz / 单声道 / 16bit PCM）"}), 400
            execute = request.form.get("execute")
            raw_device_id = request.form.get("device_id")
            text = None
        else:
            body = request.get_json(silent=True) or {}
            text = body.get("text")
            execute = body.get("execute")
            raw_device_id = body.get("device_id")

        did, item, err = resolve_device(raw_device_id)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        # 语音链路同样受绑定约束：即使只是"解析"，目标车也必须是当前身份能控制的
        denied = guard_control(did, item, "/voice_command")
        if denied:
            out = denied[0].get_json()
            out.update({"text": None, "parsed": None, "command": None,
                        "executed": False, "device_id": did})
            return jsonify(out), denied[1]

        if execute is None:
            execute = voice_cfg.get("auto_execute", False)
        elif isinstance(execute, str):
            execute = execute.lower() in ("1", "true", "yes", "on")
        else:
            execute = bool(execute)

        actor = current_actor()["user"]
        out = voice_chain.run(text=text, wav_path=wav_path)
        out["device_id"] = did
        out["device_name"] = item.get("name") or did
        out["actor"] = actor
        out["auto_execute"] = bool(execute)
        out["executed"] = False
        log.info("语音指令 -> user=%s [%s] %r -> %s",
                 actor, did, out.get("text"), out.get("command"))

        if out["ok"] and execute:
            cmd = out["command"]["cmd"]
            # 再过一次 /command 的白名单与参数校验：AI 的输出绝不直接执行
            paras, err = build_paras(cmd, out["command"])
            if err:
                out["ok"] = False
                out["stage"] = "validate"
                out["error"] = err
            else:
                result, err = dispatch(cmd, paras, did, actor)
                out["dispatch"] = result if result else {"error": err}
                out["executed"] = bool(result and result["executed"])
                # 下发失败必须显式回传：ok 只代表 ASR→LLM→裁决 成功，
                # 绝不能让前端把"解析成功"误显示成"车已执行"。
                if err or not out["executed"]:
                    out["dispatch_error"] = err or "命令已送达，但车端未回执（executed=false）"
                    out["stage"] = "dispatch"
                    out["error"] = out["dispatch_error"]
                    log.warning("语音指令下发失败 -> user=%s [%s] %s %s err=%s",
                                actor, did, cmd, paras, out["dispatch_error"])
        return jsonify(out)
    finally:
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass


if __name__ == "__main__":
    print("=" * 50)
    print(" 业务后端已启动（APP -> 本服务 -> 华为云 IoTDA）")
    print(f"   🎮 控制台  http://127.0.0.1:{CFG['http_port']}/")
    print(f"   👁  俯视图  http://127.0.0.1:{CFG['http_port']}/view")
    print(f"   🗺  合规底图 http://127.0.0.1:{CFG['http_port']}/map  (M3，腾讯位置服务代理模式)")
    print(f"   🛠  管理台  http://127.0.0.1:{CFG['http_port']}/admin  (M4，需 admin 令牌)")
    print(f"   GET  /whoami /devices /field /fleet_status /status /missions")
    print(f"   POST /command /voice_command /mission /mission/<id>/cancel /mission/abort_all")
    print(f"   /api/admin/*  管理台接口（账号/绑定/严格模式/设备/场地/审计/任务）")
    voice_cfg = CFG.get("voice") or {}
    print(f" 语音链路: {'启用' if voice_cfg.get('enabled') else '关闭'}"
          f" · 自动执行: {'开' if voice_cfg.get('auto_execute') else '关（需人工确认）'}")
    print(f" 设备台账: {len(DEVICE_LIST)} 台 · 默认 {DEFAULT_DEVICE}")
    ac, users = access_cfg()
    print(f" 绑定授权: {'启用' if ac.get('enabled') else '关闭'}"
          f" · 严格模式(require_token): {'开' if ac.get('require_token') else '关（不带 token 视为访客）'}"
          f" · 账号 {len(users)} 个")
    print("=" * 50, flush=True)
    app.run(host="0.0.0.0", port=CFG["http_port"], debug=False)
