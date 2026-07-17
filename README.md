# astrbot_plugin_seedance_video

AstrBot 的 Seedance 2.0 视频生成插件。

## 配置

在 AstrBot 插件配置中填写 `api_key`。API Key 请从 Seedance 控制台创建，不要写入代码或提交到 Git。

## 命令

```text
/seedance 一只猫在霓虹城市冲浪
/seedance 一辆跑车驶过雨夜街道 | duration=8 | aspect_ratio=9:16 | resolution=1080p
/seedance 产品镜头缓慢推进 | image=https://example.com/first.jpg
```

使用 `|` 分隔提示词和参数。支持 `model`、`duration`、`aspect_ratio`、`resolution`、`audio`、`image`（1-2 个 URL，以逗号分隔）。

插件使用异步任务轮询，不需要公网 Webhook 地址；任务完成后会发送视频消息。

也可以直接发送一张自拍，并在同一条消息中输入命令和提示词：

```text
/seedance 让照片中的人物自然转身，微笑看向镜头，头发和衣服随微风摆动
```

插件会自动把聊天图片作为首帧，使用提示词控制动作。若平台无法提供图片的公开 URL，可使用 `image=https://...` 手动指定公开图片地址。

## 体积优化

Seedance API 当前没有提供码率或编码器参数。需要较小文件时，建议把默认配置设为：`resolution=480p`、`duration=4`、`generate_audio=false`。也可以直接告诉 Bot：“生成小体积视频”，工具会按低分辨率、短时长和关闭音频处理。

## 让 Bot 自动调用

插件同时注册了 LLM Tool：`seedance_generate_video`。开启 AstrBot 的工具调用后，Bot 可以先调用 omnidraw 的生图或自拍工具，再把返回的图片 URL 传给 Seedance 工具生成视频。

建议在 Bot 的系统提示词中加入：

```text
当用户要求“先生成图片/自拍，再做成视频”时，必须按下面顺序调用：

1. 调用 OmniDraw 的 `generate_selfie`，设置 `return_result=true`，不要直接把图片下发后再猜图片地址。
2. 解析工具返回 JSON 中的 `images` 列表，取真实图片 URL。
3. 调用 `seedance_generate_video`，把这个 URL 作为 `image_url`，并把用户的动作写入必填的 `prompt`。

这样 Seedance 才会使用刚刚生成的自拍作为第一帧，而不是回退到插件配置中的默认人设图。
```

如果图片是刚刚由 AstrBot 或 omnidraw 生成的：直接回复那条图片，发送下面的命令即可，不需要复制图片地址：

```text
/seedance 让自拍中的人物眨眼、挥手，然后自然地向镜头走近
```
