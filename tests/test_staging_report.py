# 和猫娘一起造火箭（SFS）插件测试 —— 分级 / 发动机遥测
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# 本文件是 sfs_bridge 插件的一部分，以 GNU General Public License v3.0 许可发布。
# 完整条款见仓库根目录的 LICENSE 文件。
"""分级与动力遥测的解析测试。

这两块是 2026-10 用户反馈「收不到分级、识别不到发动机」的直接修复点，
所以用真实的 /state 片段做回归，避免以后又被改回只读顶层 ``stage``。
"""

import asyncio
from typing import Any, Dict, List

from plugin.plugins.sfs_bridge import (
    SfsBridgePlugin,
    _describe_propulsion,
    _describe_stages,
    _describe_state,
)

# 实机抓到的 /state 片段（蓝星1号，11 级、25 台引擎、液体燃料 99.4%）
FLIGHT: Dict[str, Any] = {
    "ok": True,
    "in_world": True,
    "flying": True,
    "rocket": "",
    "planet": "Earth",
    "height": 92.56,
    "speed": 0.0,
    "throttle": 0.5,
    "throttle_on": False,
    "stage": -1,
    "mass": 1574.943,
    "stages": {
        "count": 11,
        "current_index": 0,
        "current_parts": 15,
        "summary": "#1(15件) / #2(6件) / #3(2件)",
    },
    "propulsion": {
        "thrust": 0,
        "twr": 0,
        "engines_on": 0,
        "engines_total": 25,
        "fuel_percent": 0.994,
        "rcs_fuel_percent": None,
        "isp": 240,
    },
}

ON_FIRE: Dict[str, Any] = {
    **FLIGHT,
    "throttle_on": True,
    "propulsion": {
        **FLIGHT["propulsion"],
        "thrust": 120.0,
        "twr": 1.4,
        "engines_on": 1,
        "engines_total": 1,
    },
}

OLD_MOD: Dict[str, Any] = {
    "in_world": True,
    "flying": True,
    "height": 10.0,
    "stage": 2,
}


class _Recorder:
    """只借 control_sfs / sfs_command 的命令分发逻辑，不碰网络。"""

    _post_command = SfsBridgePlugin._post_command
    control_sfs = SfsBridgePlugin.control_sfs
    sfs_command = SfsBridgePlugin.sfs_command

    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def _record(self, name: str, value: float = 0.0) -> Dict[str, Any]:
        self.sent.append((name, value))
        return {"ok": True}


# ---------------------------------------------------------------------------
# 分级
# ---------------------------------------------------------------------------


def test_stages_from_new_mod() -> None:
    parts = _describe_stages(FLIGHT)
    assert len(parts) == 1
    text = parts[0]
    assert "共 11 级" in text
    assert "当前第 1 级（15 件）" in text
    assert "#2(6件)" in text


def test_stages_ignore_top_level_negative_stage() -> None:
    """顶层 stage 恒为 -1（模组读不到 Staging.stageId），不能据此说「当前分级 -1」。"""
    assert "-1" not in "".join(_describe_stages(FLIGHT))


def test_stages_fallback_to_old_field() -> None:
    assert _describe_stages(OLD_MOD) == ["当前分级 2"]


def test_stages_empty_when_nothing_known() -> None:
    assert _describe_stages({"in_world": True}) == []


# ---------------------------------------------------------------------------
# 动力
# ---------------------------------------------------------------------------


def test_propulsion_all_engines_off() -> None:
    text = "".join(_describe_propulsion(FLIGHT))
    assert "引擎 0/25 台点火" in text
    assert "按回车" not in text
    assert "ignite" in text
    assert "engine_on" in text
    assert "燃料 99%" in text
    assert "比冲 240 秒" in text
    # 推力为 0 也要照实说，不能省
    assert "推力 0.0 kN" in text


def test_propulsion_engines_lit_has_no_hint() -> None:
    text = "".join(_describe_propulsion(ON_FIRE))
    assert "引擎 1/1 台点火" in text
    assert "按回车" not in text
    assert "推重比 1.40" in text


def test_propulsion_missing_returns_empty() -> None:
    assert _describe_propulsion({"in_world": True}) == []


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------


def test_describe_state_includes_staging_and_power() -> None:
    text = _describe_state(FLIGHT)
    assert "分级：共 11 级" in text
    assert "引擎 0/25 台点火" in text
    assert "油门闸门关着" in text
    # 顶层 stage=-1 时不要再输出旧的「当前分级 -1」
    assert "当前分级 -1" not in text


def test_describe_state_on_fire() -> None:
    text = _describe_state(ON_FIRE)
    assert "引擎 1/1 台点火" in text
    assert "推力 120.0 kN" in text


def test_describe_state_old_mod_still_works() -> None:
    text = _describe_state(OLD_MOD)
    assert "当前分级 2" in text
    assert "推力" not in text


# ---------------------------------------------------------------------------
# 指令：点火必须走 ignite（回车），不能靠空格
# ---------------------------------------------------------------------------


def _make_plugin() -> _Recorder:
    plugin = _Recorder()
    calls: List[Any] = []

    async def fake_post(command: str, value: float = 0.0) -> Dict[str, Any]:
        calls.append((command, value))
        return {"ok": True}

    plugin._post_command = fake_post  # type: ignore[method-assign]
    plugin.sent = calls
    return plugin


def test_control_sfs_accepts_ignite() -> None:
    plugin = _make_plugin()
    result = asyncio.run(plugin.control_sfs(command="ignite"))
    assert result["is_error"] is False
    assert plugin.sent == [("ignite", 0.0)]
    assert "点火" in result["output"]["message"]


def test_control_sfs_rejects_unknown() -> None:
    plugin = _make_plugin()
    result = asyncio.run(plugin.control_sfs(command="launch_missile"))
    assert result["is_error"] is True
    assert plugin.sent == []


def test_sfs_command_accepts_ignite() -> None:
    plugin = _make_plugin()
    result = asyncio.run(plugin.sfs_command(command="ignite"))
    assert result.value.get("ok") is True
    assert "点火" in result.value.get("message", "")
    assert plugin.sent == [("ignite", 0.0)]


def test_both_entry_points_accept_engine_on() -> None:
    """程序化搭出来的火箭没有分级程序，必须能强制点燃引擎。"""
    plugin = _make_plugin()
    result = asyncio.run(plugin.control_sfs(command="engine_on"))
    assert result["is_error"] is False
    assert "点燃" in result["output"]["message"]

    entry = asyncio.run(plugin.sfs_command(command="engine_on"))
    assert entry.value.get("ok") is True
    assert plugin.sent == [("engine_on", 0.0), ("engine_on", 0.0)]

