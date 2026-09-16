# 航天模拟器助手插件测试
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# 本文件是 sfs_bridge 插件的一部分，以 GNU General Public License v3.0 许可发布。
# 完整条款见仓库根目录的 LICENSE 文件。

import base64
import io
from pathlib import Path

from plugin.plugins.sfs_bridge import (
    _as_bool,
    _as_int,
    _as_text,
    _describe_state,
    _encode_for_vision,
)


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "sfs_bridge"' in text
    assert 'entry = "plugin.plugins.sfs_bridge:SfsBridgePlugin"' in text


def test_as_helpers() -> None:
    assert _as_text("  x  ") == "x"
    assert _as_text(None) == ""
    assert _as_text(12) == ""

    assert _as_bool(True) is True
    assert _as_bool("on") is True
    assert _as_bool("no") is False
    assert _as_bool(None, True) is True

    assert _as_int("5", 1, 1, 10) == 5
    assert _as_int("bad", 3, 1, 10) == 3
    assert _as_int(999, 3, 1, 10) == 10
    assert _as_int(0, 3, 1, 10) == 1


def test_describe_state_out_of_world() -> None:
    text = _describe_state({"in_world": False})
    assert "不在世界场景" in text


def test_describe_state_full() -> None:
    text = _describe_state({
        "in_world": True,
        "flying": True,
        "rocket": "测试号",
        "planet": "Earth",
        "height": 1234.5,
        "speed": 78.9,
        "throttle": 0.8,
        "stage": 2,
        "mass": 45.6,
    })

    assert "测试号" in text
    assert "Earth" in text
    assert "1234.5 米" in text
    assert "78.9 米/秒" in text
    assert "80%" in text
    assert "当前分级 2" in text


def test_describe_state_handles_missing_numbers() -> None:
    text = _describe_state({"in_world": True, "flying": True})
    assert text  # 不应抛异常


def test_encode_for_vision_rejects_garbage() -> None:
    encoded, mime, note = _encode_for_vision(
        b"not-an-image", max_width=512, quality=80
    )
    assert encoded is None
    assert "失败" in note or "无法" in note


def test_encode_for_vision_compresses_large_png() -> None:
    """1080p 的 PNG 会超出 N.E.K.O. 的 2MB base64 限制，必须被压缩。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return

    image = Image.new("RGB", (1920, 1080), (30, 60, 120))
    # 加一些噪点，避免纯色被压得过小、测不出压缩路径
    for x in range(0, 1920, 40):
        for y in range(0, 1080, 40):
            image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png = buffer.getvalue()

    encoded, mime, note = _encode_for_vision(png, max_width=1024, quality=80)

    assert encoded is not None
    assert mime == "image/jpeg"
    # 必须满足 N.E.K.O. 的单图限制
    assert len(encoded) <= 2 * 1024 * 1024
    # 解回来确认是有效 JPEG
    decoded = base64.b64decode(encoded)
    assert decoded[:2] == b"\xff\xd8"


def test_encode_for_vision_keeps_small_image() -> None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return

    image = Image.new("RGB", (64, 64), (10, 20, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    encoded, mime, _ = _encode_for_vision(
        buffer.getvalue(), max_width=1024, quality=80
    )
    assert encoded is not None
    assert mime == "image/jpeg"
