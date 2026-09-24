# 和猫娘一起造火箭（SFS）插件 —— 造火箭控制台（Hosted UI 层）
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# 本文件是 sfs_bridge 插件的一部分，以 GNU General Public License v3.0 许可发布。
# 完整条款见仓库根目录的 LICENSE 文件。

"""造火箭控制台：Hosted UI 的上下文（@ui.context）与动作（@ui.action）。

面板需要的能力分两类，故意用两种方式实现：

1. **插件里已经有的能力** —— 遥测、飞行指令、界面清单、点击、按键、零件目录、
   放置零件、火箭构成、截图。这些逻辑已经写在 ``__init__.py`` 里了，面板直接复用：
   在原方法上叠加 ``@ui.action`` 把它们暴露给面板，本模块**不重写一遍**
   （两份实现迟早会跑偏，而且改一处忘一处）。
2. **只有面板才需要的能力** —— 聚合快照、画面预览、按住/松开、蓝图、视角、
   Agent 独占模式、配置保存。集中放在本模块，免得 ``__init__.py`` 继续变长。

约定：对外只返回 JSON 安全的 ``Ok``/``Err``；面板拿到的 ``result`` 就是这个 payload。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, ui

# 面板 surface 在 plugin.toml 里声明的 context id，两边必须一致，
# 否则 host 的 get_ui_context 会报 "UI context not found"。
PANEL_CONTEXT_ID = "dashboard"

# 配置段名：与 plugin.toml / config.example.toml 保持一致。
_CFG_SECTION = "sfs_bridge"
_VLM_SECTION = "vlm_fallback"

# self.config.update() 的预算。SDK 内部会把它压到 4.5 秒以内，
# 超时也只是退化成「内存里先生效」，所以不要给太大。
_CONFIG_WRITE_TIMEOUT = 4.5

# 连不上时别让面板卡住：探测用的超时比正常请求短得多。
_PING_TIMEOUT = 3.0

# 面板按钮「按住」的自动松开上限（毫秒），防止面板崩了以后一直加速。
_MAX_HOLD_MS = 30000

# 飞控指令白名单：与 sfs_command 的 allowed 集合一致，
# 这里多带一份中文标签，好让面板不用自己维护文案。
UI_COMMANDS: List[Dict[str, Any]] = [
    {"id": "throttle_on", "label": "点火", "tone": "danger", "hint": "打开油门（不改变油门大小）"},
    {"id": "throttle_off", "label": "熄火", "tone": "default", "hint": "关闭油门"},
    {"id": "stage", "label": "执行下一级", "tone": "warning", "hint": "等同按空格：分离并点燃下一级"},
    {"id": "staging_program", "label": "分级控制程序", "tone": "default", "hint": "等同按回车"},
    {"id": "rcs_toggle", "label": "RCS 开关", "tone": "info", "hint": "切换 RCS；开启后 W/A/S/D 才有平移与俯仰"},
    {"id": "rcs_on", "label": "开 RCS", "tone": "info", "hint": "显式打开 RCS"},
    {"id": "rcs_off", "label": "关 RCS", "tone": "default", "hint": "显式关闭 RCS"},
]

# SFS 默认键位。vk 与 UnityEngine.KeyCode 数值一致。
UI_KEYS: List[Dict[str, Any]] = [
    {"vk": 81, "name": "Q", "label": "左转", "group": "转向"},
    {"vk": 69, "name": "E", "label": "右转", "group": "转向"},
    {"vk": 87, "name": "W", "label": "上抬 / 前移", "group": "RCS 平移"},
    {"vk": 83, "name": "S", "label": "下压 / 后移", "group": "RCS 平移"},
    {"vk": 65, "name": "A", "label": "左移", "group": "RCS 平移"},
    {"vk": 68, "name": "D", "label": "右移", "group": "RCS 平移"},
    {"vk": 32, "name": "空格", "label": "点火 / 下一级", "group": "常用"},
    {"vk": 13, "name": "回车", "label": "分级控制程序", "group": "常用"},
    {"vk": 82, "name": "R", "label": "RCS 开关", "group": "常用"},
    {"vk": 16, "name": "Shift", "label": "油门加大", "group": "常用"},
    {"vk": 17, "name": "Ctrl", "label": "油门减小", "group": "常用"},
    {"vk": 27, "name": "Esc", "label": "返回 / 暂停", "group": "常用"},
]


class SfsUiMixin:
    """造火箭控制台的上下文与动作，由 ``SfsBridgePlugin`` 混入。

    只依赖宿主类（``SfsBridgePlugin``）已有的属性与方法；下面的属性声明仅用于
    说明与静态检查，运行时不赋值，宿主类的 ``__init__`` 负责给真实值。
    """

    # ── 宿主类提供的运行时状态（见 __init__.py）──────────────────────────
    _bridge_url: str
    _timeout: float
    _shot_timeout: float
    _click_wait: float
    _max_width: int
    _jpeg_quality: int
    _vision_enabled: bool
    _vision_prompt: str
    _vlm: Dict[str, Any]

    logger: Any
    config: Any
    ctx: Any

    # ── 内部工具 ─────────────────────────────────────────────────────────

    def _host(self) -> Any:
        """返回宿主类所在的模块（也就是 ``__init__.py``）。

        用它复用 ``__init__.py`` 里的纯函数（``_as_text`` / ``_encode_for_vision`` …），
        而**不**写成模块级 ``from . import ...``：宿主导入本混入时它自己还没执行完，
        那样会形成循环导入。这里是在运行时按类反查，所以永远拿得到。
        """
        import sys

        return sys.modules[type(self).__module__]

    def _ui_safe_int(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        host = self._host()
        return int(host._as_int(value, default, minimum, maximum))

    def _ui_effective_config(self) -> Dict[str, Any]:
        """面板要用的「当前生效值」。

        注意：``vlm_fallback.api_key`` 是密钥，**只回报有没有配置**，
        原值不出插件进程（面板也不该拿到它）。
        """
        host = self._host()
        vlm = self._vlm if isinstance(self._vlm, dict) else {}
        return {
            "bridge_url": getattr(self, "_bridge_url", "") or "",
            "timeout_seconds": float(getattr(self, "_timeout", 12.0)),
            "post_click_wait_seconds": float(getattr(self, "_click_wait", 3.0)),
            "screenshot_timeout_seconds": float(getattr(self, "_shot_timeout", 20.0)),
            "screenshot_max_width": int(getattr(self, "_max_width", 1024)),
            "screenshot_jpeg_quality": int(getattr(self, "_jpeg_quality", 80)),
            "vision_enabled": bool(getattr(self, "_vision_enabled", True)),
            "vision_prompt": getattr(self, "_vision_prompt", "") or "",
            "vlm_enabled": host._as_bool(vlm.get("enabled")),
            "vlm_base_url": host._as_text(vlm.get("base_url")),
            "vlm_model": host._as_text(vlm.get("model")),
            "vlm_max_tokens": host._as_int(vlm.get("max_tokens"), 600, 64, 4096),
            "vlm_api_key_configured": bool(host._as_text(vlm.get("api_key"))),
        }

    def _ui_remember(self, connected: bool, error: str = "") -> None:
        self._ui_last_connected = connected
        self._ui_last_error = error if not connected else ""

    async def _ui_ping(self) -> Tuple[bool, str, int]:
        """只读连通性探测：打一次 ``/ping``。

        返回 ``(connected, error, latency_ms)``。刻意不用 ``self._get_json``，
        因为它用的是 12 秒的业务超时——游戏没开时面板会被吊住。
        """
        url = f"{str(getattr(self, '_bridge_url', '') or '').rstrip('/')}/ping"
        client = self._get_client()  # type: ignore[attr-defined]
        timeout = min(_PING_TIMEOUT, float(getattr(self, "_timeout", 12.0)))
        started = time.monotonic()
        try:
            response = await client.get(url, timeout=timeout)
        except Exception as exc:  # httpx 之外也可能是别的东西炸
            return False, self._ui_offline_message(exc), 0
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return False, f"游戏桥接服务返回状态 {response.status_code}。", latency
        return True, "", latency

    def _ui_offline_message(self, exc: Any = None) -> str:
        url = str(getattr(self, "_bridge_url", "") or "") or "（未配置地址）"
        message = (
            f"连不上游戏桥接服务（{url}）。请依次确认："
            "1) 游戏正在运行；2) 已在 Mods\\SFS-Agent\\ 装好 SFS-Agent 模组；"
            "3) 装完模组后重启过游戏；4) 模组侧的端口和这里的地址一致。"
        )
        if exc is not None:
            message += f"（底层错误：{exc}）"
        return message

    async def _ui_try_get(self, path: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """读一个接口，失败不抛异常，返回 ``(数据 或 None, 错误说明)``。"""
        try:
            data = await self._get_json(path)  # type: ignore[attr-defined]
        except SdkError as exc:
            return None, str(exc)
        except Exception as exc:  # pragma: no cover - 兜底
            self.logger.warning("[sfs_bridge] 面板读取 %s 失败：%s", path, exc)
            return None, f"读取 {path} 失败：{exc}"
        if not isinstance(data, dict):
            return None, f"{path} 返回的不是对象。"
        return data, ""

    @staticmethod
    def _ui_scene_label(state: Dict[str, Any], build: Dict[str, Any]) -> str:
        """把「现在在哪个场景」压成一个稳定标识，供面板选择要显示哪张卡片。"""
        mode = build.get("mode")
        if isinstance(mode, str) and mode in {"build", "flight", "idle"}:
            return mode
        if not state.get("in_world"):
            return "menu"
        if state.get("flying"):
            return "flight"
        return "unknown"

    # ── 面板上下文 ───────────────────────────────────────────────────────
    #
    # ⚠️ 这个 provider 会被 host 在**每次** hosted action 调用前重新求值
    # （ui_query_service 用它取动作白名单），所以它必须又快又不能抛异常：
    #   · 不做网络请求（连通性交给 sfs_ui_ping / sfs_ui_snapshot）；
    #   · 任何异常都自己吞掉，返回一个最小可用的 dict。

    @ui.context(id=PANEL_CONTEXT_ID, title="造火箭控制台")
    async def get_dashboard_ui_context(self) -> Dict[str, Any]:
        try:
            return {
                "connected": getattr(self, "_ui_last_connected", None),
                "last_error": getattr(self, "_ui_last_error", ""),
                "bridge_url": str(getattr(self, "_bridge_url", "") or ""),
                "vision_enabled": bool(getattr(self, "_vision_enabled", True)),
                "config": self._ui_effective_config(),
                "commands": list(UI_COMMANDS),
                "keys": list(UI_KEYS),
            }
        except Exception as exc:  # pragma: no cover - provider 绝不能抛
            self.logger.warning("[sfs_bridge] 面板上下文构造失败：%s", exc)
            return {"connected": None, "last_error": "", "bridge_url": "", "config": {},
                    "commands": list(UI_COMMANDS), "keys": list(UI_KEYS)}

    # ── 连接与快照 ───────────────────────────────────────────────────────

    @ui.action(id="sfs_ui_ping", label="检测连接", tone="info", group="连接", order=10)
    @plugin_entry(
        id="sfs_ui_ping",
        name="检测游戏连接",
        description=(
            "面板用：探测 SFS-Agent 桥接服务是否在线。只发一次 /ping，"
            "不读遥测也不碰游戏状态，适合面板按秒轮询。"
        ),
        input_schema={"type": "object", "properties": {}},
        timeout=15,
        llm_result_fields=["connected", "message"],
    )
    async def sfs_ui_ping(self, **_):
        connected, error, latency = await self._ui_ping()
        self._ui_remember(connected, error)
        if not connected:
            return Ok({
                "connected": False,
                "latency_ms": 0,
                "bridge_url": str(getattr(self, "_bridge_url", "") or ""),
                "message": error or "连不上游戏。",
            })
        return Ok({
            "connected": True,
            "latency_ms": latency,
            "bridge_url": str(getattr(self, "_bridge_url", "") or ""),
            "message": f"已连接（{latency} 毫秒）",
        })

    @ui.action(id="sfs_ui_snapshot", label="刷新面板数据", tone="primary", group="连接", order=20)
    @plugin_entry(
        id="sfs_ui_snapshot",
        name="读取造火箭面板快照",
        description=(
            "面板用：一次性读取连接状态、飞行遥测、火箭构成、界面元素与蓝图列表。"
            "面板打开、点刷新、以及自动刷新时调用；比逐个 entry 调用少绕几趟。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_ui": {
                    "type": "boolean",
                    "description": "是否顺带枚举当前界面的可点击元素（默认 true）",
                },
                "include_blueprints": {
                    "type": "boolean",
                    "description": "是否顺带读取蓝图列表（默认 true）",
                },
            },
        },
        timeout=45,
        llm_result_fields=["connected", "scene", "summary"],
    )
    async def sfs_ui_snapshot(
        self,
        include_ui: bool = True,
        include_blueprints: bool = True,
        **_,
    ):
        host = self._host()
        connected, error, latency = await self._ui_ping()
        self._ui_remember(connected, error)
        if not connected:
            return Ok({
                "connected": False,
                "error": error,
                "latency_ms": 0,
                "bridge_url": str(getattr(self, "_bridge_url", "") or ""),
                "telemetry": {},
                "telemetry_summary": error,
                "ui": {"count": 0, "elements": []},
                "build": {},
                "build_summary": "",
                "blueprints": [],
                "scene": "offline",
                "fetched_at": time.time(),
            })

        async def _load(path: str, enabled: bool) -> Tuple[Dict[str, Any], str]:
            if not enabled:
                return {}, ""
            data, failure = await self._ui_try_get(path)
            return (data or {}), failure

        (state, state_err), (build, build_err), (ui_data, ui_err), (bp_data, bp_err) = (
            await asyncio.gather(
                _load("/state", True),
                _load("/build", True),
                _load("/ui", bool(include_ui)),
                _load("/blueprints", bool(include_blueprints)),
            )
        )

        elements = ui_data.get("elements") if isinstance(ui_data.get("elements"), list) else []
        blueprints = [
            str(item)
            for item in (bp_data.get("blueprints") or [])
            if isinstance(item, (str, int, float))
        ]
        scene = self._ui_scene_label(state, build)
        summary = host._describe_state(state) if state else ""

        errors = {
            key: value
            for key, value in {
                "state": state_err,
                "build": build_err,
                "ui": ui_err,
                "blueprints": bp_err,
            }.items()
            if value
        }

        return Ok({
            "connected": True,
            "error": "",
            "latency_ms": latency,
            "bridge_url": str(getattr(self, "_bridge_url", "") or ""),
            "telemetry": state,
            "telemetry_summary": summary,
            "ui": {"count": ui_data.get("count", len(elements)), "elements": elements},
            "build": build,
            "build_summary": host._describe_build(build) if build else "",
            "blueprints": blueprints,
            "scene": scene,
            "errors": errors,
            "fetched_at": time.time(),
        })

    # ── 画面预览 ─────────────────────────────────────────────────────────

    @ui.action(id="sfs_ui_screenshot", label="抓取画面", tone="info", group="画面", order=10)
    @plugin_entry(
        id="sfs_ui_screenshot",
        name="抓取游戏画面（面板预览）",
        description=(
            "面板用：截一张游戏画面并交给宿主临时图片接口，返回可直接显示的 URL；"
            "宿主接口不可用时退回内联数据 URL。它只给面板看，不会送进模型。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "inline": {
                    "type": "boolean",
                    "description": "强制返回内联 data URL（不走宿主临时图片接口）",
                },
            },
        },
        # 内部截图超时最长 60 秒，再加宿主图片接口的 8 秒，所以这里留到 90。
        timeout=90,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_ui_screenshot(self, inline: bool = False, **_):
        png = await self._capture_screen()  # type: ignore[attr-defined]
        if not png:
            return Ok({
                "ok": False,
                "message": (
                    "截图失败。请确认游戏正在运行、已安装 SFS-Agent 模组，"
                    "并且游戏窗口没有被最小化。"
                ),
            })

        host = self._host()
        # 面板预览不需要模型级的清晰度：缩到 1280 以内、质量不超过 72，
        # 免得为了看一眼画面搬 2MB 的 base64 穿过 ZMQ。
        preview_width = max(320, min(int(getattr(self, "_max_width", 1024)), 1280))
        preview_quality = max(30, min(int(getattr(self, "_jpeg_quality", 80)), 72))
        encoded, mime, note = host._encode_for_vision(
            png, max_width=preview_width, quality=preview_quality
        )

        if not inline:
            # 优先走宿主的临时图片接口：base64 留在 host 侧，面板只拿一个 URL。
            try:
                part = await self.ctx.images.upload(png, mime="image/png", timeout=8.0)
                url = part.get("url") if isinstance(part, dict) else ""
                if isinstance(url, str) and url.strip():
                    return Ok({
                        "ok": True,
                        "url": url.strip(),
                        "mime": "image/jpeg",
                        "inline": False,
                        "png_bytes": len(png),
                        "note": note,
                        "message": f"已抓取画面（{note}）。",
                    })
            except Exception as exc:
                self.logger.warning("[sfs_bridge] 临时图片上传失败，退回内联：%s", exc)

        if encoded is None:
            return Ok({"ok": False, "message": f"画面已抓取但无法处理：{note}"})
        return Ok({
            "ok": True,
            "url": f"data:{mime};base64,{encoded}",
            "mime": mime,
            "inline": True,
            "png_bytes": len(png),
            "note": note,
            "message": f"已抓取画面（{note}，内联返回）。",
        })

    # ── 按住 / 松开 ──────────────────────────────────────────────────────

    @ui.action(id="sfs_ui_hold_key", label="按住按键", tone="warning", group="飞行", order=40)
    @plugin_entry(
        id="sfs_ui_hold_key",
        name="按住游戏按键（面板）",
        description=(
            "面板用：按下并一直按住某个键（RCS 平移、持续转向要用）。"
            "给了 hold_ms 就自动松开，避免面板关掉后还在加速。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vk": {"type": "integer", "description": "虚拟键码，例如 W=87"},
                "hold_ms": {
                    "type": "integer",
                    "description": f"自动松开的毫秒数；0 表示一直按住（上限 {_MAX_HOLD_MS}）",
                },
            },
        },
        timeout=45,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_ui_hold_key(self, vk: int = 0, hold_ms: int = 0, **_):
        host = self._host()
        try:
            code = int(vk)
        except (TypeError, ValueError):
            return Err(SdkError("需要提供虚拟键码 vk。"))
        if code <= 0:
            return Err(SdkError("需要提供虚拟键码 vk（例如 W=87、Q=81）。"))

        try:
            result = await self._post_json("/key_down", {"vk": code})  # type: ignore[attr-defined]
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("[sfs_bridge] 面板按住失败：%s", exc)
            return Err(SdkError(f"按住失败（未送达）：{exc}"))

        detail = host._as_dict(result)
        if not host._as_bool(detail.get("ok")):
            reason = host._as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(f"按住失败：{reason}"))

        key = host._as_text(detail.get("key")) or str(code)
        already = host._as_bool(detail.get("already_held"))
        message = f"已按住 {key}"
        if already:
            message += "（此前已在按住）"

        if isinstance(hold_ms, int) and hold_ms > 0:
            await asyncio.sleep(min(hold_ms, _MAX_HOLD_MS) / 1000.0)
            try:
                await self._post_json("/key_up", {"vk": code})  # type: ignore[attr-defined]
                message += f"，已自动松开（{min(hold_ms, _MAX_HOLD_MS)} 毫秒）"
            except Exception as exc:  # pragma: no cover
                message += f"，但自动松开失败：{exc}"
        else:
            message += "，用完记得点「全部松开」"

        return Ok({"ok": True, "held": key, "already_held": already, "message": message})

    @ui.action(id="sfs_ui_release_key", label="松开按键", tone="default", group="飞行", order=50)
    @plugin_entry(
        id="sfs_ui_release_key",
        name="松开游戏按键（面板）",
        description=(
            "面板用：松开之前按住的键；不填 vk 就松开全部（面板上的「全部松开」急停）。"
            "持续推力用完必须松开，否则会一直朝那个方向加速。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vk": {"type": "integer", "description": "要松开的虚拟键码；0 表示全部松开"},
            },
        },
        timeout=30,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_ui_release_key(self, vk: int = 0, **_):
        host = self._host()
        try:
            code = int(vk or 0)
        except (TypeError, ValueError):
            code = 0

        try:
            if code > 0:
                result = await self._post_json("/key_up", {"vk": code})  # type: ignore[attr-defined]
            else:
                result = await self._post_json("/release_all", {})  # type: ignore[attr-defined]
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("[sfs_bridge] 面板松开失败：%s", exc)
            return Err(SdkError(f"松开失败（未送达）：{exc}"))

        detail = host._as_dict(result)
        if not host._as_bool(detail.get("ok")):
            return Err(SdkError("松开失败。"))

        if code > 0:
            key = host._as_text(detail.get("key")) or str(code)
            was_held = host._as_bool(detail.get("was_held"))
            message = f"已松开 {key}" + ("" if was_held else "（它本来就没被按住）")
        else:
            released = detail.get("released", 0)
            message = f"已全部松开（{released} 个按住的键）"
        return Ok({"ok": True, "message": message, "held": detail.get("held")})

    # ── 蓝图 ─────────────────────────────────────────────────────────────

    @ui.action(id="sfs_ui_blueprints", label="读取蓝图列表", tone="info", group="建造", order=10)
    @plugin_entry(
        id="sfs_ui_blueprints",
        name="读取蓝图列表（面板）",
        description="面板用：列出玩家存档里的火箭蓝图名字。",
        input_schema={"type": "object", "properties": {}},
        timeout=40,
        llm_result_fields=["ok", "count", "message"],
    )
    async def sfs_ui_blueprints(self, **_):
        try:
            data = await self._get_json("/blueprints")  # type: ignore[attr-defined]
        except SdkError as exc:
            return Ok({"ok": False, "count": 0, "blueprints": [], "message": str(exc)})
        except Exception as exc:  # pragma: no cover
            self.logger.warning("[sfs_bridge] 面板读取蓝图失败：%s", exc)
            return Ok({"ok": False, "count": 0, "blueprints": [], "message": f"读取蓝图失败：{exc}"})

        items = [
            str(item)
            for item in (data.get("blueprints") or [])
            if isinstance(item, (str, int, float))
        ]
        return Ok({
            "ok": True,
            "count": data.get("count", len(items)),
            "blueprints": items,
            "message": f"共 {len(items)} 个蓝图。",
        })

    @ui.action(
        id="sfs_ui_load_blueprint",
        label="载入蓝图",
        tone="warning",
        group="建造",
        order=20,
        confirm="载入蓝图会替换建造台里当前的火箭（未保存的改动会丢失）。继续？",
    )
    @plugin_entry(
        id="sfs_ui_load_blueprint",
        name="载入火箭蓝图（面板）",
        description=(
            "面板用：把整枚蓝图加载进建造场景。名字必须是 sfs_ui_blueprints 返回过的；"
            "会替换建造台里当前的火箭。"
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "蓝图名字"}},
        },
        timeout=120,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_ui_load_blueprint(self, name: str = "", **_):
        host = self._host()
        clean = host._as_text(name)
        if not clean:
            return Err(SdkError("需要蓝图名字；先刷新一下蓝图列表。"))
        try:
            result = await self._post_json("/blueprint_load", {"name": clean})  # type: ignore[attr-defined]
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("[sfs_bridge] 面板载入蓝图失败：%s", exc)
            return Err(SdkError(f"载入蓝图失败：{exc}"))

        detail = host._as_dict(result)
        if not host._as_bool(detail.get("ok")):
            reason = host._as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(
                f"载入蓝图失败：{reason}。请确认游戏处于建造场景（不是主菜单或飞行中），"
                "且名字与蓝图列表一致。"
            ))
        return Ok({
            "ok": True,
            "message": host._as_text(detail.get("result")) or f"已载入蓝图「{clean}」。",
        })

    # ── 视角 ─────────────────────────────────────────────────────────────

    @ui.action(id="sfs_ui_camera", label="调整视角", tone="default", group="飞行", order=60)
    @plugin_entry(
        id="sfs_ui_camera",
        name="调整游戏视角（面板）",
        description=(
            "面板用：调整游戏视角。zoom_delta 正数拉远、负数拉近；"
            "distance / x / y / rotation 是绝对值。只传想改的字段。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "zoom_delta": {"type": "number", "description": "相对缩放：正数拉远、负数拉近"},
                "distance": {"type": "number", "description": "绝对相机距离"},
                "x": {"type": "number", "description": "相机中心 x"},
                "y": {"type": "number", "description": "相机中心 y"},
                "rotation": {"type": "number", "description": "相机角度（度）"},
            },
        },
        timeout=30,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_ui_camera(
        self,
        zoom_delta: Optional[float] = None,
        distance: Optional[float] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        rotation: Optional[float] = None,
        **_,
    ):
        host = self._host()
        payload: Dict[str, float] = {}
        for key, value in (
            ("zoom_delta", zoom_delta),
            ("distance", distance),
            ("x", x),
            ("y", y),
            ("rotation", rotation),
        ):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[key] = float(value)
        if not payload:
            return Err(SdkError(
                "需要至少给一个参数：zoom_delta（正数拉远 / 负数拉近）、distance、x、y、rotation。"
            ))

        try:
            result = await self._post_json("/camera", payload)  # type: ignore[attr-defined]
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("[sfs_bridge] 面板调整视角失败：%s", exc)
            return Err(SdkError(f"调整视角失败：{exc}"))

        detail = host._as_dict(result)
        if not host._as_bool(detail.get("ok")):
            reason = host._as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(f"调整视角失败：{reason}"))
        return Ok({
            "ok": True,
            "message": host._as_text(detail.get("result")) or "已调整视角。",
            "applied": payload,
        })

    # ── Agent 独占模式 ───────────────────────────────────────────────────

    @ui.action(
        id="sfs_ui_exclusive",
        label="Agent 独占模式",
        tone="danger",
        group="高级",
        order=10,
        confirm="开启独占模式后，游戏会忽略你自己的鼠标与键盘，只接受猫娘的操作。确定要开吗？",
    )
    @plugin_entry(
        id="sfs_ui_exclusive",
        name="切换 Agent 独占模式（面板）",
        description=(
            "面板用：切换模组的 Agent 独占模式。开启后游戏忽略用户自己的鼠标与键盘，"
            "只接受 agent 派发的输入；游戏内按 F10 可应急解除。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true 开启、false 关闭"},
            },
        },
        timeout=20,
        llm_result_fields=["ok", "exclusive", "message"],
    )
    async def sfs_ui_exclusive(self, on: bool = False, **_):
        host = self._host()
        # 模组侧用的是 ExtractString（只认带引号的值），所以这里必须发字符串，
        # 发 JSON 布尔会被当成「没给」，变成切换而不是设定。
        payload = {"on": "true" if host._as_bool(on) else "false"}
        try:
            result = await self._post_json("/exclusive", payload)  # type: ignore[attr-defined]
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("[sfs_bridge] 面板切换独占模式失败：%s", exc)
            return Err(SdkError(f"切换独占模式失败：{exc}"))

        detail = host._as_dict(result)
        if not host._as_bool(detail.get("ok")):
            reason = host._as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(f"切换独占模式失败：{reason}"))

        exclusive = host._as_bool(detail.get("exclusive"))
        return Ok({
            "ok": True,
            "exclusive": exclusive,
            "overlay": host._as_text(detail.get("overlay")),
            "message": "已开启独占模式：游戏忽略你的鼠标与键盘（F10 应急解除）。"
            if exclusive else "已关闭独占模式：你的操作恢复正常。",
        })

    # ── 设置保存 ─────────────────────────────────────────────────────────

    @ui.action(
        id="sfs_ui_save_settings",
        label="保存设置",
        tone="success",
        group="设置",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="sfs_ui_save_settings",
        name="保存造火箭面板设置",
        description=(
            "面板用：把桥接地址、超时、截图与视觉参数写回插件配置并立即生效。"
            "vlm_api_key 留空表示保持原值不变（密钥不回显，也不会被清掉）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bridge_url": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "post_click_wait_seconds": {"type": "number"},
                "screenshot_timeout_seconds": {"type": "number"},
                "screenshot_max_width": {"type": "integer"},
                "screenshot_jpeg_quality": {"type": "integer"},
                "vision_enabled": {"type": "boolean"},
                "vision_prompt": {"type": "string"},
                "vlm_enabled": {"type": "boolean"},
                "vlm_base_url": {"type": "string"},
                "vlm_model": {"type": "string"},
                "vlm_max_tokens": {"type": "integer"},
                "vlm_api_key": {"type": "string", "writeOnly": True},
            },
        },
        timeout=30,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_ui_save_settings(
        self,
        bridge_url: str = "",
        timeout_seconds: Optional[float] = None,
        post_click_wait_seconds: Optional[float] = None,
        screenshot_timeout_seconds: Optional[float] = None,
        screenshot_max_width: Optional[int] = None,
        screenshot_jpeg_quality: Optional[int] = None,
        vision_enabled: Optional[bool] = None,
        vision_prompt: Optional[str] = None,
        vlm_enabled: Optional[bool] = None,
        vlm_base_url: Optional[str] = None,
        vlm_model: Optional[str] = None,
        vlm_max_tokens: Optional[int] = None,
        vlm_api_key: Optional[str] = None,
        **_,
    ):
        host = self._host()
        current = self._ui_effective_config()

        # ── 校验：与 __init__.py 的 on_startup 用同一套范围，避免两处漂移 ──
        clean_url = host._as_text(bridge_url) or current["bridge_url"]
        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            return Err(SdkError(
                "桥接地址必须以 http:// 或 https:// 开头，"
                "例如 http://127.0.0.1:21578。"
            ))

        def _number(value: Any, default: float, low: float, high: float) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return float(default)
            if number != number:  # NaN
                return float(default)
            return min(max(number, low), high)

        section = {
            "bridge_url": clean_url,
            "timeout_seconds": _number(
                timeout_seconds, current["timeout_seconds"], 3.0, 60.0
            ),
            "post_click_wait_seconds": _number(
                post_click_wait_seconds, current["post_click_wait_seconds"], 0.5, 30.0
            ),
            "screenshot_timeout_seconds": _number(
                screenshot_timeout_seconds, current["screenshot_timeout_seconds"], 5.0, 60.0
            ),
            "screenshot_max_width": self._ui_safe_int(
                screenshot_max_width, current["screenshot_max_width"], 320, 2048
            ),
            "screenshot_jpeg_quality": self._ui_safe_int(
                screenshot_jpeg_quality, current["screenshot_jpeg_quality"], 30, 95
            ),
            "vision_enabled": host._as_bool(
                vision_enabled, current["vision_enabled"]
            ),
            "vision_prompt": host._as_text(vision_prompt) or current["vision_prompt"],
        }
        vlm_section = {
            "enabled": host._as_bool(vlm_enabled, current["vlm_enabled"]),
            "base_url": host._as_text(vlm_base_url) if vlm_base_url is not None
            else current["vlm_base_url"],
            "model": host._as_text(vlm_model) if vlm_model is not None
            else current["vlm_model"],
            "max_tokens": self._ui_safe_int(
                vlm_max_tokens, current["vlm_max_tokens"], 64, 4096
            ),
        }
        # 密钥：只在面板真的提交了新值时才写，留空 = 保持原样。
        new_key = host._as_text(vlm_api_key)
        if new_key:
            vlm_section["api_key"] = new_key

        patch = {_CFG_SECTION: section, _VLM_SECTION: vlm_section}
        try:
            await self.config.update(patch, timeout=_CONFIG_WRITE_TIMEOUT)
        except Exception as exc:
            self.logger.warning("[sfs_bridge] 面板保存配置失败：%s", exc)
            return Err(SdkError(f"保存设置失败：{exc}"))

        # 立刻应用到运行时：本插件没有 config_change 钩子，等框架回调就晚了。
        self._bridge_url = section["bridge_url"]
        self._timeout = section["timeout_seconds"]
        self._shot_timeout = section["screenshot_timeout_seconds"]
        self._click_wait = section["post_click_wait_seconds"]
        self._max_width = section["screenshot_max_width"]
        self._jpeg_quality = section["screenshot_jpeg_quality"]
        self._vision_enabled = section["vision_enabled"]
        self._vision_prompt = section["vision_prompt"]
        merged_vlm = dict(self._vlm) if isinstance(self._vlm, dict) else {}
        merged_vlm.update(vlm_section)
        self._vlm = merged_vlm

        return Ok({
            "ok": True,
            "config": self._ui_effective_config(),
            "message": "设置已保存并立即生效。"
            + ("（自带视觉模型已启用）" if merged_vlm.get("enabled") else ""),
        })


__all__ = ["SfsUiMixin", "PANEL_CONTEXT_ID", "UI_COMMANDS", "UI_KEYS"]