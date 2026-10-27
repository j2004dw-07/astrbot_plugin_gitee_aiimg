"""Agnes Image generation, editing, and multi-image composition backend."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from astrbot.api import logger

from .image_format import guess_image_mime_and_ext
from .output_spec import (
    OutputIntent,
    aspect_ratio_from_size,
    normalize_aspect_ratio,
    normalize_exact_size,
)
from .retry_config import parse_max_retries
from .sora2_video_service import _parse_retry_after_seconds

AGNES_DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_DEFAULT_MODEL = "agnes-image-2.5-flash"
AGNES_SUPPORTED_MODELS = (
    "agnes-image-2.5-flash",
    "agnes-image-2.1-flash",
    "agnes-image-2.0-flash",
)
AGNES_SUPPORTED_TIERS = ("1K", "2K", "3K", "4K")
AGNES_SUPPORTED_RATIOS = (
    "1:1",
    "3:4",
    "4:3",
    "16:9",
    "9:16",
    "2:3",
    "3:2",
    "21:9",
)
AGNES_IMAGE_SIZE_MAP: dict[str, dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024",
        "3:4": "864x1152",
        "4:3": "1152x864",
        "16:9": "1312x736",
        "9:16": "736x1312",
        "2:3": "832x1248",
        "3:2": "1248x832",
        "21:9": "1568x672",
    },
    "2K": {
        "1:1": "2048x2048",
        "3:4": "1728x2304",
        "4:3": "2304x1728",
        "16:9": "2624x1472",
        "9:16": "1472x2624",
        "2:3": "1664x2496",
        "3:2": "2496x1664",
        "21:9": "3136x1344",
    },
    "3K": {
        "1:1": "3072x3072",
        "3:4": "2592x3456",
        "4:3": "3456x2592",
        "16:9": "3936x2208",
        "9:16": "2208x3936",
        "2:3": "2496x3744",
        "3:2": "3744x2496",
        "21:9": "4704x2016",
    },
    "4K": {
        "1:1": "4096x4096",
        "3:4": "3456x4608",
        "4:3": "4608x3456",
        "16:9": "5248x2944",
        "9:16": "2944x5248",
        "2:3": "3328x4992",
        "3:2": "4992x3328",
        "21:9": "6272x2688",
    },
}

AGNES_EXACT_SIZE_TO_TIER_RATIO: dict[str, tuple[str, str]] = {
    exact_size: (tier, ratio)
    for tier, ratios in AGNES_IMAGE_SIZE_MAP.items()
    for ratio, exact_size in ratios.items()
}

_ADAPTIVE_RATIO_FALLBACKS = {
    "5:4": "4:3",
    "4:5": "3:4",
}

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_REFERENCE_IMAGES = 8
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def normalize_agnes_image_base_url(raw: str) -> str:
    base = str(raw or AGNES_DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = AGNES_DEFAULT_BASE_URL
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Agnes 接口地址无效")

    for suffix in (
        "/v1/images/generations",
        "/images/generations",
        "/v1/images/edits",
        "/images/edits",
        "/v1/images",
        "/images",
    ):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break

    if not re.search(r"/v\d+$", base.lower()):
        base += "/v1"
    return base


def _tier_from_exact_size(exact_size: str) -> str:
    if exact_size in AGNES_EXACT_SIZE_TO_TIER_RATIO:
        return AGNES_EXACT_SIZE_TO_TIER_RATIO[exact_size][0]
    width_text, height_text = exact_size.split("x", 1)
    longest_edge = max(int(width_text), int(height_text))
    if longest_edge <= 1400:
        return "1K"
    if longest_edge <= 2650:
        return "2K"
    if longest_edge <= 3500:
        return "3K"
    return "4K"


def _ratio_from_exact_size(exact_size: str) -> str | None:
    if exact_size in AGNES_EXACT_SIZE_TO_TIER_RATIO:
        return AGNES_EXACT_SIZE_TO_TIER_RATIO[exact_size][1]
    ratio = aspect_ratio_from_size(exact_size)
    if not ratio:
        return None
    ratio = _ADAPTIVE_RATIO_FALLBACKS.get(ratio, ratio)
    if ratio in AGNES_SUPPORTED_RATIOS:
        return ratio
    return None


def _normalize_tier(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in AGNES_SUPPORTED_TIERS:
        return text
    if text in {"1024"}:
        return "1K"
    if text in {"2048"}:
        return "2K"
    if text in {"3072"}:
        return "3K"
    if text in {"4096"}:
        return "4K"
    return None


def _normalize_agnes_ratio(
    value: Any, *, allow_adaptive_fallback: bool = False
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = normalize_aspect_ratio(text)
    if not normalized:
        return None
    if allow_adaptive_fallback:
        normalized = _ADAPTIVE_RATIO_FALLBACKS.get(normalized, normalized)
    if normalized in AGNES_SUPPORTED_RATIOS:
        return normalized
    return None


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _rewrite_loopback_ref(ref: str, endpoint_url: str) -> str:
    try:
        r = urlsplit(ref)
        e = urlsplit(endpoint_url)
    except Exception:
        return ref
    if (r.hostname or "").lower() not in _LOOPBACK_HOSTS:
        return ref
    host = e.hostname or ""
    if not host or host.lower() in _LOOPBACK_HOSTS:
        return ref
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{r.port}" if r.port else host
    return r._replace(netloc=netloc).geturl()


def _extract_fallback_image_ref(data: dict[str, Any]) -> str | None:
    for key in ("url", "image_url", "imageUrl", "b64_json", "image", "b64"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _decode_base64_payload(text: str) -> bytes:
    compact = re.sub(r"\s+", "", str(text or "").strip())
    if not compact:
        return b""
    if compact.startswith("data:image/") and "," in compact:
        _, compact = compact.split(",", 1)
    for candidate in (compact, compact.replace("-", "+").replace("_", "/")):
        padding = "=" * ((4 - len(candidate) % 4) % 4)
        try:
            raw = base64.b64decode(candidate + padding, validate=False)
            if raw:
                return raw
        except Exception:
            continue
    return b""


class AgnesImagesBackend:
    """Agnes AI `/v1/images/generations` backend for text-to-image and multi-image editing."""

    @property
    def supports_ordered_references(self) -> bool:
        return bool(self.supports_edit)

    def __init__(
        self,
        *,
        imgr,
        base_url: str = AGNES_DEFAULT_BASE_URL,
        api_keys: list[str] | None = None,
        api_key: str | None = None,
        timeout: int = 600,
        max_retries: int = 2,
        default_model: str = AGNES_DEFAULT_MODEL,
        default_size: str = "2K",
        default_ratio: str = "1:1",
        supports_edit: bool = True,
        extra_body: dict | None = None,
        proxy_url: str | None = None,
        output_format: str = "jpeg",
    ):
        self.imgr = imgr
        self.base_url = normalize_agnes_image_base_url(base_url)
        self.api_url = f"{self.base_url}/images/generations"

        keys = [str(k).strip() for k in (api_keys or []) if str(k).strip()]
        single = str(api_key or "").strip()
        if single and single not in keys:
            keys.append(single)
        self.api_keys = keys

        self.timeout = max(1, min(int(timeout or 600), 3600))
        self.max_retries = parse_max_retries(max_retries)
        self.default_model = (
            str(default_model or AGNES_DEFAULT_MODEL).strip() or AGNES_DEFAULT_MODEL
        )
        self.default_size = str(default_size or "2K").strip() or "2K"
        self.default_ratio = str(default_ratio or "1:1").strip() or "1:1"
        self.supports_edit = bool(supports_edit)
        if extra_body is not None and not isinstance(extra_body, dict):
            raise ValueError("Agnes 额外请求体必须为 JSON 对象")
        self.extra_body = copy.deepcopy(extra_body) if extra_body else {}
        self.proxy_url = str(proxy_url or "").strip() or None
        self.output_format = str(output_format or "jpeg").strip().lower()

        self._key_index = 0
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if self.proxy_url:
                for kw in ({"proxy": self.proxy_url}, {"proxies": self.proxy_url}):
                    try:
                        self._client = httpx.AsyncClient(
                            timeout=float(self.timeout),
                            follow_redirects=False,
                            **kw,
                        )
                        return self._client
                    except TypeError:
                        continue
                    except Exception as exc:
                        logger.warning(
                            "[agnes_images] failed to build proxy client: %s", exc
                        )
                        break
            self._client = httpx.AsyncClient(
                timeout=float(self.timeout),
                follow_redirects=False,
            )
        return self._client

    def _next_key(self) -> str:
        if not self.api_keys:
            raise ValueError("请配置 Agnes API Key")
        key = self.api_keys[self._key_index % len(self.api_keys)]
        self._key_index = (self._key_index + 1) % len(self.api_keys)
        return key

    def _redact(self, text: str) -> str:
        out = str(text or "")
        for key in self.api_keys:
            if key:
                out = out.replace(key, "[redacted]")
        return out[:300]

    def _error_detail(self, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return "未提供错误详情"
        try:
            data = json.loads(text)
        except ValueError:
            return self._redact(text)
        if not isinstance(data, dict):
            return self._redact(text)
        detail = (
            data.get("error")
            or data.get("detail")
            or data.get("message")
            or "未提供错误详情"
        )
        if isinstance(detail, dict):
            detail = (
                detail.get("message")
                or detail.get("detail")
                or detail.get("code")
                or "上游请求失败"
            )
        return self._redact(str(detail))

    def resolve_output_intent(self, intent: OutputIntent) -> dict[str, str]:
        if intent.exact_size:
            return {"size": intent.exact_size}
        out: dict[str, str] = {}
        if intent.aspect_ratio:
            out["aspect_ratio"] = intent.aspect_ratio
        if intent.resolution:
            out["resolution"] = intent.resolution
        return out

    def _resolve_size_and_ratio(
        self,
        *,
        size: str | None,
        resolution: str | None,
        aspect_ratio: str | None,
        ratio: str | None,
        merged_extra: dict[str, Any],
    ) -> tuple[str, str]:
        inferred_ratio_from_size: str | None = None
        resolved_tier: str | None = None

        raw_size = str(size or "").strip()
        raw_resolution = str(resolution or "").strip()
        extra_size = str(merged_extra.pop("size", "") or "").strip()

        if raw_resolution:
            resolved_tier = _normalize_tier(raw_resolution)
            if not resolved_tier:
                exact = normalize_exact_size(raw_resolution)
                if exact:
                    resolved_tier = _tier_from_exact_size(exact)
                    inferred_ratio_from_size = _ratio_from_exact_size(exact)
                else:
                    raise ValueError(
                        "Agnes 分辨率仅支持 1K, 2K, 3K, 4K 或有效像素尺寸"
                    )

        if raw_size:
            tier_candidate = _normalize_tier(raw_size)
            if tier_candidate:
                resolved_tier = tier_candidate
            else:
                exact = normalize_exact_size(raw_size)
                if not exact:
                    raise ValueError(
                        "Agnes size 仅支持 1K, 2K, 3K, 4K 或有效像素尺寸"
                    )
                if not resolved_tier:
                    resolved_tier = _tier_from_exact_size(exact)
                inferred_ratio_from_size = _ratio_from_exact_size(exact)
                if inferred_ratio_from_size is None:
                    raise ValueError("Agnes 画幅不受支持")

        if not resolved_tier and extra_size:
            tier_candidate = _normalize_tier(extra_size)
            if tier_candidate:
                resolved_tier = tier_candidate
            else:
                exact = normalize_exact_size(extra_size)
                if not exact:
                    raise ValueError(
                        "Agnes size 仅支持 1K, 2K, 3K, 4K 或有效像素尺寸"
                    )
                resolved_tier = _tier_from_exact_size(exact)
                inferred_ratio_from_size = _ratio_from_exact_size(exact)
                if inferred_ratio_from_size is None:
                    raise ValueError("Agnes 画幅不受支持")

        default_ratio_from_default_size: str | None = None
        if not resolved_tier:
            tier_candidate = _normalize_tier(self.default_size)
            if tier_candidate:
                resolved_tier = tier_candidate
            else:
                exact = normalize_exact_size(self.default_size)
                if not exact:
                    raise ValueError(
                        "Agnes 默认尺寸仅支持 1K, 2K, 3K, 4K 或有效像素尺寸"
                    )
                resolved_tier = _tier_from_exact_size(exact)
                default_ratio_from_default_size = _ratio_from_exact_size(exact)

        extra_ratio = merged_extra.pop("ratio", None) or merged_extra.pop(
            "aspect_ratio", None
        )

        if ratio is not None and str(ratio).strip():
            resolved_ratio = _normalize_agnes_ratio(
                ratio, allow_adaptive_fallback=False
            )
            if not resolved_ratio:
                raise ValueError("Agnes 画幅不受支持")
        elif aspect_ratio is not None and str(aspect_ratio).strip():
            resolved_ratio = _normalize_agnes_ratio(
                aspect_ratio, allow_adaptive_fallback=True
            )
            if not resolved_ratio:
                raise ValueError("Agnes 画幅不受支持")
        elif inferred_ratio_from_size:
            resolved_ratio = inferred_ratio_from_size
        elif extra_ratio is not None and str(extra_ratio).strip():
            resolved_ratio = _normalize_agnes_ratio(
                extra_ratio, allow_adaptive_fallback=False
            )
            if not resolved_ratio:
                raise ValueError("Agnes 画幅不受支持")
        elif default_ratio_from_default_size:
            resolved_ratio = default_ratio_from_default_size
        else:
            resolved_ratio = _normalize_agnes_ratio(
                self.default_ratio, allow_adaptive_fallback=False
            )
            if not resolved_ratio:
                raise ValueError("Agnes 画幅不受支持")

        return resolved_tier, resolved_ratio

    def build_payload(
        self,
        prompt: str,
        images: list[bytes] | None = None,
        *,
        model: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        ratio: str | None = None,
        extra_body: dict | None = None,
        require_ordered_references: bool = False,
    ) -> dict[str, Any]:
        if extra_body is not None and not isinstance(extra_body, dict):
            raise ValueError("Agnes 额外请求体必须为 JSON 对象")

        final_model = str(model or self.default_model or "").strip()
        if not final_model:
            raise ValueError("请配置 Agnes 生图模型")
        if final_model.lower().startswith("agnes-video"):
            raise ValueError("Agnes 生图渠道不能填写视频模型 ID")

        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("请填写图片提示词")

        merged_extra: dict[str, Any] = copy.deepcopy(self.extra_body)
        if extra_body:
            merged_extra.update(copy.deepcopy(extra_body))

        nested_extra: dict[str, Any] = {}
        if "extra_body" in merged_extra:
            inner = merged_extra.pop("extra_body")
            if not isinstance(inner, dict):
                raise ValueError("Agnes extra_body 必须为 JSON 对象")
            nested_extra.update(inner)

        if "n" in merged_extra:
            n_val = merged_extra.pop("n")
            if type(n_val) is not int or n_val != 1:
                raise ValueError("Agnes 生图不支持批量参数 n")
        if "n" in nested_extra:
            n_val = nested_extra.pop("n")
            if type(n_val) is not int or n_val != 1:
                raise ValueError("Agnes 生图不支持批量参数 n")

        return_base64 = merged_extra.pop("return_base64", None)
        if return_base64 is None and "return_base64" in nested_extra:
            return_base64 = nested_extra.pop("return_base64")
        if return_base64 is not None and not isinstance(return_base64, bool):
            raise ValueError("Agnes return_base64 必须为布尔值")

        final_size, final_ratio = self._resolve_size_and_ratio(
            size=size,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            ratio=ratio,
            merged_extra=merged_extra,
        )

        # Move remaining extra fields (such as response_format, image, etc.) into
        # payload["extra_body"] as required by Agnes's /v1/images/generations contract.
        for key, value in merged_extra.items():
            if key in {"model", "prompt"}:
                continue
            nested_extra[key] = value

        if require_ordered_references and images:
            for field in ("image", "images", "image_url", "image[]"):
                nested_extra.pop(field, None)

        encoded_refs: list[str] = []
        if images:
            if len(images) > _MAX_REFERENCE_IMAGES:
                raise ValueError(f"Agnes 最多支持 {_MAX_REFERENCE_IMAGES} 张参考图片")
            for blob in images:
                if not isinstance(blob, bytes) or not blob:
                    raise ValueError("Agnes 参考图片不能为空")
                if len(blob) >= _MAX_REFERENCE_IMAGE_BYTES:
                    raise ValueError("Agnes 参考图片需小于 10 MB")
                mime, _ = guess_image_mime_and_ext(blob)
                b64 = base64.b64encode(blob).decode("ascii")
                encoded_refs.append(f"data:{mime};base64,{b64}")

        configured_images = nested_extra.get("image")
        if configured_images is not None:
            if not isinstance(configured_images, list):
                raise ValueError("Agnes extra_body.image 必须为列表")
            if encoded_refs:
                nested_extra["image"] = [*encoded_refs, *configured_images]
        elif encoded_refs:
            nested_extra["image"] = encoded_refs

        self.validate_nested_extra(nested_extra)

        payload: dict[str, Any] = {
            "model": final_model,
            "prompt": clean_prompt,
            "size": final_size,
            "ratio": final_ratio,
        }
        if return_base64 is not None:
            payload["return_base64"] = return_base64
        if nested_extra:
            payload["extra_body"] = nested_extra
        return payload

    @staticmethod
    def validate_nested_extra(nested_extra: dict[str, Any]) -> None:
        if "response_format" in nested_extra:
            fmt = nested_extra["response_format"]
            if fmt not in {"url", "b64_json"}:
                raise ValueError("Agnes response_format 仅支持 url 或 b64_json")

        if "image" in nested_extra:
            refs = nested_extra["image"]
            if not isinstance(refs, list) or not refs:
                raise ValueError("Agnes extra_body.image 必须为非空列表")
            if len(refs) > _MAX_REFERENCE_IMAGES:
                raise ValueError(f"Agnes 最多支持 {_MAX_REFERENCE_IMAGES} 张参考图片")
            if any(
                not isinstance(u, str)
                or not u.startswith(("https://", "http://", "data:image/"))
                for u in refs
            ):
                raise ValueError("Agnes 参考图片需为有效 URL 或 Data URI")

    async def _post_generations(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self._next_key()
        client = self._get_client()
        attempts = self.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except (httpx.TransportError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(4.0, 1.0 * (2**attempt)))
                    continue
                if self.max_retries == 0:
                    raise
                raise RuntimeError(
                    f"Agnes 生图请求失败: {self._redact(str(exc) or '请求超时')}"
                ) from exc

            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("Agnes 响应过大")

            if not 200 <= response.status_code < 300:
                detail = self._error_detail(response.text)
                message = f"Agnes HTTP {response.status_code}: {detail}"
                if (
                    response.status_code in _RETRYABLE_STATUS_CODES
                    and attempt + 1 < attempts
                ):
                    retry_after = _parse_retry_after_seconds(
                        response.headers.get("retry-after")
                    )
                    delay = (
                        min(retry_after, 15.0)
                        if retry_after is not None
                        else min(4.0, 1.0 * (2**attempt))
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(message)

            try:
                data = response.json()
            except ValueError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(4.0, 1.0 * (2**attempt)))
                    continue
                raise RuntimeError("Agnes 返回无效 JSON 响应") from exc

            if not isinstance(data, dict) or not data:
                raise RuntimeError("Agnes 返回空响应")
            return data

        raise RuntimeError(
            f"Agnes 生图请求失败: {self._redact(str(last_error or ''))}"
        ) from last_error

    async def _save_response_data(self, data: dict[str, Any]) -> Path:
        items = data.get("data")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                b64_json = item.get("b64_json")
                if isinstance(b64_json, str) and b64_json.strip():
                    raw = _decode_base64_payload(b64_json)
                    if not raw:
                        raise RuntimeError("Agnes Base64 图片解码失败")
                    return await self.imgr.save_image(
                        raw, output_format=self.output_format
                    )
                url = item.get("url")
                if isinstance(url, str) and url.strip():
                    clean_url = _rewrite_loopback_ref(url.strip(), self.api_url)
                    return await self.imgr.download_image(
                        clean_url, output_format=self.output_format
                    )

        ref = _extract_fallback_image_ref(data)
        if ref:
            if ref.startswith("data:image/"):
                raw = _decode_base64_payload(ref)
                if not raw:
                    raise RuntimeError("Agnes Base64 图片解码失败")
                return await self.imgr.save_image(raw, output_format=self.output_format)
            if ref.startswith(("https://", "http://")):
                clean_url = _rewrite_loopback_ref(ref, self.api_url)
                return await self.imgr.download_image(
                    clean_url, output_format=self.output_format
                )
            raw = _decode_base64_payload(ref)
            if raw:
                return await self.imgr.save_image(raw, output_format=self.output_format)

        raise RuntimeError("Agnes 响应未包含有效图片数据")

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        ratio: str | None = None,
        extra_body: dict | None = None,
    ) -> Path:
        payload = self.build_payload(
            prompt,
            model=model,
            size=size,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            ratio=ratio,
            extra_body=extra_body,
        )
        t0 = time.perf_counter()
        data = await self._post_generations(payload)
        out = await self._save_response_data(data)
        logger.info(
            "[AgnesImages][generate] model=%s size=%s ratio=%s in %.2fs",
            payload["model"],
            payload["size"],
            payload["ratio"],
            time.perf_counter() - t0,
        )
        return out

    async def edit(
        self,
        prompt: str,
        images: list[bytes],
        *,
        model: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        ratio: str | None = None,
        extra_body: dict | None = None,
        require_ordered_references: bool = False,
    ) -> Path:
        if not self.supports_edit:
            raise RuntimeError("该后端不支持改图/图生图")
        if not images:
            raise ValueError("至少需要一张图片")

        payload = self.build_payload(
            prompt,
            images,
            model=model,
            size=size,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            ratio=ratio,
            extra_body=extra_body,
            require_ordered_references=require_ordered_references,
        )
        t0 = time.perf_counter()
        data = await self._post_generations(payload)
        out = await self._save_response_data(data)
        logger.info(
            "[AgnesImages][edit] model=%s images=%d size=%s ratio=%s in %.2fs",
            payload["model"],
            len(images),
            payload["size"],
            payload["ratio"],
            time.perf_counter() - t0,
        )
        return out
