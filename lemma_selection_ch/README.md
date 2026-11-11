# 独立 Classical Houdini（实验一）

本目录由当前项目的同一个 Git 仓库管理，核心算法在 `src/method.py`。公共后端、冻结池读取、预算、计数和结果格式与 `../lemma_selection_ps/` 完全一致。

`data/frozen_pool/experiment1_v1/` 已从根目录冻结包原样复制完成，包含 109 个任务，全部文件与 PS、根目录中的包一致。CH 每次从完整池开始，拥有完整 600 秒任务预算，不使用 GLS 的选择状态或结果缓存。

候选学习阶段只检查候选，目标属性在形成归纳固定点后另行检查。所有生成的断言使用唯一名字，并由 EBMC `--property` 精确选择；原始断言和环境假设保留在输入中。候选见证不再依赖目标是否带显式时钟、是否包含时序运算符。

单周期布尔 `|->` 可规范化后提取状态取值。其他 SVA 在见证不足时交给原生逐候选归纳义务检查：每次都使用完整当前候选集作为源端假设，假设在后继端关闭，保留候选自身的时钟、`disable iff`、`|=>`、`$past` 等语义。只凭有效反例删除候选，不把有界无反例当作归纳固定点。真实多时钟尚未解析、见证不完整、固定点不足、查询超时与工具错误分别记录原因；新增查询全部计入共同预算。

当前公共后端版本为 `ebmc-selected-obligations-v5`。三方法的归纳查询统一先使用 `--k-induction --bound 1`；仅当 EBMC 明确报告 `there is no property suitable for k-induction` 时，添加 `--buechi` 重新检查同一义务。两次尝试共享原查询超时，其他错误、崩溃和超时不触发重试。时序转换需要运行环境的 `PATH` 能找到 Spot 的 `ltl2tgba`，全部进程和耗时计入共同预算。选择前统一进行可编译性检查，仅隔离能够明确定位到候选声明的编译错误，冻结文本保持原样；隔离与等价归约展开的记录均保存在查询／结果文件中。多行候选的生成断言位于完整声明之后。断言路径由 `--show-properties` 的实际展开结果确定，支持子模块、参数化及 generate 层级；多实例歧义明确报错。路径查询、编译检查与验证均计入总预算和 EBMC 进程数。已有 `v2`／`v3`／`v4` 批次按其原配置保留，不能作为 `v5` 结果恢复或混合统计。

在项目根目录运行：

```bash
python3 -B lemma_selection_ch/scripts/run.py --help
python3 -B lemma_selection_ch/scripts/run.py --ebmc-path /absolute/path/to/ebmc-5.9
```

默认执行 main 与 hard、各三遍，结果写入 `experiment1_results/ch/repeat_<1|2|3>/<group>/<task>/`。`--smoke` 用于独立的小范围验收；`--dry-run` 不启动后端。

安装、冻结、GLS 适配、完整实验命令、统计口径和汇总方式见 [实验一说明](../lemma_selection_ps/README.md)。
