"""
多账号连通性对比（诊断工具）
=====================================================
用途：同一套「正确用法」下，横向对比多个 IAM 账号的 AK/SK 谁能调通 IoTDA。
      正确用法 = 实例专属应用侧端点 + BasicCredentials + derived_predicate + with_region + 不带 instance_id
      判定接口 = ListDevices（只读，不改任何资源）

凭证从哪来（本脚本不再硬编码任何 AK/SK）：
  1) 默认：只测同目录 config.json 里的那一套；
  2) 要对比多套：设环境变量 IOTDA_ACCOUNTS，值是 JSON 数组，每项 [名称, AK, SK]，例如

     Windows cmd:
       set IOTDA_ACCOUNTS=[["主账号","HPUAXXXX","abc..."],["子用户","HPUAYYYY","def..."]]
       D:\\miniconda\\python.exe test_accounts.py

     PowerShell:
       $env:IOTDA_ACCOUNTS='[["主账号","HPUAXXXX","abc..."]]'

历史说明：旧版本的 ACCOUNTS 里写死了 4 组老师账号的明文 AK/SK（含主账号），
          2026-11-07 已移除。删除前实测确认这 4 组当时**仍然有效**，
          属于真实可用的泄露凭证，正确处置见 README / 操作手册「安全与收尾管理」一节。
"""
import json
import os
from pathlib import Path

from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkiotda.v5 import IoTDAClient, ListDevicesRequest

CFG = json.loads((Path(__file__).with_name("config.json")).read_text(encoding="utf-8"))
PROJECT_ID = CFG["project_id"]
REGION_ID = CFG["region"]
ENDPOINT = CFG["endpoint"]


def load_accounts():
    """优先读环境变量里的多账号列表，否则退回 config.json 的单账号"""
    raw = os.environ.get("IOTDA_ACCOUNTS", "").strip()
    if raw:
        try:
            items = json.loads(raw)
            accs = [(str(a[0]), str(a[1]), str(a[2])) for a in items]
            if accs:
                print(f"（凭证来源：环境变量 IOTDA_ACCOUNTS，共 {len(accs)} 套）")
                return accs
        except Exception as e:
            print(f"⚠ IOTDA_ACCOUNTS 解析失败（{type(e).__name__}: {e}），改用 config.json")
    print("（凭证来源：config.json，单账号）")
    return [("config.json 账号", CFG["ak"], CFG["sk"])]


def test(name: str, ak: str, sk: str) -> bool:
    try:
        credentials = BasicCredentials(ak, sk, PROJECT_ID)
        credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
        client = (
            IoTDAClient.new_builder()
            .with_credentials(credentials)
            .with_region(Region(REGION_ID, ENDPOINT))
            .build()
        )
        req = ListDevicesRequest()
        req.limit = 5
        resp = client.list_devices(req)
        devices = list(resp.devices or [])
        ids = ", ".join(d.device_id.split("_")[-1] + "=" + str(d.status) for d in devices)
        print(f"  ✅ {name}: HTTP 200, 设备[{ids}]")
        return True
    except exceptions.ClientRequestException as e:
        print(f"  ❌ {name}: {e.status_code} {e.error_code}")
        return False
    except Exception as e:
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    print("多账号连通性对比（正确 SDK 用法 + 实例专属端点，只读）")
    print(f"region={REGION_ID}  instance_endpoint={ENDPOINT}\n")
    results = {name: test(name, ak, sk) for name, ak, sk in load_accounts()}
    print("\n汇总：", " / ".join(f"{k}={'通' if v else '不通'}" for k, v in results.items()))
