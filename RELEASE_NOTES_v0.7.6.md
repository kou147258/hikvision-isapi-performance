## v0.7.6 — NVR 相机画面、按码流分档命名、录像状态、通道名称

四组缺陷全部在真机上复现（DS-7708N-I4 DVR、DS-8632N-I8 NVR、DS-FB2127 / DS-FCN8027-VIK / DS-2DF8C832MX-ZDK 三台 IPC），并且每个都先写了会失败的测试再动手改。

### 1. NVR / DVR 相机画面不显示（IPC 正常）

`async_camera_image` 只对 `DEVICE_TYPE_NETWORK_VIDEO_RECORDER` 走代理端点，但本模块自己的 docstring 写的是「NVR / DVR」——**DVR 被漏掉了**，于是落到 IPC 直连分支。

在 DS-7708N-I4（`deviceType=DVR`）上实测：

```
/ISAPI/Streaming/channels/1/picture                    -> HTTP 400
/ISAPI/ContentMgmt/StreamingProxy/channels/1/picture   -> HTTP 503
/ISAPI/ContentMgmt/StreamingProxy/channels/101/picture -> HTTP 200, 30142 B JPEG
```

一条路径里其实有**两个** bug：缺 DVR 分支，以及 **id 格式错误**——录像机需要 `{通道号}01`，而不是 InputProxy 的裸通道号。

端点与 id 的选择已提取为 `_snapshot_path()`，无需网络即可单测。`async_camera_image` 还增加了 JPEG 魔数校验：部分固件会用 200 返回 XML 错误页（上面那个 503 响应体就是），当作图片返回会让 HA 卡片显示破损预览。

### 2. 「录像中」显示未在运行

你机群里的 NVR，`InputProxyChannelList` 和 `/InputProxy/channels/{id}/status` **都不含 `<recordStatus>`**（实测 0 次出现），且所有 `/ContentMgmt/Recording/*` 路径返回 404 —— **根本没有数据源**。

原代码把「取不到值」强制转成 `False`，实体再 `bool(...)`，于是开关显示「关」、二进制传感器显示「未在运行」，**断言了一个设备从未说过的事实**。

现改为三态（`_tri_record_status`）：`None` 表示未知，与运动检测传感器本来的行为一致。升级后这两项会显示 **unknown**（而不是误导性的「未在运行」）。

> 说明：录像状态在你的 NVR 上依然无值，这是**设备侧不提供**，不是集成能补的。若要真实录像状态，需要设备固件支持 `recordStatus` 或可用的 Recording 端点。

### 3. 每个码流按摄像机名称独立成实体（你提的需求）

编码 / 分辨率 / 帧率 / 码率 / 音频编码现在**按主码流、子码流分别生成**，并带摄像机名：

```
摄像机12 主码流 视频编码   = H.264      摄像机12 子码流 视频编码 = H.264
摄像机12 主码流 分辨率     = 3840x2160   摄像机12 子码流 分辨率   = 704x576
摄像机12 主码流 码率       = 16384 kbps  摄像机12 子码流 码率     = 512 kbps
摄像机13 主码流 视频编码   = H.265      摄像机13 子码流 视频编码 = H.264
```

实测你的录像机 01（13 通道）：每通道 11 个实体，共 143 个，其中 123 个有值。

档位**无法从字段读取**——两类设备的 `/Streaming/channels` 里都没有 `streamType` 之类的元素（已实测确认）。只能从码流 id 推导，而两类设备编号体系不同：

| 设备 | 码流 id | 归属通道字段 |
|---|---|---|
| NVR DS-8632N-I8 | `101` / `102` / `104` | `Video/dynVideoInputChannelID` |
| IPC DS-FB2127 | `1` / `2` / `3` | `Video/videoInputChannelID` |

`_channel_summary` 原先只读 `videoInputChannelID`，所以 NVR 上归属通道为 `None`、档位无从推导。现在两个字段都捕获。

两个设计取舍：

- **`translation_key` 刻意设为 `None`**。真实 HA 中 translation_key 优先于 `name`，而这些实体共享同一个 key，所有通道会渲染成同一个标签，摄像机名就永远出不来。
- **主码流 key 保持 v0.7.4 的原样**（`channel_{id}_<field>`），升级不会让你在实体注册表里已有的实体变成孤儿；子码流是新增的 `channel_{id}_sub_<field>`。

### 4. IPC 通道名与在线状态读错了 XML 标签

`_parse_streaming_channels_list` 查的是 `<name>` 和 `<online>`，但真实元素是 **`<channelName>`** 和 **`<enabled>`**（IPC 与 NVR 的抓包均逐字确认）。两个查找都返回 None，于是每台 IPC 都显示兜底名「Channel 1」、通道在线传感器恒为 False —— 而摄像机其实正在推流。

修复后实测：

```
IPC仓库     id=1    name='摄像机06'  online=True
IPC摄像机10 id=101  name='摄像机10'        online=True
IPC摄像机12   id=101  name='摄像机12'          online=True
```

这个 bug 的 docstring 里写的示例 XML 也是错的标签名 —— 这正是它能通过审查的原因，现已改为贴真实抓包结构。

### 测试

**350 passed, 3 skipped**（v0.7.3 为 318）。新增三个测试文件共 49 个用例，全部使用真机抓包 XML：

- `test_v074_snapshot_and_recording.py`（17）
- `test_v075_stream_tiers.py`（25）
- `test_v076_streaming_channel_names.py`（7）

### 已知但本版未修的问题

**IPC 的每个码流被当成独立通道**：实测单台 IPC 的 `channels` 列表会解析出 3–5 个「通道」（`101`/`102`/`103`/`104`），但它们的 `videoInputChannelID` 全是 `1`，即同一台摄像机。因此 IPC 上会生成 3–5 套重复的相机/开关/传感器实体。

修复它需要改动 `unique_id` 与快照路径取值，而后果（快照是否仍能取到、已有实体会不会变孤儿）无法离线验证，因此本版**不动**，留待能连真机验证时单独处理。

### 升级建议

升级后请在 HA 中**重新加载**集成（设置 → 设备与服务 → ⋯ → 重新加载）。新增的子码流实体会在加载后出现；「录像中」两项会从「未在运行」变为 unknown（这是正确的，因为设备未提供该数据）。
