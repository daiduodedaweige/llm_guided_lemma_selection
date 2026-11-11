实验一已使用修复后的后端重新启动。

独立 tmux 服务名：experiment1。三个会话：experiment1_ps、experiment1_ch、experiment1_gls；三个方法并行，每个方法内部串行。PS 会话的 summary 窗口等待三者完成后自动汇总。

同一冻结候选池的 main 78 个任务、hard 31 个任务，各三遍，总计 981 次运行。每任务预算 600 秒，单次 EBMC 最多 120 秒；GLS 使用 gpt-6.1-sol、xhigh。公共后端版本为 ebmc-selected-obligations-v2，采用 fail_fast 工具错误策略。

结果位置：<method>/repeat_<1|2|3>/<group>/<task>/。启动命令和时间见 execution_plan.json；运行日志见 logs/ps.log、logs/ch.log、logs/gls.log；入口退出状态见 logs/<method>_exit.json，汇总状态见 logs/summary_status.json。退出码 1 表示存在任务级 ERROR，自动汇总仍要求全部 18 个批次完成及 981 条结果齐全。

查看：tmux -L experiment1 attach -t experiment1_ps（也可使用 experiment1_ch 或 experiment1_gls）。
