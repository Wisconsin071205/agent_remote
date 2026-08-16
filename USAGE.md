# VASPilot 使用说明

胡伟团队专用智能体：面向真实 HPC 环境的 VASP 计算编排工具。
架构：Windows 本机 → Vlab 中转网关 → 计算服务器（cl9 为 PBS、cl12 为 Slurm，自动识别）。

## 1. 环境准备

- **Python 3.12**（推荐；3.14 也可运行但科学包用 3.12）
- **Vlab PEM 密钥**：从 Vlab 平台「SSH 密钥管理」下载，默认放在 `~/.ssh/vlab-identity.pem`
- **DeepSeek API Key**（可选，用于智能体对话）

## 2. 启动

双击 `启动 VASPilot.cmd`，或：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-ui.ps1
```

- 浏览器自动打开 http://127.0.0.1:8765/
- **单实例**：重复启动不会起第二个服务，只会再开一个新网页窗口
- 关闭浏览器不影响服务；要停止服务，关闭启动时那个命令行窗口

## 3. 首次配置（设置 → 齿轮图标）

| 页面 | 填写内容 |
|---|---|
| 连接设置 | Vlab PEM 私钥路径（如 `C:\Users\你\.ssh\vlab-identity.pem`），**保存后自动持久化** |
| 模型服务 | 服务商名称/API 地址/模型名/API Key（Key 以 Windows 凭据加密保存，绝不明文落盘） |
| 外观 | 主题（深色/浅色/跟随系统）、壁纸（静态图片或动态视频） |

## 4. 连接计算服务器

1. 左侧「服务器」面板点 **＋** 添加服务器：
   - 名称（如 cl9）、目标地址（user@host）、SSH 端口
   - 远端根路径（工具只能在此边界内操作，留空 = 主目录）
   - 连接保持时长（如 8h）
   - **任务调度器**：自动检测 / Slurm / PBS（不确定就选自动检测）
2. 点服务器行选中，点「**连接**」→ 弹出 SSH 窗口 → 输入**密码 + 六位验证码**（只在这时输入，任何地方都不保存）
3. 状态点变绿 = 已连接。断开点「断开」。

每台服务器的调度器可随时查询：
```powershell
scripts\vasp-agent.ps1 diagnostic -Diagnostic scheduler -ServerName cl9
```

## 5. 与智能体对话

主界面直接提问，例如：

- 「检查我的任务队列，并解释每个任务的状态」
- 「诊断 /public/home/user/calc 为什么没收敛」
- 「检查 cl9 的队列，再检查 cl12 的最近计算」（模型会自己用 switch_active_server 切换）
- 「画一下 DOSCAR 的态密度图」（模型提议 python3/gnuplot 命令 → 你批准 → 服务器执行 → 下载图片）

**消息流呈现**（类似 DeepSeek Harness）：
- 思考过程：可折叠的「思考过程」块（多轮思考分段显示）
- 工具调用：⚙ 工具名 + 参数（如 `⚙ submit · directory=… script=run.slurm …`）
- 工具结果：▸ 可点开的完整结果块

**审批机制**：所有写操作（提交/取消/删除/上传/分析命令等）都会弹确认框，逐项批准；可勾选「全程免确认」（仅当前会话）。

## 6. 人工终端（网页版）

服务器行悬停出现 **`>_`** 按钮 → 点击打开独立终端窗口：
- 自动登录 Vlab（PEM 免密）→ 自动跳转到该服务器 → 输入密码+验证码
- 每开一个窗口 = 一个独立会话，与主界面互不影响，可同时开多个
- 关闭窗口即断开。此终端**只有你本人能操作**，模型看不到、碰不到

## 7. 项目（服务器/本机两种模式）

- **本机模式**（默认）：项目只在本机分组对话历史，不碰任何文件
- **服务器模式**：新建项目时勾选「改为在服务器上创建项目目录」→ 选服务器 + 填远端路径 → 创建时自动 `mkdir -p`，项目绑定该远端目录

## 8. 命令行工具（可脱离网页使用）

### 8.1 网关操作（vasp-agent.ps1）

```powershell
$env:VLAB_IDENTITY_FILE = "C:\Users\你\.ssh\vlab-identity.pem"
scripts\vasp-agent.ps1 servers                                   # 服务器目录与状态
scripts\vasp-agent.ps1 connect -ServerName cl9                   # 交互输密码+OTP
scripts\vasp-agent.ps1 jobs -ServerName cl9                      # 队列（自动走 PBS/Slurm）
scripts\vasp-agent.ps1 recent -ServerName cl9                    # 最近计算
scripts\vasp-agent.ps1 submit -RemotePath 计算目录 -JobScript run.pbs
scripts\vasp-agent.ps1 cancel -JobId 12345 -ConfirmJobId 12345
scripts\vasp-agent.ps1 vasp-inspect -RemotePath 计算目录          # 输入/结果检查 JSON
scripts\vasp-agent.ps1 vasp-validate -RemotePath 计算目录         # 提交前预检
scripts\vasp-agent.ps1 vasp-progress -RemotePath 计算目录         # 收敛进度
scripts\vasp-agent.ps1 read -RemotePath 计算目录/OUTCAR
scripts\vasp-agent.ps1 tail -RemotePath 计算目录/OSZICAR -Lines 200
scripts\vasp-agent.ps1 run -RemotePath 计算目录 -Command "python3 plot_dos.py"
scripts\vasp-agent.ps1 diagnostic -Diagnostic scheduler -ServerName cl9
```

`run` 只允许白名单前缀：`python3 python gnuplot bash sh awk bc cat grep tail head wc sort uniq paste module`，300 秒超时、单条命令最长 2000 字符、禁止引号与 `; | &` 串联，其余（rm/scp/ssh…）一律拒绝。

**长脚本请用脚本文件模式**（HPC 标准做法，避免多层 shell 转义问题）：

```powershell
# 1. 上传脚本到计算目录
scripts\vasp-agent.ps1 upload -LocalPath plot_dos.py -RemotePath 计算目录/plot_dos.py
# 2. 用短命令执行
scripts\vasp-agent.ps1 run -RemotePath 计算目录 -Command "python3 plot_dos.py 500"
```

### 8.2 确定性工作流工具（py -3.12 推荐）

```powershell
# 生成 relax→static 计划（白名单参数、幂等、拒绝覆盖）
py -3.12 scripts\workflow_prepare.py relax-static --from-dir 模板目录 --base-dir 输出目录 --set ENCUT=520 --dry-run

# 结果解析（pymatgen 增强；同目录两次解析逐字节一致）
py -3.12 scripts\vasp_parse.py 计算目录 --workflow relax -o manifest.json

# 失败诊断（L1 只检测不改文件；--propose 出补丁预览）
py -3.12 scripts\custodian_detect.py 计算目录 --propose

# L2 应用已批准的补丁（白名单文件、备份、审计）
py -3.12 scripts\apply_patch.py 计算目录 --patch INCAR.proposed.patch

# 工作流状态机（PREPARED→VALIDATED→APPROVED→…→REVIEWED）
py -3.12 scripts\workflow_state.py init 计算目录 --workflow relax-static
py -3.12 scripts\workflow_state.py advance 计算目录 --to VALIDATED --by 你

# Slurm 专用适配器（离线自检 16 项）
py -3.12 scripts\slurm_adapter.py selftest

# 一键冒烟测试（19 项）
py -3.12 scripts\test-tools.py
```

### 8.3 智能体命令行

```powershell
$env:DEEPSEEK_API_KEY = "你的key"
$env:VLAB_IDENTITY_FILE = "C:\Users\你\.ssh\vlab-identity.pem"

# 普通模式（22 个受审批工具，可切服务器、可跑分析命令）
py -3 scripts\deepseek-agent.py "检查我的任务队列"

# 确定性模式（仅 10 个高层工作流工具，无 Shell、提交强制审批号）
py -3 scripts\deepseek-agent.py --deterministic "诊断 NaOH_opt 是否收敛"
```

## 9. 图片/截图怎么给模型看（OCR 桥）

本模型不支持直接读图。把图片存到磁盘后告诉我路径，我用 Windows 自带 OCR 提取文字：

```powershell
powershell -File scripts\ocr-image.ps1 -ImagePath 截图.png
```

## 10. 安全设计速览

- **删除即隔离**：remove 把文件移入 `.vaspilot-trash/`（带时间戳，可恢复）；永久删除用 `purge`（仅限隔离区内路径 + 路径打两遍）
- **路径边界**：所有远端操作被限制在服务器根路径内，无法越界
- **写操作审批**：逐项人工确认；跨服务器传输永远需要审批（不参与免确认）
- **凭据隔离**：密码/OTP 只在 SSH 窗口输入；API Key 加密保存；POTCAR 全文不进入任何记录（只存哈希与 TITEL）
- **三级修复**：L1 只检测 → L2 补丁预览经人工批准后应用 → L3 白名单自动修复（当前为空集）

## 11. 常见问题

| 问题 | 处理 |
|---|---|
| 填了 PEM 路径仍提示未填写 | 设置里**保存**后刷新页面；路径已持久化到 `~/.vaspilot/local.json` |
| 服务器显示断开 | 点「连接」重新输入密码+验证码（保持期过后需要重连，正常现象） |
| 提交作业没反应 | 检查服务器调度器是否识别正确（诊断 scheduler）；确认已连接 |
| 模型说不支持画图 | 让它用 `run_remote_command`（如 `python3 plot_dos.py`），你批准后执行 |
| 想同时用多台服务器 | 直接说「先看 cl9 再看 cl12」，模型会自动切换 |
| GitHub 推送失败 | 已配置 SSH 协议（22 端口直连）：`git push origin main` |
| 忘记有哪些命令 | 网关：`vasp-agent.ps1 -?`；各工具：`py 脚本名 --help` |

## 12. 更新与提交

所有修改提交到 `main` 分支并推送到 GitHub（SSH 协议）。远程：`git@github.com:Wisconsin071205/agent_remote.git`。
