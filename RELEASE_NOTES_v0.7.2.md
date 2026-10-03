## v0.7.2 — VBR 码率解析修复

### 修复：VBR 码流的 `video_bitrate_kbps` 恒为 None

通过对五台真实设备的端点实测发现：DS-8632N-I8（192.168.10.9）的**每个通道**码率都显示 unknown，而 DS-FB2127 的 CBR 通道能正确显示 2048 kbps。

根因：`_parse_streaming_detail` 只查找 `videoAverageBitrate` 与 `constantBitRate`，这两个都是 **CBR 模式**字段。VBR 码流两者都不返回，设备实际给出的是：

```xml
<videoQualityControlType>VBR</videoQualityControlType>
<fixedQuality>90</fixedQuality>
<vbrUpperCap>16384</vbrUpperCap>
<vbrLowerCap>32</vbrLowerCap>
```

因此所有 VBR 通道的码率 sensor 都停留在 unknown。

**修复**：新增 `vbrUpperCap` 作为最后一级回退。它是设备 Web UI 中显示的"码率上限"配置值，并非瞬时码率，但这是 VBR 流最接近实际配置语义的数值，远好于显示未知。当 `constantBitRate` / `videoAverageBitrate` 存在时仍优先使用它们。

### 新增测试

`tests/test_v071_vbr_bitrate.py`（5 个用例），均使用实测抓取的真实 XML：

- VBR 通道正确解析出 `vbrUpperCap`
- 数值正确传播到 `channels[]` 列表
- CBR 通道行为不变（无回归）
- 两者同时存在时 CBR 优先
- 三种字段全缺时返回 None 而非崩溃

测试结果：**284 passed, 3 skipped**（较 v0.7.1 的 279 passed 新增 5 个）。

### 兼容性

未改动任何实体 key、unique_id 或翻译键，升级无需重新配置。

---

### 关于版本号

v0.7.1 的 tag 已指向不含本次修复的提交，因此本次发布为 v0.7.2。v0.7.1 本身仍然有效（包含 PTZ 能力检测修复与凭据编码校验恢复），只是不含 VBR 码率修复。

v0.7.0 已废弃（基于被截断的源码构建，删除了约 2500 行正常实现），请勿安装。
