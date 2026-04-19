# GenerateAgent (Feishu-first Bid Generation Pipeline)

This folder contains the business-side implementation for bid-document generation with **minimal changes** to GenericAgent.

## Design

- Reuse GenericAgent built-in Feishu frontend: `frontends/fsapp.py`
- Keep core framework untouched (`agent_loop.py`, `ga.py`, `llmcore`)
- Add only business pipeline + configs in this folder

## Folder Layout

- `scripts/bid_pipeline.py`: one-shot pipeline entry
- `config/bid_schema.json`: field schema
- `config/bid_rules.json`: validation rules
- `config/template_mapping.json`: docx placeholder mappings
- `config/fixed_sections.json`: compliance-fixed sections
- `examples/input.json`: sample task input
- `output/`: generated artifacts

## Run (local)

```bash
cd GenerateAgent
python3 scripts/bid_pipeline.py --input-json examples/input.json
```

Output example:

- `output/<task_id>/投标文件.docx`
- `output/<task_id>/qa_report.json`
- `output/<task_id>/normalized_payload.json`

## How to trigger from Feishu (GenericAgent)

Use GenericAgent Feishu frontend as-is, then ask agent to execute pipeline command:

```bash
python3 /path/to/GenerateAgent/scripts/bid_pipeline.py --input-json /path/to/task_input.json
```

The task input JSON can be assembled from Feishu message text + downloaded attachments.

## Notes

- Current extraction logic is rule-based baseline.
- OCR/LLM connectors are intentionally left as extension points in `extract_supplier_fields`.
- Template replacement uses `{{field_name}}` placeholders in `word/document.xml`.


## LLM API Config

Set one of the following:

1. File config: `config/llm_config.json`
- `api_url`
- `api_key`
- `model`
- `temperature`
- `timeout_sec`

2. Environment variables (override file config):
- `LLM_API_URL`
- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_TEMPERATURE`
- `LLM_TIMEOUT_SEC`

Run with LLM (default):

```bash
python3 scripts/bid_pipeline.py --input-json examples/input.json
```

Run without LLM (fallback rules):

```bash
python3 scripts/bid_pipeline.py --input-json examples/input.json --disable-llm
```


## Rule Mode (No LLM)

This is the default and recommended mode for tender-format-first generation.

```bash
cd GenerateAgent
python3 scripts/bid_pipeline.py --project-dir examples/project_rule_case --mode rule
```

Expected output:
- `output/<task_id>/投标文件.docx`
- `output/<task_id>/qa_report.json`
- `output/<task_id>/section_plan.json`

Input directory contract:
- `<project_dir>/招标文件/招标文件.docx`
- `<project_dir>/手填资料/基础信息.json`
- `<project_dir>/附件/*`
