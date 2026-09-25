# Hikvision ISAPI Performance

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

## Entities

| Entity | Type | Description |
|---|---|---|
| Model | sensor | text (from `/ISAPI/System/deviceInfo`) |
| Serial Number | sensor | text |
| Firmware Version | sensor | text |
| Device Status | sensor | "OK" / "Error" / etc. |
| CPU Usage | sensor | % |
| Memory Usage | sensor | % |
| Uptime | sensor | duration |
| Channel Count | sensor | int (count of detected channels) |
| Channel N Camera | camera | still snapshot from `/picture` |
| Channel N Recording | switch | on/off (per-channel) |
| Reboot Device | button | sends `PUT /ISAPI/System/reboot` |

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

## 实体

| 实体 | 类型 | 说明 |
|---|---|---|
| 型号 | sensor | 文本（来自 `/ISAPI/System/deviceInfo`）|
| 序列号 | sensor | 文本 |
| 固件版本 | sensor | 文本 |
| 设备状态 | sensor | "OK" / "Error" 等 |
| CPU 使用率 | sensor | % |
| 内存使用率 | sensor | % |
| 运行时长 | sensor | duration |
| 通道数 | sensor | int |
| 通道 N 通道 | camera | 快照（来自 `/picture`）|
| 通道 N 录像 | switch | 每通道开关 |
| 重启设备 | button | 发送 `PUT /ISAPI/System/reboot` |

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
