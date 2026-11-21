"""
应用侧认证矩阵测试（诊断工具）
=====================================================
依据官方文档 https://support.huaweicloud.com/sdkreference-iothub/iot_10_10003.html
标准版/企业版实例正确用法：
  1. BasicCredentials + with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
  2. 自建 Region(region_id, 应用侧专属 endpoint) + .with_region()
  3. 请求里【不带】instance_id（带了反而 403）

凭证来源：同目录 config.json —— 本脚本不再硬编码任何 AK/SK。
测试矩阵：2 个候选端点 × 带/不带 instance_id = 4 组，全部用 ListDevices（只读）判定。

用途：当 app.py 报 403 IOTDA.000021 时跑一遍，能立刻区分是
      「端点错」/「签名方式错」/「多带了 instance_id」，不用靠猜。

历史说明：本脚本原先是 403 排错的现场留档，旧版本里写着老师的实例 ID 和
明文 AK/SK；2026-11-07 已把凭证改为从 config.json 读，实例 ID 改为随配置走。

运行（注意解释器）：
  D:\\miniconda\\python.exe test_api_auth.py
"""
import json
import sys
import traceback
from pathlib import Path

from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkiotda.v5 import IoTDAClient, ListDevicesRequest

CFG = json.loads((Path(__file__).with_name("config.json")).read_text(encoding="utf-8"))
AK = CFG["ak"]
SK = CFG["sk"]
PROJECT_ID = CFG["project_id"]
REGION_ID = CFG["region"]
INSTANCE_ID = CFG["instance_id"]

# 候选 endpoint：①实例专属应用侧地址（官方推荐，标准版）②通用共享版地址（基础版入口，标准版必失败）
ENDPOINTS = [
    ("实例专属应用侧", CFG["endpoint"]),
    ("通用应用侧", f"https://iotda.{REGION_ID}.myhuaweicloud.com"),
]


def make_client(endpoint: str) -> IoTDAClient:
    """官方指南：标准版实例用法"""
    credentials = BasicCredentials(AK, SK, PROJECT_ID)
    credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
    region = Region(REGION_ID, endpoint)
    return (
        IoTDAClient.new_builder()
        .with_credentials(credentials)
        .with_region(region)
        .build()
    )


def try_list_devices(label: str, endpoint: str, use_instance: bool):
    print(f"\n{'=' * 60}")
    print(f"[{label}] endpoint = {endpoint}")
    print(f"  ListDevices with instance_id={INSTANCE_ID if use_instance else '(不带)'}")
    client = make_client(endpoint)
    try:
        req = ListDevicesRequest()
        if use_instance:
            req.instance_id = INSTANCE_ID
        req.limit = 5
        resp = client.list_devices(req)
        devices = list(resp.devices or [])
        print(f"  ✅ 成功！HTTP 200，设备数：{len(devices)}")
        for d in devices:
            print(f"     - {d.device_id}  status={d.status}")
        return True
    except exceptions.ClientRequestException as e:
        print(f"  ❌ 失败 status={e.status_code} error_code={e.error_code}")
        print(f"     error_msg={e.error_msg}")
        print(f"     request_id={e.request_id}")
        return False
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("华为云 IoTDA 应用侧认证测试（凭证来自 config.json，只读）")
    print(f"AK={AK[:6]}…  project_id={PROJECT_ID[:8]}…  region={REGION_ID}  instance={INSTANCE_ID}")

    results = {}
    for label, ep in ENDPOINTS:
        for use_inst in (True, False):
            key = f"{label}+{'带instance' if use_inst else '不带instance'}"
            results[key] = try_list_devices(label, ep, use_inst)

    print("\n" + "=" * 60)
    print("汇总（正确结果应当是：实例专属 + 不带 instance 通过，其余三组失败）：")
    for k, v in results.items():
        print(f"  {'✅' if v else '❌'}  {k}")
    sys.exit(0 if results.get("实例专属应用侧+不带instance") else 1)
