from __future__ import annotations

import asyncio
import base64
import dataclasses
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_orig_dataclass = dataclasses.dataclass


def _compat_dataclass(*args, **kwargs):
    kwargs.pop("slots", None)
    return _orig_dataclass(*args, **kwargs)


dataclasses.dataclass = _compat_dataclass

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - stdlib unittest fallback
    httpx = types.ModuleType("httpx")

    class _URL(str):
        def __new__(cls, url: str):
            return super().__new__(cls, str(url))

    class _Request:
        def __init__(self, method: str, url: str, headers=None, content: bytes = b""):
            self.method = method
            self.url = _URL(url)
            self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
            self.content = content

    class _Response:
        def __init__(self, status_code: int, *, text: str | None = None, json=None, headers=None):
            self.status_code = status_code
            self._json = json
            self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
            if text is not None:
                self.text = text
            elif json is not None:
                import json as _json

                self.text = _json.dumps(json)
            else:
                self.text = ""
            self.content = self.text.encode("utf-8")

        def json(self):
            if self._json is not None:
                return self._json
            import json as _json

            return _json.loads(self.text)

    class _MockTransport:
        def __init__(self, handler):
            self.handler = handler

    class _AsyncClient:
        def __init__(self, *args, transport=None, **kwargs):
            self._transport = transport

        async def post(self, url: str, *, json=None, headers=None):
            import json as _json

            raw = _json.dumps(json).encode("utf-8") if json is not None else b""
            req = _Request("POST", url, headers=headers, content=raw)
            return self._transport.handler(req)

        async def aclose(self):
            return None

    class _HTTPError(Exception):
        pass

    class _TransportError(_HTTPError):
        pass

    class _TimeoutException(_TransportError):
        pass

    class _ReadTimeout(_TimeoutException):
        pass

    class _NetworkError(_TransportError):
        pass

    class _RemoteProtocolError(_TransportError):
        pass

    httpx.URL = _URL
    httpx.Request = _Request
    httpx.Response = _Response
    httpx.MockTransport = _MockTransport
    httpx.AsyncClient = _AsyncClient
    httpx.HTTPError = _HTTPError
    httpx.TransportError = _TransportError
    httpx.TimeoutException = _TimeoutException
    httpx.ReadTimeout = _ReadTimeout
    httpx.NetworkError = _NetworkError
    httpx.RemoteProtocolError = _RemoteProtocolError
    sys.modules["httpx"] = httpx

try:
    import PIL.Image  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - stdlib unittest fallback
    pil_mod = types.ModuleType("PIL")
    pil_img = types.ModuleType("PIL.Image")
    pil_mod.Image = pil_img
    sys.modules["PIL"] = pil_mod
    sys.modules["PIL.Image"] = pil_img

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - stdlib unittest fallback
    import re as _re

    class _RaisesCtx:
        def __init__(self, exc_type, match=None):
            self.exc_type = exc_type
            self.match = match
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, _tb):
            if exc_type is None:
                raise AssertionError(f"Expected {self.exc_type} to be raised")
            if not issubclass(exc_type, self.exc_type):
                return False
            self.value = exc_val
            if self.match and not _re.search(self.match, str(exc_val)):
                raise AssertionError(f"{exc_val!r} does not match {self.match!r}")
            return True

    class _Mark:
        @staticmethod
        def asyncio(fn):
            return fn

        @staticmethod
        def parametrize(_names, _values):
            def _decorator(fn):
                fn._parametrize = (_names, _values)
                return fn

            return _decorator

    pytest = types.ModuleType("pytest")
    pytest.fixture = lambda fn: fn
    pytest.mark = _Mark()
    pytest.raises = lambda exc_type, match=None: _RaisesCtx(exc_type, match=match)
    sys.modules["pytest"] = pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sora2_video_service import _load_module


class _DummyImageManager:
    def __init__(self):
        self.downloaded: list[tuple[str, str]] = []
        self.saved: list[tuple[bytes, str]] = []

    async def download_image(self, url: str, *, output_format: str = "jpeg") -> Path:
        self.downloaded.append((url, output_format))
        return Path("/tmp/agnes_downloaded.jpg")

    async def save_image(
        self, image_bytes: bytes, *, output_format: str = "jpeg"
    ) -> Path:
        self.saved.append((image_bytes, output_format))
        return Path("/tmp/agnes_saved.jpg")


@pytest.fixture
def module():
    sora = _load_module()
    return importlib.import_module(sora.__package__ + ".agnes_images_backend")


@pytest.fixture
def output_spec_module():
    sora = _load_module()
    return importlib.import_module(sora.__package__ + ".output_spec")


@pytest.fixture
def imgr():
    return _DummyImageManager()


@pytest.fixture
def backend(module, imgr):
    return module.AgnesImagesBackend(
        imgr=imgr,
        base_url="https://apihub.agnes-ai.com/v1",
        api_keys=["secret-agnes-key-1", "secret-agnes-key-2"],
        default_model="agnes-image-2.5-flash",
        default_size="2K",
        default_ratio="1:1",
        output_format="webp_lossless",
    )


def mock_transport(backend, responses):
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, data, *headers = item
        if isinstance(data, str):
            return httpx.Response(
                status, text=data, headers=headers[0] if headers else {}
            )
        return httpx.Response(status, json=data, headers=headers[0] if headers else {})

    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return calls


@pytest.mark.asyncio
async def test_generate_sends_tier_and_ratio_and_downloads_url(
    backend, imgr, output_spec_module
):
    calls = mock_transport(
        backend,
        [
            (
                200,
                {
                    "created": 1776495432,
                    "data": [
                        {
                            "url": "https://apihub.agnes-ai.com/v1/files/img_output.png",
                            "b64_json": None,
                        }
                    ],
                    "model": "agnes-image-2.5-flash",
                },
            )
        ],
    )
    intent = output_spec_module.OutputIntent(aspect_ratio="16:9", resolution="4K")
    kwargs = backend.resolve_output_intent(intent)
    out = await backend.generate("cyberpunk city skyline", **kwargs)

    assert out == Path("/tmp/agnes_downloaded.jpg")
    assert imgr.downloaded == [
        ("https://apihub.agnes-ai.com/v1/files/img_output.png", "webp_lossless")
    ]
    assert len(calls) == 1
    assert calls[0].url == httpx.URL(
        "https://apihub.agnes-ai.com/v1/images/generations"
    )
    assert calls[0].headers["authorization"] == "Bearer secret-agnes-key-1"
    payload = json.loads(calls[0].content)
    assert payload == {
        "model": "agnes-image-2.5-flash",
        "prompt": "cyberpunk city skyline",
        "size": "4K",
        "ratio": "16:9",
    }
    await backend.close()


@pytest.mark.asyncio
async def test_edit_preserves_ordered_references_in_extra_body_without_collage(
    backend, imgr
):
    png_1 = b"\x89PNG\r\n\x1a\nfirst"
    png_2 = b"\x89PNG\r\n\x1a\nsecond"
    b64_out = base64.b64encode(b"generated-image-bytes").decode("ascii")
    calls = mock_transport(
        backend,
        [
            (
                200,
                {
                    "created": 1776495432,
                    "data": [{"url": None, "b64_json": b64_out}],
                    "model": "agnes-image-2.5-flash",
                },
            )
        ],
    )
    assert backend.supports_ordered_references is True
    backend.extra_body = {
        "response_format": "b64_json",
        "image": ["https://example.com/should-be-dropped-when-ordered.png"],
    }

    out = await backend.edit(
        "combine identity and outfit",
        [png_1, png_2],
        aspect_ratio="3:4",
        resolution="2K",
        require_ordered_references=True,
    )

    assert out == Path("/tmp/agnes_saved.jpg")
    assert imgr.saved == [(b"generated-image-bytes", "webp_lossless")]
    payload = json.loads(calls[0].content)
    assert payload["model"] == "agnes-image-2.5-flash"
    assert payload["size"] == "2K"
    assert payload["ratio"] == "3:4"
    assert "response_format" not in payload
    assert "image" not in payload
    assert payload["extra_body"]["response_format"] == "b64_json"
    assert payload["extra_body"]["image"] == [
        "data:image/png;base64," + base64.b64encode(png_1).decode("ascii"),
        "data:image/png;base64," + base64.b64encode(png_2).decode("ascii"),
    ]
    assert backend.extra_body["image"] == [
        "https://example.com/should-be-dropped-when-ordered.png"
    ]
    await backend.close()


@pytest.mark.parametrize(
    "exact_size,expected_tier,expected_ratio",
    [
        ("1024x1024", "1K", "1:1"),
        ("1312x736", "1K", "16:9"),
        ("2624x1472", "2K", "16:9"),
        ("3936x2208", "3K", "16:9"),
        ("5248x2944", "4K", "16:9"),
        ("3328x4992", "4K", "2:3"),
        ("6272x2688", "4K", "21:9"),
        ("2048x1152", "2K", "16:9"),
        ("1024x768", "1K", "4:3"),
        ("1040x832", "1K", "4:3"),
        ("832x1040", "1K", "3:4"),
    ],
)
def test_exact_sizes_map_to_agnes_tier_and_ratio(
    backend, exact_size, expected_tier, expected_ratio
):
    payload = backend.build_payload("a portrait", size=exact_size)
    assert payload["size"] == expected_tier
    assert payload["ratio"] == expected_ratio


def test_adaptive_aspect_ratio_fallbacks_for_5_4_and_4_5(backend):
    p1 = backend.build_payload("portrait", aspect_ratio="4:5", resolution="3K")
    assert p1["size"] == "3K" and p1["ratio"] == "3:4"
    p2 = backend.build_payload("landscape", aspect_ratio="5:4", resolution="1K")
    assert p2["size"] == "1K" and p2["ratio"] == "4:3"


@pytest.mark.parametrize(
    "kwargs,extra,match",
    [
        ({"prompt": "   "}, {}, "提示词"),
        ({"prompt": "cat", "model": "agnes-video-2.5-flash"}, {}, "视频模型"),
        ({"prompt": "cat", "resolution": "8K"}, {}, "分辨率"),
        ({"prompt": "cat", "size": "invalid"}, {}, "size"),
        ({"prompt": "cat", "ratio": "7:5"}, {}, "画幅"),
        ({"prompt": "cat"}, {"n": 2}, "批量参数 n"),
        ({"prompt": "cat"}, {"return_base64": "yes"}, "布尔值"),
        ({"prompt": "cat"}, {"response_format": "webp"}, "response_format"),
        ({"prompt": "cat"}, {"image": "https://example.com/a.png"}, "列表"),
        ({"prompt": "cat"}, {"image": ["file:///etc/passwd"]}, "URL"),
        (
            {"prompt": "cat"},
            {"image": ["https://example.com/a.png"] * 9},
            "最多支持 8",
        ),
    ],
)
def test_rejects_invalid_parameters_before_submission(backend, kwargs, extra, match):
    backend.extra_body = extra
    with pytest.raises(ValueError, match=match):
        backend.build_payload(**kwargs)


def test_rejects_oversized_reference_image(backend):
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024)
    with pytest.raises(ValueError, match="10 MB"):
        backend.build_payload("edit", [oversized])


@pytest.mark.asyncio
async def test_retry_and_key_rotation_and_secret_redaction(backend, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    backend.max_retries = 1
    calls = mock_transport(
        backend,
        [
            (
                429,
                {"error": {"message": "rate limited for secret-agnes-key-1"}},
                {"retry-after": "1"},
            ),
            (
                200,
                {
                    "data": [
                        {"url": "https://apihub.agnes-ai.com/v1/files/recovered.png"}
                    ]
                },
            ),
            (
                401,
                {"error": {"message": "invalid key secret-agnes-key-2"}},
            ),
        ],
    )
    await backend.generate("first call")
    assert len(calls) == 2

    with pytest.raises(RuntimeError) as exc_info:
        await backend.generate("second call")
    assert "secret-agnes-key-2" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)
    assert len(calls) == 3
    await backend.close()


@pytest.mark.asyncio
async def test_zero_retries_sends_single_request_on_timeout(backend, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", sleep)
    backend.max_retries = 0
    calls = mock_transport(backend, [httpx.ReadTimeout("read timeout")])
    with pytest.raises(httpx.ReadTimeout):
        await backend.generate("single try")
    assert len(calls) == 1
    sleep.assert_not_awaited()
    await backend.close()


class _MonkeyPatch:
    def __init__(self):
        self._patches = []

    def setattr(self, target: str, value):
        p = patch(target, value)
        p.start()
        self._patches.append(p)

    def undo(self):
        for p in reversed(self._patches):
            p.stop()


class AgnesImagesBackendUnittest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sora = _load_module()
        self.mod = importlib.import_module(sora.__package__ + ".agnes_images_backend")
        self.out_mod = importlib.import_module(sora.__package__ + ".output_spec")
        self.imgr = _DummyImageManager()
        self.backend = self.mod.AgnesImagesBackend(
            imgr=self.imgr,
            base_url="https://apihub.agnes-ai.com/v1",
            api_keys=["secret-agnes-key-1", "secret-agnes-key-2"],
            default_model="agnes-image-2.5-flash",
            default_size="2K",
            default_ratio="1:1",
            output_format="webp_lossless",
        )

    async def test_generate_and_edit_and_retries(self):
        await test_generate_sends_tier_and_ratio_and_downloads_url(
            self.backend, self.imgr, self.out_mod
        )
        self.imgr = _DummyImageManager()
        self.backend.imgr = self.imgr
        await test_edit_preserves_ordered_references_in_extra_body_without_collage(
            self.backend, self.imgr
        )
        mp = _MonkeyPatch()
        try:
            self.backend.extra_body = {}
            await test_retry_and_key_rotation_and_secret_redaction(self.backend, mp)
            await test_zero_retries_sends_single_request_on_timeout(self.backend, mp)
        finally:
            mp.undo()

    def test_sizing_and_validation(self):
        for exact_size, expected_tier, expected_ratio in getattr(
            test_exact_sizes_map_to_agnes_tier_and_ratio, "_parametrize", (None, [])
        )[1]:
            test_exact_sizes_map_to_agnes_tier_and_ratio(
                self.backend, exact_size, expected_tier, expected_ratio
            )
        test_adaptive_aspect_ratio_fallbacks_for_5_4_and_4_5(self.backend)
        for kwargs, extra, match in getattr(
            test_rejects_invalid_parameters_before_submission, "_parametrize", (None, [])
        )[1]:
            test_rejects_invalid_parameters_before_submission(
                self.backend, kwargs, extra, match
            )
        self.backend.extra_body = {}
        test_rejects_oversized_reference_image(self.backend)

