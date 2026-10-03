"""v0.8 步骤1：修 `_parse_storage` 的存储状态误报。

真机实测（fixtures_v08/）暴露的缺陷：

  coordinator.py:700 的判定是
      if hdd_status and hdd_status not in ("normal", "ok"):
          status_aggregate = "exception"

  但海康的逐盘 status 有这些**正常**取值：
      ok        — 正常
      idle      — 备用盘/休眠（旧抓包 192_168_10_9 的 hdd5 实测为 idle）
      notexist  — 空盘位（176.64 的 hdd3 实测为 notexist）
      unformatted — 未格式化

  176.64（DS-8632N-I8，5 盘位）实测：
      hdd1=ok  hdd2=ok  hdd3=notexist  hdd4=ok  hdd5=ok
  → 3 块盘正常 + 1 个空槽，现有逻辑却把整机判成 exception，
    用户看到"存储异常"告警，但硬盘其实是好的。

修复契约：
  1. 返回新增 `hdds` 字段：逐盘明细列表（id/name/type/status/capacity_mb/free_mb）
  2. `notexist` 的盘位**跳过**：不计入 total_mb、不出现在 hdds 列表
  3. 健康白名单扩展为 {ok, normal, idle, unformatted} → 判为正常
  4. 其余未知值 → exception，并在 `status_detail` 暴露原始值便于诊断
  5. 新增 `hdd_error_count`：异常盘数量

测试全部使用仓库内真机抓包，不手写假数据、不访问网络。
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    _parse_storage,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _root(host: str) -> ET.Element:
    """读取指定设备的 storage 抓包。"""
    f = FIXTURES / f"{host.replace('.', '_')}__C_hdd__ISAPI_ContentMgmt_storage.xml"
    assert f.exists(), f"missing fixture {f.name}"
    return ET.fromstring(f.read_text(encoding="utf-8", errors="replace"))


# ── 1. 核心缺陷：空槽不得判成异常 ────────────────────────────────


def test_empty_slot_does_not_make_storage_exceptional():
    """176.64 有 5 盘位，hdd3=notexist（空槽），其余 ok。

    整机状态必须是 normal，不能是 exception。这是本轮修复的主缺陷。
    """
    st = _parse_storage(_root("10.18.176.64"))
    assert st["status"] == "normal", (
        f"空盘位 notexist 被误判为异常：{st['status']}"
    )


def test_empty_slot_excluded_from_capacity():
    """notexist 盘位 capacity=0，但不得作为一块"盘"计入明细。"""
    st = _parse_storage(_root("10.18.176.64"))
    # 5 个盘位中 hdd3 是空槽 → 只有 4 块物理存在的盘
    assert len(st["hdds"]) == 4, f"空槽应被跳过，实得 {len(st['hdds'])} 块盘"
    assert all(h["status"] != "notexist" for h in st["hdds"])
    ids = [h["id"] for h in st["hdds"]]
    assert ids == ["1", "2", "4", "5"], f"应排除 hdd3，实得 {ids}"


def test_capacity_sums_only_existing_disks():
    """总容量 = 4 × 9537536 MB（排除 notexist 的 hdd3，其 capacity=0）。"""
    st = _parse_storage(_root("10.18.176.64"))
    assert st["total_mb"] == 4 * 9537536


# ── 2. idle（备用盘）是正常态 ─────────────────────────────────────


def test_idle_status_counts_as_normal():
    """idle = 备用盘/休眠，是正常态，不得判为异常。

    实测来源：旧抓包 192_168_10_9 的 hdd5 为 idle。本轮 176.64 的
    hdd5 是 ok，故用构造 XML 锁定该分支（idle 是设备真实会返回的值）。
    """
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><hddName>hdd1</hddName><hddType>SATA</hddType>
        <status>idle</status><capacity>9537536</capacity>
        <freeSpace>0</freeSpace><property>RW</property></hdd>
      </hddList><workMode>quota</workMode></storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["status"] == "normal"
    assert st["hdds"][0]["status"] == "idle"


def test_unformatted_counts_as_normal():
    """unformatted（未格式化）是正常态，用户可自行格式化，不是故障。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><status>unformatted</status><capacity>1000</capacity>
        <freeSpace>0</freeSpace></hdd></hddList></storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["status"] == "normal"


# ── 3. 真实故障必须仍然报警 ───────────────────────────────────────


def test_error_status_still_raises_exception():
    """error 是真故障，必须判 exception 并计入 hdd_error_count。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><status>ok</status><capacity>1000</capacity>
        <freeSpace>0</freeSpace></hdd>
      <hdd><id>2</id><status>error</status><capacity>1000</capacity>
        <freeSpace>0</freeSpace></hdd>
      </hddList></storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["status"] == "exception"
    assert st["hdd_error_count"] == 1


def test_unknown_status_raises_and_is_exposed():
    """未知状态值必须判 exception，且原始值暴露到 status_detail 便于诊断。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><status>reparing</status><capacity>1000</capacity>
        <freeSpace>0</freeSpace></hdd></hddList></storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["status"] == "exception"
    assert st["hdd_error_count"] == 1
    # 原始值必须可见，否则用户不知道是什么故障
    assert any("reparing" in str(v) for v in st["status_detail"])


def test_mixed_states_only_error_counted():
    """ok + idle + notexist + error → exception，异常数=1，物理盘=3。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><status>ok</status><capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      <hdd><id>2</id><status>idle</status><capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      <hdd><id>3</id><status>notexist</status><capacity>0</capacity><freeSpace>0</freeSpace></hdd>
      <hdd><id>4</id><status>error</status><capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      </hddList></storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["status"] == "exception"
    assert st["hdd_error_count"] == 1
    assert len(st["hdds"]) == 3
    assert st["total_mb"] == 3000


# ── 4. 逐盘明细字段完整 ───────────────────────────────────────────


def test_per_hdd_detail_fields():
    """每块盘的明细必须含 id/name/type/status/capacity_mb/free_mb。"""
    st = _parse_storage(_root("10.18.176.64"))
    h = st["hdds"][0]
    for key in ("id", "name", "type", "status", "capacity_mb", "free_mb"):
        assert key in h, f"缺字段 {key}"
    assert h["id"] == "1"
    assert h["name"] == "hdd1"
    assert h["type"] == "SATA"
    assert h["status"] == "ok"
    assert h["capacity_mb"] == 9537536
    assert h["free_mb"] == 0


def test_single_hdd_nvr():
    """176.65（DS-7708N-I4，单盘 ok）：1 块盘，normal。"""
    st = _parse_storage(_root("10.18.176.65"))
    assert len(st["hdds"]) == 1
    assert st["status"] == "normal"
    assert st["total_mb"] == 953869
    assert st["hdd_error_count"] == 0


def test_third_nvr_has_manufacturer_field_ignored():
    """192.168.10.17 的盘带 <manufacturer>unknow</manufacturer>，不得干扰解析。"""
    st = _parse_storage(_root("192.168.10.17"))
    assert len(st["hdds"]) == 1
    assert st["status"] == "normal"
    assert st["total_mb"] == 1907729


# ── 5. 无盘设备（9 台 IPC）───────────────────────────────────────


def test_ipc_without_disks_returns_empty():
    """IPC 的 hddList 为空 → total_mb=None，hdds=[]，不得报错。"""
    st = _parse_storage(_root("10.18.176.18"))
    assert st["hdds"] == []
    assert st["total_mb"] is None
    assert st["status"] == "unknown"
    assert st["hdd_error_count"] == 0


def test_none_root_returns_empty_shape():
    """root=None（端点全部失败）→ 返回结构仍含新字段，不抛异常。"""
    st = _parse_storage(None)
    assert st["hdds"] == []
    assert st["hdd_error_count"] == 0
    assert st["status"] == "unknown"
    assert st["total_mb"] is None


# ── 6. 回归：V5 直连字段形态不受影响 ──────────────────────────────


def test_v5_direct_shape_still_works():
    """V5 的 totalCapacity/usedCapacity/freeCapacity 直连形态不能被破坏。"""
    xml = """<Storage><totalCapacity>2000000</totalCapacity>
      <usedCapacity>1234567</usedCapacity><freeCapacity>765433</freeCapacity>
      <status>normal</status></Storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["total_mb"] == 2000000
    assert st["used_mb"] == 1234567
    assert st["free_mb"] == 765433
    assert st["status"] == "normal"
    # 直连形态无逐盘列表
    assert st["hdds"] == []
    assert st["hdd_error_count"] == 0


def test_v4_bytes_shape_still_converts():
    """V4 NVR 的 <HDD><size>(bytes) 形态仍按 10^6 换算为 MB。"""
    xml = """<Storage><hddList>
      <HDD><id>1</id><size>2000396746752</size>
        <freeSize>1000000000000</freeSize><status>normal</status></HDD>
      </hddList></Storage>"""
    st = _parse_storage(ET.fromstring(xml))
    assert st["total_mb"] == 2000396.7
    assert st["free_mb"] == 1000000.0
    assert st["used_mb"] == 1000396.7
    assert len(st["hdds"]) == 1


def test_free_zero_does_not_break_used_calc():
    """v0.6.33 修过的坑：free=0 不能让 used_mb 变 None。"""
    st = _parse_storage(_root("10.18.176.64"))
    assert st["free_mb"] == 0
    assert st["used_mb"] == st["total_mb"], "free=0 时 used 应等于 total"
