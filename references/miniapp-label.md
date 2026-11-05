# 官方微信小程序码标签

`scripts/render_miniapp_label.py` 只离线排版，不联网获取码、不读取凭据、不操作硬件或App。官方码由已获授权的上游流程取得；不能自行用普通二维码编码设备ID或网址冒充小程序码。来源JSON与摘要检查保证输入自洽，不等于本脚本重新向微信验证来源或完成纸面扫码。

## 输入

使用当前授权私有任务目录中的绝对路径 `identity.json`，保留通用身份字段：`device_id`、`model`、`variant`、`hardware_revision`、`firmware`、`core`，及30×20mm、copies=1。附加字段：

- `miniapp_code_path`：完整官方原图绝对路径，JPEG或PNG、正方形、至少180px。JPEG是有效原图格式，不能按普通QR图片格式假设拒绝。
- `miniapp_code_source`：来源JSON绝对路径。要求 `official_api_response=true`、`endpoint=https://api.weixin.qq.com/wxa/getwxacode`、`env_version=release`、`path` 为相对小程序页面及查询、`image_sha256` 为原图SHA256。可附 `image_size`，存在时必须相符。
- `website`：官方HTTPS源，不含用户信息、非标准端口、路径、query或fragment。印出完整主机名，省略scheme；ASCII主机名小写，Unicode域名按浏览器规则转IDNA，不截断子域或后缀。

页面查询必须恰有一个 `device_id` 且等于任务完整ID；不允许凭据类查询。来源文件不应带access token或其他秘密。本工具只抽取页面路径、端点、环境及文件摘要到metadata，不复制AppID/凭据或整个来源文件。不要将私有图片/身份/网址写入技能assets或可复用测试。

```sh
python3 scripts/render_miniapp_label.py --job "$JOBDIR/identity.json" --output "$JOBDIR"
```

可用 `--latin-font`、`--narrow-font`、`--chinese-font` 指向安装字体；默认Arial Bold、Arial Narrow Bold、Hiragino Sans GB.ttc index0。缺字或不合版需提供合适字体并重新预览，不改为缩小/截断文字。

## 固定原生版式

| 内容 | 原生坐标/字号 |
|---|---|
| 完整官方码图 | (162,6)，180×180，结束(342,186) |
| 型号 | (12,12)，22px粗体，最多138px宽 |
| 变体中文 | (12,42)，24px，描边1px，最多138px宽 |
| PCB修订 | (12,78)，20px，带PCB前缀 |
| 固件标题 | (12,114)，20px中文，描边1px |
| 完整应用版本 | (12,148)，22px窄粗体 |
| 完整设备ID | (12,186)，28px窄粗体，最多330px宽 |
| 官网完整主机名 | 水平居中，y209，22px粗体，最多330px宽 |

所有文字侧边至少12px，底部不超过230px；码图可用顶部6px边距。每个文字框必须与完整码图框及其他文字框不重叠，超宽/越界明确报错。核心固件只入metadata，不作为无标签小字打印；不生成质检通过之类结论。

完整码图先Lanczos缩放至180×180，再以阈值160转1-bit，不抖动、不裁切、不替换圆形中心Logo或其他特征。字体在原生字号绘制后阈值转1-bit；无后续布局缩放。

输出 `label.png`（1-bit，354×236，300dpi）、`preview.png`（最近邻4倍）、`metadata.json`，包含精确可见字符串、所有框/字号、core元数据、码源JSON与原图摘要、页面路径、`code_type=wechat_mini_program`。`wechat_physical_scan`和`physical_output`保持未验证，用户微信扫纸面是最终验收；不使用普通QR解码成功替代。

## 离线检查与状态

```sh
python3 -m unittest discover -s tests -p test_miniapp_label.py -v
```

测试使用隔离临时目录中的合成黑白特征图，明确不是实际官方码；只验证来源拒绝、摘要和设备ID匹配、全图保留、中文/完整ID及像素边界、原生输出、不重叠/越界。合成图不随发布包作为码样例。

本渲染器不生成小程序码，不包含 AppID、secret、服务端授权逻辑或临时登录码功能；只接受用户提供的官方码原图和来源文件。JSON 中的 official 标记是输入声明，哈希只证明文件一致性，不能证明来源真实。由授权提供方确认来源，纸面微信扫码由使用者另验。不得上传来源文件中的凭据或把来源检查描述成微信认证。
