"""AstrBot plugin for Seedance 2.0 asynchronous video generation."""

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_seedance", "astrbotvideogenerate", "Seedance 2.0 视频生成", "1.0.0")
class SeedancePlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}
        self.api_key = str(self.config.get("api_key", "")).strip() or self._read_local_key()
        self.base_url = str(self.config.get("base_url", "https://api.seedance2.ai")).rstrip("/")
        self.poll_interval = max(3, int(self.config.get("poll_interval", 5)))
        self.timeout = max(60, int(self.config.get("timeout", 900)))

    @staticmethod
    def _read_local_key() -> str:
        """Use .key.txt only as a local development fallback."""
        key_file = Path(__file__).with_name(".key.txt")
        try:
            return key_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @filter.llm_tool(name="seedance_generate_video")
    async def seedance_generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str,
        image_url: str = "",
        duration: int = 5,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
    ) -> str:
        """调用 Seedance 生成视频。

        当用户想把一张图片、自拍或其他参考图制作成视频时调用。若上一步图片工具返回了图片 URL，必须传入 image_url。prompt 写视频中的动作、镜头和画面要求。没有图片时生成文生视频。
        """
        if not isinstance(prompt, str) or not prompt.strip():
            return "缺少视频提示词，请说明想让画面中的人物或物体做什么。"
        if not self.api_key:
            return "Seedance API Key 未配置。"
        duration = min(15, max(4, int(duration)))
        input_data: dict[str, Any] = {
            "prompt": prompt,
            "generation_type": "image-to-video" if image_url else "text-to-video",
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "generate_audio": bool(self.config.get("generate_audio", True)),
            "watermark": False,
        }
        if image_url:
            input_data["image_urls"] = [image_url]
        task_id = await self._create({"model": self.config.get("model", "seedance-2-0"), "input": input_data})
        result = await self._poll(task_id)
        video_url = self._find_video_url(result)
        return video_url or "任务完成，但 API 没有返回视频地址。"

    @filter.command("seedance")
    async def generate(self, event: AstrMessageEvent):
        """用法: /seedance 提示词 | 可选参数见 README。"""
        if not self.api_key:
            yield event.plain_result("Seedance 插件尚未配置 API Key。请在插件配置中填写 api_key。")
            return

        text = event.message_str.removeprefix("/seedance").strip()
        message_images = self._extract_image_urls(event)
        if not text:
            yield event.plain_result("用法：/seedance 提示词\n示例：/seedance 一只猫在霓虹城市冲浪")
            return

        prompt, options = self._parse(text)
        image_urls = options.get("image_urls") or message_images
        try:
            duration = min(15, max(4, int(options.get("duration", self.config.get("duration", 5)))))
        except (TypeError, ValueError):
            duration = 5
        payload = {
            "model": options.get("model", self.config.get("model", "seedance-2-0")),
            "input": {
                "prompt": prompt,
                "generation_type": "image-to-video" if image_urls else "text-to-video",
                "duration": duration,
                "aspect_ratio": options.get("aspect_ratio", self.config.get("aspect_ratio", "16:9")),
                "resolution": options.get("resolution", self.config.get("resolution", "720p")),
                "generate_audio": options.get("audio", str(self.config.get("generate_audio", True))).lower() != "false",
                "watermark": False,
            },
        }
        if image_urls:
            payload["input"]["image_urls"] = image_urls[:2]

        yield event.plain_result("已提交 Seedance 视频任务，生成完成后会自动发送结果，请稍候。")
        try:
            task_id = await self._create(payload)
            result = await self._poll(task_id)
            video_url = self._find_video_url(result)
            if video_url:
                yield event.chain_result([Video.fromURL(video_url)])
            else:
                yield event.plain_result(f"任务已完成，但未找到视频地址：{json.dumps(result, ensure_ascii=False)[:1500]}")
        except Exception as exc:
            logger.exception("Seedance task failed")
            yield event.plain_result(f"Seedance 生成失败：{exc}")

    def _parse(self, text: str) -> tuple[str, dict[str, Any]]:
        parts = [part.strip() for part in text.split("|")]
        prompt = parts[0]
        opts: dict[str, Any] = {}
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = [x.strip() for x in part.split("=", 1)]
            if key in {"image", "image_urls"}:
                opts["image_urls"] = [x.strip() for x in value.split(",") if x.strip()]
            else:
                opts[key] = value
        return prompt, opts

    @staticmethod
    def _extract_image_urls(event: AstrMessageEvent) -> list[str]:
        """Extract images from this message and its reply/quote chain.

        This lets users reply to an image previously generated by another
        AstrBot plugin and turn that image into a video without copying URLs.
        """
        urls: list[str] = []
        visited: set[int] = set()

        def walk(value: Any, depth: int = 0) -> None:
            if value is None or depth > 4 or id(value) in visited:
                return
            visited.add(id(value))
            if isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, depth + 1)
                return
            if isinstance(value, str):
                return
            if value.__class__.__name__.lower() == "image":
                for field in ("url", "file", "src"):
                    image_url = getattr(value, field, None)
                    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
                        if image_url not in urls:
                            urls.append(image_url)
                        break
            for field in ("message", "chain", "content", "reply", "quote"):
                child = getattr(value, field, None)
                if child is not None:
                    walk(child, depth + 1)

        walk(getattr(event, "message_obj", None))
        return urls

    async def _create(self, payload: dict[str, Any]) -> str:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.base_url}/v1/videos/generations", headers=self._headers(), json=payload) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(self._error(data, response.status))
                nested = data.get("data", {}) if isinstance(data.get("data", {}), dict) else {}
                task_id = data.get("id") or data.get("task_id") or data.get("taskId") or nested.get("id") or nested.get("taskId")
                if not task_id:
                    raise RuntimeError(f"API 未返回任务 ID：{data}")
                return str(task_id)

    async def _poll(self, task_id: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=60)
        elapsed = 0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while elapsed < self.timeout:
                async with session.get(f"{self.base_url}/v1/tasks/{task_id}", headers=self._headers()) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(self._error(data, response.status))
                status = str(data.get("status") or data.get("data", {}).get("status", "")).lower()
                if status in {"completed", "succeeded", "success"}:
                    return data
                if status in {"failed", "error", "cancelled", "canceled", "timeout"}:
                    raise RuntimeError(self._error(data, status))
                await asyncio.sleep(self.poll_interval)
                elapsed += self.poll_interval
        raise TimeoutError(f"任务 {task_id} 超过 {self.timeout} 秒仍未完成")

    @staticmethod
    def _error(data: Any, status: Any) -> str:
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or data.get("data", {}).get("failed_reason") or f"HTTP {status}")
        return f"HTTP {status}: {data}"

    @staticmethod
    def _find_video_url(data: Any) -> str | None:
        if isinstance(data, str):
            return data if data.startswith(("http://", "https://")) and ".mp4" in data.lower() else None
        if isinstance(data, dict):
            for key in ("video_url", "url", "video"):
                if isinstance(data.get(key), str) and data[key].startswith("http"):
                    return data[key]
            for value in data.values():
                found = SeedancePlugin._find_video_url(value)
                if found:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = SeedancePlugin._find_video_url(value)
                if found:
                    return found
        return None
