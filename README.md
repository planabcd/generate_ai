# generate_ai

招投标文件自动生成工作区（当前以 `GenerateAgent` 为主）。

## 目录总览

- `GenerateAgent/`：本项目主要实现（模板解析、字段提取、投标文件生成）。
- `GenericAgent/`：上游框架代码（当前以嵌套仓库形式存在）。
- `docs/archive/`：历史需求、设计、技术文档和参考素材归档。
- `package.json` / `convert-md-to-pdf.js`：Markdown 转 PDF 辅助脚本。

## 快速使用（规则模式）

```bash
cd GenerateAgent
python3 scripts/bid_pipeline.py --project-dir examples/project_rule_case --mode rule
```

生成结果会输出到：`GenerateAgent/output/task_*/`。

## LLM 模式（可选）

```bash
cd GenerateAgent
python3 scripts/bid_pipeline.py --project-dir examples/project_llm_unstructured_case --mode llm
```

请先在 `GenerateAgent/config/llm_config.json` 配置模型参数与 API Key。

## 归档说明

根目录历史文档与参考文件已归档到 `docs/archive/`，分类如下：

- `requirements/`：需求文档（md/pdf）
- `design/`：设计方案与测试用例
- `technical/`：技术方案
- `tasks/`：任务清单
- `screenshots/`：需求沟通截图
- `references/`：参考 docx（模板、AI 样例）

详见：`docs/archive/README.md`。

## 备注

- `GenericAgent` 在 git 中显示为 `160000`（gitlink），属于嵌套仓库形态。
- 若后续希望作为普通目录纳入当前仓库，需要单独处理子仓库关系。
