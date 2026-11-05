# B1 Pro BLE：Python 适配层

`scripts/niimbot_ble.py` 复用 `niimbot_usb` 的严格帧解析、身份/纸张状态检查、原生栅格和单份打印状态机，**不调用 USB 打开函数或枚举USB端口**。不使用 Bleak，不修改系统Python，不启动 NIIMBOT App，不自动配对/重连/重印。

## 原生 helper 契约

按[桥说明](ble-bridge.md)构建私有App权限容器，显式传 `--helper` 为 `scripts/launch_ble_bridge.py` 的绝对路径；不要传裸二进制。启动器负责 LaunchServices 及私有管道；脚本不自动构建或安装。路径不合法或事件不匹配时失败，不能回退为裸Python蓝牙访问。

子进程命令约定：`HELPER scan --seconds 8`、`HELPER bridge --device UUID`。输入/输出均为一行一个JSON对象，stdout不应混入日志；stderr原样保存到诊断。

| 场景 | JSON契约 |
|---|---|
| 扫描 | `{"event":"discovered","device":"UUID","name":"名称或null","RSSI":-50}`，结束 `{"event":"complete"}` |
| 桥就绪 | `{"event":"ready","device":"UUID","name":"名称","service":"服务UUID","characteristic":"特征UUID","max_write":182}` |
| 写入请求 | stdin `{"op":"write","id":1,"hex":"封包十六进制"}` |
| 本地写入接受 | stdout `{"event":"written","id":1}`；这不是打印机ACK |
| 通知 | `{"event":"data","hex":"十六进制"}`；可分片或包含多个协议帧 |
| 错误/断连 | `{"event":"error","message":"说明"}`，或disconnected/disconnect事件、stdout EOF；立即失败 |

`ready` 必须显式提供 `notify=true`、`write_without_response=true`、`write_mode=without_response`，且在成功订阅通知后才发送。最大载荷必须来自 **without-response**，不是ATT MTU或with-response上限。Python核对设备UUID、已知服务/特征、明确属性及max_write，缺字段直接拒绝。

服务 `e7810a71-73ae-499d-8c15-faa9aef0c3f2`，特征 `bef8d6c9-9c21-4c9e-b632-bd58c1009f9f`。最大无响应载荷须≥58B，354px行=45B位图+6B行头+7B帧开销；一帧一次写入，**不拆帧、不合包**。Python把连续写入间隔限制为至少10ms；原生侧也必须正确等待CoreBluetooth无响应写入可用，不能替换为with-response或重复发送。

Python桥使用stdout读线程和队列；同步read支持超时、分片及粘包；write等待相同ID的written事件期间保留所有data，原始JSON/stdderr及协议hex均进最终JSON。B1 Pro固件2.16实测会在打印期间发送独立的 `00000000` 非帧通知：只有完整通知恰为4个零、传输缓冲为空且协议解析器处于帧边界时才单独记录到 `bridge_out_of_band`；不把它当ACK，也不丢弃帧内部的4个零。其他坏头/坏校验仍报错，不能泛化为丢弃任意损坏数据。未知事件、错ID、坏hex、断连、超时不自动重试。启动就绪限15秒；就绪后status限15秒、print限40秒，子进程退出清理另有短界限。退出只结束自己创建的helper，不终止NIIMBOT App，不发额外退纸/复位指令。

## 命令与证据层级

先经共享渲染器离线生成并核对原生 `label.png`；BLE脚本没有隐式渲染或改图。参考技能入口的scan/status/print命令。

- `scan_completed`：扫描事件流正常结束，只证明发现；`protocol_completed=false`。
- `bridge_ready`：已选定GATT链路；仍须协议型号4097及准确序列号核对。
- `protocol_completed`：status已完成查询，或print已完成共享打印流程。
- 页ACK、页计数、打印结束ACK单独报告；`physical_output`始终为`unverified`，纸上清晰度与扫码另验。
- 失败保留原始事件和 `possible_partial_output`，可能已有部分出纸或打印任务停留；不得自动重打。

报告可能含设备身份/UUID，留在授权私有任务目录。不要将真实序列号、UUID或任务内容写入可复用源码/测试。

## 固定来源与当前状态


[niimbluelib commit f451be49，bluetooth_impl.ts](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/client/bluetooth_impl.ts#L101)：notify先订阅，136行选择notify/writeWithoutResponse，162行无响应写入。任务与封包来源见 [USB协议](usb-protocol.md)。

2026-11-06，macOS B1 Pro 的通知、无响应写入和512B最大载荷已观察验证；固件2.16、硬件2.01。单份30×20mm任务页ACK、页计数1及结束ACK通过，路径没有打开USB。BLE纸面清晰度及纸上扫码未验证。独立4零通知处理仅适用于文中明确的帧边界，不容忍一般损坏帧。芯片/SoC型号未知。
