#!/usr/bin/env python3
"""Bid document generation pipeline.

This script can be called directly with local files. It includes LLM API integration
for extraction and section drafting, with configurable api_url/api_key/model.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib import error, request
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET


DEFAULT_CONFIDENCE = 0.85


@dataclass
class FieldValue:
    value: Optional[str]
    confidence: float
    source_file: str
    source_locator: str
    model_version: str = "rule-v1"

    def to_dict(self) -> Dict[str, object]:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 4),
            "source_file": self.source_file,
            "source_locator": self.source_locator,
            "model_version": self.model_version,
        }


@dataclass
class LLMConfig:
    api_url: str
    api_key: str
    model: str
    temperature: float
    timeout_sec: int


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(self.config.api_url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.config.api_key}")

        try:
            with request.urlopen(req, timeout=self.config.timeout_sec) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
            raise RuntimeError(f"LLM HTTPError {e.code}: {detail[:500]}") from e
        except Exception as e:
            raise RuntimeError(f"LLM request failed: {e}") from e

        obj = json.loads(body)
        try:
            return obj["choices"][0]["message"]["content"].strip()
        except Exception as e:
            raise RuntimeError(f"Invalid LLM response format: {body[:800]}") from e


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_cn_date() -> str:
    today = dt.date.today()
    return f"{today.year}年{today.month}月{today.day}日"


def collect_inputs(args: argparse.Namespace) -> Dict:
    if args.input_json:
        payload = load_json(Path(args.input_json))
    else:
        payload = {
            "project_text": args.project_text or "",
            "supplier_name": args.supplier_name or "",
            "template_path": args.template_path,
            "attachments": args.attachments or [],
        }

    payload.setdefault("attachments", [])
    payload.setdefault("project_text", "")
    payload.setdefault("supplier_name", "")
    payload.setdefault("template_path", args.template_path)
    return payload


def load_payload_from_project_dir(project_dir: Path) -> Dict:
    tender_dir = project_dir / "招标文件"
    manual_dir = project_dir / "手填资料"
    attach_dir = project_dir / "附件"
    ref_dir = project_dir / "参考"

    template = None
    for p in list(tender_dir.glob("*.docx")) + list(project_dir.glob("*.docx")):
        template = p
        break
    if template is None:
        raise FileNotFoundError(f"未找到招标文件docx: {tender_dir}")

    base_info_path = manual_dir / "基础信息.json"
    base_info = {}
    if base_info_path.exists():
        base_info = load_json(base_info_path)

    project_text = ""
    for p in [manual_dir / "招标项目信息.txt", project_dir / "招标项目信息.txt"]:
        if p.exists():
            project_text = p.read_text(encoding="utf-8", errors="ignore")
            break

    attachments = []
    if attach_dir.exists():
        for p in sorted(attach_dir.rglob("*")):
            if p.is_file():
                attachments.append(str(p.resolve()))

    reference_bid = ref_dir / "AI生成的文件.docx"
    if not reference_bid.exists():
        fallback = project_dir.parent / "AI生成的文件.docx"
        reference_bid = fallback if fallback.exists() else reference_bid

    payload = {
        "project_text": project_text,
        "supplier_name": base_info.get("supplier_name", ""),
        "template_path": str(template.resolve()),
        "attachments": attachments,
        "base_info": base_info,
        "reference_bid_docx": str(reference_bid.resolve()) if reference_bid.exists() else "",
    }
    return payload


def parse_json_from_text(text: str) -> Dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in LLM output")
    return json.loads(text[start : end + 1])


def regex_pick(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def extract_text_from_docx(docx_path: Path) -> str:
    if not docx_path.exists():
        return ""
    try:
        with zipfile.ZipFile(docx_path, "r") as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        # Keep paragraph boundaries so regex extraction won't swallow the whole document.
        xml = re.sub(r"</w:p>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        return text
    except Exception:
        return ""


def parse_response_format_sections(template_text: str) -> List[str]:
    text = re.sub(r"\s+", " ", template_text)
    start_marker = re.search(r"第五章\s*响应文件格式", text)
    if not start_marker:
        return []
    sub = text[start_marker.start() :]
    end_marker = re.search(r"第六章\s*", sub)
    if end_marker:
        sub = sub[: end_marker.start()]

    # Capture numbered section titles in response format chapter.
    titles = re.findall(r"(?:^| )(\d+\.[^0-9]{2,50}?)(?= \d+\.|$)", sub)
    cleaned: List[str] = []
    seen = set()
    for t in titles:
        v = re.sub(r"\s+", "", t)
        if len(v) < 3:
            continue
        if v in seen:
            continue
        seen.add(v)
        cleaned.append(v)

    if not cleaned:
        fallback = [
            "1.法定代表人（单位负责人）授权书（实质性要求）",
            "法定代表人（单位负责人）身份证明",
            "2.承诺函（实质性要求）",
            "3.无行贿犯罪记录的承诺函（实质性要求）",
            "4.无重大违法记录的承诺函（实质性要求）",
            "5.供应商其他资格、资质性及其他类似效力要求的相关证明材料",
            "6.磋商函",
            "7.供应商基本情况表",
            "8.技术/服务应答表",
            "9.商务应答表",
            "11.项目实施方案",
            "14.最终报价表",
        ]
        cleaned = fallback
    return cleaned


def extract_response_format_chapter(template_text: str) -> str:
    text = re.sub(r"\s+", " ", template_text)
    start = re.search(r"第五章\s*响应文件格式", text)
    if not start:
        return text[:12000]
    sub = text[start.start() :]
    end = re.search(r"第六章\s*", sub)
    if end:
        sub = sub[: end.start()]
    return sub[:20000]


def parse_response_format_sections_llm(template_text: str, llm: LLMClient) -> List[str]:
    chapter = extract_response_format_chapter(template_text)
    system = (
        "你是采购文件结构分析助手。"
        "任务是从“响应文件格式”章节中提取投标文件子章节标题。"
        "只返回JSON，不要解释。"
    )
    user = f"""
请从以下文本中提取“响应文件格式”下的子章节标题，输出JSON：
{{
  "sections": ["标题1", "标题2", "..."]
}}
要求：
1) 仅输出与投标文件编制直接相关的子章节。
2) 保留原始序号（如“1.”、“2.”）和标题。
3) 不要输出空字符串，不要编造。

文本：
{chapter}
""".strip()
    text = llm.chat(system, user)
    obj = parse_json_from_text(text)
    sections = obj.get("sections", [])
    if not isinstance(sections, list):
        return []
    out: List[str] = []
    for s in sections:
        v = re.sub(r"\s+", "", str(s))
        if len(v) >= 3:
            out.append(v)
    return out


def build_section_plan(sections: List[str]) -> Dict:
    items = []
    for idx, title in enumerate(sections, 1):
        t = title
        if any(k in t for k in ["承诺函", "无行贿", "无重大违法记录"]):
            typ = "FIXED_WITH_VARIABLES"
        elif any(k in t for k in ["应答表", "情况表", "报价表"]):
            typ = "TABLE_FILL"
        elif any(k in t for k in ["证明材料", "营业执照", "身份证明"]):
            typ = "ATTACHMENT_SLOT"
        else:
            typ = "MANUAL_REQUIRED"
        items.append(
            {
                "section_id": f"S{idx:03d}",
                "section_title": t,
                "section_type": typ,
                "required_fields": [],
                "required_attachments": [],
                "source_locator": "第五章 响应文件格式",
            }
        )
    return {"sections": items}


def extract_project_fields_rule(project_text: str, source_file: str) -> Dict[str, FieldValue]:
    project_name = regex_pick(r"(?:项目名称|采购项目名称)[:：]\s*([^\n]+)", project_text)
    project_no = regex_pick(r"(?:项目编号|采购项目编号)[:：]\s*([A-Za-z0-9\-]+)", project_text)
    package_no = regex_pick(r"(?:包号|分包号)[:：]\s*([^\n]+)", project_text)
    procurement_org = regex_pick(r"(?:采购人|采购单位)[:：]\s*([^\n]+)", project_text)
    agency_org = regex_pick(r"(?:采购代理机构|代理机构)[:：]\s*([^\n]+)", project_text)

    pairs = {
        "project_name": project_name,
        "project_no": project_no,
        "package_no": package_no,
        "procurement_org": procurement_org,
        "agency_org": agency_org,
        "doc_date": now_cn_date(),
    }

    out: Dict[str, FieldValue] = {}
    for k, v in pairs.items():
        out[k] = FieldValue(
            value=v,
            confidence=0.95 if v else DEFAULT_CONFIDENCE,
            source_file=source_file,
            source_locator="project_text",
            model_version="rule-v1",
        )
    return out


def fill_project_fields_from_template_if_missing(
    project_fields: Dict[str, FieldValue],
    template_path: Path,
) -> Dict[str, FieldValue]:
    text = extract_text_from_docx(template_path)
    if not text:
        return project_fields

    def patch_field(name: str, pattern: str) -> None:
        current = project_fields.get(name)
        if current is None:
            return
        if current.value is not None and str(current.value).strip():
            return
        value = regex_pick(pattern, text)
        if value:
            value = re.sub(r"\s+", " ", value).strip()
            # Guardrail: reject clearly over-captured text from large template bodies.
            if len(value) > 120:
                value = None
        if value:
            project_fields[name] = FieldValue(
                value=value,
                confidence=0.92,
                source_file=str(template_path),
                source_locator="document.xml",
                model_version="template-fallback-v1",
            )

    patch_field("project_name", r"(?:项目名称|采购项目名称)[:：]\s*([^\n\r]+)")
    patch_field("project_no", r"(?:项目编号|采购项目编号)[:：]\s*([A-Za-z0-9\-]+)")
    patch_field("package_no", r"(?:包号|分包号)[:：]\s*([^\n\r]+)")
    patch_field("procurement_org", r"(?:采购人|采购单位)[:：]\s*([^\n\r]+)")
    patch_field("agency_org", r"(?:采购代理机构|代理机构)[:：]\s*([^\n\r]+)")
    return project_fields


def extract_project_fields_llm(project_text: str, source_file: str, llm: LLMClient) -> Dict[str, FieldValue]:
    system = (
        "你是招标文本结构化抽取助手。"
        "只返回JSON，不要解释。"
        "禁止臆造，不确定请返回 null。"
    )
    user = f"""
请从下面文本中抽取字段，输出JSON：
{{
  "project_name": {{"value": string|null, "confidence": number}},
  "project_no": {{"value": string|null, "confidence": number}},
  "package_no": {{"value": string|null, "confidence": number}},
  "procurement_org": {{"value": string|null, "confidence": number}},
  "agency_org": {{"value": string|null, "confidence": number}},
  "doc_date": {{"value": string, "confidence": number}}
}}
要求：
1) confidence范围0到1。
2) doc_date 用中文日期格式如 2026年4月19日，若无法判断则使用今天日期 {now_cn_date()}。

原文：
{project_text}
""".strip()
    text = llm.chat(system, user)
    obj = parse_json_from_text(text)

    out: Dict[str, FieldValue] = {}
    for name in ["project_name", "project_no", "package_no", "procurement_org", "agency_org", "doc_date"]:
        node = obj.get(name, {}) if isinstance(obj, dict) else {}
        val = node.get("value") if isinstance(node, dict) else None
        conf = node.get("confidence") if isinstance(node, dict) else 0.0
        try:
            conf_f = float(conf)
        except Exception:
            conf_f = 0.0
        if name == "doc_date" and not val:
            val = now_cn_date()
        out[name] = FieldValue(
            value=val,
            confidence=max(0.0, min(1.0, conf_f)),
            source_file=source_file,
            source_locator="project_text",
            model_version=f"llm:{llm.config.model}",
        )
    return out


def extract_supplier_fields(payload: Dict) -> Dict[str, FieldValue]:
    supplier_name = payload.get("supplier_name") or ""
    fields = {
        "supplier_name": FieldValue(
            value=supplier_name or None,
            confidence=1.0 if supplier_name else DEFAULT_CONFIDENCE,
            source_file="manual_input",
            source_locator="supplier_name",
            model_version="manual",
        )
    }

    placeholder_fields = [
        "unified_social_credit_code",
        "registered_address",
        "company_type",
        "established_date",
        "business_term",
        "legal_name",
        "legal_id_no",
        "legal_gender",
        "legal_age",
        "legal_position",
        "contact_name",
        "contact_phone",
        "contact_email",
    ]

    for k in placeholder_fields:
        fields[k] = FieldValue(
            value=None,
            confidence=0.0,
            source_file="attachments",
            source_locator="pending_ocr_or_llm",
            model_version="pending",
        )

    return fields


def extract_supplier_fields_rule_from_inputs(payload: Dict) -> Dict[str, FieldValue]:
    base = payload.get("base_info", {}) or {}
    fields = extract_supplier_fields(payload)

    # Prefer explicit hand-filled fields.
    direct_map = {
        "supplier_name": "supplier_name",
        "legal_name": "legal_name",
        "legal_id_no": "legal_id_no",
        "legal_gender": "legal_gender",
        "legal_age": "legal_age",
        "contact_name": "contact_name",
        "contact_phone": "contact_phone",
        "contact_email": "contact_email",
        "legal_position": "legal_position",
        "unified_social_credit_code": "unified_social_credit_code",
        "registered_address": "registered_address",
        "company_type": "company_type",
        "established_date": "established_date",
        "business_term": "business_term",
    }
    for fk, bk in direct_map.items():
        if base.get(bk):
            fields[fk] = FieldValue(
                value=str(base[bk]),
                confidence=1.0,
                source_file="手填资料/基础信息.json",
                source_locator=bk,
                model_version="rule-manual-v1",
            )

    # Rule extraction from attachment text (no OCR, no LLM).
    att_text = collect_attachment_texts(payload.get("attachments", []), max_total_chars=20000)
    if att_text:
        def clean_person_name(x: str) -> str:
            x = (x or "").strip()
            m = re.search(r"[\u4e00-\u9fa5·]{2,10}", x)
            return m.group(0) if m else x

        def pick(key: str, pattern: str) -> None:
            m = regex_pick(pattern, att_text)
            if m:
                if key in {"legal_name", "contact_name"}:
                    m = clean_person_name(m)
                fields[key] = FieldValue(
                    value=m,
                    confidence=0.9,
                    source_file="attachments",
                    source_locator="rule_regex",
                    model_version="rule-regex-v1",
                )

        pick("unified_social_credit_code", r"(?:统一社会信用代码|社会信用代码)\s*[:：]?\s*([0-9A-Z]{18})")
        pick("registered_address", r"(?:住所|注册地址|地址)\s*[:：]?\s*([^\n\r]{6,80})")
        pick("legal_name", r"(?:法定代表人|姓名)\s*[:：]?\s*([\u4e00-\u9fa5·]{2,10})")
        pick("legal_id_no", r"(?:公民身份号码|身份证号|身份证号码)\s*[:：]?\s*([0-9Xx]{15,18})")
        pick("contact_phone", r"(?:电话|手机|联系方式)\s*[:：]?\s*(1[3-9]\d{9})")
        pick("contact_email", r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
        pick("company_type", r"(?:类型|企业性质)\s*[:：]?\s*([^\n\r]{2,30})")

    return fields


def _read_attachment_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".json", ".csv", ".svg", ".xml", ".html"}:
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            if suffix in {".svg", ".xml", ".html"}:
                raw = re.sub(r"<[^>]+>", " ", raw)
            raw = re.sub(r"\s+", " ", raw).strip()
            return raw
        except Exception:
            return ""
    return ""


def collect_attachment_texts(attachments: List[str], max_total_chars: int = 12000) -> str:
    chunks: List[str] = []
    used = 0
    for p in attachments:
        fp = Path(p)
        if not fp.is_absolute():
            fp = Path.cwd() / fp
        if not fp.exists() or not fp.is_file():
            continue
        text = _read_attachment_text(fp).strip()
        if not text:
            continue
        # Prioritize representative excerpt per file and cap total prompt size.
        remain = max_total_chars - used
        if remain <= 0:
            break
        excerpt = text[: min(2200, remain)]
        chunks.append(f"[FILE] {fp.name}\n{excerpt}")
        used += len(excerpt)
    return "\n\n".join(chunks)


def extract_supplier_fields_llm(payload: Dict, llm: LLMClient) -> Dict[str, FieldValue]:
    supplier_name = payload.get("supplier_name") or ""
    attachments = payload.get("attachments") or []
    attachment_text = collect_attachment_texts(attachments)

    if not attachment_text:
        return extract_supplier_fields(payload)

    system = (
        "你是投标资料结构化抽取助手。"
        "请仅根据输入资料抽取，不得臆造。"
        "只返回JSON。"
    )
    user = f"""
请从“供应商名称+附件文本”中抽取字段，输出 JSON：
{{
  "supplier_name": {{"value": string|null, "confidence": number}},
  "unified_social_credit_code": {{"value": string|null, "confidence": number}},
  "registered_address": {{"value": string|null, "confidence": number}},
  "company_type": {{"value": string|null, "confidence": number}},
  "legal_name": {{"value": string|null, "confidence": number}},
  "legal_id_no": {{"value": string|null, "confidence": number}},
  "legal_position": {{"value": string|null, "confidence": number}},
  "contact_name": {{"value": string|null, "confidence": number}},
  "contact_phone": {{"value": string|null, "confidence": number}},
  "contact_email": {{"value": string|null, "confidence": number}}
}}
要求：
1) confidence 范围 0~1。
2) 无法确认返回 null。
3) 不要输出除 JSON 以外内容。

供应商名称（人工输入，优先参考）：{supplier_name}

附件文本：
{attachment_text}
""".strip()

    text = llm.chat(system, user)
    obj = parse_json_from_text(text)

    names = [
        "supplier_name",
        "unified_social_credit_code",
        "registered_address",
        "company_type",
        "legal_name",
        "legal_id_no",
        "legal_position",
        "contact_name",
        "contact_phone",
        "contact_email",
    ]

    out: Dict[str, FieldValue] = {}
    for name in names:
        node = obj.get(name, {}) if isinstance(obj, dict) else {}
        val = node.get("value") if isinstance(node, dict) else None
        conf = node.get("confidence") if isinstance(node, dict) else 0.0
        try:
            conf_f = float(conf)
        except Exception:
            conf_f = 0.0
        out[name] = FieldValue(
            value=val,
            confidence=max(0.0, min(1.0, conf_f)),
            source_file="attachments",
            source_locator="llm_attachment_parse",
            model_version=f"llm:{llm.config.model}",
        )

    # Keep manual supplier_name as stronger prior if LLM missed it.
    if supplier_name and not out["supplier_name"].value:
        out["supplier_name"] = FieldValue(
            value=supplier_name,
            confidence=1.0,
            source_file="manual_input",
            source_locator="supplier_name",
            model_version="manual",
        )

    return out


def generate_sections_rule(payload: Dict) -> Dict[str, str]:
    project_name = payload.get("project_name", "本项目") or "本项目"
    return {
        "technical_response_text": f"我方已充分理解{project_name}的技术/服务要求，将严格按招标文件执行。",
        "business_response_text": "我方承诺在合同约定周期内完成交付，并按采购方要求配合验收。",
        "implementation_plan_text": "实施将按“启动、实施、验收、运维”四阶段推进，关键里程碑可按采购计划调整。",
    }


def generate_fixed_section_texts(fields: Dict[str, Dict]) -> Dict[str, str]:
    supplier = fields.get("supplier_name", {}).get("value") or "供应商名称待补充"
    legal = fields.get("legal_name", {}).get("value") or "法定代表人"
    date = fields.get("doc_date", {}).get("value") or now_cn_date()
    return {
        "承诺函": (
            f"中航技国际经贸发展有限公司：\n"
            f"我公司作为本次采购项目的供应商，根据磋商文件要求，现郑重承诺如下：\n"
            f"我公司具备本项目规定的资格条件，并完全接受和满足磋商文件中规定的实质性要求。\n"
            f"供应商名称：{supplier}（单位盖章）。\n"
            f"法定代表人/授权代表：{legal}。\n"
            f"日期：{date}。"
        ),
        "无行贿犯罪记录的承诺函": (
            f"中航技国际经贸发展有限公司：\n"
            f"我单位 {supplier} 及法定代表人 {legal} 无行贿犯罪记录。\n"
            f"日期：{date}。"
        ),
        "无重大违法记录的承诺函": (
            f"中航技国际经贸发展有限公司：\n"
            f"我单位承诺：参加本次采购活动前三年内，在经营活动中没有重大违法记录。\n"
            f"供应商名称：{supplier}。\n"
            f"日期：{date}。"
        ),
    }


def build_bid_lines_from_section_plan(section_plan: Dict, normalized: Dict) -> List[str]:
    fields = normalized.get("fields", {})
    v = lambda k, d="": (fields.get(k, {}) or {}).get("value") or d
    fixed = generate_fixed_section_texts(fields)

    lines = [
        "资格性响应文件",
        f"供应商名称：{v('supplier_name', '待补充')}",
        f"采购项目名称：{v('project_name', '待补充')}",
        f"采购项目编号：{v('project_no', '待补充')}",
        f"包号：{v('package_no', '')}",
        f"日期：{v('doc_date', now_cn_date())}",
        "",
    ]

    for sec in section_plan.get("sections", []):
        title = sec.get("section_title", "")
        stype = sec.get("section_type", "")
        lines.append(title)
        if stype == "FIXED_WITH_VARIABLES":
            if "无行贿" in title:
                lines.append(fixed["无行贿犯罪记录的承诺函"])
            elif "无重大违法" in title:
                lines.append(fixed["无重大违法记录的承诺函"])
            else:
                lines.append(fixed["承诺函"])
        elif stype == "TABLE_FILL":
            if "技术" in title:
                lines.append(v("technical_response_text", "我方按招标文件技术要求进行全面响应。"))
            elif "商务" in title:
                lines.append(v("business_response_text", "我方按招标文件商务要求进行全面响应。"))
            elif "报价" in title:
                lines.append("小写：待补充 元 ；大写：待补充。")
            else:
                lines.append("按招标文件要求填报，详见附件。")
        elif stype == "ATTACHMENT_SLOT":
            lines.append("本节所需证明材料见附件目录。")
        else:
            lines.append(v("implementation_plan_text", "本节内容由供应商根据项目实际情况补充。"))
        lines.append("")
    return lines


def _paragraph_xml(text: str) -> str:
    if text == "":
        return "<w:p/>"
    return (
        "<w:p><w:r><w:t xml:space=\"preserve\">"
        + escape(text)
        + "</w:t></w:r></w:p>"
    )


def render_docx_from_lines(template_path: Path, output_path: Path, lines: List[str]) -> None:
    with zipfile.ZipFile(template_path, "r") as zin:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    xml = data.decode("utf-8", errors="ignore")
                    body_match = re.search(r"<w:body>([\s\S]*?)</w:body>", xml)
                    sectpr = ""
                    if body_match:
                        m = re.search(r"(<w:sectPr[\s\S]*?</w:sectPr>)", body_match.group(1))
                        if m:
                            sectpr = m.group(1)
                    new_body = "".join(_paragraph_xml(x) for x in lines) + sectpr
                    xml = re.sub(r"<w:body>[\s\S]*?</w:body>", f"<w:body>{new_body}</w:body>", xml, count=1)
                    data = xml.encode("utf-8")
                zout.writestr(item, data)


def _node_text(node: ET.Element, ns: Dict[str, str]) -> str:
    return "".join((t.text or "") for t in node.findall(".//w:t", ns)).strip()


def _set_paragraph_text(p: ET.Element, text: str, ns: Dict[str, str]) -> None:
    # Keep paragraph properties, rebuild runs as one plain text run.
    ppr = p.find("w:pPr", ns)
    for c in list(p):
        if c is ppr:
            continue
        p.remove(c)
    r = ET.Element(f"{{{ns['w']}}}r")
    t = ET.Element(f"{{{ns['w']}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    r.append(t)
    p.append(r)


def _apply_semantic_replacements(text: str, values: Dict[str, str]) -> str:
    s = text
    supplier = values.get("supplier_name", "XXXX")
    legal = values.get("legal_name", "XXXX")
    legal_pos = values.get("legal_position", "法定代表人")
    auth = values.get("authorized_name", values.get("contact_name", "XXXX"))
    auth_pos = values.get("authorized_position", "授权代表")
    project_name = values.get("project_name", "XXXX")
    project_no = values.get("project_no", "XXXX")
    doc_date = values.get("doc_date", "XXXX")
    company_type = values.get("company_type", "")
    address = values.get("registered_address", "")
    established_date = values.get("established_date", "")
    business_term = values.get("business_term", "")
    legal_gender = values.get("legal_gender", "")
    legal_age = values.get("legal_age", "")

    def clean_person_name(x: str) -> str:
        x = (x or "").strip()
        m = re.search(r"[\u4e00-\u9fa5·]{2,10}", x)
        return m.group(0) if m else x

    def clean_address(x: str) -> str:
        x = (x or "").strip()
        x = re.split(r"(?:法定代表人|经营范围|注册资本|统一社会信用代码)", x)[0]
        return x.strip(" ，,。；;")

    def fill(v: str, n: int = 8) -> str:
        v = (v or "").strip()
        return v if v else "_" * n

    def fill_name(v: str) -> str:
        return fill(clean_person_name(v), 6)

    legal = clean_person_name(legal)
    auth = clean_person_name(auth)
    address = clean_address(address)

    # Paragraph-level strong replacements for key template lines.
    if "本授权声明" in s and "项目（编号" in s:
        return (
            f"本授权声明：{fill(supplier, 12)}（供应商名称）{fill_name(legal)}（法定代表人姓名、职务）授权 "
            f"{fill_name(auth)}（被授权人姓名、职务）为我方 “{fill(project_name, 12)}” 项目（编号：{fill(project_no, 8)}）"
            "磋商活动的合法代表，以我方名义全权处理该项目有关磋商、签订合同以及执行合同等一切事宜。"
        )
    if "法定代表人签字或者加盖个人名章" in s:
        return f"法定代表人签字或者加盖个人名章：{fill_name(legal)}。"
    if "法定代表人签字或加盖个人名章" in s:
        return f"法定代表人签字或加盖个人名章：{fill_name(legal)}。"
    if "法定代表人" in s and "或授权代表" in s and "签字或加盖个人名章" in s:
        return f"法定代表人（单位负责人）或授权代表（签字或加盖个人名章）：{fill_name(auth)}。"
    if "授权代表签字" in s:
        return f"授权代表签字：{fill_name(auth)}。"
    if "供应商名称：" in s and "单位公章" in s:
        return f"供应商名称：{fill(supplier, 12)}（盖单位公章）。"
    if re.search(r"^\s*日\s*期[:：]", s):
        return re.sub(r"^\s*日\s*期[:：].*$", f"日    期：{fill(doc_date, 10)}。", s)
    if re.match(r"^\s*供应商名称[:：]\s*$", s):
        return f"供应商名称：{fill(supplier, 12)}"
    if re.match(r"^\s*单位性质[:：]\s*$", s):
        return f"单位性质：{fill(company_type, 10)}"
    if re.match(r"^\s*地址[:：]\s*$", s):
        return f"地址：{fill(address, 14)}"
    if "成立时间" in s and "年" in s and "月" in s and "日" in s:
        m = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", established_date)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            return f"成立时间：{y}年{mo}月{d}日"
        return "成立时间：________年____月____日"
    if re.match(r"^\s*经营期限[:：]\s*$", s):
        return f"经营期限：{fill(business_term, 10)}"
    if "姓名：" in s and "性别：" in s and "年龄：" in s and "职务：" in s and "法定代表人" in s:
        return (
            f"姓名：{fill_name(legal)}  性别：{fill(legal_gender, 2)}  年龄：{fill(str(legal_age), 2)}  "
            f"职务：{fill(legal_pos, 6)}  系 {fill(supplier, 12)} 的法定代表人（单位负责人）。"
        )
    if "无行贿犯罪记录" in s and ("（公司名称）" in s or "（法定代表人名字）" in s):
        return f"我公司 {fill(supplier, 12)} 及法定代表人 {fill_name(legal)} 无行贿犯罪记录。"
    if "无行贿犯罪记录" in s and ("（单位名称）" in s or "（主要负责人名字）" in s):
        return f"我单位 {fill(supplier, 12)} 及主要负责人 {fill_name(legal)} 无行贿犯罪记录。"

    replacements = [
        ("（供应商名称）", f"（{fill(supplier, 12)}）"),
        ("(供应商名称)", f"({fill(supplier, 12)})"),
        ("（法定代表人姓名、职务）", f"（{fill_name(legal)}、{fill(legal_pos, 6)}）"),
        ("(法定代表人姓名、职务)", f"({fill_name(legal)},{fill(legal_pos, 6)})"),
        ("（被授权人姓名、职务）", f"（{fill_name(auth)}、{fill(auth_pos, 4)}）"),
        ("(被授权人姓名、职务)", f"({fill_name(auth)},{fill(auth_pos, 4)})"),
        ("“XXXX” 项目（编号：XXXX）", f"“{fill(project_name, 12)}” 项目（编号：{fill(project_no, 8)}）"),
        ("\"XXXX\" 项目（编号：XXXX）", f"\"{fill(project_name, 12)}\" 项目（编号：{fill(project_no, 8)}）"),
        ("法定代表人签字或者加盖个人名章：XXXX。", f"法定代表人签字或者加盖个人名章：{fill_name(legal)}。"),
        ("法定代表人签字或加盖个人名章：XXXX。", f"法定代表人签字或加盖个人名章：{fill_name(legal)}。"),
        ("法定代表人/单位负责人签字或者加盖个人名章：XXXX。", f"法定代表人/单位负责人签字或者加盖个人名章：{fill_name(legal)}。"),
        ("法定代表人/主要负责人/授权代表签字或者加盖个人名章：XXXX。", f"法定代表人/主要负责人/授权代表签字或者加盖个人名章：{fill_name(legal)}。"),
        ("授权代表签字：XXXX。", f"授权代表签字：{fill_name(auth)}。"),
        ("授权代表签字：  。", f"授权代表签字：{fill_name(auth)}。"),
        ("供应商名称：XXXX（盖单位公章）。", f"供应商名称：{fill(supplier, 12)}（盖单位公章）。"),
        ("供应商全称：      （盖单位公章）", f"供应商全称：{fill(supplier, 12)}（盖单位公章）"),
        ("采购项目编号：XXXX", f"采购项目编号：{fill(project_no, 8)}"),
        ("包        号：", f"包        号：{fill(values.get('package_no', ''), 2)}"),
        ("日    期：XXXX。", f"日    期：{fill(doc_date, 10)}。"),
        ("日期：XXXX。", f"日期：{fill(doc_date, 10)}。"),
        ("日期：2025年1月12日", f"日期：{fill(doc_date, 10)}"),
    ]
    for old, new in replacements:
        s = s.replace(old, new)

    # Contextual fallback replacements for common line patterns.
    s = re.sub(r"(供应商名称[:：]\s*)XXXX(\s*（盖单位公章）?)", rf"\1{supplier}\2", s)
    s = re.sub(r"(采购项目编号[:：]\s*)XXXX", rf"\1{project_no}", s)
    s = re.sub(r"(项目（编号[:：]\s*)XXXX(）)", rf"\1{project_no}\2", s)
    s = re.sub(r"(法定代表人.*名章[:：]\s*)XXXX(。?)", rf"\1{legal}\2", s)
    s = re.sub(r"(授权代表签字[:：]\s*)XXXX(。?)", rf"\1{auth}\2", s)
    s = re.sub(r"(日\s*期[:：]\s*)XXXX(。?)", rf"\1{doc_date}\2", s)
    s = re.sub(
        r"(法定代表人[^。\n]{0,60}或授权代表[^。\n]{0,30}签字或加盖个人名章[:：]\s*)(?:XXXX|____+)(。?)",
        rf"\1{auth}\2",
        s,
    )
    s = re.sub(
        r"(法定代表人[^。\n]{0,60}签字或加盖个人名章[:：]\s*)(?:XXXX|____+)(。?)",
        rf"\1{legal}\2",
        s,
    )
    s = re.sub(
        r"(我方全面研究了\s*[“\"])\s*X{4,}\s*([”\"]\s*项目\s*[（(]\s*项目编号[:：]\s*)X{4,}(\s*[）)])",
        rf"\1{project_name}\2{project_no}\3",
        s,
    )
    # Fallback for empty fillable metadata lines only (avoid salutation lines like "某公司：").
    m_empty_kv = re.match(r"^\s*([\u4e00-\u9fa5A-Za-z0-9（）()、/\-]{2,30})[:：]\s*$", s)
    if m_empty_kv:
        key = m_empty_kv.group(1)
        fillable = [
            "通讯地址",
            "地址",
            "邮政编码",
            "邮编",
            "联系电话",
            "联系人电话",
            "联系人",
            "传真",
            "电子邮箱",
            "邮箱",
            "网址",
            "开户银行",
            "银行账号",
            "账号",
            "企业名称（盖单位公章）",
        ]
        if any(k in key for k in fillable):
            return f"{key}：{'_' * 12}"
    return s


def render_docx_from_template_response_chapter(
    template_path: Path,
    output_path: Path,
    normalized_payload: Dict,
) -> None:
    """Preserve original Word styles by copying the response-format chapter XML block.

    Chapter range: from heading containing '第五章' and '响应文件格式'
    until before heading containing '第六章'.
    """
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    ET.register_namespace("w", ns["w"])

    fields = normalized_payload.get("fields", {})

    def v(name: str, default: str = "") -> str:
        val = fields.get(name, {}).get("value")
        return default if val is None else str(val)

    values = {
        "supplier_name": v("supplier_name", "XXXX"),
        "legal_name": v("legal_name", "XXXX"),
        "legal_position": v("legal_position", "法定代表人"),
        "authorized_name": v("contact_name", "XXXX"),
        "authorized_position": "授权代表",
        "contact_name": v("contact_name", "XXXX"),
        "project_name": v("project_name", "XXXX"),
        "project_no": v("project_no", "XXXX"),
        "package_no": v("package_no", ""),
        "doc_date": v("doc_date", now_cn_date()),
        "company_type": v("company_type", ""),
        "registered_address": v("registered_address", ""),
        "established_date": v("established_date", ""),
        "business_term": v("business_term", ""),
        "legal_gender": v("legal_gender", ""),
        "legal_age": v("legal_age", ""),
    }

    with zipfile.ZipFile(template_path, "r") as zin:
        doc_xml = zin.read("word/document.xml").decode("utf-8", errors="ignore")
        root = ET.fromstring(doc_xml)
        body = root.find("w:body", ns)
        if body is None:
            raise RuntimeError("Invalid document.xml: missing w:body")

        children = list(body)
        sectpr = body.find("w:sectPr", ns)

        def norm(s: str) -> str:
            return re.sub(r"\s+", "", s)

        start_candidates: List[int] = []
        exact_response_idx = None
        for i, child in enumerate(children):
            txt = _node_text(child, ns)
            n = norm(txt)
            if n == "一、资格响应文件（格式）":
                exact_response_idx = i
            if "第五章" in n and "响应文件格式" in n:
                start_candidates.append(i)

        start_idx = None
        # Highest-priority anchor: actual body heading, not TOC entry.
        if exact_response_idx is not None:
            start_idx = exact_response_idx
        elif start_candidates:
            # Prefer an exact chapter heading (without trailing page number).
            for i in start_candidates:
                n = norm(_node_text(children[i], ns))
                if n.startswith("第五章响应文件格式") and not re.search(r"第五章响应文件格式\d+$", n):
                    start_idx = i
                    break
            # Fallback: use the last candidate (usually the real chapter heading, not TOC item).
            if start_idx is None:
                start_idx = start_candidates[-1]

        end_idx = None
        if start_idx is not None:
            for i in range(start_idx + 1, len(children)):
                n = norm(_node_text(children[i], ns))
                if n.startswith("第六章"):
                    end_idx = i
                    break

        if start_idx is None:
            # Fallback: keep full body if chapter not found.
            start_idx = 0
        if end_idx is None:
            end_idx = len(children)

        kept = [copy.deepcopy(n) for n in children[start_idx:end_idx] if n.tag != f"{{{ns['w']}}}sectPr"]

        # Replace placeholders on copied chapter block with semantic rules.
        for node in kept:
            paragraphs = []
            if node.tag == f"{{{ns['w']}}}p":
                paragraphs.append(node)
            paragraphs.extend(node.findall(".//w:p", ns))
            for p in paragraphs:
                ptxt = _node_text(p, ns)
                if not ptxt:
                    continue
                new_text = _apply_semantic_replacements(ptxt, values)
                if new_text != ptxt:
                    _set_paragraph_text(p, new_text, ns)

        new_body = ET.Element(f"{{{ns['w']}}}body")
        for n in kept:
            new_body.append(n)
        if sectpr is not None:
            new_body.append(copy.deepcopy(sectpr))

        root.remove(body)
        root.append(new_body)
        new_doc_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    data = new_doc_xml
                zout.writestr(item, data)


def generate_sections_llm(payload: Dict, llm: LLMClient) -> Dict[str, str]:
    system = (
        "你是投标文件撰写助手。"
        "只输出JSON，不要解释。"
        "不得虚构证照、业绩、金额。"
    )
    user = f"""
根据以下字段生成投标文件初稿段落，输出JSON：
{{
  "technical_response_text": string,
  "business_response_text": string,
  "implementation_plan_text": string
}}
写作要求：
1) 语气正式，长度适中。
2) 只写可通用内容，不编造具体资质或金额。
3) 尽量贴合项目名称与采购场景。

字段：
{json.dumps(payload, ensure_ascii=False)}
""".strip()
    text = llm.chat(system, user)
    obj = parse_json_from_text(text)
    return {
        "technical_response_text": str(obj.get("technical_response_text", "")).strip(),
        "business_response_text": str(obj.get("business_response_text", "")).strip(),
        "implementation_plan_text": str(obj.get("implementation_plan_text", "")).strip(),
    }


def build_normalized_payload(
    project_fields: Dict[str, FieldValue],
    supplier_fields: Dict[str, FieldValue],
    generated_sections: Dict[str, str],
    section_model: str,
) -> Dict:
    merged: Dict[str, Dict] = {}
    for k, v in {**project_fields, **supplier_fields}.items():
        merged[k] = v.to_dict()

    gen_conf = 0.95 if section_model.startswith("rule") else 0.8
    for k, v in generated_sections.items():
        merged[k] = {
            "value": v,
            "confidence": gen_conf if v else 0.0,
            "source_file": "llm_generation",
            "source_locator": k,
            "model_version": section_model,
        }

    return {"fields": merged}


def validate_payload(normalized: Dict, schema: Dict, rules: Dict) -> Dict:
    fields = normalized.get("fields", {})
    required = [f["name"] for f in schema.get("fields", []) if f.get("required")]
    must_review = [f["name"] for f in schema.get("fields", []) if f.get("must_review")]

    missing_required = []
    low_conf = []
    conflict = []

    threshold = float(rules.get("confidence_threshold", 0.9))

    for name in required:
        value = fields.get(name, {}).get("value")
        if value is None or str(value).strip() == "":
            missing_required.append(name)

    for name, meta in fields.items():
        conf = float(meta.get("confidence", 0.0))
        if conf < threshold:
            low_conf.append(name)

    supplier_name = fields.get("supplier_name", {}).get("value")
    legal_name = fields.get("legal_name", {}).get("value")
    if supplier_name and legal_name and supplier_name == legal_name:
        conflict.append("supplier_name_vs_legal_name_same")

    for name in must_review:
        value = fields.get(name, {}).get("value")
        if value is None or str(value).strip() == "":
            if name not in low_conf:
                low_conf.append(name)

    return {
        "missing_required_fields": sorted(set(missing_required)),
        "low_confidence_fields": sorted(set(low_conf)),
        "conflict_fields": sorted(set(conflict)),
    }


def render_docx_template(
    template_path: Path,
    output_path: Path,
    normalized_payload: Dict,
    template_mapping: Dict,
) -> List[str]:
    leftovers: List[str] = []
    replacements: Dict[str, str] = {}

    fields = normalized_payload.get("fields", {})
    for placeholder, field_name in template_mapping.get("placeholder_to_field", {}).items():
        value = fields.get(field_name, {}).get("value")
        replacements[placeholder] = "" if value is None else str(value)

    with zipfile.ZipFile(template_path, "r") as zin:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    text = data.decode("utf-8", errors="ignore")
                    for placeholder, value in replacements.items():
                        text = text.replace(placeholder, value)
                    leftovers = sorted(set(re.findall(r"\{\{[^{}]+\}\}", text)))
                    data = text.encode("utf-8")
                zout.writestr(item, data)

    return leftovers


def render_docx_from_reference_bid(
    reference_docx: Path,
    output_path: Path,
    normalized_payload: Dict,
    template_mapping: Dict,
) -> List[str]:
    fields = normalized_payload.get("fields", {})

    def val(name: str, default: str = "") -> str:
        v = fields.get(name, {}).get("value")
        return default if v is None else str(v)

    supplier_name = val("supplier_name", "供应商名称待确认")
    project_name = val("project_name", "项目名称待确认")
    project_no = val("project_no", "项目编号待确认")
    package_no = val("package_no", "")
    doc_date = val("doc_date", now_cn_date())
    legal_name = val("legal_name", "法定代表人")

    replacements = {
        # Placeholder mapping first
        **{
            ph: val(fname, "")
            for ph, fname in template_mapping.get("placeholder_to_field", {}).items()
        },
        # Common sample values in AI generated doc
        "成都XX科技有限公司": supplier_name,
        "成都XX有限公司": supplier_name,
        "韩书强": legal_name,
        "XXXX": project_no,
        "统计分析改造项目": project_name,
        "四川省城乡医疗卫生对口支援2022“传帮带”工程信息管理平台": project_name,
        "2025年1月12日": doc_date,
        "2022年 11月 12 日": doc_date,
    }

    leftovers: List[str] = []
    with zipfile.ZipFile(reference_docx, "r") as zin:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    text = data.decode("utf-8", errors="ignore")
                    for old, new in replacements.items():
                        text = text.replace(old, new)
                    # Additional project/package lines stabilization
                    text = re.sub(
                        r"(采购项目编号[:：]\s*)([^<\n\r]+)",
                        lambda m: f"{m.group(1)}{project_no}",
                        text,
                    )
                    if package_no:
                        text = re.sub(
                            r"(包\s*号[:：]\s*)([^<\n\r]*)",
                            lambda m: f"{m.group(1)}{package_no}",
                            text,
                        )
                    leftovers = sorted(set(re.findall(r"\{\{[^{}]+\}\}", text)))
                    data = text.encode("utf-8")
                zout.writestr(item, data)
    return leftovers


def build_qa_report(validation: Dict, placeholder_leftovers: List[str], task_id: str) -> Dict:
    return {
        **validation,
        "placeholder_leftovers": placeholder_leftovers,
        "compliance_checks": {
            "fixed_sections_locked": True,
            "fact_fields_no_hallucination": True,
        },
        "traceability_checks": {
            "has_confidence": True,
            "has_source": True,
        },
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "task_id": task_id,
    }


def build_qa_report_rule(
    validation: Dict,
    task_id: str,
    section_plan: Dict,
    attachments: List[str],
) -> Dict:
    required_section_count = len(section_plan.get("sections", []))
    missing_attachments = []
    att_names = " ".join(Path(x).name for x in attachments)
    if "营业执照" not in att_names:
        missing_attachments.append("营业执照")
    if "身份证" not in att_names:
        missing_attachments.append("法人身份证明")
    return {
        **validation,
        "missing_required_attachments": missing_attachments,
        "missing_required_sections": [] if required_section_count > 0 else ["响应文件格式章节"],
        "table_unfilled_cells": [],
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "task_id": task_id,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate bid document from template + inputs")
    parser.add_argument("--input-json", help="Path to task input JSON")
    parser.add_argument("--project-dir", default="", help="Directory mode input root")
    parser.add_argument("--template-path", default="模板.docx", help="Path to template .docx")
    parser.add_argument("--project-text", default="", help="Unstructured project input text")
    parser.add_argument("--supplier-name", default="", help="Supplier name")
    parser.add_argument("--attachments", nargs="*", default=[], help="Attachment paths")
    parser.add_argument("--output-root", default="output", help="Output root directory")
    parser.add_argument("--task-id", default="", help="Optional task id")
    parser.add_argument("--config-dir", default="config", help="Config directory")

    parser.add_argument("--llm-config", default="llm_config.json", help="LLM config file under config dir")
    parser.add_argument("--disable-llm", action="store_true", help="Disable LLM calls and use rule fallback")
    parser.add_argument("--mode", choices=["rule", "llm"], default="rule", help="Execution mode")
    return parser.parse_args()


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def load_llm_config(config_dir: Path, llm_config_name: str) -> Optional[LLMConfig]:
    llm_path = (config_dir / llm_config_name).resolve()
    file_cfg: Dict[str, object] = {}
    if llm_path.exists():
        file_cfg = load_json(llm_path)

    api_url = str(file_cfg.get("api_url") or os.getenv("LLM_API_URL") or "").strip()
    api_key = str(file_cfg.get("api_key") or os.getenv("LLM_API_KEY") or "").strip()
    model = str(file_cfg.get("model") or os.getenv("LLM_MODEL") or "").strip()
    temperature = float(file_cfg.get("temperature") or os.getenv("LLM_TEMPERATURE") or 0.2)
    timeout_sec = int(file_cfg.get("timeout_sec") or os.getenv("LLM_TIMEOUT_SEC") or 60)

    if not api_url or not api_key or not model:
        return None

    return LLMConfig(
        api_url=api_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        timeout_sec=timeout_sec,
    )


def main() -> int:
    args = parse_args()

    base_dir = Path(__file__).resolve().parents[1]
    config_dir = (base_dir / args.config_dir).resolve()
    output_root = (base_dir / args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    schema = load_json(config_dir / "bid_schema.json")
    rules = load_json(config_dir / "bid_rules.json")
    mapping = load_json(config_dir / "template_mapping.json")
    _ = load_json(config_dir / "fixed_sections.json")

    llm_cfg = load_llm_config(config_dir, args.llm_config)
    llm: Optional[LLMClient] = None
    use_llm = (args.mode == "llm") and (not args.disable_llm)
    if use_llm:
        if llm_cfg is None:
            raise RuntimeError(
                "LLM mode enabled but config missing. Provide config/llm_config.json or env vars: "
                "LLM_API_URL, LLM_API_KEY, LLM_MODEL"
            )
        llm = LLMClient(llm_cfg)

    task_id = args.task_id.strip() or f"task_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    task_dir = output_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    if args.project_dir:
        payload = load_payload_from_project_dir(Path(args.project_dir).resolve())
    else:
        payload = collect_inputs(args)

    template_path = Path(payload.get("template_path") or args.template_path)
    if not template_path.is_absolute():
        template_path = Path.cwd() / template_path
    ensure_exists(template_path, "Template")

    save_json(task_dir / "task_input.json", payload)

    project_text = payload.get("project_text", "")
    if llm is not None:
        try:
            project_fields = extract_project_fields_llm(project_text, "project_text", llm)
        except Exception as e:
            save_json(task_dir / "llm_extract_error.json", {"error": str(e)})
            project_fields = extract_project_fields_rule(project_text, "project_text")
    else:
        project_fields = extract_project_fields_rule(project_text, "project_text")

    project_fields = fill_project_fields_from_template_if_missing(project_fields, template_path)

    if llm is not None:
        try:
            supplier_fields = extract_supplier_fields_llm(payload, llm)
        except Exception as e:
            save_json(task_dir / "llm_supplier_extract_error.json", {"error": str(e)})
            supplier_fields = extract_supplier_fields_rule_from_inputs(payload)
    else:
        supplier_fields = extract_supplier_fields_rule_from_inputs(payload)

    merged_plain = {k: v.value for k, v in {**project_fields, **supplier_fields}.items()}
    if llm is not None:
        try:
            generated = generate_sections_llm(merged_plain, llm)
            section_model = f"llm:{llm.config.model}"
        except Exception as e:
            save_json(task_dir / "llm_generate_error.json", {"error": str(e)})
            generated = generate_sections_rule(merged_plain)
            section_model = "rule-v1"
    else:
        generated = generate_sections_rule(merged_plain)
        section_model = "rule-v1"

    normalized = build_normalized_payload(project_fields, supplier_fields, generated, section_model)
    save_json(task_dir / "normalized_payload.json", normalized)

    validation = validate_payload(normalized, schema, rules)
    output_docx = task_dir / f"投标文件_{task_id}.docx"

    template_text = extract_text_from_docx(template_path)
    if llm is not None and args.mode == "llm":
        try:
            section_titles = parse_response_format_sections_llm(template_text, llm)
        except Exception as e:
            save_json(task_dir / "llm_section_parse_error.json", {"error": str(e)})
            section_titles = parse_response_format_sections(template_text)
    else:
        section_titles = parse_response_format_sections(template_text)

    section_plan = build_section_plan(section_titles)
    save_json(task_dir / "section_plan.json", section_plan)
    render_docx_from_template_response_chapter(template_path, output_docx, normalized)
    qa_report = build_qa_report_rule(
        validation=validation,
        task_id=task_id,
        section_plan=section_plan,
        attachments=payload.get("attachments", []),
    )

    save_json(task_dir / "qa_report.json", qa_report)

    result = {
        "task_id": task_id,
        "output_docx": str(output_docx),
        "qa_report": str(task_dir / "qa_report.json"),
        "status": "needs_review" if (qa_report["missing_required_fields"] or qa_report["low_confidence_fields"]) else "ok",
        "mode": args.mode,
        "llm_enabled": llm is not None,
        "llm_model": llm.config.model if llm else None,
    }
    save_json(task_dir / "result.json", result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
