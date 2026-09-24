// 和猫娘一起造火箭（SFS）插件 —— 造火箭控制台面板
// Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
//
// 本文件是 sfs_bridge 插件的一部分，以 GNU General Public License v3.0 许可发布。
// 完整条款见仓库根目录的 LICENSE 文件。
//
// 这个面板在宿主提供的 sandbox iframe 里运行，**不是**普通网页：
//   · 只允许 import 相对文件与 "@neko/plugin-ui"，别的裸模块解析不了；
//   · 拿不到 fetch / window.electronShell，一切数据都走 props.api.call；
//   · 面板通过 props.api.call 调到的每个动作，都必须在 plugin.toml 的
//     permissions 里声明 action:call，并在后端用 @ui.action 暴露（见 ui_api.py）。

import {
  Alert,
  Button,
  ButtonGroup,
  Card,
  DataTable,
  Divider,
  Field,
  Grid,
  ImagePreview,
  Inline,
  Input,
  KeyValue,
  NumberInput,
  Page,
  PasswordInput,
  Slider,
  Stack,
  StatCard,
  StatusBadge,
  Switch,
  Text,
  TextBlock,
  Textarea,
  Tip,
  Toolbar,
  ToolbarGroup,
  Warning,
  useEffect,
  useLocalState,
  useRef,
  useState,
  useConfirm,
  useToast,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"

// ─────────────────────────────────────────────────────────────────────────
// 常量
// ─────────────────────────────────────────────────────────────────────────

// 自动刷新间隔。游戏是本地回环，4 秒够用又不至于把游戏问烦。
const POLL_MS = 4000

// 宿主桥接（ui-kit 的 requestHost）默认超时只有 30 秒，而这里的动作会真的去等游戏：
// 抓图要等游戏渲染一帧、点击后要等它切场景、载入蓝图要重建整枚火箭。
// 所以每个动作显式带上限，且必须 ≥ 后端入口声明的 timeout，否则面板会先超时。
const CALL_TIMEOUT_MS = 60_000
const TIMEOUTS: Record<string, number> = {
  sfs_ui_snapshot: 60_000,
  sfs_ui_screenshot: 95_000,
  sfs_screenshot: 95_000,
  sfs_click: 90_000,
  sfs_ui_load_blueprint: 130_000,
}

function timeoutFor(actionId: string): number {
  return TIMEOUTS[actionId] || CALL_TIMEOUT_MS
}

const TABS = [
  { id: "overview", label: "总览" },
  { id: "screen", label: "画面" },
  { id: "screenui", label: "界面" },
  { id: "flight", label: "飞行" },
  { id: "build", label: "建造" },
  { id: "settings", label: "设置" },
]

// 后端在 state.commands 里会带一份同样的表；这里留一份兜底，
// 免得插件没启动时「飞行」页空成一片。两边都是游戏的固定事实，不是业务逻辑。
const FALLBACK_COMMANDS = [
  { id: "throttle_on", label: "点火", tone: "danger", hint: "打开油门（不改油门大小）" },
  { id: "throttle_off", label: "熄火", tone: "default", hint: "关闭油门" },
  { id: "stage", label: "执行下一级", tone: "warning", hint: "等同按空格" },
  { id: "staging_program", label: "分级控制程序", tone: "default", hint: "等同按回车" },
  { id: "rcs_toggle", label: "RCS 开关", tone: "info", hint: "切换 RCS" },
  { id: "rcs_on", label: "开 RCS", tone: "info", hint: "显式打开 RCS" },
  { id: "rcs_off", label: "关 RCS", tone: "default", hint: "显式关闭 RCS" },
]

// vk 与 UnityEngine.KeyCode 数值一致；SFS 的默认键位见 README。
const FALLBACK_KEYS = [
  { vk: 81, name: "Q", label: "左转", group: "转向" },
  { vk: 69, name: "E", label: "右转", group: "转向" },
  { vk: 87, name: "W", label: "上抬 / 前移", group: "RCS 平移" },
  { vk: 83, name: "S", label: "下压 / 后移", group: "RCS 平移" },
  { vk: 65, name: "A", label: "左移", group: "RCS 平移" },
  { vk: 68, name: "D", label: "右移", group: "RCS 平移" },
  { vk: 32, name: "空格", label: "点火 / 下一级", group: "常用" },
  { vk: 13, name: "回车", label: "分级控制程序", group: "常用" },
  { vk: 82, name: "R", label: "RCS 开关", group: "常用" },
  { vk: 16, name: "Shift", label: "油门加大", group: "常用" },
  { vk: 17, name: "Ctrl", label: "油门减小", group: "常用" },
  { vk: 27, name: "Esc", label: "返回 / 暂停", group: "常用" },
]

// ─────────────────────────────────────────────────────────────────────────
// 类型
// ─────────────────────────────────────────────────────────────────────────

type Orbit = {
  apoapsis?: number
  periapsis?: number
  eccentricity?: number
  period?: number
}

type Telemetry = {
  in_world?: boolean
  flying?: boolean
  rocket?: string
  planet?: string
  height?: number
  speed?: number
  throttle?: number
  throttle_on?: boolean
  stage?: number
  mass?: number
  has_control?: boolean
  angle?: number
  target_angle?: number
  pitch_angle?: number
  flight_path_angle?: number
  velocity_x?: number
  velocity_y?: number
  has_orbit?: boolean
  orbit?: Orbit
  orbit_error?: string
  atmosphere_height?: number
  planet_radius?: number
  planet_mu?: number
  true_anomaly?: number
  time_to_apo?: number
  time_to_peri?: number
  held_keys?: unknown
  game_time?: number
  error?: string
}

type UiElement = { index?: number; label?: string; x?: number; y?: number }

type PartKind = { name?: string; count?: number }

type Build = {
  mode?: string
  part_count?: number
  stage_count?: number
  total_mass?: number
  part_kinds?: PartKind[]
}

type Snapshot = {
  connected?: boolean
  error?: string
  latency_ms?: number
  bridge_url?: string
  telemetry?: Telemetry
  telemetry_summary?: string
  ui?: { count?: number; elements?: UiElement[] }
  build?: Build
  build_summary?: string
  blueprints?: string[]
  scene?: string
  errors?: Record<string, string>
  fetched_at?: number
}

type EffectiveConfig = {
  bridge_url?: string
  timeout_seconds?: number
  post_click_wait_seconds?: number
  screenshot_timeout_seconds?: number
  screenshot_max_width?: number
  screenshot_jpeg_quality?: number
  vision_enabled?: boolean
  vision_prompt?: string
  vlm_enabled?: boolean
  vlm_base_url?: string
  vlm_model?: string
  vlm_max_tokens?: number
  vlm_api_key_configured?: boolean
}

type CommandDef = { id?: string; label?: string; tone?: string; hint?: string }
type KeyDef = { vk?: number; name?: string; label?: string; group?: string }

type PanelState = {
  connected?: boolean | null
  last_error?: string
  bridge_url?: string
  vision_enabled?: boolean
  config?: EffectiveConfig
  commands?: CommandDef[]
  keys?: KeyDef[]
}

type SettingsDraft = Record<string, any>

// ─────────────────────────────────────────────────────────────────────────
// 小工具（纯函数，无 hooks）
// ─────────────────────────────────────────────────────────────────────────

/** props.api.call 解析成 `{plugin_id, action_id, result}`；也容错裸 dict 形状。 */
function unwrap(envelope: any): Record<string, any> {
  if (envelope && typeof envelope === "object") {
    const nested = (envelope as any).result
    if (nested && typeof nested === "object") return nested as Record<string, any>
    return envelope as Record<string, any>
  }
  return {}
}

type Failure = { notRunning: boolean; message: string }

/** 把宿主抛回来的错误整理成人能看的文案，并识别「插件没启动」。 */
function describeError(error: any): Failure {
  const message = String(
    (error && (error.message || error.error)) || error || "未知错误",
  )
  const code = String((error && error.code) || "")
  const notRunning =
    code === "PLUGIN_NOT_RUNNING" || /PLUGIN_NOT_RUNNING|插件未启动|not running/i.test(message)
  return {
    notRunning,
    message: code && !message.includes(code) ? `${code}：${message}` : message,
  }
}

function isNum(value: any): value is number {
  return typeof value === "number" && Number.isFinite(value)
}

function num(value: any, digits = 1, suffix = ""): string {
  return isNum(value) ? `${value.toFixed(digits)}${suffix}` : "—"
}

function asText(value: any): string {
  if (typeof value === "string") return value
  if (value === null || value === undefined) return ""
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

/** /state 的 throttle 是 0-1（后端 _describe_state 里乘 100 显示）。 */
function throttleText(value: any): string {
  return isNum(value) ? `${Math.round(value * 100)}%` : "—"
}

function durationText(seconds: any): string {
  if (!isNum(seconds)) return "—"
  const total = Math.max(0, Math.round(seconds))
  if (total >= 3600) return `${(total / 3600).toFixed(1)} 小时`
  if (total >= 60) return `${(total / 60).toFixed(1)} 分钟`
  return `${total} 秒`
}

function heldKeysText(held: any): string {
  if (Array.isArray(held)) {
    const names = held.map((item) => (typeof item === "string" ? item : JSON.stringify(item)))
    return names.length ? names.join("、") : "（没有按键被按住）"
  }
  if (held && typeof held === "object") {
    const names = Object.keys(held)
    return names.length ? names.join("、") : "（没有按键被按住）"
  }
  return "（未知）"
}

function sceneMeta(scene: string | undefined): { label: string; tone: string } {
  switch (scene) {
    case "offline":
      return { label: "未连接", tone: "danger" }
    case "menu":
      return { label: "主菜单 / 加载中", tone: "warning" }
    case "build":
      return { label: "建造场景", tone: "info" }
    case "flight":
      return { label: "飞行中", tone: "success" }
    case "idle":
      return { label: "空闲", tone: "default" }
    default:
      return { label: "未知场景", tone: "default" }
  }
}

function numberOr(value: any, fallback: number): number {
  // 空的数字输入框会给出 ""，而 Number("") 是 0 —— 那不是用户想填的值。
  if (value === "" || value === null || value === undefined) return fallback
  const parsed = typeof value === "number" ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

// ─────────────────────────────────────────────────────────────────────────
// 面板
// ─────────────────────────────────────────────────────────────────────────

export default function SfsBridgePanel(props: PluginSurfaceProps<PanelState>) {
  const state = (props.state || {}) as PanelState
  const effective = (state.config || {}) as EffectiveConfig

  const commands: CommandDef[] =
    Array.isArray(state.commands) && state.commands.length > 0 ? state.commands : FALLBACK_COMMANDS
  const keys: KeyDef[] =
    Array.isArray(state.keys) && state.keys.length > 0 ? state.keys : FALLBACK_KEYS

  const toast = useToast()
  const confirm = useConfirm()

  // props.api 每次渲染都可能是新对象；放进 ref，避免把它写进依赖导致轮询重建。
  const apiRef = useRef(props.api)
  apiRef.current = props.api

  const [tab, setTab] = useLocalState<string>("tab", "overview")
  const [snapshot, setSnapshot] = useLocalState<Snapshot>("snapshot", {})
  const [lastError, setLastError] = useLocalState<string>("lastError", "")
  const [pluginMissing, setPluginMissing] = useLocalState<boolean>("pluginMissing", false)
  const [autoRefresh, setAutoRefresh] = useLocalState<boolean>("autoRefresh", true)
  const [probing, setProbing] = useState(false)
  const [busyAction, setBusyAction] = useState("")
  const [screenUrl, setScreenUrl] = useLocalState<string>("screenUrl", "")
  const [screenNote, setScreenNote] = useLocalState<string>("screenNote", "")
  const [clickAfter, setClickAfter] = useLocalState<string>("clickAfter", "")
  const [selectedBlueprint, setSelectedBlueprint] = useLocalState<string>("selectedBlueprint", "")
  const [selectedPart, setSelectedPart] = useLocalState<string>("selectedPart", "")
  const [catalog, setCatalog] = useLocalState<string[]>("partsCatalog", [])
  const [partFilter, setPartFilter] = useLocalState<string>("partFilter", "")
  const [placedX, setPlacedX] = useLocalState<number>("placedX", 0)
  const [placedY, setPlacedY] = useLocalState<number>("placedY", 8)
  const [throttleDraft, setThrottleDraft] = useLocalState<number>("throttleDraft", 0)
  const [exclusiveOn, setExclusiveOn] = useLocalState<boolean>("exclusiveOn", false)
  const [clickIndex, setClickIndex] = useLocalState<number>("clickIndex", 0)
  const [clickX, setClickX] = useLocalState<number>("clickX", 0.5)
  const [clickY, setClickY] = useLocalState<number>("clickY", 0.5)
  const [draft, setDraft] = useLocalState<SettingsDraft>("settingsDraft", {})

  // 两个独立的 in-flight 闸门：
  // 轮询和用户点击不能互相吞掉（一个闸门会让「保存后自动刷新」自我死锁）。
  const actionRef = useRef(false)
  const pollingRef = useRef(false)
  const aliveRef = useRef(true)

  const telemetry: Telemetry = (snapshot.telemetry || {}) as Telemetry
  const build: Build = (snapshot.build || {}) as Build
  const elements: UiElement[] = Array.isArray(snapshot.ui?.elements) ? snapshot.ui!.elements! : []
  const blueprints: string[] = Array.isArray(snapshot.blueprints) ? snapshot.blueprints! : []
  const scene = sceneMeta(snapshot.connected ? snapshot.scene : "offline")

  const connected = snapshot.connected === true
  const neverProbed = snapshot.fetched_at === undefined && !lastError

  // ── 草稿初始化：生效值到手后灌一次，之后不再覆盖用户正在敲的内容 ──
  useEffect(() => {
    if (!effective || !effective.bridge_url) return
    if (draft && Object.keys(draft).length > 0) return
    setDraft({
      bridge_url: asText(effective.bridge_url),
      timeout_seconds: numberOr(effective.timeout_seconds, 12),
      post_click_wait_seconds: numberOr(effective.post_click_wait_seconds, 3),
      screenshot_timeout_seconds: numberOr(effective.screenshot_timeout_seconds, 20),
      screenshot_max_width: numberOr(effective.screenshot_max_width, 1024),
      screenshot_jpeg_quality: numberOr(effective.screenshot_jpeg_quality, 80),
      vision_enabled: !!effective.vision_enabled,
      vision_prompt: asText(effective.vision_prompt),
      vlm_enabled: !!effective.vlm_enabled,
      vlm_base_url: asText(effective.vlm_base_url),
      vlm_model: asText(effective.vlm_model),
      vlm_max_tokens: numberOr(effective.vlm_max_tokens, 600),
      vlm_api_key: "",
    })
  }, [effective.bridge_url, draft])

  // ── 数据读取 ──────────────────────────────────────────────────────────

  /** 让宿主重新求值 @ui.context（拿到新的生效配置）。失败不影响主流程。 */
  async function refreshPanelContext(): Promise<void> {
    try {
      await apiRef.current.refresh()
    } catch (error) {
      // 面板数据刷新失败只是「生效值」显示旧了一点，不值得打断用户。
      void error
    }
  }

  /** 读一次完整快照。不加闸门：调用方自己负责节流。 */
  async function loadSnapshot(): Promise<Snapshot | null> {
    try {
      const result = unwrap(
        await apiRef.current.call(
          "sfs_ui_snapshot",
          { include_ui: true, include_blueprints: true },
          { timeoutMs: timeoutFor("sfs_ui_snapshot") },
        ),
      )
      if (!aliveRef.current) return null
      const next = result as Snapshot
      setSnapshot(next)
      setPluginMissing(false)
      setLastError(next.connected ? "" : asText(next.error))
      return next
    } catch (error) {
      const failure = describeError(error)
      if (!aliveRef.current) return null
      setPluginMissing(failure.notRunning)
      setLastError(failure.message)
      setSnapshot((prev) => ({ ...(prev || {}), connected: false, error: failure.message }))
      return null
    }
  }

  /** 轮询入口（独立闸门：上一次没回来就跳过这一轮）。 */
  async function poll(): Promise<void> {
    if (pollingRef.current) return
    pollingRef.current = true
    try {
      await loadSnapshot()
    } finally {
      pollingRef.current = false
    }
  }

  /** 用户点出来的动作：统一闸门 + 统一报错 + 可选刷新[完整快照]。 */
  async function run(
    actionId: string,
    args: Record<string, any> = {},
    options: {
      success?: string
      refresh?: boolean
      timeoutMs?: number
      refreshContext?: boolean
      toast?: boolean
    } = {},
  ): Promise<Record<string, any> | null> {
    if (actionRef.current) {
      toast.warning("上一步还没结束，稍等一下再点。")
      return null
    }
    actionRef.current = true
    setBusyAction(actionId)
    try {
      const result = unwrap(await apiRef.current.call(actionId, args, {
        timeoutMs: options.timeoutMs || timeoutFor(actionId),
        // 用户点的按钮：确认框会 await 掉同步事件作用域，所以显式标记。
        // 这决定 PLUGIN_NOT_RUNNING 是立刻报错（用户能马上看到提示）还是静默重试。
        userInitiated: true,
      }))
      setPluginMissing(false)
      const success =
        options.success || asText((result as any).message) || "完成"
      // 有些动作要按返回值决定说好话还是坏话，它们自己弹，别弹两条。
      if (options.toast !== false) toast.success(success)
      if (options.refresh !== false) await loadSnapshot()
      if (options.refreshContext) await refreshPanelContext()
      return result
    } catch (error) {
      const failure = describeError(error)
      setPluginMissing(failure.notRunning)
      setLastError(failure.message)
      if (failure.notRunning) {
        toast.warning("插件还没启动：先到插件页启动「和猫娘一起造火箭(SFS)」。")
      } else {
        toast.error(failure.message)
      }
      return null
    } finally {
      actionRef.current = false
      if (aliveRef.current) setBusyAction("")
    }
  }

  // 打开面板先拉一次；关掉时置死，避免卸载后写 state。
  useEffect(() => {
    aliveRef.current = true
    void loadSnapshot()
    return () => {
      aliveRef.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 自动刷新：只重读快照，不动用户正在填的表单。
  useEffect(() => {
    if (!autoRefresh) return
    const timer = setInterval(() => {
      if (aliveRef.current) void poll()
    }, POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh])

  // ── 具体操作 ──────────────────────────────────────────────────────────

  async function probeConnection() {
    setProbing(true)
    try {
      const result = unwrap(await apiRef.current.call("sfs_ui_ping", {}, {
        timeoutMs: timeoutFor("sfs_ui_ping"),
      }))
      setPluginMissing(false)
      if (result.connected) {
        toast.success(asText(result.message) || "已连接")
        setLastError("")
        await loadSnapshot()
      } else {
        setLastError(asText(result.message))
        toast.error(asText(result.message) || "连不上游戏")
      }
    } catch (error) {
      const failure = describeError(error)
      setPluginMissing(failure.notRunning)
      setLastError(failure.message)
      toast.error(failure.message)
    } finally {
      if (aliveRef.current) setProbing(false)
    }
  }

  async function grabScreen(inline: boolean) {
    if (actionRef.current) {
      toast.warning("上一步还没结束。")
      return
    }
    actionRef.current = true
    setBusyAction("screen")
    try {
      const result = unwrap(await apiRef.current.call("sfs_ui_screenshot", { inline }, {
        timeoutMs: timeoutFor("sfs_ui_screenshot"),
        userInitiated: true,
      }))
      if (result.ok && asText(result.url)) {
        setScreenUrl(asText(result.url))
        setScreenNote(asText(result.note))
        toast.success(asText(result.message) || "已抓取画面")
      } else {
        toast.error(asText(result.message) || "抓取画面失败")
      }
    } catch (error) {
      const failure = describeError(error)
      setPluginMissing(failure.notRunning)
      toast.error(failure.message)
    } finally {
      actionRef.current = false
      if (aliveRef.current) setBusyAction("")
    }
  }

  async function clickElement(element: UiElement) {
    const index = isNum(element.index) ? element.index : -1
    const result = await run(
      "sfs_click",
      index >= 0 ? { index } : { x: element.x, y: element.y },
      { success: `已点击 #${index} ${asText(element.label)}` },
    )
    if (result) setClickAfter(asText(result.after))
  }

  async function clickByIndex() {
    const index = Math.round(numberOr(clickIndex, 0))
    const result = await run("sfs_click", { index }, { success: `已点击元素 #${index}` })
    if (result) setClickAfter(asText(result.after))
  }

  async function clickByPosition() {
    const result = await run(
      "sfs_click",
      { x: numberOr(clickX, 0.5), y: numberOr(clickY, 0.5) },
      { success: `已点击 (${numberOr(clickX, 0.5).toFixed(2)}, ${numberOr(clickY, 0.5).toFixed(2)})` },
    )
    if (result) setClickAfter(asText(result.after))
  }

  async function sendCommand(id: string, value = 0) {
    await run(
      "sfs_command",
      value ? { command: id, value } : { command: id },
      { success: `已发送指令 ${id}` },
    )
  }

  async function applyThrottle() {
    const value = Math.max(0, Math.min(100, numberOr(throttleDraft, 0))) / 100
    await run("sfs_command", { command: "set_throttle", value }, {
      success: `已把油门设为 ${Math.round(value * 100)}%`,
    })
  }

  async function tapKey(key: KeyDef) {
    const vk = Math.round(numberOr(key.vk, 0))
    await run("sfs_key", { vk }, { success: `已发送按键 ${asText(key.name) || vk}` })
  }

  async function holdKey(key: KeyDef) {
    const vk = Math.round(numberOr(key.vk, 0))
    await run("sfs_ui_hold_key", { vk }, { success: `已按住 ${asText(key.name) || vk}` })
  }

  async function releaseKey(key?: KeyDef) {
    const vk = key ? Math.round(numberOr(key.vk, 0)) : 0
    await run(
      "sfs_ui_release_key",
      { vk },
      { success: key ? `已松开 ${asText(key.name)}` : "已全部松开" },
    )
  }

  async function moveCamera(zoomDelta: number) {
    await run("sfs_ui_camera", { zoom_delta: zoomDelta }, {
      success: zoomDelta > 0 ? "已拉远视角" : "已拉近视角",
    })
  }

  async function rotateCamera(rotation: number) {
    await run("sfs_ui_camera", { rotation }, { success: `视角旋转到 ${rotation}°` })
  }

  async function toggleExclusive(next: boolean) {
    if (next) {
      const accepted = await confirm({
        title: "开启 Agent 独占模式",
        message:
          "开启后，游戏会忽略你自己的鼠标与键盘，只接受猫娘派发的输入。" +
          "需要在游戏里按 F10 才能应急解除。确定要开吗？",
        tone: "danger",
        confirmLabel: "开启独占",
        cancelLabel: "取消",
      })
      if (!accepted) return
    }
    const result = await run("sfs_ui_exclusive", { on: next })
    if (result) setExclusiveOn(!!result.exclusive)
  }

  async function reloadBlueprints() {
    const result = await run("sfs_ui_blueprints", {}, { refresh: false, toast: false })
    if (result) {
      if (result.ok) {
        toast.success(asText(result.message) || `共 ${result.count || 0} 个蓝图`)
        setSnapshot((prev) => ({ ...(prev || {}), blueprints: (result.blueprints || []) as string[] }))
      } else {
        toast.error(asText(result.message) || "读取蓝图失败")
      }
    }
  }

  async function loadSelectedBlueprint() {
    if (!selectedBlueprint) {
      toast.warning("先在列表里点一行选中蓝图。")
      return
    }
    const accepted = await confirm({
      title: "载入蓝图",
      message: `会把「${selectedBlueprint}」整枚加载进建造台，替换当前火箭（未保存的改动会丢失）。继续吗？`,
      tone: "warning",
      confirmLabel: "载入",
      cancelLabel: "取消",
    })
    if (!accepted) return
    await run("sfs_ui_load_blueprint", { name: selectedBlueprint })
  }

  async function loadParts() {
    const result = await run("sfs_parts", {}, { toast: false })
    if (result) {
      const list = Array.isArray(result.parts) ? (result.parts as string[]) : []
      setCatalog(list)
      toast.success(`读到 ${list.length} 个零件名`)
    }
  }

  async function placeSelectedPart() {
    if (!selectedPart) {
      toast.warning("先在零件表里点一行选中零件。")
      return
    }
    await run(
      "sfs_place",
      { name: selectedPart, x: numberOr(placedX, 0), y: numberOr(placedY, 0) },
      { success: `已放置「${selectedPart}」` },
    )
  }

  function setField(name: string, value: any) {
    setDraft((prev) => ({ ...(prev || {}), [name]: value }))
  }

  function fieldValue(name: string, fallback: any): any {
    const current = (draft || {})[name]
    // 只有「还没填过」才用生效值兜底：空字符串是用户主动清空的，不能顶回去。
    if (current === undefined || current === null) return fallback
    return current
  }

  async function saveSettings() {
    const mode = asText(fieldValue("bridge_url", effective.bridge_url))
    if (!mode.startsWith("http://") && !mode.startsWith("https://")) {
      toast.error("桥接地址要以 http:// 或 https:// 开头。")
      return
    }
    const result = await run("sfs_ui_save_settings", {
      bridge_url: mode,
      timeout_seconds: numberOr(fieldValue("timeout_seconds", 12), 12),
      post_click_wait_seconds: numberOr(fieldValue("post_click_wait_seconds", 3), 3),
      screenshot_timeout_seconds: numberOr(fieldValue("screenshot_timeout_seconds", 20), 20),
      screenshot_max_width: numberOr(fieldValue("screenshot_max_width", 1024), 1024),
      screenshot_jpeg_quality: numberOr(fieldValue("screenshot_jpeg_quality", 80), 80),
      vision_enabled: !!fieldValue("vision_enabled", true),
      vision_prompt: asText(fieldValue("vision_prompt", "")),
      vlm_enabled: !!fieldValue("vlm_enabled", false),
      vlm_base_url: asText(fieldValue("vlm_base_url", "")),
      vlm_model: asText(fieldValue("vlm_model", "")),
      vlm_max_tokens: numberOr(fieldValue("vlm_max_tokens", 600), 600),
      // 留空 = 不改：密钥不回显，面板也不该把空值写回去。
      vlm_api_key: asText((draft || {}).vlm_api_key),
    }, { refreshContext: true })
    if (result && result.ok) {
      setField("vlm_api_key", "")
    }
  }

  function resetDraft() {
    setDraft({})
    toast.info("已恢复为当前生效值。")
  }

  // ── 派生数据 ──────────────────────────────────────────────────────────

  const heldText = heldKeysText(telemetry.held_keys)
  const orbit: Orbit = telemetry.orbit || {}
  const hasOrbit = telemetry.has_orbit === true && isNum(orbit.periapsis)
  const partKinds: PartKind[] = Array.isArray(build.part_kinds) ? build.part_kinds! : []
  // 零件目录可能上千条，不塞进每次轮询的快照里：点「读取零件目录」时才拉一次，
  // 结果留在本地 state（useLocalState 跨渲染存活）。
  const filter = partFilter.trim().toLowerCase()
  const visibleParts = filter
    ? catalog.filter((name) => String(name).toLowerCase().includes(filter))
    : catalog

  const elementRows = elements.map((element, position) => ({
    key: isNum(element.index) ? element.index : position,
    index: isNum(element.index) ? element.index : position,
    label: asText(element.label) || "（无标签）",
    x: element.x,
    y: element.y,
    raw: element,
  }))

  const blueprintRows = blueprints.map((name) => ({ key: name, name }))
  const partRows = visibleParts.map((name) => ({ key: name, name }))
  const kindRows = partKinds.map((item, position) => ({
    key: `${asText(item.name)}-${position}`,
    name: asText(item.name) || "（未命名）",
    count: numberOr(item.count, 0),
  }))

  // 自定义渲染器对同一个 vnode 复用是危险的，所以这里做成函数：
  // 每次调用返回一个全新的元素。
  const renderSceneBadge = () => (
    <StatusBadge
      tone={snapshot.connected ? (scene.tone as any) : "danger"}
      label={
        pluginMissing
          ? "插件未启动"
          : snapshot.connected
            ? scene.label
            : neverProbed
              ? "正在检测…"
              : "未连接"
      }
    />
  )

  // ── 各页 ──────────────────────────────────────────────────────────────

  const overviewTab = (
    <Stack>
      {pluginMissing ? (
        <Alert tone="warning">
          插件还没启动，面板暂时拿不到数据。请到 N.E.K.O. 的插件页面启动
          「和猫娘一起造火箭(SFS)」，然后点下面的「刷新全部」。
        </Alert>
      ) : null}

      {!pluginMissing && lastError && !snapshot.connected ? (
        <Alert tone="danger">{lastError}</Alert>
      ) : null}

      {snapshot.errors && Object.keys(snapshot.errors).length > 0 ? (
        <Alert tone="warning">
          部分数据没读到：
          {Object.entries(snapshot.errors)
            .map(([key, value]) => `${key}：${value}`)
            .join("；")}
        </Alert>
      ) : null}

      <Card title="连接">
        <Stack>
          <Inline align="center" justify="space-between" wrap>
            <Inline align="center" gap={8}>
              {renderSceneBadge()}
              <Text>{asText(snapshot.bridge_url || state.bridge_url) || "（未配置地址）"}</Text>
            </Inline>
            <Inline align="center" gap={8}>
              {isNum(snapshot.latency_ms) && snapshot.connected ? (
                <Text>{`往返 ${snapshot.latency_ms} 毫秒`}</Text>
              ) : null}
              {snapshot.fetched_at ? (
                <Text>{`更新于 ${new Date(snapshot.fetched_at * 1000).toLocaleTimeString()}`}</Text>
              ) : null}
            </Inline>
          </Inline>
          <Toolbar>
            <ToolbarGroup>
              <Button
                tone="primary"
                disabled={busyAction !== "" || probing}
                onClick={() => void loadSnapshot()}
              >
                {busyAction === "sfs_ui_snapshot" ? "读取中…" : "刷新全部"}
              </Button>
              <Button tone="info" disabled={probing || busyAction !== ""} onClick={() => void probeConnection()}>
                {probing ? "检测中…" : "检测连接"}
              </Button>
            </ToolbarGroup>
            <ToolbarGroup>
              <Switch
                checked={autoRefresh}
                label="自动刷新"
                onChange={(value: boolean) => setAutoRefresh(value)}
              />
            </ToolbarGroup>
          </Toolbar>
          <Tip>
            自动刷新每 {Math.round(POLL_MS / 1000)} 秒读一次快照（遥测 + 界面 + 构成 + 蓝图）。
            跟游戏对话时也可以关掉，只手动刷。
          </Tip>
        </Stack>
      </Card>

      <Card title="飞行遥测">
        <Stack>
          {!snapshot.connected ? (
            <Alert tone="info">
              还没连上游戏。先开游戏、装好 SFS-Agent 模组并重启过游戏，再点「检测连接」。
            </Alert>
          ) : null}
          <Grid cols={3} gap={10}>
            <StatCard label="高度" value={num(telemetry.height, 1, " 米")} />
            <StatCard label="速度" value={num(telemetry.speed, 1, " 米/秒")} />
            <StatCard label="油门" value={throttleText(telemetry.throttle)} />
            <StatCard label="当前分级" value={isNum(telemetry.stage) && telemetry.stage >= 0 ? String(telemetry.stage) : "—"} />
            <StatCard label="总质量" value={num(telemetry.mass, 1, " 吨")} />
            <StatCard label="所在天体" value={asText(telemetry.planet) || "—"} />
          </Grid>

          <KeyValue
            items={[
              { key: "rocket", label: "飞行器", value: asText(telemetry.rocket) || "—" },
              {
                key: "flying",
                label: "是否在飞行",
                value: telemetry.flying ? "飞行中" : telemetry.in_world ? "在世界里（未飞行）" : "不在世界场景",
              },
              { key: "control", label: "是否有控制权", value: telemetry.has_control ? "有" : "没有" },
              { key: "throttle_on", label: "油门开关", value: telemetry.throttle_on ? "已打开" : "已关闭" },
              { key: "angle", label: "火箭朝向", value: num(telemetry.angle, 1, "°") },
              { key: "pitch", label: "攻角", value: num(telemetry.pitch_angle, 1, "°") },
              { key: "fpa", label: "速度方向角", value: num(telemetry.flight_path_angle, 1, "°") },
              { key: "target", label: "目标角", value: num(telemetry.target_angle, 1, "°") },
              { key: "vx", label: "水平速度", value: num(telemetry.velocity_x, 1, " 米/秒") },
              { key: "vy", label: "垂直速度", value: num(telemetry.velocity_y, 1, " 米/秒") },
              { key: "atmo", label: "大气层顶", value: num(telemetry.atmosphere_height, 0, " 米") },
              { key: "radius", label: "天体半径", value: num(telemetry.planet_radius, 0, " 米") },
              { key: "mu", label: "引力参数 μ", value: num(telemetry.planet_mu, 0) },
              { key: "held", label: "按住中的按键", value: heldText },
            ]}
          />

          {asText(telemetry.error) ? <Alert tone="danger">{asText(telemetry.error)}</Alert> : null}
          {asText(snapshot.telemetry_summary) ? <Text>{asText(snapshot.telemetry_summary)}</Text> : null}
        </Stack>
      </Card>

      <Card title="轨道">
        {hasOrbit ? (
          <Grid cols={4} gap={10}>
            <StatCard label="远点" value={num(orbit.apoapsis, 1, " 米")} />
            <StatCard label="近点" value={num(orbit.periapsis, 1, " 米")} />
            <StatCard label="离心率" value={num(orbit.eccentricity, 3)} />
            <StatCard label="周期" value={durationText(orbit.period)} />
          </Grid>
        ) : (
          <Stack>
            <Alert tone="info">
              还没入轨：轨道根数要进入太空之后才有。判断入轨看**近点**有没有高过大气层顶，
              不是现实里的 100 公里卡门线。
            </Alert>
            <KeyValue
              items={[
                { key: "apo_t", label: "到远点", value: durationText(telemetry.time_to_apo) },
                { key: "peri_t", label: "到近点", value: durationText(telemetry.time_to_peri) },
                { key: "anomaly", label: "真近点角", value: num(telemetry.true_anomaly, 1, "°") },
              ]}
            />
            {asText(telemetry.orbit_error) ? (
              <Alert tone="warning">{asText(telemetry.orbit_error)}</Alert>
            ) : null}
          </Stack>
        )}
      </Card>

      <Card title="火箭构成">
        <Stack>
          <Text>{asText(snapshot.build_summary) || "还没读到火箭数据。"}</Text>
          <Grid cols={4} gap={10}>
            <StatCard label="场景" value={asText(build.mode) || "—"} />
            <StatCard label="零件数" value={num(build.part_count, 0)} />
            <StatCard label="分级数" value={num(build.stage_count, 0)} />
            <StatCard label="总质量" value={num(build.total_mass, 2, " 吨")} />
          </Grid>
          <DataTable
            data={kindRows}
            columns={[
              { key: "name", label: "零件" },
              { key: "count", label: "数量" },
            ]}
            rowKey="key"
            emptyText="没有读到零件构成（可能不在建造/飞行场景）"
            maxRows={40}
          />
        </Stack>
      </Card>
    </Stack>
  )

  const screenTab = (
    <Stack>
      {pluginMissing ? <Alert tone="warning">插件未启动，先到插件页启动它。</Alert> : null}
      <Card title="游戏画面">
        <Stack>
          <Toolbar>
            <ToolbarGroup>
              <Button
                tone="primary"
                disabled={busyAction !== ""}
                onClick={() => void grabScreen(false)}
              >
                {busyAction === "screen" ? "抓取中…" : "抓取画面"}
              </Button>
              <Button
                tone="default"
                disabled={busyAction !== ""}
                onClick={() => void grabScreen(true)}
              >
                抓取（内联）
              </Button>
            </ToolbarGroup>
          </Toolbar>

          <ImagePreview
            src={screenUrl}
            caption={screenNote ? `预览：${screenNote}` : "预览"}
            emptyText="还没抓取画面"
            alt="Spaceflight Simulator 画面"
          />

          <Text>
            这里是给你自己看的。要让猫娘看画面，直接对她说「你看我造的火箭」「看看我现在飞到哪了」——
            她会调 see_sfs_screen。
          </Text>
          <Tip>
            「抓取画面」优先走宿主的临时图片接口，面板只拿一个本地 URL；接口不可用时自动退回内联
            data URL。截图需要游戏渲染一帧，卡顿时可能等几秒。
          </Tip>
        </Stack>
      </Card>
    </Stack>
  )

  const uiTab = (
    <Stack>
      {pluginMissing ? <Alert tone="warning">插件未启动，先到插件页启动它。</Alert> : null}
      <Alert tone="info">
        这里的点击是**游戏内事件派发**：不会移动你的鼠标，游戏窗口也不用在前台；
        但它会真实改变游戏状态（开始游戏、载入存档等）。游戏切场景很慢，
        点完等几秒再看新列表，别急着判定「没反应」。
      </Alert>

      <Card title="界面按钮">
        <Stack>
          <Toolbar>
            <ToolbarGroup>
              <Button
                tone="primary"
                disabled={busyAction !== ""}
                onClick={() =>
                  void run("sfs_ui", {}, { refresh: false, toast: false }).then((result) => {
                    if (result) {
                      setSnapshot((prev) => ({
                        ...(prev || {}),
                        ui: { count: result.count, elements: (result.elements || []) as UiElement[] },
                      }))
                      toast.success(asText(result.summary) || "已刷新界面列表")
                    }
                  })
                }
              >
                刷新界面列表
              </Button>
            </ToolbarGroup>
            <ToolbarGroup>
              <Text>{`共 ${elements.length} 个可点击元素`}</Text>
            </ToolbarGroup>
          </Toolbar>

          <DataTable
            data={elementRows}
            columns={[
              { key: "index", label: "#" },
              { key: "label", label: "按钮" },
              { key: "x", label: "x", render: (row: any) => num(row.x, 3) },
              { key: "y", label: "y", render: (row: any) => num(row.y, 3) },
            ]}
            rowKey="key"
            emptyText="当前界面没有读到可点击按钮（可能在加载，或游戏没运行）"
            maxRows={80}
            onSelect={(row: any) => void clickElement(row.raw as UiElement)}
          />
          <Tip>点表格里的一行 = 点击那个按钮。分级界面里未选中存档时，Play / Rename / Delete 是灰的，会被自动过滤掉 —— 先点存档卡片。</Tip>
        </Stack>
      </Card>

      <Card title="按坐标 / 序号点击">
        <Stack>
          <Grid cols={3} gap={10}>
            <Field label="元素序号" help="按 list 里的 # 号点击，比坐标可靠">
              <NumberInput value={clickIndex} onChange={(value) => setClickIndex(numberOr(value, 0))} />
            </Field>
            <Field label="x（0-1）" help="左上角 0，右下角 1">
              <NumberInput value={clickX} step={0.01} min={0} max={1} onChange={(value) => setClickX(numberOr(value, 0.5))} />
            </Field>
            <Field label="y（0-1）" help="从上面算起">
              <NumberInput value={clickY} step={0.01} min={0} max={1} onChange={(value) => setClickY(numberOr(value, 0.5))} />
            </Field>
          </Grid>
          <Inline gap={8}>
            <Button tone="warning" disabled={busyAction !== ""} onClick={() => void clickByIndex()}>
              按序号点击
            </Button>
            <Button tone="warning" disabled={busyAction !== ""} onClick={() => void clickByPosition()}>
              按坐标点击
            </Button>
          </Inline>
        </Stack>
      </Card>

      {clickAfter ? (
        <Card title="点击后的界面">
          <TextBlock text={clickAfter} />
        </Card>
      ) : null}
    </Stack>
  )

  const keyGroups: Array<{ group: string; items: KeyDef[] }> = []
  keys.forEach((key) => {
    const group = asText(key.group) || "其它"
    const bucket = keyGroups.find((item) => item.group === group)
    if (bucket) bucket.items.push(key)
    else keyGroups.push({ group, items: [key] })
  })

  const flightTab = (
    <Stack>
      {pluginMissing ? <Alert tone="warning">插件未启动，先到插件页启动它。</Alert> : null}

      <Card title="油门">
        <Stack>
          <Field label={`油门（当前 ${throttleText(telemetry.throttle)}）`} help="0% 熄火，100% 满推力">
            <Slider
              min={0}
              max={100}
              step={1}
              value={numberOr(throttleDraft, 0)}
              onChange={(value: number) => setThrottleDraft(value)}
            />
          </Field>
          <Inline gap={8}>
            <Button tone="primary" disabled={busyAction !== ""} onClick={() => void applyThrottle()}>
              设为 {Math.round(numberOr(throttleDraft, 0))}%
            </Button>
            <Button
              tone="default"
              disabled={busyAction !== ""}
              onClick={() => {
                setThrottleDraft(Math.round(numberOr(telemetry.throttle, 0) * 100))
                toast.info("已同步为游戏里的当前油门。")
              }}
            >
              同步当前值
            </Button>
          </Inline>
          <Tip>改油门走的是游戏内指令，不经过键盘，最可靠。</Tip>
        </Stack>
      </Card>

      <Card title="飞行指令">
        <Stack>
          <Inline gap={8} wrap justify="start">
            {commands.map((command) => (
              <Button
                key={asText(command.id)}
                tone={(asText(command.tone) || "default") as any}
                disabled={busyAction !== ""}
                onClick={() => void sendCommand(asText(command.id))}
              >
                {asText(command.label) || asText(command.id)}
              </Button>
            ))}
          </Inline>
          <KeyValue
            items={commands.map((command) => ({
              key: asText(command.id),
              label: asText(command.label) || asText(command.id),
              value: asText(command.hint) || "",
            }))}
          />
        </Stack>
      </Card>

      <Card title="按键（转向 / RCS / 常用）">
        <Stack>
          <Warning>
            转向与 RCS 平移是**持续推力**：只点一下几乎没效果。
            「按住」之后要读遥测确认，再「松开」；忘了松开会一直朝那个方向加速。
            RCS 平移要先按 R 打开 RCS 才有效。
          </Warning>

          {keyGroups.map((bucket) => (
            <Stack key={bucket.group} gap={6}>
              <Text>{bucket.group}</Text>
              <Inline gap={8} wrap>
                {bucket.items.map((key) => (
                  <ButtonGroup key={asText(key.vk)}>
                    <Button
                      tone="default"
                      disabled={busyAction !== ""}
                      onClick={() => void tapKey(key)}
                    >
                      {`${asText(key.name)} ${asText(key.label)}`}
                    </Button>
                    <Button
                      tone="info"
                      disabled={busyAction !== ""}
                      onClick={() => void holdKey(key)}
                    >
                      {`按住 ${asText(key.name)}`}
                    </Button>
                  </ButtonGroup>
                ))}
              </Inline>
            </Stack>
          ))}

          <Inline gap={8}>
            <Button tone="danger" disabled={busyAction !== ""} onClick={() => void releaseKey()}>
              全部松开（急停）
            </Button>
          </Inline>
          <KeyValue items={[{ key: "held", label: "按住中的按键", value: heldText }]} />
        </Stack>
      </Card>

      <Card title="视角">
        <Stack>
          <Inline gap={8} wrap>
            <Button tone="default" disabled={busyAction !== ""} onClick={() => void moveCamera(-20)}>
              拉近
            </Button>
            <Button tone="default" disabled={busyAction !== ""} onClick={() => void moveCamera(30)}>
              拉远
            </Button>
            <Button tone="default" disabled={busyAction !== ""} onClick={() => void rotateCamera(-15)}>
              左转 15°
            </Button>
            <Button tone="default" disabled={busyAction !== ""} onClick={() => void rotateCamera(15)}>
              右转 15°
            </Button>
          </Inline>
          <Tip>想看清洗某个零件、或画面里东西太小/太大时用。视角调整是相对的，多点几下也行。</Tip>
        </Stack>
      </Card>

      <Card title="高级：Agent 独占模式">
        <Stack>
          <Inline align="center" justify="space-between">
            <Text>开启后游戏忽略你的鼠标与键盘，只接受猫娘的操作</Text>
            <Switch
              checked={exclusiveOn}
              label={exclusiveOn ? "独占中" : "普通"}
              onChange={(value: boolean) => void toggleExclusive(value)}
            />
          </Inline>
          <Warning>
            独占模式下你自己点不动游戏（应急处理：在游戏里按 F10，或回到这里关掉）。
            只有在完全交给猫娘操作时才开。
          </Warning>
          <Tip>
            模组的 /state 不返回独占状态，所以这个开关**只反映面板自己最近一次操作**：
            如果你在游戏里按 F10 解除、或重开了面板，它会显示为关闭，请以游戏内的提示为准。
          </Tip>
        </Stack>
      </Card>
    </Stack>
  )

  const buildTab = (
    <Stack>
      {pluginMissing ? <Alert tone="warning">插件未启动，先到插件页启动它。</Alert> : null}

      <Card title="载入蓝图（推荐）">
        <Stack>
          <Alert tone="info">
            蓝图里每个零件的尺寸、纹理和分级都由游戏自己解析，所以坐标一定正确、一定能飞。
            想造火箭优先用这条，而不是手拼零件。
          </Alert>
          <Toolbar>
            <ToolbarGroup>
              <Button tone="info" disabled={busyAction !== ""} onClick={() => void reloadBlueprints()}>
                读取蓝图列表
              </Button>
              <Button
                tone="warning"
                disabled={busyAction !== "" || !selectedBlueprint}
                onClick={() => void loadSelectedBlueprint()}
              >
                {selectedBlueprint ? `载入「${selectedBlueprint}」` : "载入选中蓝图"}
              </Button>
            </ToolbarGroup>
          </Toolbar>
          <DataTable
            data={blueprintRows}
            columns={[{ key: "name", label: "蓝图" }]}
            rowKey="key"
            selectedKey={selectedBlueprint}
            emptyText="没有读到蓝图（先在建造界面右上角 Save 存一个，或游戏没运行）"
            maxRows={60}
            onSelect={(row: any) => setSelectedBlueprint(asText(row.name))}
          />
          <Tip>点表格里的一行选中它（只是选中，不会动游戏），再用上面的按钮载入。载入会替换建造台里当前的火箭。</Tip>
        </Stack>
      </Card>

      <Card title="单个零件（手拼，不推荐）">
        <Stack>
          <Warning>
            手拼零件必须贴合：间距由零件高度决定，算错就装不上；拼错了发射时上面的零件会掉下来把火箭砸爆。
            除非只想改某几个零件，否则优先载入蓝图。
          </Warning>
          <Inline gap={8}>
            <Button tone="info" disabled={busyAction !== ""} onClick={() => void loadParts()}>
              读取零件目录
            </Button>
            <Input
              value={partFilter}
              placeholder="按名字筛选，例如 tank / engine"
              onChange={(value: string) => setPartFilter(value)}
            />
          </Inline>
          <DataTable
            data={partRows}
            columns={[{ key: "name", label: "零件内部名" }]}
            rowKey="key"
            selectedKey={selectedPart}
            emptyText="还没读到零件目录，点上面的「读取零件目录」"
            maxRows={60}
            onSelect={(row: any) => setSelectedPart(asText(row.name))}
          />
          <Divider />
          <Grid cols={3} gap={10}>
            <Field label="选中零件">
              <Text>{selectedPart || "（在上面的表里点一行）"}</Text>
            </Field>
            <Field label="x（0 为画面中心）">
              <NumberInput value={placedX} onChange={(value) => setPlacedX(numberOr(value, 0))} />
            </Field>
            <Field label="y（0 为画面中心，向上为正）">
              <NumberInput value={placedY} onChange={(value) => setPlacedY(numberOr(value, 8))} />
            </Field>
          </Grid>
          <Inline>
            <Button
              tone="warning"
              disabled={busyAction !== "" || !selectedPart}
              onClick={() => void placeSelectedPart()}
            >
              放置零件
            </Button>
          </Inline>
        </Stack>
      </Card>

      <Card title="当前火箭">
        <Stack>
          <Text>{asText(snapshot.build_summary) || "还没读到火箭数据。"}</Text>
          <Grid cols={3} gap={10}>
            <StatCard label="零件数" value={num(build.part_count, 0)} />
            <StatCard label="分级数" value={num(build.stage_count, 0)} />
            <StatCard label="总质量" value={num(build.total_mass, 2, " 吨")} />
          </Grid>
          <DataTable
            data={kindRows}
            columns={[
              { key: "name", label: "零件" },
              { key: "count", label: "数量" },
            ]}
            rowKey="key"
            emptyText="没有读到零件构成"
            maxRows={40}
          />
        </Stack>
      </Card>
    </Stack>
  )

  const vlmConfigured = !!effective.vlm_api_key_configured

  const settingsTab = (
    <Stack>
      {pluginMissing ? <Alert tone="warning">插件未启动，先到插件页启动它。</Alert> : null}

      <Card title="桥接服务">
        <Stack>
          <Field
            label="桥接地址"
            required
            help="SFS-Agent 模组监听的地址，默认 http://127.0.0.1:21578。改端口要同时改模组侧的 sfs-agent.ini，只改一处两边就连不上。"
          >
            <Input
              value={asText(fieldValue("bridge_url", effective.bridge_url))}
              placeholder="http://127.0.0.1:21578"
              onChange={(value: string) => setField("bridge_url", value)}
            />
          </Field>
          <Grid cols={3} gap={10}>
            <Field label="单次请求超时（秒）" help="3-60；机械硬盘读存档慢就调大">
              <NumberInput
                value={numberOr(fieldValue("timeout_seconds", 12), 12)}
                min={3}
                max={60}
                onChange={(value) => setField("timeout_seconds", value)}
              />
            </Field>
            <Field label="点击后等待（秒）" help="0.5-30；切场景慢就调大，太短会读到旧界面">
              <NumberInput
                value={numberOr(fieldValue("post_click_wait_seconds", 3), 3)}
                min={0.5}
                max={30}
                step={0.5}
                onChange={(value) => setField("post_click_wait_seconds", value)}
              />
            </Field>
            <Field label="截图超时（秒）" help="5-60；截图要等游戏渲染一帧">
              <NumberInput
                value={numberOr(fieldValue("screenshot_timeout_seconds", 20), 20)}
                min={5}
                max={60}
                onChange={(value) => setField("screenshot_timeout_seconds", value)}
              />
            </Field>
          </Grid>
        </Stack>
      </Card>

      <Card title="画面与视觉识别">
        <Stack>
          <Grid cols={2} gap={10}>
            <Field label="送识别前最大宽度（像素）" help="320-2048；调小更省流量，调大细节更多">
              <NumberInput
                value={numberOr(fieldValue("screenshot_max_width", 1024), 1024)}
                min={320}
                max={2048}
                step={64}
                onChange={(value) => setField("screenshot_max_width", value)}
              />
            </Field>
            <Field label="JPEG 质量" help="30-95；60-85 通常够用">
              <NumberInput
                value={numberOr(fieldValue("screenshot_jpeg_quality", 80), 80)}
                min={30}
                max={95}
                onChange={(value) => setField("screenshot_jpeg_quality", value)}
              />
            </Field>
          </Grid>
          <Field
            label="把画面交给 N.E.K.O. 的视觉模型识别"
            help="关掉后 see_sfs_screen 会如实返回「图片被跳过」，其它工具不受影响"
          >
            <Switch
              checked={!!fieldValue("vision_enabled", true)}
              label={fieldValue("vision_enabled", true) ? "已开启" : "已关闭"}
              onChange={(value: boolean) => setField("vision_enabled", value)}
            />
          </Field>
          <Field label="视觉提示词" help="告诉模型这张图是什么、要看什么">
            <Textarea
              value={asText(fieldValue("vision_prompt", effective.vision_prompt))}
              onChange={(value: string) => setField("vision_prompt", value)}
            />
          </Field>
        </Stack>
      </Card>

      <Card title="可选：自带视觉模型">
        <Stack>
          <Tip>
            默认留空，画面由 N.E.K.O. 自己的「视觉聊天模型」识别（设置 → API → 视觉聊天模型）。
            只有想让本插件用另一个 OpenAI 兼容模型单独看图时，才在这里填。
          </Tip>
          <Field label="启用自带视觉模型">
            <Switch
              checked={!!fieldValue("vlm_enabled", false)}
              label={fieldValue("vlm_enabled", false) ? "已启用" : "未启用"}
              onChange={(value: boolean) => setField("vlm_enabled", value)}
            />
          </Field>
          <Field label="Base URL" help="任意 OpenAI 兼容接口，例如 https://dashscope.aliyuncs.com/compatible-mode/v1">
            <Input
              value={asText(fieldValue("vlm_base_url", effective.vlm_base_url))}
              placeholder="https://…/v1"
              onChange={(value: string) => setField("vlm_base_url", value)}
            />
          </Field>
          <Grid cols={2} gap={10}>
            <Field label="模型名" help="例如 qwen-vl-max、glm-4v、gpt-4o-mini">
              <Input
                value={asText(fieldValue("vlm_model", effective.vlm_model))}
                placeholder="qwen-vl-max"
                onChange={(value: string) => setField("vlm_model", value)}
              />
            </Field>
            <Field label="描述最大长度" help="64-4096">
              <NumberInput
                value={numberOr(fieldValue("vlm_max_tokens", 600), 600)}
                min={64}
                max={4096}
                step={64}
                onChange={(value) => setField("vlm_max_tokens", value)}
              />
            </Field>
          </Grid>
          <Field
            label="API Key"
            help={
              vlmConfigured
                ? "已配置。留空表示保持原值不变（密钥不会回显）。"
                : "还没配置。密钥只写不读，面板不会显示它。"
            }
          >
            <PasswordInput
              value={asText((draft || {}).vlm_api_key)}
              placeholder={vlmConfigured ? "输入新值以替换" : "粘贴 API Key"}
              onChange={(value: string) => setField("vlm_api_key", value)}
            />
          </Field>
        </Stack>
      </Card>

      <Card title="保存">
        <Stack>
          <Toolbar>
            <ToolbarGroup>
              <Button tone="success" disabled={busyAction !== ""} onClick={() => void saveSettings()}>
                {busyAction === "sfs_ui_save_settings" ? "保存中…" : "保存设置"}
              </Button>
              <Button tone="default" disabled={busyAction !== ""} onClick={resetDraft}>
                恢复为当前生效值
              </Button>
              <Button tone="info" disabled={probing} onClick={() => void probeConnection()}>
                测试连接
              </Button>
            </ToolbarGroup>
          </Toolbar>
          <Alert tone="info">
            保存会写回插件配置并立即生效（不用重启插件）。不过改端口时，模组侧
            sfs-agent.ini 也要一起改，否则两边对不上。
          </Alert>
        </Stack>
      </Card>
    </Stack>
  )

  const body =
    tab === "screen"
      ? screenTab
      : tab === "screenui"
        ? uiTab
        : tab === "flight"
          ? flightTab
          : tab === "build"
            ? buildTab
            : tab === "settings"
              ? settingsTab
              : overviewTab

  return (
    <Page title="造火箭控制台" subtitle="和猫娘一起造火箭（SFS Bridge）">
      <Stack>
        <Toolbar>
          <ToolbarGroup>
            <ButtonGroup>
              {TABS.map((item) => (
                <Button
                  key={item.id}
                  tone={tab === item.id ? "primary" : "default"}
                  onClick={() => {
                    setTab(item.id)
                  }}
                >
                  {item.label}
                </Button>
              ))}
            </ButtonGroup>
          </ToolbarGroup>
          <ToolbarGroup>
            {renderSceneBadge()}
            {isNum(snapshot.latency_ms) && snapshot.connected ? (
              <Text>{`${snapshot.latency_ms} 毫秒`}</Text>
            ) : null}
          </ToolbarGroup>
        </Toolbar>

        {busyAction && busyAction !== "screen" ? (
          <Text>{`正在执行：${busyAction} …`}</Text>
        ) : null}

        {body}
      </Stack>
    </Page>
  )
}