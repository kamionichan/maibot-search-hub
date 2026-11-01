# 第三方许可说明

本插件按根目录 [LICENSE](LICENSE) 中的 MIT 条款分发。

## 移植的代码

| 本插件文件 | 来源 | 许可 |
|---|---|---|
| `hub/image.py` 中的图片清洗（`prepare_image`） | 前身项目 maibot-picimagesearch 的 `image_prepare.py`（其中又复用了 maibot-google-lens 的相关代码） | MIT，原文见下 |
| `hub/image_web.py` 中的受限会话、验证码识别、日志遮挡 | picimagesearch-api 的 `pic_client.py` | MIT，原文见下（与 maibot-picimagesearch 同一份） |
| `hub/image_web.py` 中 Yandex、Bing 的请求方式与响应解析，`hub/image_sources.py` 中 AnimeTrace、SauceNAO 的接口用法 | [PicImageSearch](https://github.com/kitUIN/PicImageSearch) | MIT，原文见下 |

安装包不内嵌 PicImageSearch、lxml、pyquery，也不依赖它们；只使用 Host 自带的 httpx、Pillow、jieba 和 maibot-plugin-sdk。

## maibot-picimagesearch / picimagesearch-api MIT 许可原文

```
MIT License

Copyright (c) 2026 maibot-google-lens and maibot-picimagesearch contributors

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

## PicImageSearch MIT 许可原文

```
MIT License

Copyright (c) 2021 kitUIN

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
