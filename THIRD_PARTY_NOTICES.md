# 第三方来源与许可证

## niimbluelib

封包、B1 Pro 状态机与栅格行计数参考并派生自 MultiMote/niimbluelib。
固定版本：`f451be49d8e30046dee0a8f9d14747fe38f303c2`。

- 源码：https://github.com/MultiMote/niimbluelib/tree/f451be49d8e30046dee0a8f9d14747fe38f303c2
- 许可：https://github.com/MultiMote/niimbluelib/blob/f451be49d8e30046dee0a8f9d14747fe38f303c2/LICENSE
- 精确协议引用见 [USB 协议](references/usb-protocol.md) 与 [BLE 协议](references/ble-protocol.md)。本仓库不捆绑上游仓库。

以下保留上游完整 MIT 许可：

```text
MIT License

Copyright (c) 2024 MultiMote

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## niimbot-web-bluetooth

BLE 无响应写入、特征及节流方案另参考 iscarelli/niimbot-web-bluetooth，固定版本 `50801f40bc91a5bde782451c0e45b7e4e826f729`。保留来源和许可，不捆绑其仓库。

https://github.com/iscarelli/niimbot-web-bluetooth/tree/50801f40bc91a5bde782451c0e45b7e4e826f729

```text
MIT License

Copyright (c) 2026 iscarelli

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Python 运行时依赖

这些包通过 pip 独立安装，不包含在源码或技能 ZIP 内。最终安装版本以包自身的许可证与附带 notices 为准；尤其 OpenCV wheel 可含第三方组件。

| 包 | 许可证 | 上游许可链接 |
|---|---|---|
| Pillow | HPND（PIL Software License）及其 notices | https://github.com/python-pillow/Pillow/blob/11.3.0/LICENSE |
| qrcode | BSD-3-Clause | https://github.com/lincolnloop/python-qrcode/blob/v8.2/LICENSE |
| opencv-python-headless | wheel 打包 MIT；OpenCV Apache-2.0 及第三方 notices | https://github.com/opencv/opencv-python/blob/4.13.0.92/LICENSE.txt ; https://github.com/opencv/opencv/blob/4.13.0/LICENSE |
| pyserial | BSD-3-Clause | https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt |
| numpy | BSD-3-Clause 及其 notices | https://github.com/numpy/numpy/blob/v2.0.2/LICENSE.txt |
| PyYAML（可选开发工具） | MIT | https://github.com/yaml/pyyaml/blob/6.0.3/LICENSE |

## 字体、系统组件与商标

不分发字体、厂商固件、数据手册或 NIIMBOT 官方图像。渲染器使用用户系统中合法安装的字体；样图是虚构设备的栅格输出，不包含字体文件。macOS Foundation/CoreBluetooth、Swift 与 codesign 由用户系统提供。

NIIMBOT 和微信等名称用于描述兼容性或输入类型；本项目为非官方社区项目，没有声称获得厂商授权或认证。
