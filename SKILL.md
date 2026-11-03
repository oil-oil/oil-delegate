---
name: oil-delegate
description: "把边界清楚的单次任务委托给 Claude Code（cc）、WorkBuddy（wb）、Antigravity（agy）、Codex、OpenCode、DeepSeek Harness（dsh）、Grok Build（grok）或 Kimi Code（kimi）CLI，返回结构化结果并续接原会话；处理首次静默扫描、启用执行器与默认选择。用户明确要求 oil-delegate、统一调用或用上述全称/别名继续这些外部 CLI Agent 时使用；不用于普通直接问答、显式 /codex、需要主线看板与多话题编排的请求。"
license: MIT
metadata:
  version: "0.2.0"
---

# oil-delegate

`SKILL_DIR` 指当前 Skill 的绝对目录；`<python>` 指 Python 3.10+（macOS/Linux 通常为 `python3`，Windows 通常为 `py -3`）；`DELEGATE` 是 `<python> "$SKILL_DIR"/scripts/delegate.py` 的简写。

## 核心规则

- 用户只说自然语言，由当前 Agent 执行包装器，不要求用户另开终端。
- 每次只委托一个边界清楚的任务；用户点名的 runner 优先，未点名时使用默认值，不自动换 runner。
- 沿用各 CLI 的模型、provider 与登录，不传递或保存密钥，不把密钥写入提示词。
- 调用方负责范围、权限和最终验收；外部 Agent 的自报结果不能替代工作区检查与真实测试。

## 先选择模式

| 模式 | 适用情况 | 路径 | 默认停止点 |
| --- | --- | --- | --- |
| 首次 Onboarding | `run` 返回 `NEEDS_ONBOARDING`，或用户要求重配 | 只读扫描 → 用户确认启用池与默认项 → 保存 | 报告配置后等待任务 |
| 新任务 | 把一次分析、评审或修改交给外部 Agent | 整理委托契约 → `run` → 读取结果并验收 | 汇报结论与验证证据 |
| 续接任务 | 对同一 CLI 的上一轮结果追问或返工 | 核对 runner 与 session ID → `run --session` | 汇报本轮变化 |
| 配置与诊断 | 查看环境、预览权限或处理失败 | `onboard --scan`、`config`、`run --dry-run` | 给出明确下一步 |

## 委托契约

提示词应简洁覆盖：

1. **目标与边界**：要查明或修改什么，明确不应改动的范围。
2. **完成标准**：预期行为、边界情况、最终必须交付的结果。
3. **入口线索**：用 `--file` 提供 1–4 个已确认文件，其余让外部 Agent 自行探索。
4. **验证**：从仓库配置确认真实测试、检查或构建命令，不写空泛的“运行测试”。
5. **最终报告**：改动与原因、涉及文件、验证命令和结果、未完成事项。

缺少仓库事实先查找，不猜测。诊断与评审要求引用文件和行号，并区分事实与推断。

## 工作流

### 1. Onboarding 与 runner 选择

首次使用或收到 `NEEDS_ONBOARDING` 时：

1. 首次 `run` 已自动完成静默扫描并返回 detected/installed runners，无需用户执行任何命令。需要刷新时运行 `DELEGATE onboard --scan`。扫描不写配置、不读取凭据、不调用模型、不安装、不登录。
2. 展示已安装 runner、版本、来源、能力和 aliases，说明“已安装不等于已认证”。首次配置前读 [Runner 适配参考](references/runners.md)，讲清 read/write 权限差异、外部服务费用与任务文本会交给对应 provider。
3. 询问启用哪些 runner，以及默认使用一个还是每次询问。得到回答后才保存：
   ```text
   DELEGATE onboard --enable <claude,workbuddy,antigravity,codex,opencode,dsh,grok,kimi> --default-runner <runner|ask>
   ```

名称不区分大小写，可使用 `cc`（claude）、`wb`（workbuddy）、`agy`（antigravity）、`dsh`、`grok-build`、`kimi-code` 等别名；启用、默认项、委托和二进制路径配置都走同一套解析，保存时统一转为正式 Runner 名。

未指定 runner 且没有默认值时，`run` 返回 `NEEDS_RUNNER_CHOICE`。询问本次选择；只有用户同意沉淀偏好时，才运行 `DELEGATE config --set-default-runner <runner>`。

查看配置或指定非标准二进制路径：

```text
DELEGATE config
DELEGATE config --set-bin <runner>=<可执行文件绝对路径>
```

### 2. 派发新任务

- 默认省略 `--mode`，使用 `write` 和各 CLI 的最高权限；分析、探索和评审也不自动降级。
- 只有用户明确要求只读、plan 或限制权限时才使用 `--mode read`。Kimi 的非交互模式自带 auto 权限，不能保证只读，因此拒绝 read，不伪装支持。
- 默认使用当前工作目录，跨项目时显式指定 `--workspace`。
- 调用：

```text
DELEGATE run --runner <名称> \
  --workspace "<项目绝对路径>" \
  --file "<相对入口文件>" \
  --prompt "<目标、边界、完成标准与真实验证命令>"
```

已配置默认 runner 时省略 `--runner`。启动前需要核对参数时追加 `--dry-run`；预览不会启动外部 Agent。

### 3. 续接原会话

读取上一轮输出的 `session_id`，只对同一 runner 续接：

```text
DELEGATE run --runner <原 runner> \
  --workspace "<原工作区>" \
  --mode <read|write> \
  --session <session_id> \
  --prompt "<本轮新发现、追问或修正要求>"
```

续接只发送本轮新信息，不重复整个任务。跨 runner 不能复用 session ID；需要第二意见时开启新任务并补充必要背景。DSH 续接必须使用原 cwd，其 ownership、preset 与 cwd 校验失败时如实报告，不绕过。

### 4. 验收与失败恢复

1. 读取 `output_path` 的摘要；需要退出码、session ID 或错误结构时读取 `result_path`，原始协议诊断才读取 `events_path`。
2. 检查工作区改动是否越界，检查测试是否被删除、跳过或弱化，再重新运行真实验证命令。
3. 需要返工时续接原会话，只发送新发现和修正要求。
4. 非零退出、协议错误或超时后不盲目重试。先根据错误修正配置、登录状态、任务范围或权限，再决定新任务或续接。
5. 缺少 CLI 或登录时停止并给出对应官方入口；安装与登录必须另行获得用户授权。

## 输出与停止条件

成功后脚本输出 `status`、`session_id`、`runner`、`output_path`、`result_path`、`events_path` 和 `elapsed`。每次运行有独立目录，包含 prompt、精简 Markdown、结构化 result、原始 stdout/stderr 与事件日志。result 里的 `resume_command` 是用户在终端直接续接该会话的命令。

给其他程序调用时追加 `--json`：stdout 只输出一个 JSON 对象，带 `contract_version`（当前为 1）。成功、失败、`NEEDS_ONBOARDING`、`NEEDS_RUNNER_CHOICE`、`DRY_RUN` 和参数错误（`status: ERROR`）都用这个格式，退出码与普通模式一致。

最终回复只保留用户需要的结论、实际改动、验证证据、未完成事项和必要 session ID。完成当前委托与验收后停止，不扩展成多话题管理，也不自动生成看板。

## 资源导航

- [references/runners.md](references/runners.md)：只在首次配置、显式只读、排查特定 CLI 或维护适配器时阅读相关 Runner 小节；日常最高权限委托不需要读取。
- [scripts/delegate.py](scripts/delegate.py)：扫描、配置、调用、解析与运行产物的确定性工具；使用 Skill 时不需要阅读源码。
- [tests/test_delegate.py](tests/test_delegate.py)：八种 Runner 的回归测试；只在开发或整改时读取相关部分。
