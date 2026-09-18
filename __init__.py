# 和猫娘一起造火箭（SFS）插件 —— 连接 Spaceflight Simulator
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""SFS Bridge Plugin（和猫娘一起造火箭）

连接 Spaceflight Simulator（航天模拟器），让猫娘：
- 读取飞行遥测（高度、速度、油门、分级）
- **查看游戏画面**（交给 N.E.K.O. 的视觉聊天模型识别）
- 对火箭设计给出建议

依赖：游戏内需安装配套的 SFS-Agent 模组（见 ../sfs-agent）。
该模组在 127.0.0.1:21578 提供 HTTP 接口：/state、/command、/screenshot。
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any, Dict, List, Optional, Tuple

import httpx
from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

# N.E.K.O. 对工具返回图片的限制（见 plugin/sdk/plugin/llm_tool.py）
_MAX_BASE64_CHARS = 2 * 1024 * 1024
_MAX_IMAGES_PER_CALL = 2

# 操作界面的流程提示。随工具返回值一起交给模型，这样猫娘在调用时就能拿到，
# 不依赖任何外部文档或角色设定。
_UI_AGENT_GUIDE = (
    "【看界面并操作】"
    "❗❗ 点按钮**必须**调用 click_sfs_ui 工具，没有别的途径。"
    "如果你发现自己在说「本喵点了」「爪子按上去了」「已经按了」这类话 —— "
    "那就说明你**忘了调用工具**，游戏此刻没有任何反应，用户会以为插件坏了。"
    "① list_sfs_ui 拿可点击元素清单（或 see_sfs_screen 看画面）；"
    "② 用 click_sfs_ui 点击，**推荐直接报元素名字**（如 name=\"Play\"），"
    "比记序号或坐标都可靠；也可以给 index 或 x/y。"
    "**只有真的调用 click_sfs_ui 才会点下去** —— "
    "只是嘴上说「我点了」游戏不会有任何变化。"
    "click_sfs_ui 的返回值里有 delivered 字段："
    "true 才表示真的送达了游戏，false 就是没发出去，不要当成点过了。"
    "③ click_sfs_ui 返回里已经带了点击后的**新界面清单**，直接看那个就行。"
    "❗游戏加载和切场景**很慢**，经常要几秒才有反应。"
    "click_sfs_ui 内部已经帮你等了一会儿再回读界面——"
    "所以界面没变化时**先别急着说「没反应」，再等一等或重新 list_sfs_ui 确认**，"
    "连续两三次都一样，才能判定操作无效。"
    "点 Play 进存档列表后，必须先点一张存档卡片，"
    "Play/Rename/Delete 才会变成可用（未选中存档时它们是灰的，会被自动过滤掉）。"
    "x/y 是 0-1 比例：左上角 (0,0)、右下角 (1,1)，与 list_sfs_ui 返回的坐标同一套。"
    "点击是游戏内事件注入，**不会移动用户的鼠标**，可以放心点；"
    "但它会真实改变游戏状态（开始游戏、载入存档等），动手前先想清楚。"
    "【SFS 默认操作方法】"
    "转向：Q 向左 / E 向右；"
    "平移与俯仰：W/S/A/D（需先按 R 打开 RCS）；"
    "油门：Shift 加大 / Ctrl 减小；"
    "RCS 开关：R；"
    "点火 / 执行下一级：空格；"
    "分级控制程序：回车。"
    "常用虚拟键码：Q=81 E=69 W=87 A=65 S=83 D=68 R=82 空格=32 回车=13 Shift=16 Ctrl=17 Esc=27。"
    "按键同样是游戏内注入，游戏不需要在前台。"
    "【持续推力要按住，别脉冲式点】"
    "RCS 平移（W/S/A/D）本质是持续推力：按一下就松，在高速下几乎推不出位移，"
    "算不准该转多少度。正确做法是 —— "
    "hold_sfs_key 按住 → get_sfs_status 看数据 → release_sfs_key 松开。"
    "**用完务必松开**，不然会一直朝那个方向加速。"
    "【造火箭（重要）】"
    "建造界面里零件要从左侧菜单**拖**到火箭上，纯点击放不上去。"
    "两条可用路径："
    "① **加载现成蓝图**（最可靠）：list_sfs_blueprints 列出玩家存档里的蓝图，"
    "load_sfs_blueprint 直接把整枚火箭生成到建造场景 —— "
    "坐标、零件尺寸（N）、纹理（T）、分级全部由游戏自己解析，一定是正确可飞的。"
    "用户说「搭一个 XX」「载入我的某枚火箭」时优先用这条。"
    "② place_sfs_part：在指定网格坐标放**单个零件**。"
    "注意单个零件是自己拼的，需要给对间距才连得成一体，"
    "不如直接加载蓝图可靠。"
    "【飞行控制】"
    "优先用 control_sfs 直接设油门或分级 —— 它不走按键，最可靠。"
    "查不到的东西如实说不知道，绝对不要编造飞行数据或界面内容。"
)

DEFAULT_BRIDGE_URL = "http://127.0.0.1:21578"
_USER_AGENT = "N.E.K.O-sfs-bridge-plugin/0.1"

# 点击之后等多久再回读界面（默认值）。
# 游戏切场景/加载很慢，立刻回读往往还是旧界面，模型就会误判成「点了没反应」。
# 用户可在 plugin.toml 的 post_click_wait_seconds 里调整。
_POST_CLICK_WAIT = 3.0


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def _describe_state(state: Dict[str, Any]) -> str:
    """把遥测数据转成一句中文描述。"""
    if not state.get("in_world"):
        return "游戏当前不在世界场景中（可能停在主菜单或正在加载）。"

    parts: List[str] = []
    rocket = _as_text(state.get("rocket"))
    if rocket:
        parts.append(f"当前飞行器：{rocket}")
    planet = _as_text(state.get("planet"))
    if planet:
        parts.append(f"所在天体：{planet}")

    height = state.get("height")
    if isinstance(height, (int, float)):
        parts.append(f"高度 {height:.1f} 米")

    speed = state.get("speed")
    if isinstance(speed, (int, float)):
        parts.append(f"速度 {speed:.1f} 米/秒")

    throttle = state.get("throttle")
    if isinstance(throttle, (int, float)):
        parts.append(f"油门 {throttle * 100:.0f}%")

    stage = state.get("stage")
    if isinstance(stage, int) and stage >= 0:
        parts.append(f"当前分级 {stage}")

    mass = state.get("mass")
    if isinstance(mass, (int, float)) and mass:
        parts.append(f"质量 {mass:.1f} 吨")

    if not state.get("flying"):
        parts.append("（当前不在飞行中，可能处于建造场景）")

    return "；".join(parts) if parts else "已连接游戏，但暂时读不到有效遥测。"


def _summarize_ui(data: Dict[str, Any], limit: int = 20) -> str:
    """把 /ui 返回的元素清单整理成可读文本。"""
    elements = data.get("elements") or []
    lines: List[str] = []
    for item in elements[:limit]:
        if not isinstance(item, dict):
            continue
        label = _as_text(item.get("label")) or "（无标签）"
        index = item.get("index")
        x = item.get("x")
        y = item.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            lines.append(f"#{index} {label}  → ({x:.3f}, {y:.3f})")
        else:
            lines.append(f"#{index} {label}  → 无坐标")
    return "\n".join(lines)



def _find_element(data: Dict[str, Any], want: str) -> Optional[Dict[str, Any]]:
    """在 /ui 清单里按名字找元素。

    匹配策略（从严到宽）：
      1. 完全相等（忽略大小写）
      2. 忽略大小写的子串包含
      3. 去掉空格后再比一次

    找不到返回 None；多个命中时取第一个。
    """
    elements = data.get("elements") or []
    want_l = want.strip().lower()
    want_compact = want_l.replace(" ", "")
    if not want_l:
        return None

    fallback: Optional[Dict[str, Any]] = None
    for item in elements:
        if not isinstance(item, dict):
            continue
        label = _as_text(item.get("label") or "")
        if not label:
            continue
        lab_l = label.strip().lower()
        if lab_l == want_l:
            return item
        if want_l in lab_l or lab_l in want_l:
            if fallback is None:
                fallback = item
        elif want_compact and want_compact in lab_l.replace(" ", ""):
            if fallback is None:
                fallback = item
    return fallback


def _describe_build(build: Dict[str, Any]) -> str:
    """把火箭设计（零件构成）转成中文描述。"""
    count = build.get("part_count")
    if not isinstance(count, int) or count <= 0:
        return (
            "当前场景里没有读到火箭零件。"
            "可能不在建造场景、火箭还没开始搭建，或者游戏没有运行。"
        )

    mode = _as_text(build.get("mode"))
    mode_label = {
        "build": "建造场景",
        "flight": "飞行场景",
        "idle": "空闲",
    }.get(mode, mode or "未知场景")

    parts: List[str] = [f"当前在{mode_label}，火箭共 {count} 个零件"]

    mass = build.get("total_mass")
    if isinstance(mass, (int, float)) and mass:
        parts.append(f"总质量约 {mass:.2f} 吨")

    stages = build.get("stage_count")
    if isinstance(stages, int) and stages > 0:
        parts.append(f"{stages} 个分级")

    kinds = build.get("part_kinds") or []
    if isinstance(kinds, list):
        detail_items: List[str] = []
        for item in kinds[:8]:
            if not isinstance(item, dict):
                continue
            name = _as_text(item.get("name"))
            number = item.get("count")
            if name and isinstance(number, int):
                detail_items.append(f"{name}×{number}")
        if detail_items:
            parts.append("主要零件：" + "、".join(detail_items))

    return "；".join(parts) + "。"


def _encode_for_vision(
    png_bytes: bytes,
    *,
    max_width: int,
    quality: int,
) -> Tuple[Optional[str], str, str]:
    """把 PNG 截图压缩成适合交给视觉模型的 JPEG base64。

    返回 (base64 或 None, mime, 说明)。截图可能是 1080p 以上的 PNG，
    原始体积常常超过 N.E.K.O. 对单张图片 2MB 的限制，因此必须缩放并把
    质量逐级下调，直到满足限制。
    """
    try:
        from PIL import Image
    except ImportError:
        # 没有 Pillow 时，只有在原始体积够小的情况下才直接返回
        encoded = base64.b64encode(png_bytes).decode("ascii")
        if len(encoded) <= _MAX_BASE64_CHARS:
            return encoded, "image/png", "未安装 Pillow，使用原始 PNG"
        return None, "", "未安装 Pillow，且原始截图超出大小限制"

    try:
        image = Image.open(io.BytesIO(png_bytes))
        if image.mode != "RGB":
            image = image.convert("RGB")

        if image.width > max_width:
            ratio = max_width / float(image.width)
            image = image.resize(
                (max_width, max(1, int(image.height * ratio))),
                Image.LANCZOS,
            )

        for q in (quality, 70, 60, 50, 40, 30):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=q, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            if len(encoded) <= _MAX_BASE64_CHARS:
                return encoded, "image/jpeg", f"{image.width}x{image.height} q={q}"

        return None, "", "压缩后仍超出大小限制"
    except Exception as exc:  # pragma: no cover - 图像处理兜底
        return None, "", f"图像处理失败：{exc}"


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

@neko_plugin
class SfsBridgePlugin(NekoPluginBase):
    """航天模拟器桥接插件。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        self._cfg: Dict[str, Any] = {}
        self._bridge_url: str = DEFAULT_BRIDGE_URL
        self._timeout: float = 12.0
        self._shot_timeout: float = 20.0
        self._max_width: int = 1024
        self._jpeg_quality: int = 80
        self._vision_enabled: bool = True
        self._vision_prompt: str = ""
        self._vlm: Dict[str, Any] = {}

        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Any = None

    # -- 基础设施 ---------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """按事件循环缓存客户端（宿主会在不同 asyncio.run 中调用）。"""
        import asyncio

        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            loop = None

        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._timeout,
                headers={"User-Agent": _USER_AGENT},
            )
            self._client_loop = loop
        return self._client

    async def _get_json(self, path: str) -> Dict[str, Any]:
        """GET 一个 JSON 接口。"""
        url = f"{self._bridge_url.rstrip('/')}{path}"
        client = self._get_client()
        try:
            response = await client.get(url, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise SdkError("连接游戏超时，请确认游戏正在运行。") from exc
        except httpx.HTTPError as exc:
            raise SdkError(
                f"连不上游戏桥接服务（{self._bridge_url}）。"
                f"请确认：1) 游戏正在运行；2) 已安装 SFS-Agent 模组；3) 已重启过游戏。"
            ) from exc

        if response.status_code >= 400:
            raise SdkError(f"游戏桥接服务返回状态 {response.status_code}。")
        try:
            return response.json()
        except ValueError as exc:
            raise SdkError("游戏桥接服务返回的内容不是合法 JSON。") from exc

    async def _post_command(self, name: str, value: float = 0.0) -> Dict[str, Any]:
        """向游戏发送一条控制指令。"""
        url = f"{self._bridge_url.rstrip('/')}/command"
        client = self._get_client()
        try:
            response = await client.post(
                url,
                json={"name": name, "value": value},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise SdkError("发送指令超时。") from exc
        except httpx.HTTPError as exc:
            raise SdkError(f"无法连接游戏桥接服务：{exc}") from exc

        if response.status_code >= 400:
            raise SdkError(f"游戏桥接服务返回状态 {response.status_code}。")
        try:
            return response.json()
        except ValueError:
            return {"ok": True}

    async def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """向游戏桥接服务发送一个带 JSON 体的 POST 请求。"""
        url = f"{self._bridge_url.rstrip('/')}{path}"
        client = self._get_client()
        try:
            response = await client.post(url, json=payload, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise SdkError("请求游戏超时。") from exc
        except httpx.HTTPError as exc:
            raise SdkError(f"无法连接游戏桥接服务：{exc}") from exc

        if response.status_code >= 400:
            raise SdkError(f"游戏桥接服务返回状态 {response.status_code}。")
        try:
            return response.json()
        except ValueError:
            return {"ok": True}

    async def _capture_screen(self) -> Optional[bytes]:
        """抓取一张游戏画面，失败返回 None。"""
        url = f"{self._bridge_url.rstrip('/')}/screenshot"
        client = self._get_client()
        try:
            response = await client.get(url, timeout=self._shot_timeout)
        except httpx.HTTPError as exc:
            self.logger.warning("截图失败: {}", exc)
            return None

        if response.status_code >= 400:
            self.logger.warning("截图返回状态 {}", response.status_code)
            return None
        if not response.content:
            return None
        return response.content

    async def _call_fallback_vlm(self, image_b64: str, mime: str, prompt: str) -> str:
        """调用用户自配的视觉接口，把画面转成文字描述。"""
        base_url = _as_text(self._vlm.get("base_url"))
        model = _as_text(self._vlm.get("model"))
        api_key = _as_text(self._vlm.get("api_key"))
        if not base_url or not model:
            return ""

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "max_tokens": _as_int(self._vlm.get("max_tokens"), 600, 64, 4096),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                        },
                    ],
                }
            ],
        }

        client = self._get_client()
        try:
            response = await client.post(
                url, json=payload, headers=headers, timeout=self._shot_timeout
            )
        except httpx.HTTPError as exc:
            self.logger.warning("自带视觉模型调用失败: {}", exc)
            return ""

        if response.status_code >= 400:
            self.logger.warning("自带视觉模型返回 {}", response.status_code)
            return ""

        try:
            data = response.json()
        except ValueError:
            return ""

        choices = data.get("choices") or []
        if not choices:
            return ""
        message = _as_dict(_as_dict(choices[0]).get("message"))
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts = [
                _as_text(_as_dict(part).get("text"))
                for part in content
                if isinstance(part, dict)
            ]
            return " ".join(t for t in texts if t)
        return ""

    # -- 生命周期 ---------------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        try:
            cfg = await self.config.dump(timeout=5.0)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("读取配置失败，使用默认值: {}", exc)
            cfg = {}
        cfg = cfg if isinstance(cfg, dict) else {}
        self._cfg = cfg

        section = _as_dict(cfg.get("sfs_bridge"))
        self._bridge_url = _as_text(section.get("bridge_url")) or DEFAULT_BRIDGE_URL

        try:
            self._timeout = float(section.get("timeout_seconds", 12))
        except (TypeError, ValueError):
            self._timeout = 12.0
        self._timeout = min(max(self._timeout, 3.0), 60.0)

        try:
            self._shot_timeout = float(section.get("screenshot_timeout_seconds", 20))
        except (TypeError, ValueError):
            self._shot_timeout = 20.0
        self._shot_timeout = min(max(self._shot_timeout, 5.0), 60.0)

        try:
            self._click_wait = float(section.get("post_click_wait_seconds", _POST_CLICK_WAIT))
        except (TypeError, ValueError):
            self._click_wait = _POST_CLICK_WAIT
        # 0.5 秒太短会读到旧界面，30 秒太久会让对话卡住
        self._click_wait = min(max(self._click_wait, 0.5), 30.0)

        self._max_width = _as_int(section.get("screenshot_max_width"), 1024, 320, 2048)
        self._jpeg_quality = _as_int(section.get("screenshot_jpeg_quality"), 80, 30, 95)
        self._vision_enabled = _as_bool(section.get("vision_enabled"), True)
        self._vision_prompt = _as_text(section.get("vision_prompt")) or (
            "这是航天模拟器 Spaceflight Simulator 的当前游戏画面。"
            "请描述你看到的火箭结构、飞行阶段与环境。"
        )

        self._vlm = _as_dict(cfg.get("vlm_fallback"))

        self.logger.info(
            "SfsBridge 已就绪，桥接地址={} 视觉={} 自带模型={}",
            self._bridge_url,
            self._vision_enabled,
            _as_bool(self._vlm.get("enabled")),
        )
        return Ok({
            "status": "ready",
            "bridge_url": self._bridge_url,
            "vision_enabled": self._vision_enabled,
            "vlm_fallback_enabled": _as_bool(self._vlm.get("enabled")),
        })

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        client = self._client
        self._client = None
        self._client_loop = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as exc:  # pragma: no cover
                self.logger.debug("关闭客户端出错: {}", exc)
        return Ok({"status": "stopped"})

    # -- 插件入口 ---------------------------------------------------------

    @plugin_entry(
        id="sfs_status",
        name="航天模拟器状态",
        description="读取 Spaceflight Simulator 当前的飞行遥测：高度、速度、油门、分级、质量与所在天体。",
        timeout=30,
        llm_result_fields=["summary", "connected", "state"],
    )
    async def sfs_status(self, **_):
        """读取游戏遥测。"""
        try:
            state = await self._get_json("/state")
        except SdkError as exc:
            return Ok({
                "connected": False,
                "summary": str(exc),
                "state": {},
            })
        except Exception as exc:  # pragma: no cover
            self.logger.exception("读取遥测时出错")
            return Err(SdkError(f"读取遥测失败：{exc}"))

        return Ok({
            "connected": True,
            "summary": _describe_state(state),
            "state": state,
        })

    @plugin_entry(
        id="sfs_command",
        name="控制航天模拟器",
        description=(
            "向游戏发送控制指令。支持：set_throttle（设置油门 0-1）、"
            "throttle_on、throttle_off、stage（空格：执行下一级）、"
            "staging_program（回车：分级控制程序）、rcs_on、rcs_off、rcs_toggle。"
            "这些指令直接改游戏状态，不经过键盘。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "set_throttle",
                        "throttle_on",
                        "throttle_off",
                        "stage",
                        "staging_program",
                        "rcs_on",
                        "rcs_off",
                        "rcs_toggle",
                    ],
                    "description": "指令名称",
                },
                "value": {
                    "type": "number",
                    "description": "set_throttle 时使用的油门值，范围 0-1",
                },
            },
            "required": ["command"],
        },
        timeout=30,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_command(self, command: str, value: float = 0.0, **_):
        """发送控制指令。"""
        command = _as_text(command)
        allowed = {
            "set_throttle",
            "throttle_on",
            "throttle_off",
            "stage",
            "staging_program",
            "rcs_on",
            "rcs_off",
            "rcs_toggle",
        }
        if command not in allowed:
            return Err(SdkError(f"不支持的指令：{command}"))

        try:
            result = await self._post_command(command, float(value or 0.0))
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.exception("发送指令时出错")
            return Err(SdkError(f"发送指令失败：{exc}"))

        labels = {
            "set_throttle": f"已把油门设为 {float(value or 0.0) * 100:.0f}%",
            "throttle_on": "已点火（油门开启）",
            "throttle_off": "已关闭油门",
            "stage": "已执行下一级（分级）",
            "staging_program": "已执行分级控制程序",
            "rcs_on": "已打开 RCS",
            "rcs_off": "已关闭 RCS",
            "rcs_toggle": "已切换 RCS 开关",
        }
        return Ok({
            "ok": bool(_as_dict(result).get("ok", True)),
            "command": command,
            "message": labels.get(command, f"已发送 {command}"),
        })

    @plugin_entry(
        id="sfs_screenshot",
        name="截取游戏画面",
        description="抓取一张 Spaceflight Simulator 的画面。返回图片信息与尺寸，供进一步识别。",
        timeout=40,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_screenshot(self, **_):
        """抓取画面（不解图，仅报告是否成功）。"""
        png = await self._capture_screen()
        if not png:
            return Ok({
                "ok": False,
                "message": (
                    "截图失败。请确认游戏正在运行、已安装 SFS-Agent 模组，"
                    "并且窗口没有被最小化。"
                ),
            })

        encoded, mime, note = _encode_for_vision(
            png,
            max_width=self._max_width,
            quality=self._jpeg_quality,
        )
        if encoded is None:
            return Ok({"ok": False, "message": f"画面已抓取但无法处理：{note}"})

        return Ok({
            "ok": True,
            "mime": mime,
            "bytes_png": len(png),
            "encoded_chars": len(encoded),
            "note": note,
            "message": f"已截取画面（{note}）。",
        })

    @plugin_entry(
        id="sfs_build",
        name="读取火箭设计",
        description=(
            "读取当前火箭的零件构成、总质量与分级数，用于评审设计。"
            "建造场景与飞行场景都支持。"
        ),
        timeout=30,
        llm_result_fields=["connected", "mode", "part_count", "summary"],
    )
    async def sfs_build(self, **_):
        """读取火箭设计（零件构成）。"""
        try:
            build = await self._get_json("/build")
        except SdkError as exc:
            return Ok({"connected": False, "summary": str(exc), "build": {}})
        except Exception as exc:  # pragma: no cover
            self.logger.exception("读取设计时出错")
            return Err(SdkError(f"读取设计失败：{exc}"))

        return Ok({
            "connected": True,
            "mode": _as_text(build.get("mode")),
            "part_count": build.get("part_count", 0),
            "summary": _describe_build(build),
            "build": build,
        })

    @plugin_entry(
        id="sfs_ui",
        name="列出游戏界面元素",
        description=(
            "列出当前游戏界面上的可点击按钮及其归一化坐标，"
            "用于精确操作主菜单、建造菜单等界面。"
        ),
        timeout=40,
        llm_result_fields=["count", "elements", "summary"],
    )
    async def sfs_ui(self, **_):
        """列出 UI 元素。"""
        try:
            data = await self._get_json("/ui")
        except SdkError as exc:
            return Ok({"count": 0, "elements": [], "summary": str(exc)})
        except Exception as exc:  # pragma: no cover
            self.logger.exception("枚举界面元素时出错")
            return Err(SdkError(f"枚举界面失败：{exc}"))

        elements = data.get("elements") or []
        return Ok({
            "count": data.get("count", len(elements)),
            "elements": elements,
            "summary": f"当前界面读到 {len(elements)} 个可点击元素。",
            "guide": _UI_AGENT_GUIDE,
        })

    @plugin_entry(
        id="sfs_click",
        name="点击游戏界面",
        description=(
            "在游戏窗口的指定位置点击鼠标。x、y 为归一化坐标（0-1），"
            "相对游戏窗口客户区左上角；也可以用 index 按界面元素索引点击。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "横坐标 0-1（从左边算起）"},
                "y": {"type": "number", "description": "纵坐标 0-1（从上边算起）"},
                "index": {
                    "type": "integer",
                    "description": "按界面元素清单的索引点击（提供时优先于 x/y）",
                },
            },
        },
        timeout=30,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_click(
        self, x: float = -1, y: float = -1, index: int = -1, **_
    ):
        """点击游戏界面。"""
        try:
            if isinstance(index, int) and index >= 0:
                result = await self._post_json("/ui_click", {"index": index})
            else:
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    return Err(SdkError("需要提供 index，或 0-1 范围内的 x 与 y。"))
                result = await self._post_json("/click", {"x": float(x), "y": float(y)})
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.exception("点击时出错")
            return Err(SdkError(f"点击失败：{exc}"))

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(f"点击失败：{reason}"))

        # 游戏切界面慢，等一会儿再回读，顺便把新界面带回去
        await asyncio.sleep(self._click_wait)
        after = ""
        try:
            data = await self._get_json("/ui")
            after = _summarize_ui(data)
        except Exception:  # pragma: no cover - 回读失败不影响点击本身
            after = ""

        return Ok({
            "ok": True,
            "message": f"已点击{'元素 #' + str(index) if index >= 0 else ''}"
                       f"{f'（{x:.2f}, {y:.2f}）' if index < 0 else ''}，"
                       f"等待 {self._click_wait:.1f} 秒后界面如下。",
            "after": after or "（点击后读不到界面元素）",
            "guide": _UI_AGENT_GUIDE,
        })

    @plugin_entry(
        id="sfs_parts",
        name="列出可用零件",
        description=(
            "列出游戏内置的零件名称，用于在建造界面放置零件。"
            "只有知道确切零件名才能用 sfs_place 放置。"
        ),
        timeout=40,
        llm_result_fields=["count", "parts"],
    )
    async def sfs_parts(self, **_):
        """列出可用零件。"""
        try:
            data = await self._get_json("/build_catalog")
        except SdkError as exc:
            return Ok({"count": 0, "parts": [], "message": str(exc)})
        except Exception as exc:  # pragma: no cover
            self.logger.exception("读取零件目录时出错")
            return Err(SdkError(f"读取零件目录失败：{exc}"))

        parts = data.get("parts") or []
        return Ok({
            "count": data.get("count", len(parts)),
            "parts": parts,
            "message": f"游戏内置 {len(parts)} 种零件。",
            "guide": _UI_AGENT_GUIDE,
        })

    @plugin_entry(
        id="sfs_place",
        name="在指定位置放置零件",
        description=(
            "把指定名称的零件直接放到建造网格的坐标 (x, y) 上。"
            "建造界面里零件本来必须拖动才能放上去，这个入口绕开拖动，"
            "不需要移动鼠标，直接生成在指定位置。需要游戏处于建造场景。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "零件名称，如 Fuel Tank"},
                "x": {"type": "number", "description": "建造网格横坐标，0 为画面中心"},
                "y": {"type": "number", "description": "建造网格纵坐标，0 为画面中心"},
            },
            "required": ["name"],
        },
        timeout=40,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_place(
        self, name: str, x: float = 0.0, y: float = 0.0, **_
    ):
        """在指定位置放置零件。"""
        clean = _as_text(name)
        if not clean:
            return Err(SdkError("需要提供零件名称（可先用 list_sfs_parts 查询）。"))
        try:
            result = await self._post_json(
                "/build_place", {"name": clean, "x": float(x), "y": float(y)}
            )
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.exception("放置零件时出错")
            return Err(SdkError(f"放置零件失败：{exc}"))

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(
                f"放置零件失败：{reason}"
                "（请确认游戏处于建造场景，且零件名正确）"
            ))

        return Ok({
            "ok": True,
            "message": f"已把「{clean}」放到 ({x:.1f}, {y:.1f})。",
            "placed": detail.get("placed", 0),
        })

    @plugin_entry(
        id="sfs_key",
        name="向游戏发送按键",
        description=(
            "向游戏发送一个按键。vk 为虚拟键码（与 UnityEngine.KeyCode 数值一致），"
            "例如 13=回车、27=Esc、32=空格、81=Q、69=E、87=W、65=A、83=S、68=D、"
            "82=R、16=Shift、17=Ctrl。"
            "按键在游戏内部注入，不需要游戏窗口在前台，也不会打扰用户的其他操作。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vk": {
                    "type": "integer",
                    "description": (
                        "虚拟键码。常用：空格=32 回车=13 Esc=27 "
                        "Q=81 E=69 W=87 A=65 S=83 D=68 R=82 Shift=16 Ctrl=17"
                    ),
                },
            },
            "required": ["vk"],
        },
        timeout=30,
        llm_result_fields=["ok", "message"],
    )
    async def sfs_key(self, vk: int, **_):
        """向游戏发送按键。"""
        try:
            result = await self._post_json("/key", {"vk": int(vk)})
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:  # pragma: no cover
            self.logger.exception("发送按键时出错")
            return Err(SdkError(f"发送按键失败：{exc}"))

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return Err(SdkError(f"按键发送失败：{reason}"))

        return Ok({"ok": True, "message": f"已发送按键 {vk}。"})

    # -- LLM 工具 ---------------------------------------------------------

    @llm_tool(
        name="see_sfs_screen",
        description=(
            "查看航天模拟器（Spaceflight Simulator）当前画面。"
            "当用户说「你看我造的火箭」「看看我现在飞到哪了」「看看这个画面」时调用。"
            "图片会交给视觉模型识别，你应当根据看到的画面内容回答。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "用户关于这个画面的具体问题，留空则做整体描述",
                },
            },
        },
        timeout=60,
    )
    async def see_sfs_screen(self, question: str = "", **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：把游戏画面交给视觉模型。"""
        question = _as_text(question)

        png = await self._capture_screen()
        if not png:
            return {
                "output": {
                    "ok": False,
                    "connected": False,
                    "message": (
                        "没能抓到游戏画面。可能原因：游戏没有运行、"
                        "没有安装 SFS-Agent 模组、或游戏窗口被最小化。"
                        "请如实告诉用户画面不可用，不要猜测画面内容。"
                    ),
                },
                "is_error": False,
            }

        encoded, mime, note = _encode_for_vision(
            png,
            max_width=self._max_width,
            quality=self._jpeg_quality,
        )
        if encoded is None:
            return {
                "output": {
                    "ok": False,
                    "message": f"画面已抓取但无法处理：{note}。请如实告知用户。",
                },
                "is_error": False,
            }

        prompt = question or self._vision_prompt
        if not prompt:
            prompt = "这是航天模拟器的游戏画面，请描述你看到的内容。"

        # 1) 用户自配的视觉模型：直接把画面转成文字
        if _as_bool(self._vlm.get("enabled")):
            description = await self._call_fallback_vlm(encoded, mime, prompt)
            if description:
                return {
                    "output": {
                        "ok": True,
                        "source": "自带视觉模型",
                        "description": description,
                        "message": description,
                    },
                    "is_error": False,
                }
            # 自带模型失败则继续走 N.E.K.O. 的视觉模型

        # 2) N.E.K.O. 的视觉聊天模型：把图片放进返回信封
        if not self._vision_enabled:
            return {
                "output": {
                    "ok": False,
                    "message": (
                        "插件当前关闭了画面识别（vision_enabled = false）。"
                        "请如实告知用户，不要猜测画面内容。"
                    ),
                },
                "is_error": False,
            }

        return {
            "output": {
                "ok": True,
                "source": "N.E.K.O. 视觉模型",
                "image_note": note,
                "hint": (
                    "画面已附在本次调用中，请依据图片内容回答。"
                    "如果提示图片被跳过，说明当前会话没有配置视觉模型，"
                    "请如实告知用户需要在 N.E.K.O. 设置中配置「视觉聊天模型」。"
                ),
            },
            "images": [
                {
                    "data_b64": encoded,
                    "mime": mime,
                    "vision_prompt": prompt,
                }
            ],
        }

    @llm_tool(
        name="get_sfs_status",
        description=(
            "读取航天模拟器（Spaceflight Simulator）的飞行遥测，"
            "包括高度、速度、油门、分级与质量。"
            "当用户问「我现在飞多高」「速度多少」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "unused": {
                    "type": "string",
                    "description": "占位参数，无需填写",
                },
            },
        },
        timeout=30,
    )
    async def get_sfs_status(self, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：读取遥测。"""
        try:
            state = await self._get_json("/state")
        except Exception as exc:
            return {
                "output": {
                    "connected": False,
                    "summary": (
                        f"读不到游戏遥测（{exc}）。请如实告诉用户游戏可能没有运行，"
                        "不要编造飞行数据。"
                    ),
                },
                "is_error": False,
            }

        return {
            "output": {
                "connected": True,
                "summary": _describe_state(state),
                "state": state,
            },
            "is_error": False,
        }

    @llm_tool(
        name="get_rocket_design",
        description=(
            "读取用户在航天模拟器里当前火箭的零件构成、总质量与分级数，用于分析设计。"
            "当用户问「我这火箭用了什么零件」「一共多少零件」「总质量多少」时调用。"
            "需要评价外观时请配合 see_sfs_screen 一起使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "unused": {
                    "type": "string",
                    "description": "占位参数，无需填写",
                },
            },
        },
        timeout=30,
    )
    async def get_rocket_design(self, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：读取火箭设计。"""
        try:
            build = await self._get_json("/build")
        except Exception as exc:
            return {
                "output": {
                    "connected": False,
                    "summary": (
                        f"读不到火箭设计（{exc}）。请如实告诉用户游戏可能没有运行，"
                        "不要编造零件或质量数据。"
                    ),
                },
                "is_error": False,
            }

        return {
            "output": {
                "connected": True,
                "mode": _as_text(build.get("mode")),
                "part_count": build.get("part_count", 0),
                "summary": _describe_build(build),
                "build": build,
            },
            "is_error": False,
        }

    @llm_tool(
        name="list_sfs_ui",
        description=(
            "列出航天模拟器当前界面上的可点击按钮及坐标。"
            "适合主菜单、建造菜单这类需要精确点击的场景。"
            "拿不到清单时可以改用 see_sfs_screen 看画面后用 click_sfs_ui 点坐标。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "unused": {
                    "type": "string",
                    "description": "占位参数，无需填写",
                },
            },
        },
        timeout=40,
    )
    async def list_sfs_ui(self, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：列出界面元素。"""
        try:
            data = await self._get_json("/ui")
        except Exception as exc:
            return {
                "output": {
                    "ok": False,
                    "count": 0,
                    "message": (
                        f"读不到界面元素（{exc}）。请如实告诉用户游戏可能没有运行，"
                        "不要编造界面内容。"
                    ),
                },
                "is_error": False,
            }

        elements = data.get("elements") or []
        summary = _summarize_ui(data)

        return {
            "output": {
                "ok": True,
                "count": data.get("count", len(elements)),
                "elements": elements,
                "summary": (
                    "当前界面元素：\n" + summary
                    if summary
                    else "当前界面没有读到可点击元素。"
                ),
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="click_sfs_ui",
        description=(
            "【必须真正调用】点击航天模拟器的界面元素。"
            "想操作游戏（开始游戏、载入存档、进建造、改设置）时，"
            "**只有调用本工具才会真的点下去** —— 只是嘴上说「我点了」游戏不会有任何变化。"
            "推荐用法：先 list_sfs_ui 看清有那些元素，再用 name 报元素名字点击"
            "（比记序号或坐标都可靠），也可以给 index 或 x/y。"
            "本工具会返回点击后的新界面清单，直接看返回值即可。"
            "这是会改变游戏状态的真实操作，动手前先想清楚。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "要点击的元素名字（如 Play、Settings、My World 1），"
                        "按名字模糊匹配，最推荐这个方式"
                    ),
                },
                "index": {
                    "type": "integer",
                    "description": "list_sfs_ui 返回的元素序号，提供时优先于 name 与 x/y",
                },
                "x": {
                    "type": "number",
                    "description": "横坐标 0-1，从左边算起（给不出名字或序号时才用）",
                },
                "y": {
                    "type": "number",
                    "description": "纵坐标 0-1，从上边算起",
                },
            },
        },
        timeout=30,
    )
    async def click_sfs_ui(
        self,
        x: float = -1,
        y: float = -1,
        index: int = -1,
        name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """LLM 工具：点击界面。

        三种定位方式，优先级 index > name > x/y：
          index —— list_sfs_ui 返回的序号
          name  —— 元素名字（模糊匹配，最不容易出错）
          x/y   —— 归一化坐标

        无论成功失败，都会把结果写进插件日志（N.E.K.O. 日志里能看到），
        返回的 message 也明确说明「发出去了没有」，避免模型自己猜。
        """
        target = ""
        try:
            # ① 点之前先刷新一次清单 —— 免得拿旧清单戳新界面
            before = ""
            before_count = 0
            try:
                pre = await self._get_json("/ui")
                before_count = int(pre.get("count", 0) or 0)
                before = _summarize_ui(pre)
            except Exception:
                pre = {}

            # ② 决定点哪里
            if isinstance(index, int) and index >= 0:
                target = f"元素 #{index}"
                payload = {"index": index}
                endpoint = "/ui_click"
            elif isinstance(name, str) and name.strip():
                # 按名字在当前清单里找
                hit = _find_element(pre, name.strip())
                if hit is None:
                    msg = (
                        f"没找到名字包含「{name}」的可点击元素。"
                        f"当前界面共 {before_count} 个元素：{before}"
                    )
                    self.logger.info("[sfs_bridge] click_sfs_ui 未找到元素 name=%s", name)
                    return {
                        "output": {
                            "ok": False,
                            "message": msg,
                            "before_count": before_count,
                            "before": before,
                        },
                        "is_error": True,
                    }
                target = f"「{hit.get('label') or '(无标签)'}」#{hit.get('index')}"
                payload = {"index": int(hit.get("index", 0))}
                endpoint = "/ui_click"
            elif 0 <= x <= 1 and 0 <= y <= 1:
                target = f"({x:.2f}, {y:.2f})"
                payload = {"x": float(x), "y": float(y)}
                endpoint = "/click"
            else:
                msg = "需要提供 name、index，或 0-1 范围内的 x 与 y。"
                self.logger.info("[sfs_bridge] click_sfs_ui 参数不足")
                return {
                    "output": {"ok": False, "message": msg},
                    "is_error": True,
                }

            # ③ 真的发出去
            result = await self._post_json(endpoint, payload)
        except Exception as exc:
            # 失败也要留下痕迹，别让模型以为点过了
            self.logger.warning("[sfs_bridge] click_sfs_ui 发送失败：%s", exc)
            return {
                "output": {
                    "ok": False,
                    "delivered": False,
                    "message": f"点击失败（未送达游戏）：{exc}",
                },
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            self.logger.warning(
                "[sfs_bridge] click_sfs_ui 游戏侧拒绝 target=%s reason=%s", target, reason
            )
            return {
                "output": {
                    "ok": False,
                    "delivered": False,
                    "message": f"点击未送达：{reason}",
                },
                "is_error": True,
            }

        # 送达成功 —— 记一条，方便事后核对
        self.logger.info("[sfs_bridge] click_sfs_ui 已送达 %s", target)

        # ④ 等一会儿再回读新界面
        await asyncio.sleep(self._click_wait)
        after = ""
        after_count = 0
        try:
            data = await self._get_json("/ui")
            after_count = int(data.get("count", 0) or 0)
            after = _summarize_ui(data)
        except Exception as exc:
            after = f"（点击后读不到界面：{exc}；可用 see_sfs_screen 看画面确认）"

        changed = after_count != before_count
        return {
            "output": {
                "ok": True,
                "delivered": True,
                "message": (
                    f"已点击 {target} 并确认送达游戏；"
                    f"等待 {self._click_wait:.1f} 秒后界面"
                    + ("已变化" if changed else "**没有变化**")
                    + f"（{before_count} → {after_count} 个元素）。"
                    + ("" if changed else "这不代表失败 —— 游戏加载可能更慢，"
                       "可以再等一等或重新 list_sfs_ui 确认。")
                ),
                "before_count": before_count,
                "after_count": after_count,
                "changed": changed,
                "after": after or "（当前界面没有读到可点击元素）",
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="list_sfs_parts",
        description=(
            "列出航天模拟器里可用的零件名称。"
            "准备在建造界面搭火箭、或用户问「有哪些零件可以用」时先调用它，"
            "拿到确切零件名后再用 place_sfs_part 放置。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=40,
    )
    async def list_sfs_parts(self, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：列出可用零件。"""
        try:
            data = await self._get_json("/build_catalog")
        except Exception as exc:
            return {
                "output": {
                    "ok": False,
                    "count": 0,
                    "message": (
                        f"读不到零件目录（{exc}）。请如实告诉用户游戏可能没有运行，"
                        "不要编造零件名。"
                    ),
                },
                "is_error": False,
            }

        parts = data.get("parts") or []
        listing = "、".join(str(p) for p in parts[:60])
        return {
            "output": {
                "ok": True,
                "count": data.get("count", len(parts)),
                "parts": parts,
                "summary": f"可用零件共 {len(parts)} 种：{listing}",
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="place_sfs_part",
        description=(
            "在航天模拟器的建造界面，把指定零件直接放到坐标 (x, y) 上。"
            "建造界面里零件本来必须从菜单拖到火箭上，纯点击放不上去；"
            "这个工具绕开拖动，直接把零件生成在指定位置，也不会移动用户鼠标。"
            "x、y 是建造网格坐标，0 是画面中心，向右/向上为正。"
            "用户说「帮我加一个燃料罐」「在火箭下面装个引擎」时用它。"
            "零件名要先通过 list_sfs_parts 确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "零件名称，需与 list_sfs_parts 返回的一致",
                },
                "x": {"type": "number", "description": "网格横坐标，0 为画面中心"},
                "y": {"type": "number", "description": "网格纵坐标，0 为画面中心"},
            },
            "required": ["name"],
        },
        timeout=40,
    )
    async def place_sfs_part(
        self, name: str, x: float = 0.0, y: float = 0.0, **kwargs: Any
    ) -> Dict[str, Any]:
        """LLM 工具：在指定位置放置零件。"""
        clean = _as_text(name)
        if not clean:
            return {
                "output": {
                    "ok": False,
                    "message": "需要提供零件名称；先用 list_sfs_parts 查一下有哪些零件。",
                },
                "is_error": True,
            }
        try:
            result = await self._post_json(
                "/build_place", {"name": clean, "x": float(x), "y": float(y)}
            )
        except Exception as exc:
            return {
                "output": {"ok": False, "message": f"放置零件失败：{exc}"},
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return {
                "output": {
                    "ok": False,
                    "message": (
                        f"放置零件失败：{reason}。"
                        "请确认游戏处于建造场景（不是主菜单或飞行中），且零件名正确。"
                    ),
                },
                "is_error": True,
            }

        return {
            "output": {
                "ok": True,
                "message": f"已把「{clean}」放到 ({x:.1f}, {y:.1f})。",
                "placed": detail.get("placed", 0),
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="list_sfs_blueprints",
        description=(
            "列出玩家在航天模拟器里保存过的**火箭蓝图**。"
            "用户说「载入我存的那枚火箭」「看看我有哪些设计」「帮我搭 XX」时先调用它，"
            "拿到确切名字后再用 load_sfs_blueprint 加载。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=40,
    )
    async def list_sfs_blueprints(self, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：列出蓝图。"""
        try:
            data = await self._get_json("/blueprints")
        except Exception as exc:
            return {
                "output": {
                    "ok": False,
                    "count": 0,
                    "message": (
                        f"读不到蓝图列表（{exc}）。请如实告诉用户游戏可能没有运行，"
                        "不要编造火箭名字。"
                    ),
                },
                "is_error": False,
            }

        items = data.get("blueprints") or []
        listing = "、".join(str(x) for x in items[:60])
        return {
            "output": {
                "ok": True,
                "count": data.get("count", len(items)),
                "blueprints": items,
                "summary": f"共 {len(items)} 个蓝图：{listing}",
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="load_sfs_blueprint",
        description=(
            "把玩家存档里的某个**火箭蓝图**直接加载到建造场景，整枚火箭一次性生成。"
            "这是造火箭最可靠的路径：坐标、零件尺寸、纹理、分级全部由游戏自己解析，"
            "因此一定是正确、可以飞的。"
            "用户说「载入我的 XX 火箭」「帮我搭一个 XX」「用那个设计」时调用。"
            "名字必须先通过 list_sfs_blueprints 确认。"
            "注意：加载会**替换建造场景里当前的火箭**（如果是没保存的改动会丢失）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "蓝图名字，需与 list_sfs_blueprints 返回的一致",
                },
            },
            "required": ["name"],
        },
        timeout=120,
    )
    async def load_sfs_blueprint(
        self, name: str, **kwargs: Any
    ) -> Dict[str, Any]:
        """LLM 工具：加载蓝图。"""
        clean = _as_text(name)
        if not clean:
            return {
                "output": {
                    "ok": False,
                    "message": "需要提供蓝图名字；先用 list_sfs_blueprints 查一下有哪些。",
                },
                "is_error": True,
            }
        try:
            result = await self._post_json("/blueprint_load", {"name": clean})
        except Exception as exc:
            return {
                "output": {"ok": False, "message": f"加载蓝图失败：{exc}"},
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return {
                "output": {
                    "ok": False,
                    "message": (
                        f"加载蓝图失败：{reason}。"
                        "请确认游戏处于建造场景（不是主菜单或飞行中），且名字正确。"
                    ),
                },
                "is_error": True,
            }

        return {
            "output": {
                "ok": True,
                "message": _as_text(detail.get("result")) or f"已加载蓝图「{clean}」。",
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="hold_sfs_key",
        description=(
            "【按住不放】按下某个键并**一直保持按住**，直到调用 release_sfs_key。"
            "用于需要持续推力的操作 —— RCS 平移（W/A/S/D）就是典型："
            "之前只有「按一下就松」，在高速飞行时脉冲式按键算不出该转多少度，"
            "要么转不动要么转过头。现在可以："
            "① hold_sfs_key 按住 W；② get_sfs_status 看数据；③ release_sfs_key 松开。"
            "**用完一定要松开**，否则会一直推下去。"
            "也可以给 hold_ms 让它自动松开，省一次调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "vk": {
                    "type": "integer",
                    "description": "虚拟键码：W=87 A=65 S=83 D=68 Q=81 E=69 R=82 空格=32",
                },
                "hold_ms": {
                    "type": "integer",
                    "description": (
                        "可选的自动松开时长（毫秒）。不填就是一直按住，"
                        "需要自己调 release_sfs_key"
                    ),
                },
            },
            "required": ["vk"],
        },
        timeout=30,
    )
    async def hold_sfs_key(self, vk: int, hold_ms: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：按住某个键不放。"""
        try:
            result = await self._post_json("/key_down", {"vk": int(vk)})
        except Exception as exc:
            self.logger.warning("[sfs_bridge] hold_sfs_key 失败：%s", exc)
            return {
                "output": {"ok": False, "message": f"按住失败（未送达）：{exc}"},
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            return {
                "output": {
                    "ok": False,
                    "message": f"按住失败：{_as_text(detail.get('error')) or '未知原因'}",
                },
                "is_error": True,
            }

        key = _as_text(detail.get("key")) or str(vk)
        already = _as_bool(detail.get("already_held"))
        self.logger.info("[sfs_bridge] hold_sfs_key 按住 %s", key)

        msg = f"已按住 {key}"
        if already:
            msg += "（此前已在按住）"

        # 给了 hold_ms 就自动松开
        if isinstance(hold_ms, int) and hold_ms > 0:
            await asyncio.sleep(min(hold_ms, 30000) / 1000.0)
            try:
                await self._post_json("/key_up", {"vk": int(vk)})
                msg += f"，已自动松开（按了 {hold_ms} 毫秒）"
                self.logger.info("[sfs_bridge] hold_sfs_key 自动松开 %s", key)
            except Exception as exc:
                msg += f"，但自动松开失败：{exc}"
        else:
            msg += "，**记得调 release_sfs_key 松开**"

        return {
            "output": {"ok": True, "delivered": True, "held": key, "message": msg},
            "is_error": False,
        }

    @llm_tool(
        name="release_sfs_key",
        description=(
            "【松开】松开之前用 hold_sfs_key 按住的键。"
            "RCS 平移这类持续推力**必须**在推够之后松开，"
            "否则航天器会一直朝那个方向加速。"
            "不填 vk 就松开全部按住的键（急停）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "vk": {
                    "type": "integer",
                    "description": "要松开的虚拟键码；不填则松开全部",
                },
            },
        },
        timeout=30,
    )
    async def release_sfs_key(self, vk: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：松开按住的键。"""
        try:
            if isinstance(vk, int) and vk > 0:
                result = await self._post_json("/key_up", {"vk": int(vk)})
            else:
                result = await self._post_json("/release_all", {})
        except Exception as exc:
            self.logger.warning("[sfs_bridge] release_sfs_key 失败：%s", exc)
            return {
                "output": {"ok": False, "message": f"松开失败（未送达）：{exc}"},
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            return {
                "output": {"ok": False, "message": "松开失败"},
                "is_error": True,
            }

        if isinstance(vk, int) and vk > 0:
            key = _as_text(detail.get("key")) or str(vk)
            was = _as_bool(detail.get("was_held"))
            self.logger.info("[sfs_bridge] release_sfs_key 松开 %s was_held=%s", key, was)
            msg = f"已松开 {key}" + ("" if was else "（它本来就没被按住）")
        else:
            n = detail.get("released", 0)
            self.logger.info("[sfs_bridge] release_sfs_key 全部松开 共 %s 个", n)
            msg = f"已松开全部按住的键（{n} 个）"

        return {
            "output": {
                "ok": True,
                "delivered": True,
                "held": detail.get("held"),
                "message": msg,
            },
            "is_error": False,
        }

    @llm_tool(
        name="control_sfs_camera",
        description=(
            "调整航天模拟器的视角。"
            "想看清洗某个零件、看全整枚火箭、或画面里东西太小/太大时用它。"
            "zoom_delta 是相对缩放（**正数拉远、负数拉近**，单位大致是米）；"
            "distance 是绝对距离；x/y 是相机中心位置；rotation 是角度。"
            "只传你想改的字段即可。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "zoom_delta": {
                    "type": "number",
                    "description": "相对缩放：正数拉远、负数拉近，例如 -20 拉近、30 拉远",
                },
                "distance": {"type": "number", "description": "绝对相机距离"},
                "x": {"type": "number", "description": "相机中心 x"},
                "y": {"type": "number", "description": "相机中心 y"},
                "rotation": {"type": "number", "description": "相机角度（度）"},
            },
        },
        timeout=30,
    )
    async def control_sfs_camera(self, **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：调整视角。"""
        payload: Dict[str, Any] = {}
        for key in ("zoom_delta", "distance", "x", "y", "rotation"):
            value = kwargs.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[key] = float(value)
        if not payload:
            return {
                "output": {
                    "ok": False,
                    "message": (
                        "需要至少给一个参数：zoom_delta（正数拉远/负数拉近）、"
                        "distance、x、y、rotation。"
                    ),
                },
                "is_error": True,
            }

        try:
            result = await self._post_json("/camera", payload)
        except Exception as exc:
            return {
                "output": {"ok": False, "message": f"调整视角失败：{exc}"},
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return {
                "output": {"ok": False, "message": f"调整视角失败：{reason}"},
                "is_error": True,
            }

        return {
            "output": {
                "ok": True,
                "message": _as_text(detail.get("result")) or "已调整视角。",
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="press_sfs_key",
        description=(
            "向航天模拟器发送按键（在游戏内注入，不需要游戏窗口在前台，"
            "也不会打扰用户的其他操作）。"
            "SFS 的默认操作方式："
            "转向 Q(81) 左 / E(69) 右；"
            "平移俯仰 W(87)/S(83)/A(65)/D(68)，需先按 R(82) 打开 RCS；"
            "油门 Shift(16) 加大 / Ctrl(17) 减小；"
            "RCS 开关 R(82)；点火或执行下一级 空格(32)；分级控制程序 回车(13)；"
            "返回菜单 Esc(27)。"
            "「按住」类操作（转向、油门）在本工具里保持约 0.12 秒，"
            "时间不够可以重复调用几次。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "vk": {
                    "type": "integer",
                    "description": (
                        "虚拟键码：空格=32 回车=13 Esc=27 "
                        "Q=81 E=69 W=87 A=65 S=83 D=68 R=82 Shift=16 Ctrl=17"
                    ),
                },
                "hold_ms": {
                    "type": "integer",
                    "description": "按住时长（毫秒），默认 120；转向时可调到 400-800",
                },
            },
            "required": ["vk"],
        },
        timeout=30,
    )
    async def press_sfs_key(
        self, vk: int, hold_ms: int = 120, **kwargs: Any
    ) -> Dict[str, Any]:
        """LLM 工具：发送按键。"""
        try:
            result = await self._post_json(
                "/key", {"vk": int(vk), "hold_ms": int(hold_ms)}
            )
        except Exception as exc:
            return {
                "output": {"ok": False, "message": f"按键发送失败：{exc}"},
                "is_error": True,
            }

        detail = _as_dict(result)
        if not _as_bool(detail.get("ok")):
            reason = _as_text(detail.get("error")) or "未知原因"
            return {
                "output": {
                    "ok": False,
                    "message": f"按键发送失败：{reason}",
                },
                "is_error": True,
            }

        return {
            "output": {
                "ok": True,
                "message": f"已发送按键 {vk}（按住 {hold_ms} 毫秒）。",
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="control_sfs",
        description=(
            "直接控制航天模拟器（Spaceflight Simulator），不走按键，最可靠。"
            "当用户要求「点火」「关油门」「油门开到 80%」「分离一级」"
            "「打开 RCS」时调用。"
            "这是会改变游戏状态的真实操作，调用前请确认用户意图明确。"
            "注意：stage 只是执行下一级，跟空格键等价；"
            "要转向或平移请改用 press_sfs_key 按住 Q/E/W/A/S/D。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "set_throttle",
                        "throttle_on",
                        "throttle_off",
                        "stage",
                        "staging_program",
                        "rcs_on",
                        "rcs_off",
                        "rcs_toggle",
                    ],
                    "description": "要执行的指令",
                },
                "value": {
                    "type": "number",
                    "description": "set_throttle 时的油门值，0-1",
                },
            },
            "required": ["command"],
        },
        timeout=30,
    )
    async def control_sfs(
        self, command: str, value: float = 0.0, **kwargs: Any
    ) -> Dict[str, Any]:
        """LLM 工具：控制游戏。"""
        command = _as_text(command)
        allowed = {
            "set_throttle",
            "throttle_on",
            "throttle_off",
            "stage",
            "staging_program",
            "rcs_on",
            "rcs_off",
            "rcs_toggle",
        }
        if command not in allowed:
            return {
                "output": {"ok": False, "message": f"不支持的指令：{command}"},
                "is_error": True,
            }

        try:
            await self._post_command(command, float(value or 0.0))
        except Exception as exc:
            return {
                "output": {"ok": False, "message": f"指令发送失败：{exc}"},
                "is_error": True,
            }

        labels = {
            "set_throttle": f"已把油门设为 {float(value or 0.0) * 100:.0f}%",
            "throttle_on": "已点火",
            "throttle_off": "已关闭油门",
            "stage": "已执行下一级（分级）",
            "staging_program": "已执行分级控制程序",
            "rcs_on": "已打开 RCS",
            "rcs_off": "已关闭 RCS",
            "rcs_toggle": "已切换 RCS 开关",
        }
        return {
            "output": {
                "ok": True,
                "message": labels.get(command, "指令已发送"),
                "guide": _UI_AGENT_GUIDE,
            },
            "is_error": False,
        }

    @llm_tool(
        name="review_rocket_design",
        description=(
            "评审用户在航天模拟器里正在建造或已经发射的火箭设计。"
            "当用户问「你看我造的火箭怎么样」「这个设计合理吗」「帮我改进一下」时调用。"
            "会同时抓取当前画面与飞行遥测，交给视觉模型分析。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "用户希望重点关注的方向，例如「推重比」「级间分离」「外观」",
                },
            },
        },
        timeout=60,
    )
    async def review_rocket_design(self, focus: str = "", **kwargs: Any) -> Dict[str, Any]:
        """LLM 工具：评审火箭设计（画面 + 遥测）。"""
        focus = _as_text(focus)

        prompt = (
            "这是航天模拟器 Spaceflight Simulator 的画面，用户正在设计或飞行一枚火箭。"
            "请仔细观察画面中的火箭结构（分级、助推器、整流罩、发动机数量）与当前飞行状态，"
            "给出具体的观察与改进建议。"
        )
        if focus:
            prompt += f"用户特别关心：{focus}。"

        # 遥测与零件构成（可能失败，不影响画面分析）
        state_summary = ""
        try:
            state = await self._get_json("/state")
            state_summary = _describe_state(state)
        except Exception:
            state_summary = "（未能读取遥测）"

        try:
            build = await self._get_json("/build")
            state_summary += " " + _describe_build(build)
        except Exception:
            state_summary += " （未能读取零件构成）"

        png = await self._capture_screen()
        if not png:
            return {
                "output": {
                    "ok": False,
                    "telemetry": state_summary,
                    "message": (
                        "没能抓到游戏画面，因此无法评审设计。"
                        "请如实告知用户画面不可用，不要凭空评价火箭外观。"
                    ),
                },
                "is_error": False,
            }

        encoded, mime, note = _encode_for_vision(
            png,
            max_width=self._max_width,
            quality=self._jpeg_quality,
        )
        if encoded is None:
            return {
                "output": {
                    "ok": False,
                    "telemetry": state_summary,
                    "message": f"画面无法处理：{note}",
                },
                "is_error": False,
            }

        if _as_bool(self._vlm.get("enabled")):
            description = await self._call_fallback_vlm(encoded, mime, prompt)
            if description:
                return {
                    "output": {
                        "ok": True,
                        "source": "自带视觉模型",
                        "telemetry": state_summary,
                        "description": description,
                        "message": description,
                    },
                    "is_error": False,
                }

        if not self._vision_enabled:
            return {
                "output": {
                    "ok": False,
                    "telemetry": state_summary,
                    "message": "插件已关闭画面识别，无法评审设计。",
                },
                "is_error": False,
            }

        return {
            "output": {
                "ok": True,
                "source": "N.E.K.O. 视觉模型",
                "telemetry": state_summary,
                "image_note": note,
                "hint": (
                    "画面已附在本次调用中。请结合上面的遥测数据一起分析火箭设计。"
                    "如果提示图片被跳过，说明当前会话没有视觉模型，请如实告知用户。"
                ),
            },
            "images": [
                {
                    "data_b64": encoded,
                    "mime": mime,
                    "vision_prompt": prompt,
                }
            ],
        }


__all__ = ["SfsBridgePlugin"]
