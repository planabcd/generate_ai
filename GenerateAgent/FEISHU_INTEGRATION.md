# Feishu Integration Guide (Minimal Changes)

## Goal

Run bid generation pipeline through GenericAgent Feishu frontend with minimal code changes.

## Option A (Recommended): No code change in GenericAgent core

1. Start Feishu frontend:

```bash
python frontends/fsapp.py
```

2. In Feishu, send instruction with attachments, then ask agent to run:

```text
请将本次消息和附件整理成 /tmp/task_input.json，然后执行：
python3 /home/lx/workspace/generate/GenerateAgent/scripts/bid_pipeline.py --input-json /tmp/task_input.json
```

3. Ask agent to send back generated files from `output/<task_id>/`.

## Option B: Tiny patch in fsapp command router (optional)

Add a command keyword like `/bidgen` in `frontends/fsapp.py`:

- Parse current message + attachment paths
- Write `task_input.json`
- Run `scripts/bid_pipeline.py`
- Upload output files back to Feishu

This is still a small frontend-level change and does not touch core loop.

## Required Task Input JSON

```json
{
  "project_text": "项目名称: ...\\n项目编号: ...",
  "supplier_name": "成都XX科技有限公司",
  "template_path": "/abs/path/模板.docx",
  "attachments": ["/abs/path/license.jpg", "/abs/path/id_front.jpg"]
}
```

## Constraints

- Facts are extracted, not fabricated.
- Fixed compliance sections are locked and variable-only.
- Low-confidence fields must be reviewed manually.
