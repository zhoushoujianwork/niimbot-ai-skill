# B1 Pro USB 协议与验证边界

默认脚本USB串口，不依赖App。仅实现B1 Pro（4097），BLE 适配见单独文档；不声明其他型号支持。

## 固定公开来源

协议参考 [niimbluelib，MIT，commit f451be49](https://github.com/MultiMote/niimbluelib/tree/f451be49d8e30046dee0a8f9d14747fe38f303c2)：

- [B1 Pro 2.12 抓包](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/test/dumps.ts#L357)：Connect、型号、序列号、状态与DC03。
- [串口](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/client/serial_impl.ts#L39)：115200baud。
- [任务分派](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/print_tasks/index.ts#L83)与[D110MV4PrintTask](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/print_tasks/D110MV4PrintTask.ts#L19)：B1 Pro使用9B PrintStart和13B SetPageSize，不发普通B1的PageStart。
- [行生成](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/packets/packet_generator.ts#L254)与[计数函数](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/utils/utils.ts#L97)：按实际打印头宽度分三段，容纳不了行宽时回退总计数；本工具逐行run=1。


## 帧与流程

普通帧 `55 55 CMD LEN DATA XOR AA AA`，XOR为命令、单字节长度和所有数据异或；Connect前额外加03。收包严格校验，不跳过损坏帧、不清空队列。安装的POSIX pyserial.open默认调用_reset_input_buffer；本工具局部子类将该钩子覆盖为空操作，随后读取并保存已有raw事件，未修改安装文件。

| 操作 | 请求→响应 | 数据 |
|---|---|---|
| 连接 | C1→C2 | 01，期望03 |
| 协议 | A5→B5 | 01；data[11]*100+data[12]，300/301→4，≥302→5 |
| 型号/序列号 | 40→48/4B | 08/0B，型号1001、序列号ASCII |
| 头宽/分辨率 | DC→DE | 03，10B，offset4-5头宽u16BE，offset6=3为300dpi |
| 盖/纸 | DC→D9 | 04，13B，offset4=0盖关，5=0有纸 |
| 浓度/间隙纸 | 21→31/23→33 | 03/01 |
| 开始单张 | 01→02 | 00 01 00 00 00 00 00 01 00 |
| 页尺寸 | 13→14 | H u16BE、W u16BE、0001份、7个00 |
| 空白行 | 84，无ACK | 行号u16BE、01 |
| 位图行 | 85，无ACK | 行号u16BE、3段计数、01、45B位图 |
| 页结束 | E3→E4 | 01，ACK最长10秒 |
| 进度 | A3→B3 | 01，页数u16BE、打印%、走纸% |
| 任务结束 | F3→F4 | 01，仅打印模式 |

设置/打印ACK为01；B1 Pro 固件2.16的页尺寸响应0x14另实测支持0100，仅对此命令接受该扩展，不泛化到其他响应。00/DB错误立即停止。每行后读取反馈。打印总上限40秒（含握手），无自动重试。完成判据沿用上游页计数；打印/走纸百分比保留为观测值，完成时可能归零。分别报告页ACK、页计数/百分比、结束ACK；这些不能证明实物/扫码成功。失败不自动重发任务或发送额外退纸指令，报告possible_partial_output及最后进度；无结束ACK时可能仍有任务停留，先现场检查。

30×20mm为显式耗材配置，不是物理传感测量。300dpi原图354×236，打印头轴354；MSB-first、1黑，45B末6位为0。静态头宽567与上游抓包576不一致，运行时读DC03，拒绝非300dpi或不合理头宽。

RFID 1A→1B仅有条码、序列号、容量/用量、类型，无直接宽高字段；本工具不把容量当尺寸，也不依赖在线SKU目录。

## 匿名验证记录与边界

2026-11-06，macOS 上的 B1 Pro（USB VID:PID 3513:0002）查询型号4097、300dpi、打印头576点、盖关/有纸/RFID通过。固件2.16、硬件2.01；芯片型号未知。单份30×20mm任务页ACK、页计数1及结束ACK通过；USB纸面效果获得使用者确认，纸上扫码未验证。

后台厂商App并行访问时曾出现截断/混杂响应和额外重连，因此保持严格帧校验和进程占用检查。失败可能已部分出纸或任务未结束，应先查看报告与实物，不自动重印。上述是限定设备与版本的历史观察，不能替代当前设备检查。
