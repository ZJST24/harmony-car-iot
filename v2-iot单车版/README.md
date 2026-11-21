# IoT-Stage - 华为云 IoTDA 接入阶段（云上寻检车）

> 项目课《全场景智能寻检车 APP 开发实战》第三阶段配套工程。
> 与 [lab01-tcp-sim](../lab01-tcp-sim/)（TCP 直连阶段）**完全独立、互不覆盖**，便于课堂上分阶段演示对比。

## 一、方案路径：为什么走「业务后端中转」路线

TCP 阶段是 APP 直连小车，简单直观；一旦小车上云，直连就不成立了。
本阶段采用**三层架构（路线 B）**：

```
鸿蒙 APP（应用端，待开发）
        │  HTTP（轮询 /status + POST /command）
        ▼
app.py 业务后端（Flask，本机 :8000）          ← 凭证隔离、命令白名单、审计日志
        │  HTTPS + AK/SK 签名（应用侧 REST API）
        ▼
华为云 IoTDA（标准版免费实例 S0）
        │  MQTT over TLS（命令推送 ↓ / 属性上报 ↑）
        ▼
car_mqtt_client.py 小车端（MQTT 客户端）
```

**为什么中间要加一层业务后端，而不是让 APP 直接调华为云 API？**

| 关注点 | APP 直连云平台 | 业务后端中转（本方案） |
|---|---|---|
| AK/SK 凭证 | 必须下发到每台手机，学生反编译即泄露 | 只存后端，APP 零持有 |
| 命令管控 | 无法限制，学生可绕过 APP 直接调 API | 白名单校验（只放行 5 条命令）+ 参数检查 |
| 审计 | 无 | commands.log 记录每条命令的谁/何时/结果 |
| 对学生暴露的接口 | 华为云 OpenAPI 全貌 | 两个极简 HTTP 接口 |

这也是工业界的真实做法：米家、涂鸦等消费级 IoT 平台的 APP 都不直接持有云端主凭证。

## 二、已完成工作（截至 2026-10-30）

| # | 工作项 | 状态 |
|---|---|---|
| 1 | 华为云 IoTDA 标准版免费实例开通（S0，1000 设备/日 1 万条消息） | ✅ |
| 2 | 产品 smart-car + 物模型定义与上传（5 属性 + 5 命令） | ✅ |
| 3 | 设备 car-001 注册（一机一密） | ✅ |
| 4 | 小车端 MQTT 客户端（连云、收命令、报属性） | ✅ 实测通过 |
| 5 | 应用侧 REST API 调通（403 问题破案，见下文） | ✅ 实测通过 |
| 6 | 业务后端 Flask（/health /status /command） | ✅ 实测通过 |
| 7 | **三层端到端闭环**（HTTP → 云 → MQTT → 车动 → 回执） | ✅ 全部 executed:true |
| 8 | APP UI 端（工程 CarCtrlCloud，`@ohos.net.http` 轮询 + 命令） | ✅ 模拟器实测通过 |
| 9 | 浏览器 Canvas 俯视图（轮询 + 航位推算） | ✅ |

## 三、文件清单

```
iot-stage/
├── README.md                  ← 本文档
├── model/                     ← 物模型（上传到 IoTDA 控制台用）
│   ├── smart-car-model.json   ← 物模型源文件（人类可读版）
│   └── smart-car-model.zip    ← 上传用 zip（IoTDA 只收 zip，目录结构有要求）
└── server/
    ├── config.json            ← 凭证与实例配置（敏感，勿发学生、勿提交仓库）
    ├── car_mqtt_client.py     ← 小车端：MQTT 客户端连华为云
    ├── app.py                 ← 业务后端：Flask，APP 与云之间的中转层
    ├── commands.log           ← 命令审计日志（app.py 自动追加）
    ├── test_api_auth.py       ← 诊断脚本：应用侧认证矩阵测试（403 排查留档）
    └── test_accounts.py       ← 诊断脚本：多账号连通性对比
└── web/
    └── car_cloud_view.html    ← 浏览器云俯视图（轮询 /status + 航位推算，双击打开）
```

### 每个脚本的分工

| 脚本 | 角色 | 一句话说明 |
|---|---|---|
| `car_mqtt_client.py` | **小车端（设备端）** | 扮演 MQTT 客户端连华为云；订阅命令主题收指令驱动运动模型，周期上报 x/y/heading/speed/battery 属性 |
| `app.py` | **业务后端（应用端代理）** | Flask 服务；把华为云 OpenAPI 封装成 /health /status /command 三个极简接口给 APP |
| `test_api_auth.py` | 诊断工具 | 用不同端点×签名方式组合测 ListDevices，定位认证问题（403 破案功臣） |
| `test_accounts.py` | 诊断工具 | 同一正确用法下对比 4 个 IAM 账号（子用户/主账号）的连通性 |

> 呼应课程称呼规范：本阶段小车端角色翻转——TCP 阶段它是**服务端**（等人来连），现在它是**MQTT 客户端**（主动连云）。这是 MQTT 体系的重要特点：所有设备都是客户端，broker（消息 broker，即 IoTDA）才是服务端。

## 四、使用步骤

### 前置依赖（仅一次）

```bash
D:\anaconda3\python.exe -m pip install paho-mqtt flask
# 华为云 SDK 已从官方 zip 解压到 Python310 用户目录（含 2 处本地补丁，见坑 6）
```

注意：本机要**显式用 `D:\anaconda3\python.exe`**（3.10），不要用裸 `python`（那是别的版本，没有这些包）。

> **本机注记（2026-11-07，非原文）**：本文档最初写作环境是老师机器，`D:\anaconda3\python.exe` 在**别人机器上通常不存在**——裸 `python` 往往是 3.13 且没有任何依赖，会直接 `ModuleNotFoundError: No module named 'paho'`。
> 换机器时先用 `where python` / `python -V` 确认，再挑一个 **3.10+** 的解释器把 `paho-mqtt flask huaweicloudsdkcore huaweicloudsdkiotda` 装上；本文档示例里的判断依据是"版本对不对"，不是"路径对不对"。
> 另外小车主脚本的实际文件名是 `car_mqtt_client_001.py` / `car_mqtt_client_002.py`（一台车一个文件），下文简写的 `car_mqtt_client.py` 并不存在。

### 启动顺序（两个终端）

```bash
# 终端 1：先让小车上云（设备在线）
cd iot-stage/server
D:\anaconda3\python.exe car_mqtt_client.py

# 终端 2：再起业务后端
cd iot-stage/server
D:\anaconda3\python.exe app.py
```

小车端看到 `[连接] 已连上华为云 IoTDA`、后端打印横幅即就绪。

### 验证三层闭环（不用 APP，curl 即可）

```bash
# 探活 + 设备是否在线
curl http://127.0.0.1:8000/health
# → {"ok":true,"device_online":true}

# 查小车最新状态（数据源：设备影子）
curl http://127.0.0.1:8000/status
# → {"x":400,"y":70,"heading":345,"speed":0,"battery":86.1,"online":true,...}

# 下发命令（同步命令：等小车执行回执后才返回）
curl -X POST http://127.0.0.1:8000/command -H "Content-Type: application/json" -d "{\"cmd\":\"FORWARD\",\"speed\":60}"
# → {"ok":true,"command_id":"...","executed":true,"response_name":"FORWARD"}

# 命令白名单演示（管控层价值）：非法命令直接被后端拒绝
curl -X POST http://127.0.0.1:8000/command -H "Content-Type: application/json" -d "{\"cmd\":\"DELETE_ALL\"}"
# → 400 {"ok":false,"error":"未知命令 DELETE_ALL，可选：['FORWARD',...]"}
```

> 注意：同一 device_id 只能跑一份小车客户端（双实例互踢）；同一个账号在控制台手动模拟设备也会互踢。

### 打开云俯视图（浏览器观察端）

双击 `web/car_cloud_view.html`（默认连 `http://localhost:8000`，可改）。与 lab01 的俯视图对比着看，是本阶段最好的总结素材：

| | lab01 car_view.html | 本阶段 car_cloud_view.html |
|---|---|---|
| 数据链路 | WS 直连车端（100ms 推送） | HTTP 轮询业务后端（2s）→ 云端设备影子（5s 一报） |
| 位置平滑 | 不需要（帧率够高） | **航位推算**：两次影子之间按 speed/heading 本地外推，白圈 = 最近影子上报点 |
| 在线徽标 | 连/断 二态 | 绿"车在线"/黄"车离线"/灰"后端不通" 三态（对应车端 MQTT / 后端两个独立故障点） |

> 航位推算（dead reckoning）是无人车与游戏联机的经典补偿技术：车端 5 秒一报太稀，浏览器按速度朝向自己"脑补"中间过程，影子更新到达时自动校正累积漂移。

## 五、关键概念讲解（备课要点）

### 1. 物模型：云端的「设备说明书」

IoTDA 是通用平台，它不知道什么是"寻检车"——物模型就是告诉平台：这个产品有什么属性（可读的状态量）、支持什么命令（可下发的动作）。本项目的物模型刻意与 TCP 阶段的协议一一对应，方便学生迁移理解：

| TCP 阶段 | IoT 阶段 | 说明 |
|---|---|---|
| `STATE` 帧 | 属性 x/y/heading/speed/battery | 车辆状态上报 |
| `CMD:FORWARD:60` 帧 | 命令 FORWARD + paras {speed:60} | 平台下发命令 |
| ACK 回执 | 命令响应（response + request_id） | 车辆执行确认 |

上传格式坑：IoTDA 只收 zip，且目录结构必须是 `profile/devicetype-capability.json + service/service1/profile/servicetype-capability.json`；description 字段有字符白名单（`°`、全角冒号等会被拒）。

### 2. MQTT 鉴权：一机一密（本阶段第一个大坑）

设备连云不走用户名密码登录，而是**每台设备用密钥现场算签名**：

```
timestamp = UTC 时间 YYYYMMDDHH        ← 必须是 UTC，不是北京时间！
password  = HMAC-SHA256(密钥=timestamp, 内容=设备secret) 的十六进制
clientId  = {device_id}_0_1_{timestamp}  ← "1"表示校验时间戳
username  = {device_id}
```

两个经典错误（都导致 rc=4 鉴权失败）：HMAC 的密钥和内容顺序写反；时间戳用了北京时间。连 MQTTS 8883 端口（TLS 加密）。

### 3. 消息流：两个方向靠"主题"路由

```
命令下发（云端 → 车）：车订阅 $oc/devices/{id}/sys/commands/#
                      消息从主题尾部拿 request_id
命令回执（车 → 云端）：车发布 .../commands/response/request_id={id}
属性上报（车 → 云端）：车发布 $oc/devices/{id}/sys/properties/report
                      payload: {"services":[{"service_id":"car","properties":{...}}]}
```

上报节奏有讲究：**运动中每 5 秒、静止每 60 秒**。因为免费实例每天只给 1 万条消息，全班演示如果 100ms 一报（TCP 阶段的节奏），几分钟就烧完额度——这是真实的云成本意识教学点。

### 4. 应用侧 SDK：403 破案实录（本阶段最大的坑，也是最好的教学素材）

**症状**：设备侧 MQTT 一切正常，但用 SDK 调应用侧 REST API 恒定 403 `IOTDA.000021`（"User not found by IAM token or not subscribed to IoTDA"）。曾怀疑账号权限问题——换了 3 个子用户 + 主账号全试过，错误信息极有误导性。

**真相**：SDK 用法错了，三处叠加：

| # | 错误 | 正确做法 | 原理 |
|---|---|---|---|
| 1 | 端点用通用 `iotda.cn-east-3.myhuaweicloud.com` | 用**实例专属** `https://e4e0263312.st1.iotda-app.cn-east-3.myhuaweicloud.com` | 通用域名是基础版（共享版）的入口；标准版实例有专属域名（和设备侧 `iotda-device` 同构） |
| 2 | `BasicCredentials + with_endpoint` | `BasicCredentials + with_derived_predicate(...) + with_region(Region(...))` | 专属域名（数字开头）不匹配通用域名正则 → SDK 自动切换 **V11 衍生签名**（DerivationAKSKSigner，服务名 iotdm）。漏配则签名算法不对，服务端验签失败 |
| 3 | 请求带 `instance_id` 参数 | **一律不带** | 实例 ID 已隐含在专属域名里，带了反而 403（官方文档未明说，实测发现） |

正确用法（app.py 生产代码）：

```python
from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.region.region import Region

credentials = BasicCredentials(ak, sk, project_id)
credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
client = IoTDAClient.new_builder() \
    .with_credentials(credentials) \
    .with_region(Region(region_id, endpoint)) \
    .build()
```

**复盘教训**：错误信息说"用户不存在或未订阅"，方向全错——问题不在身份，在请求本身。排错时应先穷尽"自己用法错"的假设（对照官方 SDK 指南逐行核对），再怀疑平台。当时甚至起草了工单模板，差点提交。诊断脚本 `test_api_auth.py` 就是这次的排错矩阵留档。

> 本节表格里的 `e4e0263312 / cn-east-3` 是**当年调试时用的实例**，现已换成 `9f149bee02 / cn-north-4`（见文末附录）。三条"正确做法"本身与实例无关，换实例后依然成立。

### 5. 设备影子：车不在线时的"最后状态"

`/status` 的数据源是**设备影子（Device Shadow）**——云端为每台设备缓存的最近一次上报状态。即使小车下线，APP 仍能查到最后位置；小车重新上线后影子自动更新。这是 IoT 平台解决"设备间歇在线"的标准机制。

注意一个现象（不是 bug）：影子数据随属性上报刷新（运动 5s/静止 60s），所以 STOP 后立刻查 /status，speed 可能还是旧值，下个上报周期才归零。

## 六、踩坑总账（按时间线）

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | 物模型 description 字符白名单 | 上传报 IOTDA.000006 | 描述只留中文/ASCII/空格，`°`、全角冒号都不行 |
| 2 | MQTT 鉴权三要素 | rc=4 鉴权失败 | HMAC 密钥/内容顺序 + UTC 时间戳（见讲解 2） |
| 3 | SDK 上游打包 bug | import 崩溃 | 官方 zip 3.1.220 引用了 core 里不存在的 `AuthCache` 和 `Iam`：手写 shim + 改 import（本地补丁，升级 SDK 需重验） |
| 4 | 应用侧 403 | IOTDA.000021 | 端点 + 衍生签名 + instance_id 三连错（见讲解 4，最大坑） |
| 5 | CreateCommand paras 校验 | IOTDA.000006 "paras must not be null" | 无参命令也传 `{}`，不能传 None |
| 6 | 回执类型不定 | 500：dict 无 response_name 属性 | SDK 反序列化后 resp.response 可能是 dict 也可能是模型对象，代码兼容两者 |
| 7 | Decimal 序列化 | jsonify 崩溃 | 影子里 battery 是 Decimal，先 float() 再 jsonify |
| 8 | 解释器错位 | 装了包却 ImportError | 沙箱内裸 `python` 是 3.13（无 SDK），必须 `D:\anaconda3\python.exe` |

## 七、待办（下阶段）

### 2026-11-08 里程碑进度（M1 → M4 全部落地）

| 里程碑 | 内容 | 状态 / 交付 |
|---|---|---|
| M1 多车 | `devices.json` 台账、`device_id` 参数化、002 车端上云、`GET /devices` | ✅ 见 `docs/项目交接.md` 第 11 节 |
| M2 绑定授权 | `access.json` + `X-Auth-Token` + 服务端逐次裁决 `(身份, device_id)`，APP 身份栏与选车 | ✅ 第 14 节 |
| M3 场地与任务 | `field.json` / `field_map.py` / `web/field.js` 本地矢量合规底图 + 米制坐标；`mission.py` 任务状态机（4 预设、逐步授权复检、逐步回执、轨迹、取消急停）；`/map` 腾讯位置服务代理模式地理底图 | ✅ 第 15 节、接口文档 I-19 / I-20 / I-22 |
| M4 B/S 管理台 | `/admin` + `/api/admin/*`（账号绑定、令牌轮换、严格模式、设备启停、场地标定、任务监控、审计），三份 JSON 台账原子写 + 热加载 | ✅ 第 16 节、接口文档 I-21 |

**新增入口**：`/` 控制台（场地底图 + 任务面板）、`/view` 只读俯视图（底图 + 只读任务进度）、`/map` 合规地理底图、`/admin` 管理台。
**自测**：`D:\miniconda\python.exe server/test_m3m4.py`（43 项全通过，假 dispatch 不下发真实命令，结束自动恢复台账）。
**合规红线**：默认底图是本地自绘矢量场地，不加载在线瓦片；真实地理底图只用合规服务商且页面不写 key；未校准 GCJ-02 基准点时不输出任何经纬度。

**当前第一优先级**：把 M2 的令牌与选车页放到鸿蒙真机/模拟器上跑通（改局域网 IP + 防火墙放行）；M3/M4 是后端与 Web 层能力，APP 仍走 `/status` + `/command`，无需改动。

- [x] **APP UI 端**：工程 `D:\harmony-project\ohcar`（bundleName `edu.myb.carctrlcloud`，应用名「寻检车云控」，API 22 / HarmonyOS 6.0.2）。`@ohos.net.http` 轮询 `http://10.0.2.2:8000/status` + POST `/command`；module.json5 已声明 INTERNET 权限；无 TCP 长连接，无 STATE 流——状态靠轮询、命令靠同步请求响应。**2026-10-30 模拟器实测通过，全链路验收完成**（2026-11-07 按本节约定重写，替换原 `D:\DevEco\...\CarCtrlCloud`）

### APP 端工程说明（ohcar，2026-11-07）

| 文件 | 作用 |
|---|---|
| `entry/src/main/ets/common/CarApi.ets` | HTTP 封装：`/devices` `/whoami` `/status` `/command`；**所有请求带 `X-Auth-Token` 与 `device_id`**（M2）；非 200 抛出带后端 `error` 文案的异常 |
| `entry/src/main/ets/pages/Index.ets` | 单页 UI：Canvas 场地俯视图（航位推算）+ 六项指标 + 方向键/速度/急停 + 指令记录 + **身份栏 / 选车按钮 / 设置面板令牌输入**（M2） |
| `entry/src/main/module.json5` | `ohos.permission.INTERNET`；`network_security_config` 放行 http 明文 |
| `AppScope/app.json5` | bundleName / 应用名 |

运行要点：

- 后端地址默认 `http://10.0.2.2:8000`（模拟器映射宿主 127.0.0.1）；**真机**改成电脑局域网 IP（`ipconfig` 查，随网络变化）
- 地址可在右上角 ⚙ 里修改，`PersistentStorage` 持久化，重启 APP 不丢
- **M2 绑定授权**：不带令牌＝访客，能看见两台车但只能控 1 号车；在 ⚙ 里粘贴身份令牌后按绑定关系获得对应车辆控制权，令牌与选中的车都会持久化。越权由服务端 403 裁决，APP 只如实显示（见 `docs/项目交接.md` 第 14 节）
- 命令按钮在请求在途时自动禁用，避免连点刷爆免费额度；`executed=true` 才是车端真的执行了
- 未配置签名时 `hvigorw assembleHap` 只产出 unsigned HAP，跑模拟器/真机前需在 DevEco `Signing Configs` 里自动签名
- [x] **Canvas 俯视图**：`web/car_cloud_view.html`，浏览器轮询 /status + 航位推算平滑（与 APP 同一套接口，零新增服务）。**2026-10-30 交付**
- [ ] 结课前：IoTDA 设备冻结、凭证回收（教学管理收尾）

## 附：本实例关键参数速查（敏感项见 config.json）

> 2026-11-07 更新：实例已从 `e4e0263312`（cn-east-3）切换为 **`9f149bee02`（cn-north-4）**，下表已同步。
> 旧实例 `e4e0263312` / `71b55ab7e9` 以及任何形如 `6aeed6cf…` 的 device_id **都已作废**，不要再从旧记录里抄。

| 参数 | 值 | 用在哪 |
|---|---|---|
| 区域 | **cn-north-4（华北-北京四）** | Region 构造 |
| project_id | **`01a190d8…`**（前 8 位，完整值见 config.json，不写进文档） | 请求头 X-Project-Id |
| instance_id | **9f149bee02** | 已隐含在专属域名中，请求里不带 |
| 应用侧端点 | https://9f149bee02.st1.iotda-app.cn-north-4.myhuaweicloud.com | app.py |
| 设备侧端点 | 9f149bee02.st1.iotda-device.cn-north-4.myhuaweicloud.com:8883 | car_mqtt_client_001/002.py |
| device_id | 6aeedf21cbb0cf6bb995299d_mycar_001 | 两侧（以 DEVICES-KEY-*.txt 为准） |
| AK/SK、设备密钥 | 见 config.json（仅本机） | — |

（AK/SK 属敏感凭证，只存本机 config.json，不进文档、不发学生。两个诊断脚本也于同日改为从 config.json 读取，不再硬编码任何密钥。）
