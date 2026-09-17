# 和猫娘一起造火箭（SFS Bridge）

<p align="center">
  由 <b>星河拓航Studio</b>（Galaxy Exploration Studio）开发与维护
</p>

让猫娘接入 **Spaceflight Simulator（航天模拟器）**：读取飞行遥测、**查看游戏画面**、
**操作游戏界面**（主菜单、载入存档、建造菜单、设置面板），
**把整枚蓝图直接加载进建造台**，并评审你的火箭设计。

> 📖 **第一次用请先看 [使用教程](使用教程.md)** —— 那里有「你想做什么 → 可以这样说」
> 的对照表，以及常见问题排查。本文件是技术文档。

## 它是怎么工作的

```
猫娘（N.E.K.O. 的 LLM）
   │  @llm_tool：see_sfs_screen / list_sfs_ui / click_sfs_ui ...
   ▼
sfs_bridge 插件（本插件）
   │  HTTP  127.0.0.1:21578（仅本机回环）
   ▼
SFS-Agent 模组（游戏内 C# DLL）
   │  反射 + Harmony
   ▼
Spaceflight Simulator
```

**必须先在游戏里安装配套模组**，否则插件连不上游戏。

## 安装

### 1. 安装游戏模组

从 **SFS-Agent 模组仓库**的 Releases 页面下载 `SFS-Agent.dll`：

> <https://github.com/LShangPiao/SFS-Agent/releases>

按 SFS 的「一目录一模组」规范放入：

```
<Steam>\steamapps\common\Spaceflight Simulator\Spaceflight Simulator Game\Mods\SFS-Agent\SFS-Agent.dll
```

> ⚠️ 必须放在 `Mods\SFS-Agent\` 这个**子目录**里，并且文件名与目录名一致。
> 不要直接平铺到 `Mods\` 根目录，也不要同时保留其他旧模组目录 ——
> 两个模组会抢同一个端口，导致连上的不是你期望的那个。

**然后重启游戏** —— 模组只在游戏启动时加载。

模组源码与构建脚本在同一个仓库，可以自行编译（需要 Windows 自带的 `csc.exe`
与已安装的游戏）：

```powershell
cd sfs-agent
pwsh -File build.ps1
```

编译脚本会自动把产物部署到游戏的 `Mods\SFS-Agent\`。

### 2. 启动本插件

在 N.E.K.O. 的插件页面刷新列表，启动「和猫娘一起造火箭(SFS)」。

就这两步。画面识别走 N.E.K.O. 自己的多模态模型，无需额外配置。

## 配置项

`[sfs_bridge]` 段：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bridge_url` | `http://127.0.0.1:21578` | 模组监听地址 |
| `timeout_seconds` | `12` | 单次请求超时 |
| `screenshot_timeout_seconds` | `20` | 截图超时（需等游戏渲染一帧） |
| `screenshot_max_width` | `1024` | 送识别前缩放到的最大宽度 |
| `screenshot_jpeg_quality` | `80` | JPEG 质量 |
| `vision_enabled` | `true` | 是否把画面交给视觉模型 |
| `vision_prompt` | 见模板 | 默认视觉提示词 |

## 入口与工具

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `sfs_status` | 入口 | 读取飞行遥测 |
| `sfs_command` | 入口 | 发送飞行控制指令 |
| `sfs_screenshot` | 入口 | 抓取画面并报告尺寸 |
| `sfs_build` | 入口 | 读取火箭零件构成 |
| `sfs_ui` | 入口 | 列出界面上可点击的按钮 |
| `sfs_click` | 入口 | 点击界面（坐标或索引），等 3 秒并回读界面 |
| `sfs_key` | 入口 | 发送按键 |
| `sfs_parts` | 入口 | 列出可用零件名 |
| `sfs_place` | 入口 | 把零件放到建造网格坐标 |
| `see_sfs_screen` | LLM 工具 | **看画面**：「你看我造的火箭」 |
| `get_sfs_status` | LLM 工具 | 读遥测：「我现在飞多高」 |
| `control_sfs` | LLM 工具 | 飞行控制：「点火」「油门 80%」「打开 RCS」「分离一级」 |
| `get_rocket_design` | LLM 工具 | 读设计：「我这火箭用了什么零件」 |
| `review_rocket_design` | LLM 工具 | **评审设计**：画面 + 遥测 + 零件一起分析 |
| `list_sfs_ui` | LLM 工具 | **列出界面按钮** |
| `click_sfs_ui` | LLM 工具 | **点击界面**：「开始游戏」「打开设置」，自动等 3 秒回读新界面 |
| `press_sfs_key` | LLM 工具 | 发送按键：「按 Esc 返回」「按住 Q 左转」 |
| `list_sfs_parts` | LLM 工具 | **列出可用零件** |
| `place_sfs_part` | LLM 工具 | **放置单个零件**：「在火箭下面加个引擎」 |
| `list_sfs_blueprints` | LLM 工具 | **列出蓝图**：「我存过哪些火箭」 |
| `load_sfs_blueprint` | LLM 工具 | **加载整枚蓝图**：「载入我那个 XX 火箭」 |

支持的飞行指令：`set_throttle`（0-1）、`throttle_on`、`throttle_off`、
`stage`（空格：执行下一级）、`staging_program`（回车）、
`rcs_on`、`rcs_off`、`rcs_toggle`。

常用按键（`press_sfs_key` 的 `vk`）：空格=32 回车=13 Esc=27
Q=81 E=69 W=87 A=65 S=83 D=68 R=82 Shift=16 Ctrl=17。

## SFS 默认操作方法

| 操作 | 按键 |
| --- | --- |
| 向左 / 向右转向 | Q / E |
| 平移与俯仰（需先开 RCS） | W / A / S / D |
| 油门加大 / 减小 | Shift / Ctrl |
| RCS 开关 | R |
| 点火 / 执行下一级 | 空格 |
| 分级控制程序 | 回车 |

## UI 操作是怎么做的

游戏启动后停在**主菜单**，那里还没有飞行器，所以界面操作是一条独立通路。

模组用反射枚举当前界面上所有可点击的 `SFS.UI.Button`（透过 `buttonEnabled`
过滤掉置灰的按钮），读出每个按钮的**文字标签**和**屏幕位置**，交给猫娘。
猫娘只需要按索引点击，例如：

```
#4 Play  → (0.500, 0.481)
```

### 点击是**游戏内输入派发**，不动你的鼠标

模组调用 SFS 自己的 `SFS.Input.InputManager`（`CheckMouseOverState` +
`InputStart` / `TouchEnd`）来派发点击，**不是**移动系统光标去点。因此：

- ✅ 点击期间鼠标指针**不会移动**，你可以同时用电脑做别的事
- ✅ 不需要把游戏窗口切到前台
- ✅ 实测点击主菜单 → 载入存档 → 建造场景，光标坐标全程不变
- ✅ 命中判定由游戏自己做，比按坐标硬点准
- ⚠️ 但点击**会真实改变游戏状态**（开始游戏、载入存档等），猫娘操作前请确认

按键同理：`press_sfs_key` 走 Harmony 拦截 `UnityEngine.Input`，
**也不会抢焦点**，游戏不必在前台。

> 实现注记：早期版本直接 `Invoke` 按钮的 `clickEvent`，但按钮把逻辑接在
> `onClick`（`OptionalDelegate`）上时会**返回成功却毫无效果**（实测 Esc 退出
> 确认框的 Cancel 就是这种情况）。现已统一走 `InputManager`。

### 点击后会自动等 3 秒并回读界面

游戏切场景、加载存档**很慢**，立刻回读往往还是旧界面，
模型就会误判成「点了没反应」。所以 `click_sfs_ui` 内部会：

1. 派发点击
2. **等约 3 秒**（`_POST_CLICK_WAIT`）
3. 重新读取界面，把**点击后的新元素清单**一并放进返回值

返回值里的 `after` 字段就是点击后的界面，直接看它就行，
不需要自己再调一次 `list_sfs_ui`。

### 推荐流程

1. 猫娘调 `list_sfs_ui` 拿到可点击元素清单（或 `see_sfs_screen` 看画面）
2. 调 `click_sfs_ui` 按 `index` 点击（比坐标更准），返回值里已带新界面
3. 若新界面和预期不符，**再等一等或重新 list_sfs_ui 确认** ——
   连续两三次都一样才能判定操作无效

界面是**分级**的：点「Play」进入存档列表后，「Play / Rename / Delete」这些按钮
在未选中存档时是置灰的，会被自动过滤掉 —— 先点存档卡片，它们才会出现。

## 造火箭：优先加载蓝图

建造界面里零件必须从左侧菜单**拖**到火箭上，纯点击放不上去。
插件提供两条通路，**优先用第一条**：

| 工具 | 作用 |
| --- | --- |
| `list_sfs_blueprints` | 列出玩家存档里的**火箭蓝图** |
| `load_sfs_blueprint` | **加载整枚蓝图** —— 最可靠，见下 |
| `list_sfs_parts` | 列出可用零件名 |
| `place_sfs_part` | 放**单个**零件到网格坐标（`name`, `x`, `y`, `stack`） |

### 为什么优先加载蓝图

蓝图里每个零件都带 `N`（尺寸与缩放：`width_original`/`width_a`/`width_b`/`height`）、
`T`（纹理）和**分级归属**。模组走游戏自己的解析路径生成整枚火箭，
所以坐标、尺寸、分级全部正确 —— 实测加载 141 零件的火箭，
质量 712.4t、推力 356t，数据与蓝图一致。

而**手拼单个零件**时，摆放位置必须符合"贴合点"规律（间距由零件高度决定），
不同零件的高度变量名还不统一。拼不对的后果很直观：**发射时上面的零件掉下来把火箭砸爆**。
所以除非用户明确要改某几个零件，否则都用 `load_sfs_blueprint`。

### 单个零件

`place_sfs_part(name="Fuel Tank", x=0, y=8, stack="top")`

- `x`/`y` 是建造网格坐标，`0` 是画面中心，向右 / 向上为正
- `stack` 可选 `top` / `bottom`，会尝试按已有零件的贴合点自动算位置

> 零件名用**内部名**（界面 "Valiant Engine" = 内部 `Engine Valiant`），
> 先 `list_sfs_parts` 拿到的就是内部名。
> 名字会与模组读到的目录比对，**对不上的名字一律拒绝**，
> 不会把未经验证的数据交给游戏内部。

## 关于「不会瞎编」

航天模拟器里的数据如果读不到，插件**不会**让模型凭空回答：
- 连不上游戏 → 明确返回「游戏可能没有运行，请如实告诉用户，不要编造飞行数据」
- 抓不到画面 → 明确返回「画面不可用，不要猜测画面内容」
- 画面识别不可用 → 明确返回「图片被跳过」，不会编造画面内容

## 关于星河拓航Studio

本插件由 **星河拓航Studio**（Galaxy Exploration Studio）开发与维护。

星河拓航Studio 是一个由来自五湖四海的航天爱好者组成的非正式线上航天科普组织，
成员多为在校学生。我们希望通过有趣、可靠的方式，让更多人了解真实的航天。

- 官方网站：<https://xhth.top/>
- B 站主页：<https://space.bilibili.com/3546949529635067>
- 联系邮箱：<contact@xhth.top>

欢迎航天爱好者加入交流，也欢迎反馈插件的问题与建议。

## 说明

- 桥接服务只监听 `127.0.0.1`，不对局域网或公网开放。
- 模组只做只读遥测采集与少量指令，不会修改存档文件。
- Spaceflight Simulator 为 Team Curiosity 开发的商业游戏，本项目与官方无关，
  分发的是自制的第三方模组。

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE) 许可。

Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
