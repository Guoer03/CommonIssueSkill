# RAG Retrieval Module SDD

## Status

Draft for intranet implementation. `scripts/rag_retrieve.py` is intentionally an empty placeholder in this repository. The intranet coding agent should implement it according to this document.

## Goal

Implement a RAG retrieval pre-processing module that generates `rag_results.jsonl` for the existing issue classification pipeline.

The module must run before `run_pipeline.py init`:

```bash
python3 scripts/rag_retrieve.py \
  --input records.csv \
  --examples calibrated_examples.jsonl \
  --classification-options classification_options.json \
  --out runs/job_001/rag_results.jsonl

python3 scripts/run_pipeline.py init \
  --input records.csv \
  --rag-jsonl runs/job_001/rag_results.jsonl \
  --workdir runs/job_001
```

## Non-Goals

- Do not modify TopK or Final XML output contracts.
- Do not let RAG introduce labels outside `classification_options`.
- Do not store user data, RAG state, API keys, or run state in the skill directory.
- Do not require `run_pipeline.py` to call an external vector database during TopK or Final. RAG retrieval should be completed before `init`.

## Existing Integration Points

### RAG Consumer

`scripts/run_pipeline.py` already consumes RAG through:

- `init --rag-jsonl <path>`
- `load_rag_map(path)`
- `records.rag_pool_json` in `workdir/state.sqlite`

Import behavior:

- Each row in `rag_results.jsonl` must contain `record_id` or `id`.
- Rows are grouped by that `record_id`.
- During `init`, each input record gets `rag_map.get(record_id, [])`.
- If a row contains `rag_pool`, runner parses that field as a JSON array and uses it as the whole pool for the record.
- Otherwise, each JSONL row is treated as one retrieved historical example.

### Stage Usage

TopK stage:

- Reads `rag_pool_json`.
- Passes the first `topk_rag_k` rows to the TopK prompt.
- Default `topk_rag_k` is configured in `PYTHON_RUNTIME_CONFIG`.

Final stage:

- Reads the same `rag_pool_json`.
- Calls `select_rag_for_stages`.
- Prioritizes RAG rows whose `level_1/level_2` are inside the candidate pool.
- Marks candidate-pool-external examples with `out_of_candidate_pool_reference=true`.

## Input Contracts

### Records Input

`--input` should accept the same file formats as `run_pipeline.py`:

- `.csv`
- `.tsv`
- `.jsonl`
- `.xlsx`
- `.xlsm`

The implementation may import and reuse `read_rows`, `FIELD_ALIASES`, `first_value`, and related helpers from `scripts/run_pipeline.py`.

Record fields may include:

- `id` or `record_id`
- `problem_overview`
- `probelm_details`
- `problem_details`
- `solution_details`
- Chinese aliases such as `问题概述`, `问题明细`, `问题根因`, `解决方案`

Recommended query text priority for communication-domain issues:

1. problem root cause, if present
2. problem details
3. solution details only as auxiliary evidence
4. problem overview

### Calibrated Examples Input

`--examples` should point to a calibrated historical sample file. Recommended fields:

- `case_id`: stable historical example id
- `record`: historical issue text
- `problem_overview`
- `probelm_details` or `problem_details`
- `solution_details`
- `level_1`: calibrated first-level label
- `level_2`: calibrated second-level label
- `mapping_justification`: optional reason for the historical mapping
- `source`: optional source or batch name

The implementation must filter out examples whose `level_1/level_2` are not present in `classification_options`.

### Classification Options

`--classification-options` points to the same taxonomy JSON used by `run_pipeline.py`:

```json
{
  "一级分类": {
    "二级分类": ["level3补充信息"]
  }
}
```

Only `level_1` and `level_2` participate in RAG legality checks. `level3` is auxiliary and must not be output as a final label.

## Output Contract

`--out` must write JSONL. Each line is one retrieved historical example for one target record.

Required fields:

```json
{
  "record_id": "case-001",
  "case_id": "hist-001",
  "record": "历史相似问题文本",
  "level_1": "一级分类",
  "level_2": "二级分类",
  "similarity": 0.93
}
```

Recommended optional fields:

```json
{
  "mapping_justification": "历史样例为什么这样分类",
  "problem_overview": "历史问题概述",
  "probelm_details": "历史问题明细",
  "solution_details": "历史解决方案",
  "source": "历史样例来源"
}
```

Rules:

- `record_id` must match the normalized input record id used by `run_pipeline.py`.
- `similarity` must be numeric and higher means more similar.
- Output rows should be sorted from highest to lowest similarity per `record_id`.
- Keep 10 to 20 examples per record by default.
- Do not output examples with illegal labels.

## Recommended CLI

Implement this CLI in `scripts/rag_retrieve.py`:

```bash
python3 scripts/rag_retrieve.py \
  --input records.csv \
  --examples calibrated_examples.jsonl \
  --classification-options classification_options.json \
  --out runs/job_001/rag_results.jsonl \
  --top-k 20 \
  --max-per-label 5
```

Recommended arguments:

- `--input`: required target records.
- `--examples`: required calibrated historical examples.
- `--classification-options`: required or default to `references/classification_options.json`.
- `--out`: required output JSONL.
- `--top-k`: default `20`, number of RAG rows per input record.
- `--max-per-label`: default `5`, avoid one label dominating all retrieved examples.
- `--min-score`: optional score cutoff.
- `--backend`: optional, such as `bm25`, `vector`, `hybrid`, `es`, or intranet-specific backend.

## Retrieval Strategy

Recommended implementation for best accuracy without slowing classification:

1. Build query text from each target record using the field priority above.
2. Retrieve candidates from historical examples using hybrid retrieval:
   - vector similarity for semantic match
   - BM25 or keyword match for alarms, equipment names, error codes, command names, and fault terms
3. Fuse scores with RRF or a simple weighted score.
4. Filter illegal taxonomy labels.
5. Deduplicate by `case_id`.
6. Apply label diversity using `--max-per-label`.
7. Write top `--top-k` JSONL rows per target record.

If the intranet environment already has a vector database or ES index, `rag_retrieve.py` should act as the adapter from pipeline records to that service and from service results back to `rag_results.jsonl`.

## Accuracy Guardrails

- RAG is evidence, not authority. Final classification must still choose only from `candidate_pool`.
- Candidate-pool-external RAG examples may help wording but must not decide the label.
- Historical labels must be migrated to the current taxonomy before output.
- Do not use solution actions like "升级", "重启", "倒换", "更换" as sole retrieval or classification evidence.
- Prefer root cause and detailed symptom fields over solution text.

## Performance Requirements

Target scale: up to 200k records.

Implementation should:

- stream inputs and outputs when possible;
- batch retrieval requests to vector/ES services;
- cache embeddings for calibrated examples;
- support resumability by skipping existing `record_id` outputs when practical;
- log summary counts: input records, valid examples, output rows, filtered illegal examples, empty-recall records.

## Error Handling

Fail fast when:

- input file cannot be read;
- examples file cannot be read;
- classification options are empty or invalid;
- output directory cannot be created.

Warn or count, but continue when:

- a historical example has missing text;
- a historical example has illegal labels;
- an input record has no recall result.

## Test Plan

Add tests for the intranet implementation:

- Reads CSV/JSONL records and examples.
- Filters taxonomy-illegal examples.
- Writes JSONL rows with required fields.
- Preserves `record_id` for join with `run_pipeline.py`.
- Sorts by descending `similarity`.
- Enforces `--top-k`.
- Enforces `--max-per-label`.
- Produces no rows, without crashing, for records with no matches.

After implementation, run:

```bash
python3 tests/test_run_pipeline.py
python3 tests/test_pipeline_contracts.py
python3 -m py_compile scripts/run_pipeline.py scripts/pipeline_core.py scripts/rag_retrieve.py
```

If new RAG tests are added, run those as well.
