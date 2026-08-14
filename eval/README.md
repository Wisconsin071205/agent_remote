# 对照评测协议（eval/）

三组对照（见 spec/workflows.md 第 5~6 周计划）：

| 臂 | 名称 | 执行方式 | 预期特征 |
|---|---|---|---|
| A | 人工脚本 | 专家手工写并运行脚本 | 高正确率、高人工时间、无模型成本 |
| B | 模型写脚本 | LLM 直接生成 Shell/Python 并在隔离沙箱执行 | 低成本，但写操作无结构审批、一致性差 |
| C | 确定性工具+审批 | LLM 仅能调用 agent_tools 的 10 个高层工具，写操作必经审批 | 结构安全、可复现，token 成本略高 |

## 统一 trace 格式

三个臂输出相同的 JSONL 事件流：`{task_id, arm, seq, event, ...}`
事件类型：`tool_call` / `tool_result` / `approval` / `write` / `shell` /
`done` / `api_usage`。`write` 事件必须带 `approved` 布尔；`done` 必须带
`human_time_s`；`api_usage` 带 `tokens_in/tokens_out/cost_usd`。

## 七个指标（eval/metrics.py）

1. task_success_rate —— 任务完成比例
2. config_correctness_rate —— 最终 INCAR 配置与期望相符的比例
3. unauthorized_write_rate —— 未审批写操作占全部写操作比例
4. determinism_consistency —— 同一任务两次执行 trace 指纹一致比例（忽略时间戳）
5. diagnosis_accuracy —— 诊断出的错误签名与期望精确匹配比例
6. human_time_s —— 人工操作时间（均值/总和）
7. token_cost —— 模型 token 与费用

## 运行方式

```
py eval/runner.py run --arm C --tasks eval/tasks.example.json --out eval/runs --repeat 2
py eval/runner.py import --arm A --trace a-trace.jsonl --out eval/runs
py eval/runner.py report --tasks eval/tasks.example.json --runs eval/runs
py eval/runner.py selftest
```

## 现状与计划

- **C 臂已可离线完整执行**（selftest 6 项全过）。
- A/B 臂为记录型：由人工（A）或模型沙箱（B）产生 trace 后 `import` 参与对比。
- 真实模型（B/C）与真实案例需在 20 个脱敏历史案例收集、认证轮换完成后进行。
- 任务目录约定 `eval/cases/<task-id>/`；案例收集后把 tasks.example.json
  换成 `tasks.json`（含全部 20 任务与期望配置）。
