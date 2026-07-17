"""AstrBot plugin for Seedance 2.0 asynchronous video generation."""

import asyncio
import json
import re
import tempfile
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
        self.video_retention_days = max(0, int(self.config.get("video_retention_days", 3)))
        self.video_cache_dir = Path(__file__).with_name("video_cache")
        self._cleanup_video_cache()

    def _cleanup_video_cache(self) -> None:
        if self.video_retention_days <= 0:
            return
        try:
            self.video_cache_dir.mkdir(parents=True, exist_ok=True)
            cutoff = __import__("time").time() - self.video_retention_days * 86400
            for path in self.video_cache_dir.glob("seedance_*.mp4"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    logger.info("[Seedance Cleanup] removed expired video=%s", path)
        except Exception as exc:
            logger.warning("[Seedance Cleanup] failed: %s", exc)

    def _profile_image_url(self) -> str:
        """Return the first public image URL from the active persona."""
        persona_config = self.config.get("persona_config", self.config)
        active_id = str(persona_config.get("active_persona_id", persona_config.get("active_profile_id", "default"))).strip()
        profiles = persona_config.get("profiles", [])
        if isinstance(profiles, dict):
            profiles = [dict(value, id=key) if isinstance(value, dict) else {} for key, value in profiles.items()]
        if not isinstance(profiles, list):
            return ""
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = str(profile.get("id", profile.get("profile_id", ""))).strip()
            if profile_id != active_id:
                continue
            refs = profile.get("persona_ref_image", profile.get("reference_images", []))
            if isinstance(refs, str):
                refs = [line.strip() for line in refs.splitlines() if line.strip()]
            if isinstance(refs, list):
                for ref in refs:
                    if isinstance(ref, str) and ref.startswith(("http://", "https://")):
                        return ref
        return ""

    def _profile_base_prompt(self) -> str:
        persona_config = self.config.get("persona_config", self.config)
        active_id = str(persona_config.get("active_persona_id", persona_config.get("active_profile_id", "default"))).strip()
        profiles = persona_config.get("profiles", [])
        if isinstance(profiles, dict):
            profiles = [dict(value, id=key) if isinstance(value, dict) else {} for key, value in profiles.items()]
        if isinstance(profiles, list):
            for profile in profiles:
                if isinstance(profile, dict) and str(profile.get("id", profile.get("profile_id", ""))).strip() == active_id:
                    return str(profile.get("persona_base_prompt", "")).strip()
        return ""

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
        prompt: str = "",
        image_url: str = "",
        duration: int = 5,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        model: str = "",
        generate_audio: bool = True,
    ) -> str:
        """调用 Seedance 生成视频。

        当用户想把一张图片、自拍或其他参考图制作成视频时调用。若上一步图片工具返回了图片 URL，必须传入 image_url。prompt 写视频中的动作、镜头和画面要求。没有图片时生成文生视频。
        """
        logger.info(
            "[Seedance Tool] start prompt=%s image_url=%s duration=%s aspect_ratio=%s resolution=%s",
            bool(prompt), bool(image_url), duration, aspect_ratio, resolution,
        )
        raw_message = str(getattr(event, "message_str", "") or "")
        if duration == 5:
            duration_match = re.search(r"(?:duration|时长)\s*[=:：]?\s*(\d+)\s*(?:秒|s)?|\b(\d+)\s*秒", raw_message, re.I)
            if duration_match:
                duration = int(duration_match.group(1) or duration_match.group(2))
        if resolution == "720p":
            resolution_match = re.search(r"\b(480p|720p|1080p|4k)\b", raw_message, re.I)
            if resolution_match:
                resolution = resolution_match.group(1).lower()
        if generate_audio and re.search(r"不要音频|关闭音频|无音频|不要声音|静音", raw_message):
            generate_audio = False
        logger.info("[Seedance Tool] parsed message options duration=%s resolution=%s audio=%s", duration, resolution, generate_audio)
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = getattr(event, "message_str", "") or ""
            prompt = prompt.replace("用seedence", "").replace("用seedance", "").strip(" ，,。")
        if not prompt:
            return "缺少视频提示词，请说明想让画面中的人物或物体做什么。"
        persona_prompt = self._profile_base_prompt()
        if persona_prompt:
            prompt = f"{persona_prompt}\n动作与镜头要求：{prompt}"
        logger.info("[Seedance Tool] final prompt=%s", prompt)
        if not self.api_key:
            return "Seedance API Key 未配置。"
        extracted_images = self._extract_image_urls(event)
        profile_image = self._profile_image_url()
        image_source = "tool_parameter" if image_url else ("message_or_reply" if extracted_images else ("active_persona" if profile_image else "none"))
        image_url = image_url or (extracted_images or [""])[0] or profile_image
        logger.info("[Seedance Tool] selected image=%s source=%s url=%s", bool(image_url), image_source, image_url or "-")
        duration = min(15, max(4, int(duration)))
        input_data: dict[str, Any] = {
            "prompt": prompt,
            "generation_type": "image-to-video" if image_url else "text-to-video",
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "generate_audio": bool(generate_audio),
            "watermark": False,
        }
        logger.info("[Seedance Tool] input mode=%s profile_image=%s", "image-to-video" if image_url else "text-to-video", bool(image_url))
        if image_url:
            input_data["image_urls"] = [image_url]
        selected_model = model or str(self.config.get("model", "seedance-2-0"))
        logger.info(
            "[Seedance Tool] request model=%s duration=%s aspect_ratio=%s resolution=%s audio=%s image_count=%s",
            selected_model, duration, aspect_ratio, resolution, bool(generate_audio), len(input_data.get("image_urls", [])),
        )
        task_id = await self._create({"model": selected_model, "input": input_data})
        logger.info("[Seedance Tool] submitted task_id=%s; background polling started", task_id)
        asyncio.create_task(self._finish_tool_task(event, task_id))
        return f"Seedance 视频任务已提交，任务 ID：{task_id}。生成完成后会自动发送视频，请稍候。"

    async def _finish_tool_task(self, event: AstrMessageEvent, task_id: str) -> None:
        started = asyncio.get_running_loop().time()
        try:
            logger.info("[Seedance Background] polling started task_id=%s", task_id)
            result = await self._poll(task_id)
            elapsed = asyncio.get_running_loop().time() - started
            video_url = self._find_video_url(result)
            logger.info("[Seedance Background] completed task_id=%s elapsed=%.1fs video_url=%s", task_id, elapsed, bool(video_url))
            if video_url:
                local_file = await self._download_video(video_url)
                if local_file:
                    await event.send(event.chain_result([Video.fromFileSystem(local_file)]))
                    logger.info("[Seedance Background] local video sent task_id=%s file=%s", task_id, local_file)
                else:
                    await event.send(event.chain_result([Video.fromURL(video_url)]))
                    logger.info("[Seedance Background] remote video sent task_id=%s", task_id)
            else:
                await event.send(event.plain_result("Seedance 任务完成，但没有返回视频地址。"))
                logger.error("[Seedance Background] completed without video URL task_id=%s payload=%s", task_id, result)
        except Exception as exc:
            logger.exception("Seedance background tool task failed")
            logger.error("[Seedance Background] failed task_id=%s error=%s", task_id, exc)
            await event.send(event.plain_result(f"Seedance 视频生成失败：{exc}"))

    async def _download_video(self, video_url: str) -> str:
        """Download the result before sending; some adapters cannot fetch CDN URLs."""
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(video_url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                    if response.status >= 400:
                        logger.warning("[Seedance Download] failed status=%s url=%s", response.status, video_url)
                        return ""
                    content = await response.read()
            self.video_cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="seedance_", suffix=".mp4", dir=self.video_cache_dir, delete=False) as output:
                output.write(content)
                path = output.name
            logger.info("[Seedance Download] downloaded bytes=%s file=%s", len(content), path)
            return path
        except Exception as exc:
            logger.warning("[Seedance Download] exception=%s url=%s", exc, video_url)
            return ""

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
        persona_image = self._profile_image_url()
        image_urls = options.get("image_urls") or message_images or ([persona_image] if persona_image else [])
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

        get_messages = getattr(event, "get_messages", None)
        if callable(get_messages):
            try:
                walk(get_messages())
            except Exception as exc:
                logger.debug("[Seedance Image] get_messages failed: %s", exc)
        walk(getattr(event, "message_obj", None))
        logger.info("[Seedance Image] detected public image URLs=%s", len(urls))
        return urls

    async def _create(self, payload: dict[str, Any]) -> str:
        logger.info("[Seedance API] POST /v1/videos/generations model=%s mode=%s", payload.get("model"), payload.get("input", {}).get("generation_type"))
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.base_url}/v1/videos/generations", headers=self._headers(), json=payload) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    logger.error("[Seedance API] create failed status=%s response=%s", response.status, data)
                    raise RuntimeError(self._error(data, response.status))
                nested = data.get("data", {}) if isinstance(data.get("data", {}), dict) else {}
                task_id = data.get("id") or data.get("task_id") or data.get("taskId") or nested.get("id") or nested.get("taskId")
                if not task_id:
                    logger.error("[Seedance API] create response missing task id payload=%s", data)
                    raise RuntimeError(f"API 未返回任务 ID：{data}")
                logger.info("[Seedance API] create success task_id=%s", task_id)
                return str(task_id)

    async def _poll(self, task_id: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=60)
        elapsed = 0
        attempt = 0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while elapsed < self.timeout:
                attempt += 1
                async with session.get(f"{self.base_url}/v1/tasks/{task_id}", headers=self._headers()) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        logger.error("[Seedance API] poll failed task_id=%s status=%s response=%s", task_id, response.status, data)
                        raise RuntimeError(self._error(data, response.status))
                status = str(data.get("status") or data.get("data", {}).get("status", "")).lower()
                logger.info("[Seedance API] poll task_id=%s attempt=%s elapsed=%ss status=%s", task_id, attempt, elapsed, status or "unknown")
                if status in {"completed", "succeeded", "success"}:
                    return data
                if status in {"failed", "error", "cancelled", "canceled", "timeout"}:
                    logger.error("[Seedance API] terminal failure task_id=%s status=%s payload=%s", task_id, status, data)
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
