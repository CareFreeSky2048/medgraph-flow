"""Daily incremental courseware compiler for MedGraph Flow.

Supports:
1. Extracting text from .pptx, .pdf, .docx (plus legacy .ppt/.doc via PowerPoint/Word COM on Windows)
2. Matching slide contents to Fudan syllabus_backbone.json items (preserving core_underlined weights)
3. Generating zero-API-key Prompt Packs (--mode prompt-pack) for any free chat/CLI LLM
4. Calling free OpenAI-compatible or Gemini API endpoints via env vars (--mode api)
5. Losslessly merging delta JSON into data/graph.json while validating schema & never touching progress
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
INBOX_DIR = APP_DIR / "inbox"

from schema import NODE_TYPES, RELATION_TYPES, SYLLABUS_TIERS, validate_graph  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def extract_pptx(path: Path) -> list[dict]:
    from pptx import Presentation

    prs = Presentation(str(path))
    pages = []
    for idx, slide in enumerate(prs.slides, 1):
        chunks = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                txt = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip())
                if txt:
                    chunks.append(txt)
            elif shape.has_table:
                rows = []
                for row in shape.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    chunks.append("\n".join(rows))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        text = "\n".join(chunks).strip()
        if notes:
            text = f"{text}\n[讲义备注] {notes}".strip()
        if text:
            pages.append({"page": idx, "text": text})
    return pages


def extract_pdf(path: Path) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for idx, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append({"page": idx, "text": text})
    return pages


def extract_docx(path: Path) -> list[dict]:
    import docx

    doc = docx.Document(str(path))
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    pages = []
    buf = []
    page_no = 1
    for p in paras:
        buf.append(p)
        if sum(len(x) for x in buf) >= 900:
            pages.append({"page": page_no, "text": "\n".join(buf)})
            buf = []
            page_no += 1
    if buf:
        pages.append({"page": page_no, "text": "\n".join(buf)})
    return pages


def convert_legacy_office(path: Path) -> Path:
    """Convert .ppt -> .pptx or .doc -> .docx via Windows COM if Office/WPS is installed."""
    import win32com.client  # type: ignore

    tmp_dir = Path(tempfile.mkdtemp(prefix="medgraph_conv_"))
    suffix = path.suffix.lower()
    if suffix == ".ppt":
        out_path = tmp_dir / f"{path.stem}.pptx"
        app = win32com.client.Dispatch("PowerPoint.Application")
        try:
            pres = app.Presentations.Open(str(path.resolve()), WithWindow=False)
            pres.SaveAs(str(out_path), 24)  # ppSaveAsOpenXMLPresentation
            pres.Close()
        finally:
            app.Quit()
        return out_path
    if suffix == ".doc":
        out_path = tmp_dir / f"{path.stem}.docx"
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = False
        try:
            doc = app.Documents.Open(str(path.resolve()))
            doc.SaveAs2(str(out_path), FileFormat=16)  # wdFormatDocumentDefault
            doc.Close()
        finally:
            app.Quit()
        return out_path
    raise ValueError(f"Unsupported legacy suffix: {suffix}")


def extract_courseware(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Courseware not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pptx":
        return extract_pptx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in {".ppt", ".doc"}:
        converted = convert_legacy_office(path)
        try:
            return extract_pptx(converted) if suffix == ".ppt" else extract_docx(converted)
        finally:
            shutil.rmtree(converted.parent, ignore_errors=True)
    raise ValueError(f"Unsupported courseware extension: {suffix}")


def bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]+", "", text.lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def match_syllabus_candidates(pages: list[dict], syllabus_items: list[dict], top_k: int = 12) -> list[dict]:
    combined = "\n".join(p["text"] for p in pages[:60])
    slide_bg = bigrams(combined)
    admin_noise = ("课程名称", "课程考核", "教学团队", "实践学时", "含实验室安全", "教学参考资料", "课程简介")
    scored = []
    for item in syllabus_items:
        item_txt = f"{item.get('chapter', '')} {item.get('title', '')} {item.get('raw_text', '')}"
        if any(noise in item_txt for noise in admin_noise):
            continue
        item_bg = bigrams(item_txt)
        if not item_bg:
            continue
        overlap = len(slide_bg & item_bg)
        score = overlap / max(6, len(item_bg) ** 0.7)
        if item.get("tier") == "core_underlined":
            score *= 1.2
        if score > 0.25:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def build_prompt_text(
    slide_name: str,
    pages: list[dict],
    syllabus_candidates: list[dict],
    existing_nodes: list[dict],
) -> str:
    existing_index = "\n".join(
        f"- `{n['id']}` [{n['subject']}] {n['title']} — {n['anchor_summary']}"
        for n in existing_nodes[:120]
    )
    syl_lines = "\n".join(
        f"- `{s['id']}` ({s['subject']} | {s['tier']}): {s['title']}"
        for s in syllabus_candidates
    ) or "- （未自动匹配到大纲条目，请根据课件内容自行选择对应 subject 与 syllabus_tier）"
    pages_txt = "\n".join(f"### Page/Slide {p['page']}\n{p['text']}" for p in pages)

    return f"""你是复旦基础医学跨学科知识图谱编译器。请将下列课件内容编译为**严格合法的增量 JSON（delta_graph.json）**，供手机端碎片化逻辑流复习。

## 课件来源
`{slide_name}`

## 匹配到的教学大纲参考条目（请绑定 `syllabus_refs` 并继承最高 `syllabus_tier`）
{syl_lines}

## 已有图谱概念索引（必须优先复用已有 ID，并主动建立跨学科关联边！）
{existing_index or "- （暂无已有节点）"}

## 严格输出格式要求
只输出一个合法 JSON 对象（可用 ```json 包裹），字段如下：
```json
{{
  "nodes": [
    {{
      "id": "subject_snake_case_id",
      "title": "精炼概念标题（<=22字）",
      "subject": "anatomy|histology|embryology|physiology|biochem|pathophys|pathology|pharmacology",
      "syllabus_refs": ["syl_xxx_001"],
      "syllabus_tier": "core_underlined|master|familiar|understand",
      "node_type": "mechanism_flow|structure_cloze|comparison_matrix",
      "anchor_summary": "40字以内一句话核心定位，供断点重连时10秒回顾",
      "source_slides": ["{slide_name}#P1-P5"],
      "payload": {{}}
    }}
  ],
  "edges": [
    {{
      "id": "e_src_to_dst",
      "source": "源节点ID（可为已有节点或本次新增节点）",
      "target": "目标节点ID（可为已有节点或本次新增节点）",
      "relation_type": "causal_next|prerequisite|structural_basis|clinical_manifestation|contrast_with|cross_discipline",
      "strength": 0.9,
      "bridge_text": "【必填】用1句话显式说明为什么从source推演/关联到target，保证碎片切换不断逻辑"
    }}
  ],
  "threads": [
    {{
      "id": "thread_unique_id",
      "title": "本课件主干逻辑线标题",
      "description": "一句话说明主线脉络",
      "subject_scope": ["biochem"],
      "ordered_steps": [
        {{"node_id": "节点1", "incoming_edge_id": null}},
        {{"node_id": "节点2", "incoming_edge_id": "e_1_to_2"}}
      ]
    }}
  ]
}}
```

三种 `node_type` 的 `payload` 规范：
1. `mechanism_flow`（生理/病生/机制推演）：`{{"steps": [{{"cue": "问题/触发点", "reveal": "推演结论", "mechanism_tag": "标签"}}], "takeaway": "一句话总结"}}`（steps 至少 2 步）
2. `structure_cloze`（解剖/组胚/结构要点）：`{{"bullets": ["要点含{{{{关键胶囊词}}}}"], "trap_note": "易错陷阱"}}`（至少含 1 个 `{{{{...}}}}` 胶囊）
3. `comparison_matrix`（易混鉴别对比）：`{{"columns": ["维度","甲","乙"], "rows": [["...","...","..."]], "quiz": {{"stem":"单选题题干","options":["A","B","C","D"],"answer_idx":0,"explain":"解析"}}}}`

## 原始课件正文
{pages_txt}
"""


def extract_json_block(raw_text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw_text, re.S)
    candidate = m.group(1) if m else raw_text.strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1:
        candidate = candidate[start : end + 1]
    return json.loads(candidate)


def call_llm_api(prompt: str) -> str:
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = os.environ.get("MEDGRAPH_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    api_base = os.environ.get("MEDGRAPH_API_BASE", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("MEDGRAPH_MODEL", "").strip()

    if openai_key:
        model = model or "deepseek-chat"
        url = f"{api_base}/chat/completions"
        body = json.dumps({
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "You are a precise medical curriculum knowledge graph compiler. Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {openai_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]

    if gemini_key:
        model = model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError(
        "未检测到 API Key。请设置 MEDGRAPH_API_KEY (+ MEDGRAPH_API_BASE / MEDGRAPH_MODEL) 或 GEMINI_API_KEY，"
        "或改用零 Key 的 `--mode prompt-pack` 生成提示词包。"
    )


def normalize_title(title: str) -> str:
    return re.sub(r"[\s·—\-:：()（）【】]+", "", title.lower())


def merge_delta_into_graph(graph: dict, delta: dict, timestamp: str | None = None) -> tuple[dict, dict]:
    """Losslessly merge delta_graph dict into existing graph without breaking IDs or progress."""
    ts = timestamp or now_iso()
    merged = json.loads(json.dumps(graph))
    nodes_by_id = {n["id"]: n for n in merged["nodes"]}
    nodes_by_title = {normalize_title(n["title"]): n for n in merged["nodes"]}
    id_remap: dict[str, str] = {}

    stats = {"nodes_added": 0, "nodes_enriched": 0, "edges_added": 0, "threads_added": 0}

    for inc_node in delta.get("nodes", []):
        nid = inc_node["id"]
        norm_t = normalize_title(inc_node["title"])
        existing = nodes_by_id.get(nid) or nodes_by_title.get(norm_t)
        if existing:
            id_remap[nid] = existing["id"]
            existing_slides = set(existing.get("source_slides", []))
            for s in inc_node.get("source_slides", []):
                if s not in existing_slides:
                    existing.setdefault("source_slides", []).append(s)
                    existing_slides.add(s)
            existing_refs = set(existing.get("syllabus_refs", []))
            for r in inc_node.get("syllabus_refs", []):
                if r not in existing_refs:
                    existing.setdefault("syllabus_refs", []).append(r)
                    existing_refs.add(r)
            stats["nodes_enriched"] += 1
        else:
            new_node = json.loads(json.dumps(inc_node))
            new_node.setdefault("added_at", ts)
            new_node.setdefault("syllabus_refs", [])
            new_node.setdefault("source_slides", [])
            merged["nodes"].append(new_node)
            nodes_by_id[nid] = new_node
            nodes_by_title[norm_t] = new_node
            id_remap[nid] = nid
            stats["nodes_added"] += 1

    edge_keys = {(e["source"], e["target"], e["relation_type"]) for e in merged["edges"]}
    edge_ids = {e["id"] for e in merged["edges"]}
    edge_id_remap: dict[str, str] = {}

    for inc_edge in delta.get("edges", []):
        src = id_remap.get(inc_edge["source"], inc_edge["source"])
        dst = id_remap.get(inc_edge["target"], inc_edge["target"])
        if src == dst or src not in nodes_by_id or dst not in nodes_by_id:
            continue
        key = (src, dst, inc_edge["relation_type"])
        if key in edge_keys:
            existing_e = next(e for e in merged["edges"] if (e["source"], e["target"], e["relation_type"]) == key)
            edge_id_remap[inc_edge["id"]] = existing_e["id"]
            continue
        eid = inc_edge["id"]
        while eid in edge_ids:
            eid = f"{eid}_x"
        new_edge = json.loads(json.dumps(inc_edge))
        new_edge["id"] = eid
        new_edge["source"] = src
        new_edge["target"] = dst
        new_edge.setdefault("strength", 0.85)
        merged["edges"].append(new_edge)
        edge_keys.add(key)
        edge_ids.add(eid)
        edge_id_remap[inc_edge["id"]] = eid
        stats["edges_added"] += 1

    thread_ids = {t["id"] for t in merged["threads"]}
    for inc_th in delta.get("threads", []):
        tid = inc_th["id"]
        if tid in thread_ids:
            continue
        new_th = json.loads(json.dumps(inc_th))
        new_steps = []
        for step in new_th.get("ordered_steps", []):
            nid = id_remap.get(step["node_id"], step["node_id"])
            eid = step.get("incoming_edge_id")
            if eid is not None:
                eid = edge_id_remap.get(eid, eid)
            new_steps.append({"node_id": nid, "incoming_edge_id": eid})
        new_th["ordered_steps"] = new_steps
        merged["threads"].append(new_th)
        thread_ids.add(tid)
        stats["threads_added"] += 1

    merged["generated_at"] = ts
    validate_graph(merged)
    return merged, stats


def load_graph(graph_path: Path) -> dict:
    if graph_path.exists():
        return json.loads(graph_path.read_text(encoding="utf-8"))
    syl_path = DATA_DIR / "syllabus_backbone.json"
    syl = json.loads(syl_path.read_text(encoding="utf-8")).get("items", []) if syl_path.exists() else []
    return {"schema_version": 1, "title": "MedGraph Flow", "generated_at": now_iso(), "syllabus": syl, "nodes": [], "edges": [], "threads": []}


def main():
    parser = argparse.ArgumentParser(description="Compile slides into MedGraph Flow delta JSON and merge safely.")
    parser.add_argument("--slide", help="Path to courseware (.pptx/.ppt/.pdf/.docx)")
    parser.add_argument("--mode", choices=["extract-context", "prompt-pack", "api", "merge"], required=True)
    parser.add_argument("--delta", help="Path to delta_graph.json (for --mode merge or api output)")
    parser.add_argument("--graph", default=str(DATA_DIR / "graph.json"), help="Target graph.json path")
    parser.add_argument("--chunk-size", type=int, default=25, help="Max slides per prompt pack chunk")
    parser.add_argument("--out-dir", default=str(INBOX_DIR), help="Directory for prompt packs / intermediate outputs")
    args = parser.parse_args()

    graph_path = Path(args.graph)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = load_graph(graph_path)

    if args.mode == "merge":
        if not args.delta:
            raise ValueError("--mode merge requires --delta <delta_graph.json>")
        delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
        merged, stats = merge_delta_into_graph(graph, delta)
        if graph_path.exists():
            shutil.copy2(graph_path, graph_path.with_suffix(".json.bak"))
        graph_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Merged into {graph_path}: {json.dumps(stats, ensure_ascii=False)}")
        return

    if not args.slide:
        raise ValueError(f"--mode {args.mode} requires --slide <path>")
    slide_path = Path(args.slide)
    pages = extract_courseware(slide_path)
    if not pages:
        raise RuntimeError(f"No extractable text found in {slide_path} (may be scanned images only).")
    syl_candidates = match_syllabus_candidates(pages, graph.get("syllabus", []))

    if args.mode in {"extract-context", "prompt-pack"}:
        chunks = [pages[i : i + args.chunk_size] for i in range(0, len(pages), args.chunk_size)]
        generated_files = []
        suffix_tag = "context" if args.mode == "extract-context" else "prompt_pack"
        for idx, chunk in enumerate(chunks, 1):
            prompt = build_prompt_text(slide_path.name, chunk, syl_candidates, graph["nodes"])
            out_file = out_dir / f"{slide_path.stem}_part{idx}_{suffix_tag}.md"
            out_file.write_text(prompt, encoding="utf-8")
            generated_files.append(str(out_file))
        print(f"Extracted {len(pages)} pages -> {len(generated_files)} {suffix_tag} file(s):")
        for f in generated_files:
            print(f"  {f}")
        if args.mode == "prompt-pack":
            print("下一步：把上述 prompt_pack.md 发给任意免费 AI 生成 delta.json，再执行：")
            print(f'  python "{Path(__file__).name}" --mode merge --delta delta.json')
        return

    if args.mode == "api":
        chunks = [pages[i : i + args.chunk_size] for i in range(0, len(pages), args.chunk_size)]
        combined_delta = {"nodes": [], "edges": [], "threads": []}
        for idx, chunk in enumerate(chunks, 1):
            prompt = build_prompt_text(slide_path.name, chunk, syl_candidates, graph["nodes"])
            raw = call_llm_api(prompt)
            delta_part = extract_json_block(raw)
            combined_delta["nodes"].extend(delta_part.get("nodes", []))
            combined_delta["edges"].extend(delta_part.get("edges", []))
            combined_delta["threads"].extend(delta_part.get("threads", []))
            print(f"Compiled chunk {idx}/{len(chunks)}: +{len(delta_part.get('nodes', []))} nodes, +{len(delta_part.get('edges', []))} edges")

        delta_out = Path(args.delta) if args.delta else (out_dir / f"{slide_path.stem}_delta.json")
        delta_out.write_text(json.dumps(combined_delta, ensure_ascii=False, indent=2), encoding="utf-8")
        merged, stats = merge_delta_into_graph(graph, combined_delta)
        if graph_path.exists():
            shutil.copy2(graph_path, graph_path.with_suffix(".json.bak"))
        graph_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"API compile & merge OK ({delta_out}): {json.dumps(stats, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
