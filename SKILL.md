---
name: niimbot-label
description: 离线生成设备 ID 二维码标签，并经 USB 或 macOS 原生 BLE 桥检查或打印 NIIMBOT B1 Pro；适用于已确认的 300dpi、30×20mm 单份标签，不依赖厂商 App。
---

# NIIMBOT AI Printer

非官方技能，限定 NIIMBOT B1 Pro（型号4097）、300dpi、30×20mm、单份；只在 macOS 验证。其他品牌、型号和平台均未验证，不视为支持。安装及依赖见 [README](README.md)。USB 与 BLE 共用严格帧解析、身份核对和打印状态机。

## 工作顺序

1. 取得用户提供或已授权证据中的准确身份。`device_id`、`model`、`variant`、`hardware_revision`、`firmware`、`core` 是非空字符串；`width_mm=30`、`height_mm=20`、`copies=1`。真实任务放私有目录，示例 `examples/device.json` 全为虚构。不要把打印机序列号当标签 ID，不编造版本或质检结论。
2. 离线 render，查看预览确认内容、方向及留白。原图必须为原生1-bit 354×236；禁止缩放/抖动后再打印。默认使用 macOS 安装字体，可显式传 `--font` 和 `--id-font`；超宽报错，不截断身份。
3. 选择用户要求的 USB 或 BLE 路径。枚举/扫描只是定位，随后核对协议型号和完整序列号；不要仅按相同名称选择。厂商 App 必须退出以免争用；工具只检查、不终止它，也不要求安装它。
4. status 会发送握手/查询，不打印。仅在用户已授权本次打印且预览核对完成后 print --execute；预览本身不授权打印，不重复索要已有授权。
5. 检查独立的页ACK、页计数及结束ACK。协议完成不等于实物与扫码验收；没有现场证据标记未验证。任何失败不自动重连、重打、退纸或复位，可能已经部分出纸，应先核对报告和实物。

## 离线渲染

在技能根目录运行，`python3` 必须来自已激活的独立虚拟环境；`JOBDIR` 是当前授权私有目录的绝对路径。

```sh
python3 scripts/niimbot_usb.py render --job "$JOBDIR/identity.json" --output "$JOBDIR"
```

输出 `label.png`、4倍最近邻 `preview.png` 与 `metadata.json`（身份、字体、边界框、二维码内容与图像SHA256）。普通 QR 必须通过独立 OpenCV 解码并与完整 ID 完全一致；原生版式与字段限制见 [USB 协议](references/usb-protocol.md)。

## 自动识别纸卷尺寸

用户要求自动尺寸时，先读 [RFID 目录](references/stock-catalog.md)。为 USB/BLE 的 status 与 print 传同一 `--stock-catalog` 私有 JSON 路径；按完整纸卷型号编码和类型精确匹配。未知型号先请用户确认包装尺寸并记录来源，下次自动匹配；不根据容量推算尺寸。匹配到其他尺寸不代表支持该尺寸打印，必须检查 `stock_print_supported`。

## USB

先读 [USB 协议与边界](references/usb-protocol.md)。核对 VID:PID `3513:0002`、完整描述符序列号及确切端口；端口不是永久身份。打开前检查 `lsof` 和对应 tty 别名，忙或结果不明即停止，不抢占端口。实时核对型号4097、序列号、300dpi、合理头宽、盖关和有纸。

```sh
python3 scripts/niimbot_usb.py status --port "$PORT" --expect-serial "$PRINTER_SERIAL"
python3 scripts/niimbot_usb.py print --port "$PORT" --expect-serial "$PRINTER_SERIAL" --image "$JOBDIR/label.png" --execute
```

30×20mm 是用户显式耗材配置，不是 RFID 宽高查询结果。保留严格校验及原始事件，不将混杂/截断帧解释成协议变种。status 限15秒，print 限40秒，ACK/错误/超时分别记录。

## BLE

先读 [BLE 协议](references/ble-protocol.md) 和 [macOS 桥构建](references/ble-bridge.md)。用独立带用途声明的 App，通过 LaunchServices 和系统正常授权访问蓝牙；不修改系统 Python 或权限数据库。不使用裸 Python/Bleak 替代权限桥。

`HELPER` 必须为 `scripts/launch_ble_bridge.py` 的绝对路径，不能指向裸二进制。桥需先离线构建，工具不会自动安装依赖、构建、配对或修改固件。

```sh
python3 scripts/niimbot_ble.py scan --seconds 8 --helper "$HELPER"
python3 scripts/niimbot_ble.py status --device "$BLE_UUID" --expect-serial "$PRINTER_SERIAL" --helper "$HELPER"
python3 scripts/niimbot_ble.py print --device "$BLE_UUID" --expect-serial "$PRINTER_SERIAL" --image "$JOBDIR/label.png" --execute --helper "$HELPER"
```

BLE 不打开 USB；macOS UUID 不是 MAC 地址。就绪后仍须协议身份核对。只使用已发现的 notify/writeWithoutResponse 特征，单帧不拆分、最大载荷至少58B、连续写入间隔至少10ms。启动限15秒，就绪后status限15秒、print限40秒。`written` 只代表本地队列接受，不是打印机ACK。

## 可选官方微信小程序码输入

用户明确要求小程序码时读 [输入与版式](references/miniapp-label.md)，使用 `scripts/render_miniapp_label.py`，只接受用户提供的官方原图及来源JSON。工具不联网取码、不读取secret、不生成临时登录码，不用普通QR冒充官方码。

```sh
python3 scripts/render_miniapp_label.py --job "$JOBDIR/identity.json" --output "$JOBDIR"
```

来源标记、授权端点、release路径、device_id和哈希检查只保证输入自洽，不是微信在线认证。必须由授权提供方确认真实来源；纸面微信扫码另验。不得将真实身份、序列号、UUID、业务网址、官方码原图或来源凭据写入公共仓库/发布包。

## 证据与交付

stdout JSON 可能含设备身份和完整协议/桥诊断，只保存在授权私有任务目录。打印机固件2.16、硬件2.01 的匿名历史观察见协议文档；芯片料号未证实，`chipset=null` 不得推断。

交付时说明尺寸/内容、页ACK、计数、结束ACK、实物与扫码状态。USB曾有使用者确认的纸面效果；BLE纸面及两条路径的纸上扫码未验证。离线/mock测试不代表本次打印成功。

```sh
python3 -m unittest discover -s tests -v
```
