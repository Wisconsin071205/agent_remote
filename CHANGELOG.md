# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。所有用户可见的修改都应记录在此。

## [未发布] — 确定性工作流内核（进行中）

### 案例工程与评测补全
- eval/collect_cases.py：案例收集器——scan-local（扫描本地历史对话提取候选
  远端目录）、mirror-local（本地目录脱敏镜像：POTCAR 只存 TITEL、OUTCAR 只留
  尾部、OSZICAR 留尾 200 行）、collect-remote（远端收集骨架，dry-run 预览）
- scripts/compare_parsers.py：批量解析器对比（vasp_parse vs gateway 正则），
  数值等价判断（"520" == 520.0），输出逐案例对比矩阵与一致率汇总
- eval/runner.py 补全：A 臂交互计时记录器（--auto 可测）、B 臂沙箱骨架、
  C 臂真实模型 live 模式（DEEPSEEK_API_KEY 驱动、token 记账、无 key 优雅降级）
- selftest 扩展到 10 项：三臂同格式 trace、未授权写率对比（A 0.0 / B 1.0 / C 0.0）、
  对比表渲染
- 安全：候选案例清单含真实远端路径，移出版本库并加入 .gitignore（脱敏后入库）

### 智能体接入与评测（第 5~6 周）
- deepseek-agent.py 新增 `--deterministic` 模式：模型仅暴露 10 个高层工具
  （无 Shell、无直接文件工具），提交强制 `approval_ref` 非空；
  默认模式（21 个细粒度工具）保持向后兼容
- eval/：三臂对照评测框架（A 人工脚本 / B 模型写脚本 / C 确定性工具+审批）——
  统一 trace JSONL、七个指标（成功率/配置正确率/未授权写率/一致性/
  诊断准确率/人工时间/成本）、C 臂离线可执行、determinism 双跑校验（6 项自检）

### 状态机与修复策略（第 3~4 周）
- workflow_state.py：显式状态机 PREPARED→VALIDATED→APPROVED→SUBMITTED→RUNNING→
  FINISHED→PARSED→REVIEWED；合法转换表校验、附加审计历史、异常终态
  （FAILED/TIMEOUT/REJECTED），REVIEWED 为最终态、异常态不可复活
- apply_patch.py：L2 补丁应用器——白名单文件（默认仅 INCAR）、进程内严格
  unified-diff 引擎（行号精确校验，失败整体拒绝）、.pre-*.bak 备份、
  .vaspilot-patches.jsonl 审计（前后哈希+补丁哈希+操作者）、--dry-run
- agent_tools.py：中心智能体高层工具面——10 个受约束工具 schema
  （prepare_workflow/validate_calculation/preview_changes/
  submit_approved_workflow/query_job_state/query_vasp_progress/
  diagnose_failure/propose_recovery/parse_results/generate_report）
  + 确定性分发器；模型无 Shell，提交强制 approval_ref 非空

### 安全整改
- remove 从「永久递归删除」改为「移入时间戳隔离区 .vaspilot-trash（可恢复）」；
  新增 purge（仅限隔离区内路径 + 路径双重确认）与 trash-list 命令；
  UI 与智能体工具同步更新（remove_remote_path 语义、新增 purge_remote_path）
- conversation-export.html 已列入 .gitignore（曾含认证信息），禁止入库

### 规范（spec/）
- workflows.md：三条标准工作流（relax-static / static-band-dos / convergence-scan）、
  通用状态机 PREPARED→REVIEWED、三级修复策略、六条确定性原则
- calculation-manifest.schema.json + calculation-manifest.md：CalculationManifest v1.0
  （结构/输入/环境/软件/执行/结果/修改/复核，全链路溯源与脱敏）

### 确定性工具（scripts/）
- vasp_parse.py：目录 → CalculationManifest；pymatgen 增强 + 标准库回退；
  同一目录两次解析逐字节一致（set 顺序、晶格归一化等已处理）
- slurm_adapter.py：Slurm 版本探测、sbatch --parsable、squeue/sacct JSON 查询与
  旧版文本回退、调度状态归一化（与科学状态分离）、离线样本自检 16 项
- workflow_prepare.py：relax→static 目录/输入/作业脚本/plan.json 生成；
  INCAR 参数白名单、修改审计、幂等写入、覆盖保护、--dry-run
- custodian_detect.py：VASP 故障检测（custodian/builtin 双引擎）；
  L1 只检测输出「检测—建议—证据」，L2 生成 .proposed.patch 预览，绝不修改文件

## [0.1.0] — 2026-08-14 · 版本基线

确立项目基线：**面向真实 HPC 环境的、安全可审计、确定性优先的 VASP 计算编排智能体**。

### 已具备（安全访问层）
- Windows → Vlab → 多台计算服务器的 SSH 连接（OpenSSH 复用连接、手动密码 + TOTP）
- Slurm 队列查询、任务提交、取消
- 远端文件读取、拷贝、移动、传输（跨服务器中转）
- 写操作审批、路径边界（根目录约束）、操作审计日志
- 基础 VASP 输入检查、进度解析、常见错误关键字识别
- 大模型工具调用与流式聊天界面（深色/浅色主题、壁纸）

### 本版本记录
- 初始化 Git 仓库与依赖清单（当前纯 Python 标准库，无第三方依赖）
- 将「会话导出」类可能含认证信息的文件列入 .gitignore，禁止入库
- 冻结 UI 功能扩展，转入确定性工作流内核开发

### 下一阶段（未发布）
- 三条标准工作流定义与 CalculationManifest 规范
- 确定性工具：vasp_parse（pymatgen）、slurm_adapter、workflow_prepare、custodian_detect
- remove 改为时间戳隔离区策略
- 工作流状态机与三级修复策略
- 中心智能体（仅高层工具）与对照评测
