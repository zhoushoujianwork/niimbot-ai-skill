# macOS 原生 BLE 桥与私有 App 启动器

桥只转发字节，不解释打印协议，不依赖 NIIMBOT App。需要 macOS、已配置的 swiftc 和 codesign。

## 构建与离线检查

在技能目录运行：

```sh
python3 -B scripts/build_ble_bridge.py
python3 -B -m unittest discover -s tests -p "test_ble_*.py"
"$HOME/.local/state/niimbot-label/ble-bridge/NIIMBOT BLE Bridge.app/Contents/MacOS/niimbot-ble-bridge" --self-test
```

构建不启动 App 或蓝牙。全部产物位于 `~/.local/state/niimbot-label/ble-bridge/`。
构建 JSON 返回 app、binary、launcher、plist。App 为 `NIIMBOT BLE Bridge.app`，
标识 `org.niimbot-ai-skill.ble-bridge`，带中文 `NSBluetoothAlwaysUsageDescription`、
`CFBundlePackageType=APPL` 和 `LSUIElement=true`，没有应用窗口。
Info 同时通过 `-sectcreate __TEXT __info_plist` 嵌入二进制，再对 App ad-hoc 签名并验证。
`--self-test` 直接运行二进制，检查身份、严格 hex 解码、整帧长度限制，绝不创建 CBCentralManager。

蓝牙操作通过 LaunchServices 启动带用途声明的 App，并由 macOS 正常授权。
不修改系统 Python、其他应用或权限数据库。直接运行裸二进制只用于不会创建
CBCentralManager 的 `--self-test`；实际 scan/bridge 使用启动器。

## 实际启动入口

```sh
./scripts/launch_ble_bridge.py scan --seconds 8
./scripts/launch_ble_bridge.py bridge --device DEVICE-UUID
```

Python HelperSession 的 `--helper` 应指向此启动器的绝对路径，不指向裸二进制。
默认 App 位于上述私有构建目录；可用 `NIIMBOT_BLE_APP` 指定绝对 `.app` 路径，
启动器验证包身份、权限说明和可执行文件。首次使用仍需 macOS 用户授权。

启动器执行 `open -n -g -W --stdin FIFO --stdout LOG --stderr LOG APP --args ...`。
每次创建独立 0700 临时目录、0600 FIFO 和日志；先以 RDWR 非阻塞打开 FIFO，
等原生进程写入私有 pid 文件（LaunchServices 已打开 stdin）后转发父进程输入。
父 stdin EOF 关闭最后一个 FIFO 写端，使原生 bridge 完成队列后退出。
App stdout/stderr 从常规日志按原始字节转发到启动器对应管道，不重写 JSON 事件。
SIGTERM、异常和正常退出均清理，只在私有 pid 文件权限及 ps 的完整可执行路径匹配时
终止本次原生进程，不使用 killall/pgrep；等待 open 退出并转发日志尾部，再删除本次临时目录。
启动等待上限 20 秒；原生 SIGTERM 等待 3 秒后可对已再次确认身份的本次 PID 发 SIGKILL。
保留参数 `--pid-file ABSOLUTE-PATH` 由启动器管理；原生端以 O_EXCL/O_NOFOLLOW 创建 0600 文件，
不额外输出启动事件。

## JSON 行接口

扫描默认 8 秒，范围 0 < seconds <= 15，也接受 `--seconds8`。
仅输出名称含 B1/NIIMBOT 的设备，UUID 是 macOS 原生 UUID，不是 MAC 地址：

```json
{"event":"discovered","device":"UUID","name":"B1 Pro","rssi":-55}
{"event":"scan_complete"}
```

bridge 只 retrievePeripherals 指定 UUID，不自动扫描、重连或重发。
连接、服务发现和通知启用合计最多 15 秒。固定服务为
`e7810a71-73ae-499d-8c15-faa9aef0c3f2`，运行时发现并核对特征
`bef8d6c9-9c21-4c9e-b632-bd58c1009f9f` 的 notify 与 writeWithoutResponse 属性。
使用 `.withoutResponse` 的实时最大写长度，低于 58 字节即拒绝连接准备。
通知实际开启后输出：

```json
{"event":"ready","device":"UUID","name":"B1 Pro","service":"SERVICE-UUID","characteristic":"CHAR-UUID","notify":true,"write_without_response":true,"write_mode":"without_response","max_write":180}
```

180 仅示例。stdin 一行一个完整应用帧：

```json
{"op":"write","id":1,"hex":"00abff"}
```

id 为整数，未完成请求不得重号；hex 非空、偶数长度、仅十六进制字符。
单帧不得超过 max_write，超出直接报错，绝不分块。等待队列最多 64 请求。
每请求只调用一次 `writeValue(..., .withoutResponse)`，遵守 `canSendWriteWithoutResponse`
和 `peripheralIsReady` 回调，并在原生侧保证至少 10ms 的发送间隔。
流控等待最多 15 秒。调用完成即输出 `event:written,id`，它只代表帧已交给本地
CoreBluetooth 队列，**不代表无线 ACK、打印机协议 ACK 或打印成功**。

每次通知输出 `event:data,hex`，保留通知边界；Python 原有流解析器处理分片或多帧。
无请求、通知或本地写入达 120 秒即超时。断开、授权拒绝、非法请求、超时或系统错误
输出 `event:error,message` 并非零退出。App 正常/错误退出状态不保证由 open 原样传递，
上层必须处理 JSON error 和管道关闭，不仅依赖启动器退出码。

## 隔离构建与验证边界

设置绝对路径 `NIIMBOT_BLE_BUILD_DIR` 可将构建输出放到隔离目录。随后设置
`NIIMBOT_BLE_APP` 为该目录内的 `NIIMBOT BLE Bridge.app` 绝对路径再进行实际 BLE 操作。
测试只在显式设置构建目录时运行已构建二进制的自检，不触碰其他位置的现有 helper。

2026-11-06 的历史设备检查通过 LaunchServices 扫描、连接、notify、writeWithoutResponse
及单张协议完成；纸面和扫码仍未验证。CI 仅构建、签名、自检及模拟测试，不扫描或打印。
