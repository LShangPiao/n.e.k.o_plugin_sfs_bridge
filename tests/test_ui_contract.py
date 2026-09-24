# 和猫娘一起造火箭（SFS）插件测试 —— 造火箭控制台的接口契约
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# 本文件是 sfs_bridge 插件的一部分，以 GNU General Public License v3.0 许可发布。
# 完整条款见仓库根目录的 LICENSE 文件。

"""造火箭控制台（Hosted UI）的契约测试。

面板只能通过 ``props.api.call`` 调**插件入口**，所以三件事必须被守住：

1. 面板会调的每个 ``@ui.action`` id，都同时是一个 ``@plugin_entry``
   （少了 ``@plugin_entry``，host 的 ``trigger`` 根本找不到它）；
2. ``@ui.context(id="dashboard")`` 的 id 与 plugin.toml 里 panel 的 context 一致；
3. provider 不做网络请求、不抛异常 —— host 在每次面板动作之前都会重新求值它。

本文件同时钉住两个容易悄悄改坏的点：``/exclusive`` 的 ``on`` 必须是**带引号的
字符串**（模组侧用 ExtractString 解析，JSON 布尔会被当成「没给」而变成切换），
以及设置保存的范围钳制与密钥不回显。

SDK 导不进来时（例如裸 Python 环境）用一份最小替身顶上，因此本文件在
没有安装 N.E.K.O. 依赖的机器上也能跑。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import importlib.util
import io
import json
import struct
import sys
import types
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "plugin.plugins.sfs_bridge"

# 面板（ui/panel.tsx）会调用的动作 id。改动 ui_api.py / panel.tsx 时同步这里。
EXPECTED_UI_ACTIONS = {
    # 复用 __init__.py 里已有入口的
    "sfs_status",
    "sfs_command",
    "sfs_screenshot",
    "sfs_build",
    "sfs_ui",
    "sfs_click",
    "sfs_key",
    "sfs_parts",
    "sfs_place",
    # ui_api.py 里新增的
    "sfs_ui_ping",
    "sfs_ui_snapshot",
    "sfs_ui_screenshot",
    "sfs_ui_hold_key",
    "sfs_ui_release_key",
    "sfs_ui_blueprints",
    "sfs_ui_load_blueprint",
    "sfs_ui_camera",
    "sfs_ui_exclusive",
    "sfs_ui_save_settings",
}

def _tiny_png() -> bytes:
    """用 stdlib 拼一张**合法**的 1x1 PNG。

    别凭记忆手写 base64：`Image.open` 是惰性的，坏数据要等真正解码像素时才报错，
    而没装 Pillow 的环境根本走不到那一步 —— 只有 CI（装了 Pillow）会暴露，
    表现是 `ok=False`「画面已抓取但无法处理」，本地却全绿。
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1、8bit、RGB
    scanline = zlib.compress(b"\x00\xff\x80\x00")  # filter=0 + 一个像素
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", scanline)
        + chunk(b"IEND", b"")
    )


_TINY_PNG = _tiny_png()


# ---------------------------------------------------------------------------
# SDK 替身
# ---------------------------------------------------------------------------


class SdkError(Exception):
    def __init__(self, message: str, code: str = "", **_: Any) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _Ok:
    """与真实 SDK 的 Ok 同形（frozen dataclass + is_ok），这样 payload() 两边通用。"""

    def __init__(self, value: Any) -> None:
        self.value = value

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Ok({self.value!r})"


class _Err:
    def __init__(self, error: Any) -> None:
        self.error = error

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Err({self.error!r})"


class _EntryMeta:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _install_sdk_stub() -> types.ModuleType:
    """装一份最小的 plugin.sdk.plugin 替身（属性名与真 SDK 对齐）。"""
    module = types.ModuleType("plugin.sdk.plugin")

    def neko_plugin(cls: Any) -> Any:
        return cls

    def lifecycle(**_: Any):
        def decorator(fn: Any) -> Any:
            return fn

        return decorator

    def llm_tool(**_: Any):
        def decorator(fn: Any) -> Any:
            return fn

        return decorator

    def plugin_entry(**kwargs: Any):
        def decorator(fn: Any) -> Any:
            setattr(fn, "__neko_event_meta__", _EntryMeta(**kwargs))
            return fn

        return decorator

    def ui_context(*, id: str = "main", title: Optional[str] = None):
        def decorator(fn: Any) -> Any:
            setattr(fn, "__neko_ui_context__", {"id": str(id), "title": title})
            return fn

        return decorator

    def ui_action(**kwargs: Any):
        def decorator(fn: Any) -> Any:
            setattr(fn, "__neko_ui_action__", dict(kwargs))
            return fn

        return decorator

    class _Logger:
        def info(self, *_: Any, **__: Any) -> None: ...
        def debug(self, *_: Any, **__: Any) -> None: ...
        def warning(self, *_: Any, **__: Any) -> None: ...
        def exception(self, *_: Any, **__: Any) -> None: ...

    class NekoPluginBase:
        def __init__(self, ctx: Any) -> None:
            self.ctx = ctx
            self.config = getattr(ctx, "config", None)
            self.logger = _Logger()

        def enable_file_logging(self, log_level: str = "INFO") -> Any:
            return _Logger()

    module.neko_plugin = neko_plugin
    module.lifecycle = lifecycle
    module.llm_tool = llm_tool
    module.plugin_entry = plugin_entry
    module.ui = types.SimpleNamespace(context=ui_context, action=ui_action)
    module.Ok = _Ok
    module.Err = _Err
    module.SdkError = SdkError
    module.NekoPluginBase = NekoPluginBase
    module.tr = lambda key, default=None, **_: default if default is not None else key
    module.unwrap = lambda value: getattr(value, "value", value)

    plugin_pkg = sys.modules.setdefault("plugin", types.ModuleType("plugin"))
    plugin_pkg.__path__ = []  # type: ignore[attr-defined]
    sdk_pkg = sys.modules.setdefault("plugin.sdk", types.ModuleType("plugin.sdk"))
    sdk_pkg.__path__ = []  # type: ignore[attr-defined]
    plugin_pkg.sdk = sdk_pkg  # type: ignore[attr-defined]
    sdk_pkg.plugin = module  # type: ignore[attr-defined]
    sys.modules["plugin.sdk.plugin"] = module
    return module


def _install_httpx_stub() -> types.ModuleType:
    """装一份最小的 httpx 替身：只需要异常类型（客户端会被替换掉）。"""
    module = types.ModuleType("httpx")

    class HTTPError(Exception):
        pass

    class TimeoutException(HTTPError):
        pass

    class AsyncClient:  # pragma: no cover - 测试里永远用假客户端
        def __init__(self, **_: Any) -> None:
            self.is_closed = False

        async def get(self, *_: Any, **__: Any) -> Any:
            raise HTTPError("stub client")

        async def post(self, *_: Any, **__: Any) -> Any:
            raise HTTPError("stub client")

        async def aclose(self) -> None:
            self.is_closed = True

    module.HTTPError = HTTPError
    module.TimeoutException = TimeoutException
    module.AsyncClient = AsyncClient
    sys.modules["httpx"] = module
    return module


def _try_import(name: str) -> Optional[types.ModuleType]:
    """动态导入，失败返回 None。

    这里刻意不写 ``try: import httpx``：Market CI 的 ruff 带 ``--ignore-noqa``
    和 ``--isolated``（连 ruff.toml 都不读），「只为副作用而 import」会被判成
    F401，``# noqa`` 也压不住。
    """
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _ensure_dependencies() -> types.ModuleType:
    httpx = _try_import("httpx")
    if httpx is not None:
        return httpx
    return _install_httpx_stub()


def _load_plugin_module() -> types.ModuleType:
    """导入插件包；SDK 缺失时先用替身顶上。"""
    if _try_import("plugin.sdk.plugin") is None:
        _install_sdk_stub()
    _ensure_dependencies()

    module = _try_import(PACKAGE)
    if module is not None:
        return module

    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin_module()
SDK = sys.modules["plugin.sdk.plugin"]
HTTPX = sys.modules["httpx"]


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakeLogger:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def _record(self, *args: Any, **_: Any) -> None:
        self.messages.append(" ".join(str(item) for item in args))

    info = _record
    debug = _record
    warning = _record
    exception = _record


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200, content: bytes = b"") -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """按路径返回预置响应，并记录每次请求。"""

    def __init__(self, routes: Optional[Dict[str, Any]] = None) -> None:
        self.routes: Dict[str, Any] = dict(routes or {})
        self.requests: List[Dict[str, Any]] = []
        self.is_closed = False

    @staticmethod
    def _path(url: str) -> str:
        for marker in ("21578", "127.0.0.1"):
            if marker in url:
                url = url.split(marker, 1)[1]
                break
        path = "/" + url.lstrip("/")
        return path.split("?", 1)[0]

    def _answer(self, method: str, url: str, body: Any) -> FakeResponse:
        path = self._path(url)
        self.requests.append({"method": method, "path": path, "body": body})
        entry = self.routes.get(path)
        if entry is None:
            raise HTTPX.HTTPError(f"connection refused: {path}")
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(body)
        return entry

    async def get(self, url: str, timeout: Any = None) -> FakeResponse:
        return self._answer("GET", url, None)

    async def post(self, url: str, json: Any = None, timeout: Any = None) -> FakeResponse:
        return self._answer("POST", url, json)

    async def aclose(self) -> None:
        self.is_closed = True


class FakeImages:
    """宿主的临时图片接口。默认假装不可用，用来验证内联兜底。"""

    def __init__(self, available: bool = False, url: str = "http://127.0.0.1:9/media/abc") -> None:
        self.available = available
        self.url = url
        self.uploads: List[bytes] = []

    async def upload(self, data: bytes, mime: Optional[str] = None, timeout: float = 3.0) -> Dict[str, Any]:
        if not self.available:
            raise RuntimeError("temporary image transport unavailable")
        self.uploads.append(bytes(data))
        return {"type": "image", "url": self.url, "mime": "image/jpeg"}


class FakeConfig:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.patches: List[Dict[str, Any]] = []

    async def dump(self, timeout: float = 5.0) -> Dict[str, Any]:
        return copy.deepcopy(self.data)

    async def update(self, patch: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        self.patches.append(copy.deepcopy(patch))
        for section, values in patch.items():
            if isinstance(values, dict):
                self.data.setdefault(section, {}).update(values)
            else:
                self.data[section] = values
        return copy.deepcopy(self.data)


class FakeCtx:
    def __init__(self, config: FakeConfig, images: Optional[FakeImages] = None) -> None:
        self.config = config
        self.images = images or FakeImages()
        self.logger = FakeLogger()


DEFAULT_CONFIG = {
    "sfs_bridge": {
        "bridge_url": "http://127.0.0.1:21578",
        "timeout_seconds": 12,
        "post_click_wait_seconds": 3.0,
        "screenshot_timeout_seconds": 20,
        "screenshot_max_width": 1024,
        "screenshot_jpeg_quality": 80,
        "vision_enabled": True,
        "vision_prompt": "看看画面",
    },
    "vlm_fallback": {
        "enabled": False,
        "base_url": "",
        "api_key": "secret-key",
        "model": "",
        "max_tokens": 600,
    },
}


def make_plugin(
    routes: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    images: Optional[FakeImages] = None,
) -> Any:
    """构造一个已跑过 on_startup 的插件实例，并把桥接客户端换成假的。

    这里刻意**不调用** ``SfsBridgePlugin(ctx)``：真实的 ``NekoPluginBase.__init__``
    需要一整套宿主上下文（plugin_id / config_path / store / db / ZMQ transport），
    那属于框架自己的测试范围。本文件只验证 UI 契约，所以用 ``__new__`` 造实例，
    再手工挂上被测代码真正用到的那几个属性 —— 这样在装了真实 SDK 的 CI 里
    和在本机的替身环境下都能跑。
    """
    config_obj = FakeConfig(config or DEFAULT_CONFIG)
    ctx = FakeCtx(config_obj, images)
    plugin = PLUGIN.SfsBridgePlugin.__new__(PLUGIN.SfsBridgePlugin)
    plugin.ctx = ctx
    plugin.config = config_obj
    plugin.logger = FakeLogger()
    plugin._client = None
    plugin._client_loop = None
    client = FakeClient(routes)
    plugin._get_client = lambda: client  # type: ignore[assignment]
    asyncio.run(plugin.on_startup())
    return plugin


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def payload(result: Any) -> Dict[str, Any]:
    """Ok 的值 / Err 的错误消息（按 is_ok() 判断，不依赖具体类型）。"""
    is_ok = getattr(result, "is_ok", None)
    if not callable(is_ok):
        raise AssertionError(f"入口必须返回 Ok/Err，实际是 {type(result)!r}")
    if is_ok():
        return result.value
    error = getattr(result, "error", None)
    return {"__error__": str(getattr(error, "message", None) or error)}


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


def test_manifest_declares_the_dashboard_surface() -> None:
    text = (PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8")
    assert 'entry = "ui/panel.tsx"' in text
    assert 'mode = "hosted-tsx"' in text
    assert 'context = "dashboard"' in text
    # 少一个权限，面板上的按钮就会变成 403。
    assert "action:call" in text
    assert "state:read" in text


def test_context_provider_id_matches_manifest() -> None:
    meta = getattr(PLUGIN.SfsBridgePlugin.get_dashboard_ui_context, "__neko_ui_context__", None)
    assert isinstance(meta, dict)
    assert meta["id"] == "dashboard"


def _ui_actions() -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for name in dir(PLUGIN.SfsBridgePlugin):
        member = getattr(PLUGIN.SfsBridgePlugin, name)
        meta = getattr(member, "__neko_ui_action__", None)
        if not isinstance(meta, dict):
            continue
        entry_meta = getattr(member, "__neko_event_meta__", None)
        entry_id = getattr(entry_meta, "id", None) or name
        found[str(meta.get("id") or entry_id)] = member
    return found


def test_every_panel_action_is_exposed_as_ui_action() -> None:
    exposed = set(_ui_actions())
    missing = EXPECTED_UI_ACTIONS - exposed
    assert not missing, f"面板会调但没暴露成 @ui.action：{sorted(missing)}"


def test_every_ui_action_is_also_a_plugin_entry() -> None:
    """只有 @ui.action 没有 @plugin_entry 时，host.trigger 找不到这个入口。"""
    naked = [
        action_id
        for action_id, member in _ui_actions().items()
        if getattr(member, "__neko_event_meta__", None) is None
    ]
    assert not naked, f"这些动作缺 @plugin_entry，面板点了会 404：{sorted(naked)}"


def test_ui_action_ids_match_their_entry_ids() -> None:
    """ui.action 的 id 必须和 entry 的 id 一致，否则白名单解析会绕路。"""
    mismatched = []
    for name in dir(PLUGIN.SfsBridgePlugin):
        member = getattr(PLUGIN.SfsBridgePlugin, name)
        ui_meta = getattr(member, "__neko_ui_action__", None)
        entry_meta = getattr(member, "__neko_event_meta__", None)
        if not isinstance(ui_meta, dict) or entry_meta is None:
            continue
        if ui_meta.get("id") and ui_meta.get("id") != getattr(entry_meta, "id", None):
            mismatched.append(name)
    assert not mismatched, f"动作 id 与入口 id 不一致：{sorted(mismatched)}"


def test_context_provider_is_local_and_json_safe() -> None:
    """provider 不能碰网络，也不能抛出——host 每次动作前都会调它。"""
    plugin = make_plugin()
    state = run(plugin.get_dashboard_ui_context())
    assert plugin._get_client().requests == []  # 一个请求都没发
    assert state["bridge_url"] == "http://127.0.0.1:21578"
    assert state["config"]["bridge_url"] == "http://127.0.0.1:21578"
    # 密钥只回报「有没有」，原值不出插件进程。
    assert state["config"]["vlm_api_key_configured"] is True
    assert "secret-key" not in json.dumps(state)
    json.dumps(state)  # 必须 JSON 安全


def test_context_survives_a_broken_config() -> None:
    plugin = make_plugin()
    plugin._vlm = None  # type: ignore[assignment]
    plugin._bridge_url = None  # type: ignore[assignment]
    state = run(plugin.get_dashboard_ui_context())
    json.dumps(state)
    assert state["bridge_url"] == ""


def test_ping_reports_offline_with_actionable_text() -> None:
    plugin = make_plugin(routes={})  # 没有任何路由 = 连不上
    result = payload(run(plugin.sfs_ui_ping()))
    assert result["connected"] is False
    assert "SFS-Agent" in result["message"]
    assert "重启过游戏" in result["message"]


def test_snapshot_aggregates_every_panel_section() -> None:
    routes = {
        "/ping": FakeResponse({"ok": True}),
        "/state": FakeResponse({
            "ok": True,
            "in_world": True,
            "flying": True,
            "rocket": "测试号",
            "planet": "Earth",
            "height": 1200.0,
            "speed": 88.0,
            "throttle": 0.75,
            "stage": 2,
            "mass": 40.0,
        }),
        "/build": FakeResponse({
            "ok": True,
            "mode": "flight",
            "part_count": 12,
            "stage_count": 2,
            "total_mass": 40.0,
            "part_kinds": [{"name": "Fuel Tank", "count": 3}],
        }),
        "/ui": FakeResponse({"count": 2, "elements": [
            {"index": 0, "label": "Play", "x": 0.5, "y": 0.48},
            {"index": 1, "label": "Settings", "x": 0.5, "y": 0.6},
        ]}),
        "/blueprints": FakeResponse({"ok": True, "count": 1, "blueprints": ["阿波罗"]}),
    }
    plugin = make_plugin(routes=routes)
    data = payload(run(plugin.sfs_ui_snapshot()))

    assert data["connected"] is True
    assert data["scene"] == "flight"
    assert data["telemetry"]["rocket"] == "测试号"
    assert "高度 1200.0 米" in data["telemetry_summary"]
    assert data["ui"]["count"] == 2
    assert len(data["ui"]["elements"]) == 2
    assert data["build"]["part_count"] == 12
    assert data["blueprints"] == ["阿波罗"]
    assert data["errors"] == {}
    json.dumps(data)


def test_snapshot_reports_per_section_errors_instead_of_failing() -> None:
    routes = {
        "/ping": FakeResponse({"ok": True}),
        "/state": FakeResponse({"ok": True, "in_world": False}),
        # /build、/ui、/blueprints 都缺 → 应该各自记一条 error，而不是整体失败
    }
    plugin = make_plugin(routes=routes)
    data = payload(run(plugin.sfs_ui_snapshot()))
    assert data["connected"] is True
    assert data["scene"] == "menu"
    assert set(data["errors"]) == {"build", "ui", "blueprints"}


def test_snapshot_can_skip_optional_sections() -> None:
    routes = {
        "/ping": FakeResponse({"ok": True}),
        "/state": FakeResponse({"ok": True, "in_world": False}),
        "/build": FakeResponse({"ok": True, "mode": "build"}),
    }
    plugin = make_plugin(routes=routes)
    data = payload(run(plugin.sfs_ui_snapshot(include_ui=False, include_blueprints=False)))
    assert data["connected"] is True
    assert data["ui"]["elements"] == []
    assert data["blueprints"] == []
    assert data["errors"] == {}


def test_exclusive_sends_quoted_boolean_because_the_mod_parses_strings() -> None:
    """模组用 ExtractString 解析 on：发 JSON 布尔会被当成「没给」而变成切换。"""
    routes = {"/exclusive": FakeResponse({"ok": True, "exclusive": True, "overlay": "on"})}
    plugin = make_plugin(routes=routes)
    payload(run(plugin.sfs_ui_exclusive(on=True)))
    sent = plugin._get_client().requests[-1]
    assert sent["path"] == "/exclusive"
    assert sent["body"] == {"on": "true"}

    payload(run(plugin.sfs_ui_exclusive(on=False)))
    assert plugin._get_client().requests[-1]["body"] == {"on": "false"}


def test_hold_and_release_use_the_right_endpoints() -> None:
    routes = {
        "/key_down": FakeResponse({"ok": True, "key": "W", "already_held": False}),
        "/key_up": FakeResponse({"ok": True, "key": "W", "was_held": True}),
        "/release_all": FakeResponse({"ok": True, "released": 2, "held": []}),
    }
    plugin = make_plugin(routes=routes)
    held = payload(run(plugin.sfs_ui_hold_key(vk=87)))
    assert held["ok"] is True and held["held"] == "W"

    single = payload(run(plugin.sfs_ui_release_key(vk=87)))
    assert single["ok"] is True
    assert plugin._get_client().requests[-1]["path"] == "/key_up"

    everything = payload(run(plugin.sfs_ui_release_key()))
    assert "全部松开" in everything["message"]
    assert plugin._get_client().requests[-1]["path"] == "/release_all"


def test_hold_key_requires_a_keycode() -> None:
    plugin = make_plugin()
    assert "__error__" in payload(run(plugin.sfs_ui_hold_key(vk=0)))
    assert plugin._get_client().requests == []


def test_camera_needs_at_least_one_field() -> None:
    plugin = make_plugin(routes={"/camera": FakeResponse({"ok": True})})
    assert "__error__" in payload(run(plugin.sfs_ui_camera()))
    assert plugin._get_client().requests == []

    payload(run(plugin.sfs_ui_camera(zoom_delta=-20)))
    assert plugin._get_client().requests[-1]["body"] == {"zoom_delta": -20.0}


def test_load_blueprint_rejects_empty_name() -> None:
    plugin = make_plugin()
    assert "__error__" in payload(run(plugin.sfs_ui_load_blueprint(name="  ")))
    assert plugin._get_client().requests == []


def test_screenshot_prefers_the_host_image_url() -> None:
    images = FakeImages(available=True)
    plugin = make_plugin(routes={"/screenshot": FakeResponse(content=_TINY_PNG)}, images=images)
    data = payload(run(plugin.sfs_ui_screenshot()))
    assert data["ok"] is True
    assert data["url"] == images.url
    assert data["inline"] is False
    assert images.uploads == [_TINY_PNG]


def test_screenshot_falls_back_to_inline_data_url() -> None:
    images = FakeImages(available=False)
    plugin = make_plugin(routes={"/screenshot": FakeResponse(content=_TINY_PNG)}, images=images)
    data = payload(run(plugin.sfs_ui_screenshot()))
    assert data["ok"] is True, data.get("message")
    assert data["inline"] is True
    # 装了 Pillow 会被重新编码成 JPEG，没装才原样返回 PNG，所以这里只锁形状：
    # data URL 的 mime 必须与返回值声明一致，且载荷能解出非空图片。
    scheme, _, body = str(data["url"]).partition(";base64,")
    assert scheme == f"data:{data['mime']}", data["url"][:40]
    assert body and base64.b64decode(body), "内联载荷应该是能解出来的图片"


def test_screenshot_reports_failure_when_the_game_returns_nothing() -> None:
    plugin = make_plugin(routes={"/screenshot": FakeResponse(content=b"")})
    data = payload(run(plugin.sfs_ui_screenshot()))
    assert data["ok"] is False
    assert "截图失败" in data["message"]


def test_tiny_png_fixture_is_a_real_image() -> None:
    """守住踩过的那次 CI 失败：测试图片本身要是合法的。

    `Image.open` 是惰性的，坏数据要到解码像素时才报错 —— 没装 Pillow 的机器
    全绿、装了 Pillow 的 CI 却红。所以这里在有 Pillow 时真的解一次。
    """
    assert _TINY_PNG.startswith(b"\x89PNG\r\n\x1a\n"), "PNG 签名不对"
    try:
        from PIL import Image
    except ImportError:
        return  # 本机没有 Pillow：上面的签名检查已经够用
    image = Image.open(io.BytesIO(_TINY_PNG))
    image.load()  # 真正解码，坏数据在这里才会炸
    assert image.size == (1, 1)


def test_save_settings_clamps_ranges_and_applies_immediately() -> None:
    plugin = make_plugin()
    result = payload(run(plugin.sfs_ui_save_settings(
        bridge_url="http://127.0.0.1:21579",
        timeout_seconds=999,
        post_click_wait_seconds=0.01,
        screenshot_timeout_seconds=1,
        screenshot_max_width=1,
        screenshot_jpeg_quality=999,
        vision_enabled=False,
        vision_prompt="只看火箭",
        vlm_enabled=True,
        vlm_base_url="https://example.com/v1",
        vlm_model="qwen-vl-max",
        vlm_max_tokens=99999,
    )))
    assert result["ok"] is True

    section = plugin.config.patches[-1]["sfs_bridge"]
    assert section["timeout_seconds"] == 60.0
    assert section["post_click_wait_seconds"] == 0.5
    assert section["screenshot_timeout_seconds"] == 5.0
    assert section["screenshot_max_width"] == 320
    assert section["screenshot_jpeg_quality"] == 95

    vlm = plugin.config.patches[-1]["vlm_fallback"]
    assert vlm["max_tokens"] == 4096
    assert "api_key" not in vlm  # 留空 = 保持原值，不能被清掉

    # 立刻生效，不等框架回调
    assert plugin._bridge_url == "http://127.0.0.1:21579"
    assert plugin._vision_enabled is False
    assert plugin._click_wait == 0.5
    assert plugin._max_width == 320
    assert plugin._vlm["enabled"] is True
    assert plugin._vlm["model"] == "qwen-vl-max"


def test_save_settings_writes_a_new_api_key_only_when_given() -> None:
    plugin = make_plugin()
    payload(run(plugin.sfs_ui_save_settings(vlm_api_key="  new-key-42  ")))
    assert plugin.config.patches[-1]["vlm_fallback"]["api_key"] == "new-key-42"

    payload(run(plugin.sfs_ui_save_settings(vlm_api_key="")))
    assert "api_key" not in plugin.config.patches[-1]["vlm_fallback"]


def test_save_settings_rejects_a_bad_bridge_url() -> None:
    plugin = make_plugin()
    result = payload(run(plugin.sfs_ui_save_settings(bridge_url="127.0.0.1:21578")))
    assert "__error__" in result
    assert plugin.config.patches == []


def test_blueprints_can_fail_without_raising() -> None:
    plugin = make_plugin(routes={})
    data = payload(run(plugin.sfs_ui_blueprints()))
    assert data["ok"] is False
    assert data["blueprints"] == []


def test_existing_entries_keep_their_llm_facing_behaviour() -> None:
    """叠加 @ui.action 不该动原有入口的行为（这是给面板复用它们的前提）。"""
    routes = {
        "/state": FakeResponse({"ok": True, "in_world": True, "flying": True, "height": 10.0}),
        "/command": FakeResponse({"ok": True}),
    }
    plugin = make_plugin(routes=routes)
    status = payload(run(plugin.sfs_status()))
    assert status["connected"] is True
    assert status["state"]["height"] == 10.0

    command = payload(run(plugin.sfs_command(command="rcs_on")))
    assert command["ok"] is True
    assert plugin._get_client().requests[-1]["body"] == {"name": "rcs_on", "value": 0.0}

    assert "__error__" in payload(run(plugin.sfs_command(command="launch_to_mars")))


# ---------------------------------------------------------------------------
# 宿主视角：模拟 plugin/core/host.py 的收集方式
# ---------------------------------------------------------------------------


def _walk_wrapped(member: Any) -> List[Any]:
    candidates = [member]
    if hasattr(member, "__func__"):
        candidates.append(member.__func__)
    current = getattr(member, "__wrapped__", None)
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidates.append(current)
        if hasattr(current, "__func__"):
            candidates.append(current.__func__)
        current = getattr(current, "__wrapped__", None)
    return candidates


def _read_meta(member: Any, attr: str) -> Optional[Dict[str, Any]]:
    for candidate in _walk_wrapped(member):
        meta = getattr(candidate, attr, None)
        if isinstance(meta, dict):
            return dict(meta)
    return None


def test_host_style_collection_sees_context_and_actions() -> None:
    """照 host.py 的做法（inspect.getmembers + 元数据链）收一遍，确保面板能用。"""
    import inspect

    plugin = make_plugin()
    instance = plugin

    contexts: Dict[str, Any] = {}
    actions: Dict[str, Dict[str, Any]] = {}
    entry_metas: Dict[str, Any] = {}

    for _name, member in inspect.getmembers(instance, predicate=callable):
        event_meta = getattr(member, "__neko_event_meta__", None)
        if event_meta is not None:
            entry_metas[str(getattr(event_meta, "id", ""))] = event_meta
        context_meta = _read_meta(member, "__neko_ui_context__")
        if context_meta:
            contexts[str(context_meta.get("id") or "main")] = member
        action_meta = _read_meta(member, "__neko_ui_action__")
        if action_meta:
            action_id = str(action_meta.get("id") or "")
            actions[action_id] = action_meta

    assert "dashboard" in contexts, "面板声明的 context 必须能被子进程收集到"
    assert EXPECTED_UI_ACTIONS <= set(actions), (
        f"host 收不到这些动作：{sorted(EXPECTED_UI_ACTIONS - set(actions))}"
    )
    # 每个动作都要能在 entry_map 里找到同名入口，否则 host.trigger 会 404。
    for action_id, meta in actions.items():
        entry_id = str(meta.get("id") or action_id)
        assert entry_id in entry_metas, f"{action_id} 没有对应的 @plugin_entry"


def _declared_entry_timeouts() -> Dict[str, float]:
    """从 @ui.action + @plugin_entry 块里取出入口声明的 timeout（秒）。"""
    import re

    found: Dict[str, float] = {}
    for filename in ("ui_api.py", "__init__.py"):
        text = (PLUGIN_ROOT / filename).read_text(encoding="utf-8")
        for chunk in text.split("@ui.action(")[1:]:
            head = chunk.partition("async def ")[0]
            action = re.search(r'id="([a-z_]+)"', head)
            timeout = re.search(r"timeout=\s*([0-9.]+)", head)
            if action and timeout:
                found[action.group(1)] = float(timeout.group(1))
    return found


def _panel_call_timeouts() -> Dict[str, float]:
    """面板里 TIMEOUTS 表（毫秒）。"""
    import re

    text = (PLUGIN_ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")
    block = text.split("const TIMEOUTS", 1)[1].split("}", 1)[0]
    return {key: float(value.replace("_", "")) for key, value in re.findall(r"([a-z_]+):\s*([0-9_]+)", block)}


def test_panel_timeouts_are_not_shorter_than_the_backend_entries() -> None:
    """面板等不住的动作会被判成超时，即使后端还在正常干活。

    宿主桥接默认只等 30 秒，所以重动作必须显式给上限；而那些上限又必须
    不小于入口自己声明的 timeout，否则「面板报超时 / 游戏其实成功了」。
    """
    declared = _declared_entry_timeouts()
    panel = _panel_call_timeouts()
    assert panel, "面板应当为重动作显式声明超时"
    for action_id, milliseconds in panel.items():
        backend = declared.get(action_id)
        if backend is None:
            continue
        assert milliseconds / 1000.0 >= backend, (
            f"{action_id}：面板只等 {milliseconds / 1000.0}s，入口却允许 {backend}s"
        )


def test_heavy_actions_declare_a_host_bridge_timeout() -> None:
    """抓图/点击/载入蓝图都可能远超桥接默认的 30 秒。"""
    panel = _panel_call_timeouts()
    for action_id in ("sfs_ui_screenshot", "sfs_click", "sfs_ui_load_blueprint"):
        assert panel.get(action_id, 0) > 30_000, f"{action_id} 需要显式的长超时"