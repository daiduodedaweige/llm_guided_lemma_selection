# LLM-GLS — Fixed-Pool Lemma Selection Framework

This directory reuses the Large Lemma Miners candidate generator, benchmark
layout, EBMC wrapper, command-line runner, and evaluation JSON schema. The
**non_agent live pipeline** now runs the fixed-pool LLM-GLS selector after
candidate generation. The original agent pipeline and paper replay code remain
available as reference implementations. The method is defined in
[组合选择与成本决策规则.md](组合选择与成本决策规则.md) and
[论文计划.md](论文计划.md).

Run one live task after installing requirements and EBMC 5.9. The runner reads
the currently selected model, Responses relay URL, reasoning effort, and key
file location from your Codex config. It does not put the key in this repo.
The selected model in this environment is `gpt-6.1-sol`.

~~~bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-live.txt
.venv/bin/python scripts/run_live.py \
  --pipeline non_agent --modules-dir benchmarks/main_experiment \
  --num-samples 5 --few-shot 1 --time-budget-s 600 \
  --ebmc-path /path/to/ebmc-5.9 \
  --output-dir live-results/main-gls
~~~

Use `--dry-run` to validate configuration without model or EBMC queries. The
input candidate JSON, evaluation JSON names/fields, and result summaries keep
the reference pipeline's shape. Detailed method records appear under each
run's `results/logs/gls_*.json`, including candidate IDs, checked groups,
costs, statuses, and backend logs. The optional `LLM_GLS_PRIORS_FILE`
environment variable points to frozen development-set estimates such as:

~~~json
{"initial_s": {"1": 3.0}, "step_s": {"1": 5.0}, "llm_s": 20.0, "houdini_s": 180.0}
~~~

EBMC 5.9's k-induction query does not itself emit a step-state trace. For
single-cycle Boolean SVA predicates with the same sampling clock and disable
condition, GLS now runs a separately counted `--ignore-initial --bound 1`
query. EBMC evaluates generated combinational probes in both states; those
values drive safe counterexample pruning, local repair, and Houdini deletion.
If k-induction's reachable base is refuted, separate bounded candidate checks
identify candidates that cannot be invariants. Boolean overlapped implication
is normalized while retaining its clock and disable condition. When state
valuations cannot be extracted, Houdini checks each candidate's native SVA step
obligation under the complete current hypothesis set. Source-only assumptions
are gated off in the successor. These separately counted depth-30 queries keep
`|=>`, `$past`, and delays intact; bounded UNSAT never establishes a fixed point
or a proof. Actual unresolved clocks and missing witnesses have explicit reason
codes. Each generated assertion is named and selected with `--property`, so
original target assertions cannot contaminate candidate-only checks.
Only the original k-induction result can establish `PROVED`. Freeze
development-set cost estimates before paper experiments.

Formal tool errors end the current task immediately with `ERROR`, including
errors during prechecks, Houdini prescreening, combination checks, witness
queries, and fallback. The first failure's phase, candidate IDs, exit code and
backend log are saved; no further model or formal queries run for that task.
The batch continues with the next task. Valid SAT counterexamples and
`UNKNOWN`/`TIMEOUT` retain their existing repair and budget handling. Run
metadata records `formal_error_policy: fail_fast`; older results cannot be
resumed under this policy without matching code and configuration identities.

## Reference Artifact

### Large Lemma Miners — Reproducibility Artifact

Artifact for the IJCAI 2026 paper *"Large Lemma Miners: Can LLMs do Induction Proofs for Hardware?"*

## Repository Structure

```
benchmarks/
  main_experiment/    # 78 SystemVerilog benchmarks (main experiment, excluding hard subset)
  hard/               # hard subset + sota_timings/
scripts/
  get_tables.py       # single entry point for all paper tables & plots
  entry_point.py      # experiment runner (used for Level 2 replay)
  analysis/           # table/plot builders consumed by get_tables.py
  cactus/             # cactus plot generation
templates/            # prompt templates
src/                  # core source (agentic.py, evaluation.py, models.py, prompt_llms.py, utils.py)
```

## Prerequisites

```bash
pip install -r requirements.txt
```

The verification backend (EBMC) is only needed for live runs; both reproduction levels below use cached evaluation results.

## Artifact Data (Zenodo)

The artifact archive is published on Zenodo: <https://zenodo.org/records/20832562> (DOI: [10.5281/zenodo.20832562](https://doi.org/10.5281/zenodo.20832562)).

Download `artifact.tar.gz` (~835 MB) and extract it; it unpacks to an `artifact/` directory containing:

| Directory | Contents |
|-----------|----------|
| `main_experiment/` | Per-run directories with LLM caches, eval caches, and result summaries |
| `hard/` | Same structure for the hard benchmark subset |

Set the environment variable to point at the extracted `artifact/` directory:

```bash
export ARTIFACT_DATA_DIR=/path/to/artifact
```

---

## Level 1 — Reproduce Tables & Plots from Pre-computed Results

The artifact data directory contains pre-computed run results (result summaries for every configuration). This level regenerates all paper tables and figures directly from those summaries. No LLM calls, no EBMC calls.

```bash
export ARTIFACT_DATA_DIR=/path/to/extracted/artifact
./generate_tables.sh
```

Outputs appear under `results/tables/` and `results/plots/`:
- `tables/overall_from_experiments.{tex,csv}` — main results table
- `tables/timings_hard_all.{tex,json}` — timing comparison on hard benchmarks
- `tables/solved_unsolved_by_category.latex` — per-category breakdown
- `tables/fewshot_ablation.tex` — few-shot ablation table
- `plots/num_iterations_ablation_line.png` — iterations ablation plot

A build log is written to `results/metadata/build.log`.

---

## Level 2 — Replay from Caches (Full Pipeline Replay)

The run results in the artifact data can also be recomputed from scratch. This level re-executes the full experiment pipeline end-to-end, but all LLM and evaluation calls are served from disk caches. It regenerates the result summaries, then produces the same tables and plots as Level 1.

```bash
export ARTIFACT_DATA_DIR=/path/to/extracted/artifact
./generate_tables.sh --replay --workers 6 /path/to/new-replay-directory
```

`--workers N` defaults to 1 and requires a positive integer. Replay runs the
same 792 configurations as the paper artifact in a bounded worker pool. Every
configuration has a stable ID, private workspace/output staging area, and
separate log under `results/metadata/replay-logs/`.

The results directory must be new unless `--allow-existing-results` is passed
explicitly. Normal fresh replay does not need that option.

An interrupted replay can be continued without weakening that protection:

```bash
export ARTIFACT_DATA_DIR=/path/to/extracted/artifact
./generate_tables.sh --replay --resume --workers 6 /path/to/interrupted-replay
```

Resume validates the manifest, all completed outputs, and the read-only cache
snapshot. Completed jobs are skipped; interrupted attempts are archived under
`results/metadata/replay-attempts/` and retried in private workspaces.

---

## Live Experiments with the Current Codex Model

`scripts/run_live.py` makes new LLM requests and runs EBMC. It reads the
selected model, Responses API URL, and private key-file location from
`~/.codex/config.toml`; the key itself is never copied into the repository.
It requires a provider whose `auth` is a single `cat` key file, as in the
current `sub2api` setup. A fresh output directory is mandatory, so paper
artifact caches cannot be used accidentally. `ARTIFACT_DATA_DIR` is not needed.

Install the live-only Python dependencies with
`pip install -r requirements-live.txt`. Install **EBMC 5.9** separately and
pass `--ebmc-path /path/to/ebmc` if it is not at `.venv/bin/ebmc-5.9`. The
runner checks the version before starting. This copied workspace has no local
`.venv`; create one as shown above, or pass the executable path from an
existing EBMC 5.9 installation.

Run a single non-agent configuration (five independent model responses per
module) or an agent configuration (up to seven rounds):

```bash
.venv/bin/python scripts/run_live.py \
  --pipeline non_agent --modules-dir benchmarks/main_experiment \
  --few-shot 1 --num-samples 5 \
  --output-dir live-results/main_non_agent_fs1

.venv/bin/python scripts/run_live.py \
  --pipeline agent --modules-dir benchmarks/main_experiment \
  --few-shot 1 --num-iterations 7 \
  --output-dir live-results/main_agent_fs1
```

To run the paper's main single-model matrix, use a fresh root directory:

```bash
run_root="live-results/$(date -u +%Y%m%dT%H%M%SZ)"
for benchmark in main_experiment hard; do
  for few_shot in 0 1 2 3 4; do
    for pipeline in non_agent agent; do
      .venv/bin/python scripts/run_live.py \
        --pipeline "$pipeline" --modules-dir "benchmarks/$benchmark" \
        --few-shot "$few_shot" --num-samples 5 --num-iterations 7 \
        --output-dir "$run_root/${benchmark}_${pipeline}_fs${few_shot}"
    done
  done
done
```

This is 20 configurations over the two benchmark groups, 0–4 few-shot
examples, and both pipelines. The paper also reports ablations for five
few-shot examples and Agent iteration limits 1–10; run those with the
corresponding flags and fresh output directories. `--dry-run` checks the configuration
and prints a manifest without making requests or writing results. The default
few-shot selection uses the validated, deterministic example cache; add
`--fewshot-selection live` to recompute embeddings instead. Output includes
`live-run.json`, generated lemmas, EBMC evaluations, conversations, and
summaries under `<output-dir>/<benchmark>/<model>_<pipeline>_<few-shot>_<benchmark>/`.

These are new experiments using the selected Codex model (currently
`gpt-6.1-sol`), **not** exact reruns of the paper's pinned GPT-5 model. The
original replay commands and model mappings remain unchanged. Live requests
may incur substantial proxy charges; start with one module and one sample.

## Deterministic Few-shot Selection

`data/fewshot_selection.json` stores the five nearest examples and cosine
similarities for every main and hard benchmark module. Keys are repository-
relative paths. Metadata records the pinned
`sentence-transformers/all-mpnet-base-v2` revision, sentence-transformers
version, maximum `k`, and SHA-256 hashes of benchmark modules and few-shot
inputs.

Replay automatically uses this cache for few-shot settings 1–5, so it does not
import or load the embedding model. Missing entries, changed inputs, or
requests above the cached maximum are errors. Ordinary experiments retain live
selection via `scripts/entry_point.py --fewshot-selection live`; cached
selection can be selected explicitly with:

```bash
python scripts/entry_point.py \
  --fewshot-selection cached \
  --fewshot-selection-cache data/fewshot_selection.json \
  ...
```
