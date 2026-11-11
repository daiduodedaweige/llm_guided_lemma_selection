# 实验一：固定候选池、PS／CH／GLS 与三次重复

本目录与 `../lemma_selection_ch/` 是当前 Git 仓库下的普通目录。`experiment1_v1` 候选池已冻结，并原样复制到 PS、CH 目录；三个包的全部文件与哈希一致。程序只读取冻结候选，不会重新生成候选。公共后端修复同步到根目录与两个方法目录，并更新来源哈希。

冻结池包含 main 的 78 个任务与 hard 的 31 个任务，共 109 个任务、545 份生成响应。池哈希为 `c55cf1ce89c1795a97181b5c7962cabcb8365e2595a04be1bf496d0fefa8e75c`。原始包位于根目录 `data/frozen_pool/experiment1_v1/`，两份副本位于对应方法目录的同名路径。

公共文件复制自现有代码，来源哈希记录在 `reference_sources.json`。两个方法目录中的公共运行代码必须逐字一致，启动时自动校验。各方法的核心差异集中在 `src/method.py`；GLS 入口直接只读加载根目录现有的 `src/gls.py`。

## 安装与离线验收

在项目根目录执行。可在本目录建立独立虚拟环境，EBMC 路径通过命令行指定。

```bash
python3 -m venv lemma_selection_ps/.venv
lemma_selection_ps/.venv/bin/pip install -r lemma_selection_ps/requirements.txt
lemma_selection_ps/.venv/bin/python -B -m unittest discover -s lemma_selection_ps/tests -v
lemma_selection_ps/.venv/bin/python -B -m unittest discover -s lemma_selection_ch/tests -v
```

离线验收使用临时目录、假 EBMC 和模型 mock，不连接模型服务，不使用现有生成缓存，不调用 tmux，不写入正式冻结池或正式结果。

真实 EBMC 运行还需要 Spot 的 `ltl2tgba` 转换工具。当前终端或 tmux 会话的 `PATH` 必须能找到它，可用 `ltl2tgba --version` 检查；只安装工具而不启用转换参数，仍会出现没有适用于 k-induction 的属性的错误。

设置 `EBMC_59_PATH=/absolute/path/to/ebmc-5.9` 后，CH 测试还会在临时目录执行小型真实 EBMC 回归用例，检查断言隔离、候选相互支持、跨周期 SVA 和复位语义。它们需要上述转换工具，不启动实验一批次，也不请求 LLM。

公共后端版本为 `ebmc-selected-obligations-v5`。PS／CH／GLS 的直接、候选及联合归纳查询统一先使用 `--k-induction --bound 1`；仅遇到 EBMC 明确报告 `there is no property suitable for k-induction` 时，添加 `--buechi` 重新解析并检查同一条义务。两次尝试共享原查询超时，所有进程和转换耗时计入共同预算；其他错误、崩溃和超时不触发重试。初始性与 BMC 使用各自的有界检查配置。每次查询为生成的断言设置唯一名字，先以相同源码和选项运行 `--show-properties`，从 EBMC 输出中解析实际完整实例路径，再通过 `--property` 精确选择。不会用模块类型名拼接实例路径；未找到或存在多个实例时报告明确错误，多实例不会默认选择第一个。CH 候选学习不读取目标的表达式或采样方式。缺少状态取值时，CH／GLS 的 Houdini 阶段可增加逐候选原生 SVA 归纳义务查询，全部计入正式查询次数及时间预算。有限深度无反例不会被当作归纳证明；最终成功仍须完整归纳检查通过。任务结果保存 `reason_code`，汇总保存未决原因计数。版本、源码、配置不一致的结果不能混用或恢复；已有 `v2`／`v3`／`v4` 结果不能作为 `v5` 批次恢复。

`v5` 在选择前统一检查候选的可编译性：先编译原 RTL 和目标，再检查候选声明；仅隔离编译器明确定位到某条生成声明中的语法、未声明标识符或未知函数错误。不会根据 SAT、INCONCLUSIVE 或超时隔离候选，也不会猜测补齐索引、形参或跨实例引用。编译崩溃、RTL 错误和无法定位的错误仍终止任务。冻结池原文、ID 和哈希不变，隔离记录保存在 `isolated_candidates`，可执行数量保存在 `eligible_candidate_count`。所有编译检查进程均属于 `candidate_validation` 阶段，计入 Formal Queries、Solver Time 和共同预算。

对单模块、确定长度的 `ranks` 数组，输入适配层支持两种已确认的 `.or() with (NUM_REQ'(1) << item)` 与 `.and() with (item < NUM_REQ)` 表达式，将其等价展开为逐元素位运算。查询日志记录 `source_adaptations`，转换发生时同时保存 `input.original.sv` 和实际输入 `input.sv`。其他形状保持原样交给编译器。多行引理按物理行插入，生成断言始终放在完整声明之后。

## 冻结候选池

两组生成与 GLS 预评估均已完成，`experiment1_v1` 已完成冻结。导出器读取整理后的 `manifest.json` 索引及文件哈希，核对 78＋31 个任务、生成时的 RTL 哈希、每任务 5 份响应以及原有候选提取顺序。成本统计对应保留的生成响应和单任务执行记录。导出器也兼容原始 live 输出布局。

```bash
python3 -B lemma_selection_ps/scripts/freeze_pool.py --check-only
python3 -B lemma_selection_ps/scripts/freeze_pool.py --copy-only --check-only
```

以上命令分别只读校验生成来源和已冻结包。首次导出的默认操作从 `results/gls_20261109_fs1_5samples/` 读取已有数据，冻结到根目录 `data/frozen_pool/experiment1_v1/`，再原样复制到两个方法目录的同名路径。不会调用候选生成器，不覆盖已有冻结包。若以后修复提取规则，应指定新的 `--destination` 和对应的 `--copy-to` 路径，统一重新验收。

整理后的每组目录直接包含 `candidates.json`、`evaluation.json`、`audits/` 和 `generation/`。`generation/` 中保留提示词及响应缓存，`summary/` 中保存 109 个任务的唯一最终结果。这批数据是预跑结果，正式三遍需通过下述入口独立执行。

冻结包结构：

```text
manifest.json                    # 包哈希、文件哈希、来源、范围和任务数
tasks.json                       # 稳定任务 ID、候选 ID/原文/顺序/哈希
rtl/<group>/<task>.sv            # 含目标属性、假设、初始块的完整 RTL
generation/<group>/<task>.json   # 完整生成输入、五份响应、解析状态与成本
```

仅对完全相同的候选文本去重，保留首次出现顺序；空候选任务仍保留。当前导出器面向自包含的 benchmark 文件，遇到 `include` 或 `$readmem*` 外部文件引用会要求先显式打包依赖，避免悄悄遗漏输入。

## 小范围验收与正式三遍

下列均在项目根目录执行，将 `/absolute/path/to/ebmc-5.9` 替换为实际路径。运行器只启动自己的 EBMC 子进程；单次超时仅终止该子进程。它没有 tmux 操作。

冻结后先选择少量任务验收，输出放入独立目录；`--tasks` 使用冻结清单中的 `group/name`，必须配合 `--smoke`：

```bash
python3 -B lemma_selection_ps/scripts/run.py \
  --tasks main_experiment/任务名 --repeats 1 --smoke \
  --output-root experiment1_results_acceptance \
  --ebmc-path /absolute/path/to/ebmc-5.9
```

正式命令默认包含 main 和 hard，各自独立运行三遍。实验一在三个独立 tmux 会话中并行运行三个入口，各入口内部串行；不绑核或固定划分 CPU／内存，按共享主机、全局最多三个任务并发的条件报告时间：

```bash
python3 -B lemma_selection_ps/scripts/run.py --ebmc-path /absolute/path/to/ebmc-5.9
python3 -B lemma_selection_ch/scripts/run.py --ebmc-path /absolute/path/to/ebmc-5.9
python3 -B lemma_selection_ps/scripts/run_gls_reference.py --ebmc-path /absolute/path/to/ebmc-5.9
```

可以用 `--groups main_experiment` 或 `--groups hard` 分组启动，用 `--repeats 1` 指定某一遍。完整实验为 18 个组别／方法／重复批次，共 981 次任务运行。`--dry-run` 只校验冻结包、公共代码及计划，不启动 EBMC 或 API 请求，也不创建结果目录。

已存在批次默认拒绝覆盖。`--resume` 只跳过同一批次内配置与输入完全一致的已完成任务，包括已结束的 ERROR／UNKNOWN／TIMEOUT；不向其他任务、方法或重复运行提供 LLM／证明结果缓存。未完成任务的旧目录会清理后从头重跑，不保留中断旧日志或 `.incomplete/` 归档。仅在对应运行器已经停止后恢复。

GLS 通过现有只读配置解析器读取 Responses 服务与私有密钥文件，默认选择模型 `gpt-6.1-sol`、推理强度 `xhigh`，客户端 `max_retries=0`。请求保存完整 `model`、`input`、`reasoning` 和响应 JSON，密钥不进入实验文件。GLS 加入全池 Houdini 预筛；预筛无法判定时仍可进入 LLM 选择，预筛删除的候选不会出现在模型提示词中。Prompt 版本为 `gls-fixed-pool-v3-houdini-prescreen`。

## 算法与统计口径

- PS 沿用当前 `evaluate_subsets()` 的单例分类、优先 1-inductive 后按正文排序、逐前缀检查。分类查询全部计费；得到完整单例或前缀证明后立即停止。归纳查询采用三方法统一的 `k=1` 与按需时序转换规则。
- CH 从完整候选池开始，先检查候选自身的初始性和归纳性，不假设目标属性成立；仅依据可达反驳或可靠的一步见证删除候选。到不动点后检查目标。证据不足返回 `UNKNOWN`，不猜测删除。
- GLS 在直接证明与 BMC 后，从完整池开始做有时间预留的 Houdini 预筛。证明、真实不安全或充分的池内不可证明见证可直接结束；预筛的 TIMEOUT／UNKNOWN 可继续进入 LLM 选择，ERROR 则立即结束当前任务。预筛所有实际 EBMC 查询都计入时间和 Formal Queries。
- 三方法统一在选择前隔离可定位的候选编译错误；其后采用 `formal_error_policy: fail_fast`：预检查、候选分类、组合验证、见证／定位和 Houdini 查询首次返回工具 ERROR 时，保存错误阶段、候选 ID、退出码和完整后端日志，当前任务立即结束，批次继续下一任务。不会为工具错误追加模型修复。有效 SAT 反例不属于工具错误；无法可靠求值而返回 UNKNOWN 仍按原规则处理。
- 三方法均先做目标初始性与直接归纳检查，需要时做深度 30 的目标 BMC。总预算默认 600 秒，单次 EBMC 最多 `min(120 秒, 剩余预算)`；GLS 模型请求也计入总预算。
- Formal Queries 按实际启动的 EBMC 进程计数，包括属性路径查询、分类、初始性、归纳、BMC、见证、定位和回退；已启动但超时计一次，未启动不计。`property_discovery_queries` 与 `verification_queries` 分别统计路径查询和验证进程，日志以 `kind` 区分。路径查询与随后验证共享一次查询的 120 秒上限，两者也计入任务总预算。Solver Time 是这些进程墙钟耗时之和，包含前端等工作；路径查询耗时另存为 `property_discovery_s`。与旧后端版本对比查询量时须区分统计口径。
- `PROVED` 仅由完整归纳证明产生。`PROVED up to bound` 可支持有界检查结果，不能作为归纳证明。超出预算才取得的证明归为 `TIMEOUT`。
- 目标属性的真实反例与 `TIMEOUT`、`UNKNOWN`、`ERROR` 分开保存；冻结池缺失直接报错。空候选池仍执行直接证明检查。
- Proof Time 从算法预检查前开始，包含模型、查询及算法开销；生成成本单列。`end_to_end_s = recorded_generation_s + wall_s`，历史重试等不可取得的成本保留缺失标记。

每个批次的 `run.json` 保存配置、模型／Prompt、输入哈希、EBMC 路径／版本／二进制哈希、机器、Python、并发和时间；`source_snapshot/` 保存实际运行源码。所有临时 RTL 和生成的查询都在新结果目录内。

## 统一汇总

```bash
python3 -B lemma_selection_ps/scripts/summarize.py
```

汇总入口按完整冻结清单建立任务×方法×重复网格。缺失运行单列 `MISSING`，保留在分母中，不填造时间或查询数；拒绝混合不同冻结包、公共后端、预算或不同方法内部的模型／源码配置。验收结果只能通过 `--smoke --results ... --output ...` 单独汇总。

`experiment1_results/summary/` 输出逐任务 JSON／CSV、分组比较表、指标均值和样本标准差、配对 CH／GLS 数据，以及 main／hard／all 三组的 Cactus、配对加速比和耗时分解图（PNG／PDF）。配对以同一任务、同一重复中双方都成功为条件，保留查询量差异；不使用三遍成功并集作为成功率。

公共文件如有修复，请同时同步至两个方法目录；启动时的哈希校验和离线测试会检查一致性。
