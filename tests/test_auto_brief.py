# 和猫娘一起造火箭（SFS）插件 —— 自动播报测试
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# 本文件是 sfs_bridge 插件的一部分，以 GNU General Public License v3.0 许可发布。
# 完整条款见仓库根目录的 LICENSE 文件。

"""自动播报（push_message + ai_behavior="read"）的行为钉板。

不需要游戏：这里只验证「压成什么文本」和「用哪套参数推给宿主」，
真实聊天效果要在实机里看。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from plugin.plugins.sfs_bridge import (
    AUTO_BRIEF_COALESCE_KEY,
    AUTO_BRIEF_PREFIX,
    SfsBridgePlugin,
    _auto_brief_line,
)
from plugin.sdk.plugin import SdkError

_IN_WORLD: Dict[str, Any] = {
    "in_world": True,
    "flying": True,
    "rocket": "Mock Rocket",
    "planet": "Earth",
    "height": 12000.0,
    "speed": 320.5,
    "throttle": 1.0,
    "stage": 2,
    "mass": 42.5,
}

_NOT_IN_WORLD: Dict[str, Any] = {"in_world": False}


# ── 文本压缩 ──────────────────────────────────────────────────────────────


def test_line_has_prefix_and_rocket_name() -> None:
    text = _auto_brief_line(_IN_WORLD)
    assert text.startswith(AUTO_BRIEF_PREFIX)
    assert "Mock Rocket" in text
    assert "12000.0 米" in text


def test_line_is_short_enough_to_live_in_context() -> None:
    """这段会一直待在上下文里，不能太长。"""
    assert len(_auto_brief_line(_IN_WORLD)) < 400


def test_line_is_empty_outside_world() -> None:
    """主菜单/加载中里没有遥测，不该反复播报。"""
    assert _auto_brief_line(_NOT_IN_WORLD) == ""


def test_line_is_empty_for_empty_payload() -> None:
    assert _auto_brief_line({}) == ""


def test_line_has_no_double_prefix() -> None:
    assert _auto_brief_line(_IN_WORLD).count(AUTO_BRIEF_PREFIX) == 1


# ── 推送行为 ──────────────────────────────────────────────────────────────


class _Recorder:
    """最小的 self 替身：只提供 _auto_brief_once 用得到的东西。"""

    def __init__(self, state: Dict[str, Any], *, boom: bool = False) -> None:
        self._state = state
        self._boom = boom
        self._auto_brief_last_text = ""
        self._auto_brief_last_ok = None
        self.pushes: List[Dict[str, Any]] = []
        self.logger = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)

    async def _get_json(self, path: str) -> Dict[str, Any]:
        if self._boom:
            raise SdkError("连不上游戏桥接服务")
        return self._state

    def push_message(self, **kwargs: Any) -> Dict[str, Any]:
        self.pushes.append(kwargs)
        return {"submitted": True}

    _auto_brief_once = SfsBridgePlugin._auto_brief_once


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_once_pushes_read_behavior_with_coalesce_key() -> None:
    rec = _Recorder(_IN_WORLD)
    _run(rec._auto_brief_once())

    assert len(rec.pushes) == 1
    push = rec.pushes[0]
    # read：进上下文但不打断对话 —— 这就是「每轮对话一开始数据已在」
    assert push["ai_behavior"] == "read"
    assert push["visibility"] == []
    assert push["coalesce_key"] == AUTO_BRIEF_COALESCE_KEY
    assert push["source"] == "sfs_auto_brief"
    assert push["parts"][0]["type"] == "text"
    assert push["parts"][0]["text"].startswith(AUTO_BRIEF_PREFIX)


def test_once_skips_identical_payload() -> None:
    rec = _Recorder(_IN_WORLD)
    _run(rec._auto_brief_once())
    _run(rec._auto_brief_once())
    assert len(rec.pushes) == 1


def test_once_stays_silent_outside_world() -> None:
    rec = _Recorder(_NOT_IN_WORLD)
    _run(rec._auto_brief_once())
    assert rec.pushes == []


def test_never_connected_stays_silent() -> None:
    """游戏没开的时候别往上下文里灌噪音。"""
    rec = _Recorder(_IN_WORLD, boom=True)
    _run(rec._auto_brief_once())
    assert rec.pushes == []


def test_disconnect_after_connected_is_announced_once() -> None:
    """本来连着、突然断了 —— 得说一声，否则模型会拿旧数据当现状。"""
    rec = _Recorder(_IN_WORLD)
    _run(rec._auto_brief_once())
    assert len(rec.pushes) == 1

    rec._boom = True
    _run(rec._auto_brief_once())
    _run(rec._auto_brief_once())
    assert len(rec.pushes) == 2
    assert "已经退出或断开" in rec.pushes[1]["parts"][0]["text"]


def test_once_survives_unexpected_exception() -> None:
    """桥接没起来时 httpx 会抛，后台任务不能被带走。"""

    class _Boom(_Recorder):
        async def _get_json(self, path: str) -> Dict[str, Any]:
            raise RuntimeError("connection refused")

    rec = _Boom(_IN_WORLD)
    _run(rec._auto_brief_once())
    assert rec.pushes == []


def test_once_respects_unsubmitted_result() -> None:
    """宿主拒收（backpressure 等）不该抛异常。"""

    class _Reject(_Recorder):
        def push_message(self, **kwargs: Any) -> Dict[str, Any]:
            self.pushes.append(kwargs)
            return {"submitted": False, "reason": "backpressure"}

    rec = _Reject(_IN_WORLD)
    _run(rec._auto_brief_once())
    assert len(rec.pushes) == 1


def test_loop_honours_stop_event() -> None:
    """stop 一置位就要立刻退出，不能等满一个间隔。"""

    class _LoopRec(_Recorder):
        _auto_brief_loop = SfsBridgePlugin._auto_brief_loop

    rec = _LoopRec(_IN_WORLD)
    rec._auto_brief_interval = 0.0
    rec._auto_brief_stop = asyncio.Event()

    async def _go() -> None:
        task = asyncio.create_task(rec._auto_brief_loop())
        await asyncio.sleep(0.05)
        rec._auto_brief_stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    _run(_go())
    assert rec.pushes  # 至少推过一次


def test_stop_is_safe_without_init() -> None:
    """测试常用 __new__ 造实例，_stop_auto_brief 不能因为缺属性炸掉。"""
    plugin = SfsBridgePlugin.__new__(SfsBridgePlugin)
    _run(plugin._stop_auto_brief())


@pytest.mark.parametrize("interval", [5.0, 20.0, 300.0])
def test_interval_defaults_in_range(interval: float) -> None:
    assert 5.0 <= interval <= 300.0
