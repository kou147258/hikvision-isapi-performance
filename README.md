# Hikvision ISAPI Performance

> ⚠️ **Polling interval — keep it ≥ 120 seconds.** Hikvision's ISAPI web layer aggressively rate-limits the per-user HTTP session. Short `scan_interval` (e.g. 30 s or 60 s) on multiple devices sharing one web account will trip the temporary lockout — *all* ISAPI calls from that user fail for several minutes, taking every sensor on every device offline at once. **Recommended: `scan_interval >= 120` (the default is 30 s; raise it explicitly in the integration's Options panel after install).**

<p align="right">
  🌐 <a href="#english"><b>English</b></a> · <a href="#简体中文">简体中文</a>
</p>

<a name="english"></a>

A Home Assistant custom integration for Hikvision NVRs and IP cameras over the **ISAPI HTTP API** — focused on **performance and health monitoring** (CPU / memory / uptime / reboot count / SD card write cycles / per-channel online + recording + motion / PTZ preset / reboot / recording on-off).

> **v0.6.0 — integration renamed.** The folder is
> `custom_components/hikvision_isapi_performance/`, the manifest
> domain is `hikvision_isapi_performance`, and the display name is
> "Hikvision ISAPI Performance".

This is the **performance / health-monitoring** variant — it focuses
on CPU / memory / uptime / reboot count / SD card write cycles /
per-channel online + recording + motion state / PTZ preset / reboot
/ recording on-off.

Distinct from the [hikvision-snmp](https://github.com/kou147258/hikvision-snmp)
integration which polls the SNMP MIB; this one talks to the same
devices over ISAPI (Hikvision's HTTP API), which gives access to
control surfaces (reboot, recording on-off, PTZ) and live snapshots
that SNMP doesn't expose.

## Features

- **Camera** — one entity per detected channel, still JPEG snapshots via `/ISAPI/Streaming/channels/{id}/picture`. Refreshed by the HA camera component on its standard schedule.
- **Sensors** — model, serial, firmware, device status, CPU usage, memory usage, uptime, channel count.
- **Switch** — per-channel recording on/off (`/ISAPI/ContentMgmt/InputProxy/channels/{id}/capabilities?recording=On|Off`).
- **Button** — reboot the device (`PUT /ISAPI/System/reboot`).
- **Service** — `hikvision_isapi_performance.ptz_goto_preset` to move a PTZ camera to a named preset.

## Installation

### HACS (recommended)

1. Install [HACS](https://hacs.xyz/).
2. HACS → Integrations → ⋯ → **Custom repositories** → add `https://github.com/kou147258/hikvision-isapi-performance` as **Integration**.
3. Refresh, find **Hikvision ISAPI Performance**, install.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/hikvision_isapi_performance/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Configure

1. **Settings → Devices & Services → Add Integration → Hikvision ISAPI Performance**.
2. Step 1 — enter the device IP, port (default 443), username, password, and whether to verify SSL (most Hikvision devices use self-signed certs, so leave this off).
3. The integration tests the connection by GETting `/ISAPI/System/deviceInfo`. On success, the entry is created and the coordinator's first refresh runs in the background.

> ⚠️ **Polling interval — keep it ≥ 120 seconds.** Hikvision's ISAPI web layer aggressively rate-limits the per-user HTTP session. A short scan interval (e.g. 30 s) on multiple devices sharing one web account will trip the temporary lockout, after which *all* ISAPI calls from that user fail for several minutes — taking every sensor on the device offline at once. **Recommended: `scan_interval >= 120`** (the default is 30 s; raise it explicitly in the integration's Options panel after install).

### Hikvision device prep

1. Log into the device web UI.
2. **Configuration → Network → Advanced Settings → Web Server** — ensure HTTPS is enabled (default).
3. **Configuration → System → User Management** — create a user with permissions to view live view + control PTZ (for the relevant features). The default `admin` user works for all of this.
4. **Configuration → PTZ → Preset** (PTZ cameras only) — add presets via the device UI. The integration can move to preset 1, 2, 3, etc. via the `ptz_goto_preset` service.

## Entities (v0.6.30)

The full list of entities this integration exposes. Channel N
sensors are emitted **per detected channel** (not just channel 1
anymore — v0.6.27). NIC 2 sensors are emitted **only if the
device has two physical network interfaces** (v0.6.23).

### Sensors (always-on)

| Entity | Key | Description |
|---|---|---|
| Model | `model` | text (`/ISAPI/System/deviceInfo`) |
| Serial Number | `serial_number` | text |
| Firmware Version | `firmware_version` | text |
| Firmware Release Date | `firmware_release_date` | text |
| Device Type | `device_type` | IPC / NVR / DVR |
| Device ID | `device_id` | UUID-style device ID |
| Device MAC | `device_mac` | hardware MAC from deviceInfo |
| Encoder Version | `encoder_version` | V4 firmware has it; V5 may omit |
| Device Status | `device_status` | OK / Error / Unknown |
| CPU Usage | `cpu_usage` | %, **suppressed on V4 firmware NVR/DVR** (firmware bug returns 0); see v0.6.27 |
| Memory Usage | `memory_usage_percent` | %, **v0.6.25 auto-converts V5 IPC's mixed KB/MB** |
| Memory Available | `memory_available_mb` | MB |
| Uptime | `uptime_hours` | duration |
| Time Sync Mode | `time_mode` | NTP / manual (v0.6.19) |
| Channel Count | `channel_count` | int (v0.6.30 fix — was filtered out by accident in v0.6.27) |
| Capability: Video Input Channels | `capability_video_input_channels` | from `/System/capabilities` (v0.6.28 diagnostic) |
| Storage Total / Used / Free / Usage | `storage_*_gb` | **NVR/DVR only** (v0.6.22 gating) |
| NIC 1 IP / Subnet / Gateway / MAC | `network_ip/subnet/gateway/mac` | first interface |
| NIC 1 MTU | `network_mtu` | bare integer (v0.6.27 dropped "B" suffix) |

### Sensors (per-channel, dynamic — v0.6.27)

For **every detected channel**, the integration emits:

| Entity | Key pattern |
|---|---|
| Channel N Video Codec | `channel_{N}_video_codec` |
| Channel N Resolution | `channel_{N}_video_resolution` |
| Channel N Frame Rate | `channel_{N}_video_frame_rate` (fps) |
| Channel N Bitrate | `channel_{N}_video_bitrate` (kbps) |
| Channel N Audio Codec | `channel_{N}_audio_codec` |
| Channel N Name | `channel_{N}_name` |

These need V5 firmware on the streaming endpoint; V4 NVR
firmware returns 4xx and they show "unknown". Re-evaluated on
**Reload** if you deleted any of them — they'll re-appear.

### Sensors (NIC 2 — only when device has 2 NICs, v0.6.23)

| Entity | Key |
|---|---|
| NIC 2 IP / Subnet / Gateway / MAC / MTU | `network_2_*` |

If your NVR has 2 NICs but you don't see these, **Reload** the
integration (Settings → Devices & Services → Hikvision ISAPI
Performance → ⋯ → Reload). Reload re-runs the listener pattern;
deleted entities re-register with the same unique_id.

### Binary sensors

| Entity | Key | Description |
|---|---|---|
| Device Online | `device_online` | connectivity class (v0.6.19) |
| Device Time Abnormal | `dev_time_abnormal` | PROBLEM class, ON when device clock is > 24 h off (v0.6.26, renamed v0.6.28) — detects dead CMOS battery |
| Memory Calibration Suspect | `mem_calibration_warn` | PROBLEM class, ON when memory numbers still look wrong after v0.6.25 normalisation (v0.6.29) |
| Channel N Online | `channel_{N}_online` | per-channel reachability |
| Channel N Recording | `channel_{N}_recording` | per-channel recording state |
| Channel N Motion | `channel_{N}_motion` | per-channel motion detection |

### Switches / Buttons / Cameras

| Entity | Key | Description |
|---|---|---|
| Channel N Camera | `camera_{N}` | still snapshot from `/picture` |
| Channel N Recording switch | `recording` | per-channel recording on/off |
| Reboot Device | `reboot` | sends `PUT /ISAPI/System/reboot` |
| PTZ Up/Down/Left/Right | `ptz_*` | PTZ buttons (v0.6.19, only when device supports PTZ) |

### Notes on what to keep

- **Keep** `device_online`, `device_status`, `cpu_usage`,
  `memory_usage_percent`, `memory_available_mb`, `uptime_hours`,
  `time_mode`, `channel_count`, `device_time_abnormal`,
  `mem_calibration_warn` — these are the always-on diagnostics.
- **Keep NIC 2 sensors** if you have a dual-NIC NVR.
- **Per-channel sensors**: keep those you actually use
  (codec / resolution for video dashboards, channel_name for
  friendly labels). The rest can be disabled in HA's entity
  registry.
- **Storage sensors** (NVR/DVR only): keep on NVR/DVR;
  disable on IPC (they always show "unknown" since IPC has
  no HDD).
- **PTZ buttons**: keep only on PTZ cameras.

## Services

### `hikvision_isapi_performance.ptz_goto_preset`

Move a PTZ channel to a named preset.

Fields:
- `device_id` — the target HA device
- `channel` — 1-based channel ID
- `preset` — preset number (1-256)

```yaml
service: hikvision_isapi_performance.ptz_goto_preset
data:
  device_id: abc123...
  channel: 1
  preset: 1
```

## Compatibility

- `aiohttp>=3.9.0,<4.0.0`
- Home Assistant 2025.4+
- Python 3.11+

Hikvision V5.x firmware is the primary target. V4.x and V7.x are
expected to work for the deviceInfo / status / channels / picture
endpoints; reboot and PTZ depend on the firmware supporting those
ISAPI routes.

## License

MIT © 2026 43457. See `LICENSE`.

---

<a name="简体中文"></a>

<p align="right">
  🌐 <a href="#english">English</a> · <a href="#简体中文"><b>简体中文</b></a>
</p>

# Hikvision ISAPI Performance（简体中文）

> ⚠️ **轮询间隔——请保持 ≥ 120 秒。** 海康 ISAPI web 层对单用户的 HTTP 会话限流比较激进。多台设备共用一个 web 账号 + 短 `scan_interval`（30 s / 60 s）极易触发临时封禁，封禁期间**该账号下所有设备的全部 ISAPI 调用都会失败几分钟**，所有传感器同时掉线。**建议 `scan_interval >= 120`**（默认是 30 s，安装后在集成的 Options 面板里手动调高）。

一个用于海康威视 NVR / IPC 的 Home Assistant 自定义集成，**通过 ISAPI HTTP 接口**（区别于 [hikvision-snmp](https://github.com/kou147258/hikvision-snmp) 那个走 SNMP）。支持摄像头快照、系统 sensor、通道录像开关、重启按钮和 PTZ 预置位服务。

## 功能

- **Camera 实体** — 每个检测到的通道一个实体，通过 `/ISAPI/Streaming/channels/{id}/picture` 拉快照
- **Sensor 实体** — 型号、序列号、固件版本、设备状态、CPU、内存、运行时长、通道数
- **Switch 实体** — 每通道录像开关（`/ISAPI/ContentMgmt/InputProxy/channels/{id}/capabilities?recording=On|Off`）
- **Button 实体** — 设备重启（`PUT /ISAPI/System/reboot`）
- **Service** — `hikvision_isapi_performance.ptz_goto_preset` PTZ 预置位

## 安装

#### HACS（推荐）

1. 安装 [HACS](https://hacs.xyz/)。
2. HACS → Integrations → ⋯ → **Custom repositories** → 添加 `https://github.com/kou147258/hikvision-isapi-performance`，类型选 **Integration**。
3. 刷新列表，找到 **Hikvision ISAPI Performance**，安装。
4. 重启 Home Assistant。

#### 手动安装

1. 复制 `custom_components/hikvision_isapi_performance/` 目录到 HA 的 `config/custom_components/` 下。
2. 重启 Home Assistant。

## 配置

1. **设置 → 设备与服务 → 添加集成 → Hikvision ISAPI Performance**。
2. 步骤 1 — 输入设备 IP、端口（默认 443）、用户名、密码，以及是否验证 SSL（大多数海康设备用自签名证书，关掉这个）。
3. 集成会 GET `/ISAPI/System/deviceInfo` 测试连接。成功后 entry 创建，coordinator 在后台开始首次 refresh。

> ⚠️ **轮询间隔——请保持 ≥ 120 秒。** 海康 ISAPI web 层对单用户的 HTTP 会话限流比较激进。多台设备共用一个 web 账号 + 短 scan_interval（比如 30 s）很容易触发临时封禁，封禁期间**该账号下所有设备的全部 ISAPI 调用都会失败几分钟**，所有传感器同时掉线。**建议 `scan_interval >= 120`**（默认是 30 s，安装后在集成的 Options 面板里手动改高）。

#### 海康设备端准备

1. 登录设备 web 界面。
2. **配置 → 网络 → 高级设置 → Web Server** — 确认 HTTPS 启用（默认就是）。
3. **配置 → 系统 → 用户管理** — 建一个用户，至少给"预览"和"控制 PTZ"权限。默认 `admin` 用户能搞定所有功能。
4. **配置 → PTZ → 预置位**（PTZ 摄像机才有）— 在设备 UI 上加预置位。集成可以通过 `ptz_goto_preset` service 移动到预置位 1、2、3...

## 实体（v0.6.30）

本集成暴露的完整实体清单。通道 N 类 sensor 是**每个检测到的通道**都生成一份（v0.6.27 开始）；网卡 2 类 sensor 只有**设备确实有 2 个物理网卡**才出现（v0.6.23）。

### 常驻 sensor

| 实体 | key | 说明 |
|---|---|---|
| 型号 | `model` | 文本（`/ISAPI/System/deviceInfo`）|
| 序列号 | `serial_number` | 文本 |
| 固件版本 | `firmware_version` | 文本 |
| 固件发布日期 | `firmware_release_date` | 文本 |
| 设备类型 | `device_type` | IPC / NVR / DVR |
| 设备 ID | `device_id` | UUID 风格 |
| 设备 MAC | `device_mac` | 来自 deviceInfo 的硬件 MAC |
| 编码器版本 | `encoder_version` | V4 有；V5 可能没有 |
| 设备状态 | `device_status` | OK / Error / Unknown |
| CPU 使用率 | `cpu_usage` | %。**V4 固件 NVR/DVR 屏蔽**（固件 bug 永远返回 0），见 v0.6.27 |
| 内存使用率 | `memory_usage_percent` | %。**v0.6.25 自动换算 V5 IPC 的 KB/MB 混合** |
| 内存剩余 | `memory_available_mb` | MB |
| 运行时长 | `uptime_hours` | duration |
| 时间同步模式 | `time_mode` | NTP / 手动（v0.6.19）|
| 通道数 | `channel_count` | int（v0.6.30 修复——v0.6.27 误伤了这个 key 导致永远显示未知）|
| 能力 — 视频输入通道数 | `capability_video_input_channels` | 来自 `/System/capabilities`（v0.6.28 诊断）|
| 存储总量/已用/剩余/使用率 | `storage_*_gb` | **仅 NVR/DVR**（v0.6.22 门控）|
| 网卡 1 IP / 子网 / 网关 / MAC | `network_ip/subnet/gateway/mac` | 第一块网卡 |
| 网卡 1 MTU | `network_mtu` | 纯数字（v0.6.27 去掉 "B" 后缀）|

### 每通道 sensor（动态生成 — v0.6.27）

**每个检测到的通道**都会生成以下 6 个 sensor：

| 实体 | key 模板 |
|---|---|
| 通道 N 视频编码 | `channel_{N}_video_codec` |
| 通道 N 分辨率 | `channel_{N}_video_resolution` |
| 通道 N 帧率 | `channel_{N}_video_frame_rate`（fps）|
| 通道 N 码率 | `channel_{N}_video_bitrate`（kbps）|
| 通道 N 音频编码 | `channel_{N}_audio_codec` |
| 通道 N 名称 | `channel_{N}_name` |

需要 V5 固件的 streaming endpoint；V4 NVR 固件返回 4xx，这些会显示"未知"。**Reload** 后会重新评估。如果你手动删过这些 entity，Reload 后会重新注册。

### 网卡 2 sensor（仅双网卡设备 — v0.6.23）

| 实体 | key |
|---|---|
| 网卡 2 IP / 子网 / 网关 / MAC / MTU | `network_2_*` |

如果你的 NVR 是双网卡但看不到这些，**Reload 集成**（设置 → 设备与服务 → Hikvision ISAPI Performance → ⋯ → Reload）。Reload 会重新跑 listener pattern；删掉的 entity 会用相同 unique_id 重新注册。

### Binary sensor

| 实体 | key | 说明 |
|---|---|---|
| 设备在线 | `device_online` | connectivity class（v0.6.19）|
| 设备时间异常 | `dev_time_abnormal` | PROBLEM class，设备时钟偏移 > 24 h 时 ON（v0.6.26 加，v0.6.28 重命名）—— 检测主板纽扣电池失效 |
| 内存换算疑似异常 | `mem_calibration_warn` | PROBLEM class，v0.6.25 归一化后内存数字仍异常时 ON（v0.6.29）|
| 通道 N 在线 | `channel_{N}_online` | 单通道可达性 |
| 通道 N 录像中 | `channel_{N}_recording` | 单通道录像状态 |
| 通道 N 运动检测 | `channel_{N}_motion` | 单通道运动检测 |

### Switch / Button / Camera

| 实体 | key | 说明 |
|---|---|---|
| 通道 N 通道 | `camera_{N}` | `/picture` 快照 |
| 通道 N 录像开关 | `recording` | 单通道录像开关 |
| 重启设备 | `reboot` | 发送 `PUT /ISAPI/System/reboot` |
| PTZ 上/下/左/右 | `ptz_*` | PTZ 按钮（v0.6.19，仅 PTZ 设备）|

### 保留建议

- **保留** `device_online` / `device_status` / `cpu_usage` / `memory_usage_percent` / `memory_available_mb` / `uptime_hours` / `time_mode` / `channel_count` / `device_time_abnormal` / `mem_calibration_warn` —— 这些是常驻诊断
- **保留** 网卡 2 sensor（如果你有双网卡 NVR）
- **每通道 sensor**：保留你实际用得到的（比如视频仪表盘的 codec / resolution，给通道起友好名字的 channel_name），其余在 HA 实体注册表里关掉
- **存储 sensor**（仅 NVR/DVR）：NVR/DVR 保留；IPC 关掉（IPC 没硬盘，永远显示未知）
- **PTZ 按钮**：只在 PTZ 摄像机上保留

## Service

### `hikvision_isapi_performance.ptz_goto_preset`

把 PTZ 通道移到指定预置位。

参数：
- `device_id` — 目标 HA 设备
- `channel` — 通道号（1-based）
- `preset` — 预置位编号（1-256）

```yaml
service: hikvision_isapi_performance.ptz_goto_preset
data:
  device_id: abc123...
  channel: 1
  preset: 1
```

## 兼容性

- `aiohttp>=3.9.0,<4.0.0`
- Home Assistant 2025.4+
- Python 3.11+

主目标海康 V5.x 固件。V4.x 和 V7.x 预计也能用 deviceInfo / status / channels / picture；reboot 和 PTZ 看具体固件支持。

## 许可证

MIT © 2026 43457. 详见 `LICENSE`。
