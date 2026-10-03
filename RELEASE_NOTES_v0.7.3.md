## v0.7.3 — 修复 NVR 上大量实体缺失（死监听器）

用户实测反馈：存储、编码、码流、码率全部没有，NVR 只显示单网卡。

三个缺陷，全部在真机上复现，并且每个都先写了会失败的测试再动手修。

### 缺陷 1：coordinator 监听器被声明为 `async def`，因此从未执行

HA 的 `async_add_listener` 契约是 `Callable[[], None]`；`async_update_listeners()` 同步调用回调并丢弃返回值。传入 `async def` 回调只会产生一个**没人 await 的协程，函数体永不执行**。

6 个已注册监听器中有 5 个是 async：

| 文件 | 监听器用途 | 后果 |
|---|---|---|
| `sensor.py` | NIC 2 迟到注册 | 双网卡 NVR 丢失网卡 2 |
| `sensor.py` | 每通道编码/分辨率/帧率/码率 | 全部丢失 |
| `binary_sensor.py` | 每通道在线/录像/运动 | 全部丢失 |
| `camera.py` | 每通道相机实体 | **一个相机都没有** |
| `switch.py` | 每通道录像开关 | **一个开关都没有** |

关键在于 `__init__.py` **先**转发平台、**再**把首次刷新作为后台任务启动，所以 setup 时 `coordinator.channels` 是空的，这些监听器是注册上述实体的**唯一路径**。

实测对比（DS-7708N-I4，8 通道）：

| 平台 | setup 时刻 | 刷新后 |
|---|---|---|
| sensor | 22 | **79** |
| binary_sensor | 3 | **27** |
| camera | 0 | **8** |
| switch | 0 | **8** |

DS-8632N-I8（13 通道）：sensor 22 → **109**，camera 0 → **13**。

**修复**：去掉 `async` —— 五个函数体内都没有任何 `await`。已用**变异测试**验证（移除一次性守卫后测试确实失败），确保测试有真实区分力而非空通过。

### 缺陷 2：存储传感器根本没有迟到监听器

`sensor.async_setup_entry` 用 `device_type in (NVR, DVR)` 门控 `storage_*`，但 setup 时 `device_type` 还是 `""`，门控直接拒绝，且**没有任何机制事后补注册**（NIC2 和每通道都有监听器，唯独存储没有）。

新增 `_make_storage_listener`，沿用相同的一次性模式。IPC 仍然不会得到存储实体（无硬盘，只会永远显示 unknown）。

实测存储值已正确：录像机 02 `931.5 GB`，录像机 01 `37256.0 GB`。

### 缺陷 3：NVR 通道号与码流号体系不匹配

`InputProxyChannelList` 的 id 是 `"1".."13"`，而 `/Streaming/channels` 用海康的 `{通道号}{码流号}` 编号（`"101"`、`"102"`、`"201"`）。查找时拿 `"2"` 去比 `"201"`，永不命中，导致**通道 2 及以后**的编码/分辨率/帧率/码率全部 unknown。通道 1 只是碰巧被遗留的 `first_channel` 回退掩盖了。

**修复**：新增 `_streaming_id_candidates`，先按原样匹配（IPC 的 id 本来就一致，且显式的 `"1"` 条目优先于巧合的 `"101"`），再尝试 `{通道号}01` 主码流约定。

实测 DS-8632N-I8：13 个通道中 **11 个**现在能正确解析编码 + 分辨率 + 码率。

### 设备侧数据缺口（非集成缺陷，如实说明）

- 录像机 01 的**通道 6、7** 仍为 unknown：设备只暴露了 `604`/`704`，没有 `601`/`701` 主码流
- 录像机 02（V4.1.18）的 `/Streaming/channels` 返回 **403**，该机无法提供编码/码率数据
- NVR 的 `InputProxyChannelList` 不含 `<online>`/`<recordStatus>`（实测各 0 次），在线状态靠每通道 `/status` 端点合并（生产代码已实现）

### 测试基础设施修复

- conftest 的 `SensorEntityDescription` 桩是普通类，但集成用 `@dataclass(frozen=True, kw_only=True)` 继承它，导致 import sensor.py 直接 `TypeError: unexpected keyword argument 'key'`。已改为真正的 dataclass。旧测试从未触发这个问题，因为它们用正则匹配源码文本、**从不真正 import 模块** —— 这也是 279 个测试全绿却漏掉上述真实缺陷的原因。
- 新增 `tests/test_v073_platform_listeners.py` 与 `tests/test_v073_late_entities_and_nvr_channels.py`：驱动真实的 `async_setup_entry`，配合忠实复刻 HA 监听器语义的 coordinator fake（会记录任何未被 await 的协程）。

**301 passed, 3 skipped**（v0.7.2 为 294 passed）。

### 兼容性

未改动任何实体 key、unique_id 或翻译键。升级后建议在 HA 中**重新加载**该集成（设置 → 设备与服务 → ⋯ → 重新加载），让监听器重新运行以补齐实体。
