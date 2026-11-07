# RFID 纸卷型号与尺寸匹配

USB/BLE 的 status 和 print 增加可选 `--stock-catalog /absolute/private/stock-catalog.json`。
身份校验完成后查询 RFID（1A/01 → 1B），按完整 barcode 与 consumables_type 精确匹配；
不使用打印机名称、纸卷序列号、剩余数量或容量推算宽高。status 查询不打印。

首次使用在私有任务目录创建 `{"version":1,"stocks":[]}`，运行带参数的 status，查看
`printer.rfid.barcode` 和 `consumables_type`。请用户从包装、测量或厂家资料确认宽高后，
在这个私有 JSON 的 stocks 中添加记录。以下全为虚构示例，不是可直接使用的产品目录：

```json
{
  "version": 1,
  "stocks": [{
    "barcode": "DEMO-SKU",
    "consumables_type": 1,
    "width_mm": 30,
    "height_mm": 20,
    "source": "用户确认包装宽30mm、走纸方向高20mm（填写真实确认依据）"
  }]
}
```

后续使用同一目录文件即可自动识别已确认纸卷。只记录 SKU、类型、尺寸和依据，
不将完整 RFID、纸卷 UID/序列号、真实设备报告上传到公共仓库。

匹配成功报告 stock_mm、stock_source；未命中或无 RFID 时 stock_mm=null。
损坏/截断 RFID、重复条目、非法尺寸、缺少确认依据均报错，不猜测。
未传目录的 status 同样报告未知尺寸，不再输出误导性的固定30×20mm。

**尺寸识别与打印兼容性分别报告。** 已知其他尺寸可识别，但只有30×20mm的
stock_print_supported=true；当前渲染/打印版式仍只验证该尺寸。带目录的 print
在未知/未验证尺寸时禁止发送打印设置命令。保留既有显式30×20mm工作流；
使用自动识别时，status 与 print 必须传同一目录，print 会再次读取 RFID，防止换卷。
打印仍须核对预览及已有的本次打印授权；此功能不自动打印、不重试。

协议结构参考固定上游：
[RFID parser](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/packets/packet_parser.ts)、
[big-endian reader](https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/src/packets/data_reader.ts)。
