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


SUBJECT_LOGIC_PROFILES = {
    "anatomy": {
        "label": "系统解剖学",
        "spine": "骨性/体表定位标志 → 器官形态分部与狭窄/弯曲 → 血供/淋巴引流/神经支配 → 毗邻受损体征与临床穿刺/手术入路",
        "card_bias": "多用 structure_cloze（位置/起止/穿行/支配胶囊）与 comparison_matrix（左右差异、浅深静脉、三大神经损伤对比）",
        "foundation_rule": "新讲血管/神经/脏器必须连向所在体腔、骨性标志或总论主干节点（structural_basis / prerequisite）",
        "bridge_rule": "bridge_text 必须写出【空间毗邻、走行通道或缺少瓣膜/狭窄处】如何直接决定了临床损伤定位或扩散路径",
    },
    "histology": {
        "label": "组织学与胚胎学",
        "spine": "光镜分层结构 → 电镜超微特化（连接/细胞器/微血管壁） → 功能适配机制（胚胎则按：发育时序 → 胚层分化 → 融合失败致畸形）",
        "card_bias": "多用 structure_cloze（超微结构与染色特征胶囊）与 comparison_matrix（三种软骨/三种肌/血细胞鉴别）",
        "foundation_rule": "组胚微结构节点应主动作为后续生理功能与病理形态的 structural_basis 输出锚点",
        "bridge_rule": "bridge_text 必须点明【某种超微特化（如T管/三联管、肝血窦壁、化学突触）】为何恰好支撑特定功能或成为病变易损区",
    },
    "embryology": {
        "label": "胚胎学",
        "spine": "时间窗与胚层起源 → 器官原基迁移/融合/分隔 → 关键诱导信号或融合障碍 → 对应先天畸形",
        "card_bias": "多用 mechanism_flow（动态发育演变）与 comparison_matrix（正常分化 vs 畸形结局）",
        "foundation_rule": "关联到成年系统解剖学对应器官节点（structural_basis / clinical_manifestation）",
        "bridge_rule": "bridge_text 必须写清【胚胎期哪一步分隔/闭合/迁移停滞】直接遗留了哪一成年解剖结构异常",
    },
    "biochem": {
        "label": "生物化学与分子生物学",
        "spine": "生理需求与底物来源去路 → 限速酶/辅酶与能量(ATP/NADPH)流向 → 亚细胞区室定位与别构/激素调节 → 器官间循环或酶缺陷疾病",
        "card_bias": "多用 mechanism_flow（代谢通路级联与调节开关）与 comparison_matrix（糖酵解vs糖异生、氨转运双形式、抑制类型鉴别）",
        "foundation_rule": "代谢调控与疾病卡应绑定前置酶学/辅助因子/基础代谢库节点（prerequisite）",
        "bridge_rule": "bridge_text 必须写清【代谢物积聚、ATP/NADPH失衡或限速酶开关】如何把前一步反应推向下一步或跨器官循环",
    },
    "physiology": {
        "label": "生理学",
        "spine": "稳态基准与结构前提 → 物理/电化学驱动力（离子浓度差/压力梯度） → 门控/耦联触发机制 → 负/正反馈或前馈闭环 → 定量影响因素",
        "card_bias": "多用 mechanism_flow（电生理/耦联级联推演）与 comparison_matrix（前/后负荷、局部电位vs动作电位、骨骼肌vs平滑肌）",
        "foundation_rule": "器官/细胞机制应追溯到组胚超微结构或跨膜转运基础（structural_basis / prerequisite）",
        "bridge_rule": "bridge_text 必须显式写出【驱动力（电位差/压力差/Ca²⁺触发）与反馈方向】如何把系统推向下一稳态阶段",
    },
    "pathophys": {
        "label": "病理生理学",
        "spine": "病因与诱因 → 正常生理调节轴被劫持/重设 → 全身代偿 vs 失代偿转折（因果交替/损伤抗损伤） → 多器官功能代谢紊乱与防治切入点",
        "card_bias": "多用 mechanism_flow（动态调控轴与恶性循环推演）与 comparison_matrix（发热vs过热、休克各期、代偿vs失代偿）",
        "foundation_rule": "病生轴与综合征节点必须显式挂接正常生理调节机制节点作为 prerequisite（支持 🔴 自动退回补正常生理）",
        "bridge_rule": "bridge_text 必须揭示【代偿阈值被突破、调定点重设或恶性循环放大】如何让病变从代偿走向器官损伤",
    },
    "pathology": {
        "label": "病理学",
        "spine": "致病打击因子 → 形态演变谱（适应→可逆变性→不可逆坏死/凋亡→修复机化） → 大体/光镜/电镜典型病变 → 形态阻塞/破坏如何直接引发临床结局",
        "card_bias": "三型并用：mechanism_flow（损伤演进）、structure_cloze（典型镜下大体特征胶囊）、comparison_matrix（良恶性、渗出vs漏出、坏死分型）",
        "foundation_rule": "重点脏器病变节点必须连向对应正常组织学/解剖学基础（structural_basis），支撑 🔴 时先复盘正常微结构",
        "bridge_rule": "bridge_text 必须解释【微观形态改变（如纤维化、内皮间隙增宽、水肿压迫）】如何必然推导出下一步功能障碍或转归",
    },
    "pharmacology": {
        "label": "药理学",
        "spine": "正常生理递质/病理靶点前提 → 药物体内命运(ADME关键) & 分子作用机制（激动/阻断/抑酶） → 同源推导出临床用途 + 机制可预测的不良反应与禁忌证",
        "card_bias": "多用 comparison_matrix（同类药效/耐药/谱系鉴别）与 mechanism_flow（药物-受体-器官效应推演）",
        "foundation_rule": "每组药理节点必须回连其干预的生理受体、生化关键酶或病理过程（prerequisite / cross_discipline），禁止无机制地孤立背适应症/副作用清单",
        "bridge_rule": "bridge_text 必须体现【机制推导出疗效与副作用同源性】或【耐药/药动学参数如何改变临床选药】",
    },
}


def detect_slide_subjects(pages: list[dict], syllabus_candidates: list[dict], slide_name: str) -> list[str]:
    counts: dict[str, int] = {}
    for s in syllabus_candidates:
        subj = s.get("subject")
        if subj in SUBJECT_LOGIC_PROFILES:
            counts[subj] = counts.get(subj, 0) + (2 if s.get("tier") == "core_underlined" else 1)
    # Lightweight title/keyword fallback if syllabus match is sparse
    sample_txt = (slide_name + "\n" + "\n".join(p["text"][:200] for p in pages[:6])).lower()
    hints = {
        "pharmacology": ["药理", "受体", "半衰期", "不良反应", "抗菌", "镇痛", "阿司匹林", "吗啡"],
        "pathophys": ["病理生理", "发热", "应激", "休克", "缺氧", "调定点", "水肿机制", "酸碱平衡"],
        "pathology": ["病理学", "坏死", "变性", "淤血", "炎症", "肿瘤", "镜下", "大体", "肉芽肿"],
        "biochem": ["生物化学", "代谢", "酶", "atp", "nadh", "糖", "脂", "氨基酸", "转录", "复制"],
        "anatomy": ["解剖", "动脉", "静脉", "神经", "肌", "骨", "韧带", "毗邻", "体表投影"],
        "histology": ["组织学", "光镜", "电镜", "上皮", "结缔", "软骨", "肝小叶", "突触", "肌节"],
        "physiology": ["生理学", "电位", "转运", "兴奋", "收缩", "耦联", "稳态", "反射"],
    }
    for subj, kws in hints.items():
        hits = sum(1 for k in kws if k in sample_txt)
        if hits >= 2:
            counts[subj] = counts.get(subj, 0) + hits
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [subj for subj, _ in ranked[:3]] or ["pathology", "physiology"]


def select_relevant_nodes_and_edges(pages: list[dict], graph: dict, limit: int = 80) -> tuple[list[dict], list[dict]]:
    """Rank existing nodes by relevance to incoming slides + hub connectivity (no blind truncation)."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return [], []

    combined_bg = bigrams("\n".join(p["text"] for p in pages[:60]))
    degree = {n["id"]: 0 for n in nodes}
    cross_hub = set()
    for e in edges:
        if e["source"] in degree:
            degree[e["source"]] += 1
        if e["target"] in degree:
            degree[e["target"]] += 1
        if e.get("relation_type") == "cross_discipline":
            cross_hub.add(e["source"])
            cross_hub.add(e["target"])

    scored = []
    for n in nodes:
        txt = f"{n.get('title', '')} {n.get('anchor_summary', '')} {n.get('subject', '')}"
        nbg = bigrams(txt)
        overlap = len(combined_bg & nbg) / max(4, len(nbg) ** 0.7) if nbg else 0.0
        hub_bonus = 0.25 * min(6, degree.get(n["id"], 0)) + (0.6 if n["id"] in cross_hub else 0.0)
        scored.append((overlap * 2.0 + hub_bonus, n))

    scored.sort(key=lambda x: x[0], reverse=True)
    picked_nodes = [n for _, n in scored[:limit]]
    picked_ids = {n["id"] for n in picked_nodes}
    relevant_edges = [e for e in edges if e["source"] in picked_ids or e["target"] in picked_ids][:60]
    return picked_nodes, relevant_edges


def build_prompt_text(
    slide_name: str,
    pages: list[dict],
    syllabus_candidates: list[dict],
    graph_or_nodes: dict | list[dict],
) -> str:
    graph = graph_or_nodes if isinstance(graph_or_nodes, dict) else {"nodes": graph_or_nodes, "edges": [], "threads": []}
    picked_nodes, relevant_edges = select_relevant_nodes_and_edges(pages, graph, limit=80)

    node_deg = {}
    for e in graph.get("edges", []):
        node_deg[e["source"]] = node_deg.get(e["source"], 0) + 1
        node_deg[e["target"]] = node_deg.get(e["target"], 0) + 1

    existing_index = "\n".join(
        f"- `{n['id']}` [{n['subject']} | {n.get('syllabus_tier', 'master')} | deg={node_deg.get(n['id'], 0)}] {n['title']} — {n['anchor_summary']}"
        for n in picked_nodes
    )
    existing_threads = "\n".join(
        f"- `{t['id']}` {t['title']}: {' -> '.join(s['node_id'] for s in t.get('ordered_steps', []))}"
        for t in graph.get("threads", [])
    )
    existing_edges = "\n".join(
        f"- `{e['id']}`: `{e['source']}` --[{e['relation_type']}]--> `{e['target']}` ({e['bridge_text'][:70]}...)"
        for e in relevant_edges[:35]
    )
    detected_subjs = detect_slide_subjects(pages, syllabus_candidates, slide_name)
    primary_subj = detected_subjs[0]

    # Map existing coverage of syllabus refs
    covered_syl_map: dict[str, list[str]] = {}
    for n in graph.get("nodes", []):
        for ref in n.get("syllabus_refs", []):
            covered_syl_map.setdefault(ref, []).append(n["id"])

    tier_rank = {"core_underlined": 4, "master": 3, "familiar": 2, "understand": 1}
    primary_syl = sorted(
        [s for s in syllabus_candidates if s["subject"] == primary_subj],
        key=lambda x: tier_rank.get(x["tier"], 1),
        reverse=True,
    )
    cross_syl = sorted(
        [s for s in syllabus_candidates if s["subject"] != primary_subj],
        key=lambda x: tier_rank.get(x["tier"], 1),
        reverse=True,
    )

    def fmt_syl(s: dict) -> str:
        cov = covered_syl_map.get(s["id"], [])
        cov_tag = f"已建图: {','.join(cov[:3])}" if cov else "⚠️ 尚属空白(优先覆盖)"
        tier_mark = "★重点/下划线" if s["tier"] == "core_underlined" else s["tier"]
        return f"- `{s['id']}` ({s['subject']} | {tier_mark} | {cov_tag}): {s['title']}"

    primary_syl_lines = "\n".join(fmt_syl(s) for s in primary_syl) or "- （本课未匹配到主学科大纲条目，请检查 syllabus_backbone.json）"
    cross_syl_lines = "\n".join(fmt_syl(s) for s in cross_syl) or "- （无明显跨学科命中大纲条目）"

    # Surface remaining high-priority syllabus gaps in primary subject
    candidate_ids = {s["id"] for s in syllabus_candidates}
    uncovered_gaps = [
        s for s in graph.get("syllabus", [])
        if s["subject"] == primary_subj and s["id"] not in covered_syl_map and s["id"] not in candidate_ids and s["tier"] in {"core_underlined", "master"}
    ][:10]
    gap_lines = "\n".join(
        f"- `{s['id']}` ({s['tier']}): {s['title']}" for s in uncovered_gaps
    ) or "- （暂无额外空白条目）"

    playbook_blocks = []
    for subj in detected_subjs:
        prof = SUBJECT_LOGIC_PROFILES.get(subj)
        if prof:
            playbook_blocks.append(
                f"### 【{prof['label']} (`{subj}`) 专属拆解语法】\n"
                f"- 逻辑主脊：{prof['spine']}\n"
                f"- 优先卡型：{prof['card_bias']}\n"
                f"- 地基锚点要求：{prof['foundation_rule']}\n"
                f"- `bridge_text` 写法：{prof['bridge_rule']}"
            )
    playbook_text = "\n\n".join(playbook_blocks)
    pages_txt = "\n".join(f"### Page/Slide {p['page']}\n{p['text']}" for p in pages)

    return f"""你是复旦基础医学跨学科知识图谱的**逻辑架构师与编译器**。
严禁直接按 PPT 幻灯片目录逐页转写成“内容清单卡”，也**严禁用同一种模板去硬套所有医学学科**！
你必须**以复旦教学大纲为最高指挥棒**，先剪枝非考纲冗余、逐条锚定必考条目，再依据子学科专属认知语法完成逻辑拆解，最后输出严格合法的增量 JSON（delta_graph.json）。

## 课件来源
`{slide_name}`

## 教学大纲指挥表（权重与必考覆盖门控）
### 1. 本课主学科命中的大纲条目（`★重点/下划线` 与 `master` 必须零遗漏承接；`understand` 降级折叠；未入纲冗余科研拓展请剪枝）
{primary_syl_lines}

### 2. 跨学科关联大纲线索（用于牵引 `prerequisite` / `structural_basis` / `cross_discipline` 跨科连边）
{cross_syl_lines}

### 3. 主学科其余尚属空白的核心大纲点（检查本课件是否顺带讲到）
{gap_lines}

## 自动识别的子学科逻辑拆解语法（本次构建必须严格遵循）
{playbook_text}

## 已有图谱逻辑主线（优先考虑能否用 `thread_extensions` 嫁接延伸，而非孤立建链）
{existing_threads or "- （暂无已有主线）"}

## 高相关已有概念节点（必须优先复用已有 ID，并为其新概念寻找先修/结构地基边！）
{existing_index or "- （暂无已有节点）"}

## 周边已有逻辑边参考（避免重复建边，主动补齐跨学科缺口）
{existing_edges or "- （暂无已有边）"}

## 逻辑拆解硬性准则
1. **打破 PPT 页序、先找核心矛盾**：围绕这节课要解释的 1–3 个核心医学问题重排节点，剔除行政通知/冗余举例；边缘历史或罕见拓展标为 `understand`。
2. **单卡单步认知负荷**：每卡只讲 1 个因果推进/结构格局/易混对偶（30–45秒可读完）。严禁把“病因+分类+机制+症状”全塞进一张大杂烩卡片。
3. **补漏锚点刚性要求**：每个新 `core_underlined` / `master` 机制或病理节点，尽量配置至少 1 条入向 `prerequisite` 或 `structural_basis` 边连向其解剖/组胚/生理/生化基础节点（支持手机端点击 🔴 时自动触发诊断补漏岔路）。
4. **`bridge_text` 因果检验**：每条边的 `bridge_text` 必须用 1 句话写清**“因为 `source` 发生了什么改变，所以逻辑上必然推导出/依赖于 `target`”**；禁止出现“接下来学习/本节还介绍了”等空洞衔接词。

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
      "subject_scope": ["pathology", "physiology"],
      "ordered_steps": [
        {{"node_id": "节点1", "incoming_edge_id": null}},
        {{"node_id": "节点2", "incoming_edge_id": "e_1_to_2"}}
      ]
    }}
  ],
  "thread_extensions": [
    {{
      "thread_id": "已有thread_id（可选：将新节点续接到已有主线末端）",
      "append_steps": [
        {{"node_id": "新节点ID", "incoming_edge_id": "连接旧末节点与新节点的边ID"}}
      ]
    }}
  ]
}}
```

三种 `node_type` 的 `payload` 规范：
1. `mechanism_flow`（生理/病生/机制推演）：`{{"steps": [{{"cue": "问题/触发点（引导预测而非只写标题）", "reveal": "推演结论", "mechanism_tag": "标签"}}], "takeaway": "一句话总结"}}`（steps 建议 2–4 步）
2. `structure_cloze`（解剖/组胚/结构要点）：`{{"bullets": ["要点含{{{{关键胶囊词}}}}"], "trap_note": "易错陷阱"}}`（bullets 建议 <= 5 条，只遮罩核心枢纽词）
3. `comparison_matrix`（易混鉴别对比）：`{{"columns": ["维度","甲","乙"], "rows": [["...","...","..."]], "quiz": {{"stem":"考察因果辨析的单选题干","options":["A","B","C","D"],"answer_idx":0,"explain":"解析"}}}}`

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

    thread_map = {t["id"]: t for t in merged["threads"]}
    for inc_th in delta.get("threads", []):
        tid = inc_th["id"]
        if tid in thread_map:
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
        thread_map[tid] = new_th
        stats["threads_added"] += 1

    stats["threads_extended"] = 0
    for ext in delta.get("thread_extensions", []):
        tid = ext.get("thread_id")
        target_th = thread_map.get(tid)
        if not target_th:
            continue
        existing_step_ids = {s["node_id"] for s in target_th.get("ordered_steps", [])}
        extended_any = False
        for step in ext.get("append_steps", []):
            nid = id_remap.get(step["node_id"], step["node_id"])
            if nid in existing_step_ids or nid not in nodes_by_id:
                continue
            eid = step.get("incoming_edge_id")
            if eid is not None:
                eid = edge_id_remap.get(eid, eid)
            target_th["ordered_steps"].append({"node_id": nid, "incoming_edge_id": eid})
            existing_step_ids.add(nid)
            extended_any = True
        if extended_any:
            stats["threads_extended"] += 1

    merged["generated_at"] = ts
    validate_graph(merged)
    stats["logic_audit"] = audit_logic_quality(merged, delta, id_remap)
    return merged, stats


FILLER_BRIDGE_PREFIXES = ("接下来", "下面我们", "然后学习", "此外还有", "本节介绍", "下一节")


def audit_logic_quality(graph: dict, delta: dict, id_remap: dict[str, str]) -> dict:
    """Inspect structural logic density and highlight weak/unanchored delta artifacts."""
    nodes_by_id = {n["id"]: n for n in graph.get("nodes", [])}
    in_edges_by_node = {n["id"]: [] for n in graph.get("nodes", [])}
    out_edges_by_node = {n["id"]: [] for n in graph.get("nodes", [])}
    for e in graph.get("edges", []):
        in_edges_by_node.setdefault(e["target"], []).append(e)
        out_edges_by_node.setdefault(e["source"], []).append(e)

    added_node_ids = [
        id_remap.get(n["id"], n["id"])
        for n in delta.get("nodes", [])
        if id_remap.get(n["id"], n["id"]) in nodes_by_id
    ]
    warnings = []
    orphan_nodes = []
    unanchored_core = []
    cross_discipline_edges = 0
    rescue_anchors = 0
    contrast_edges = 0

    for e in delta.get("edges", []):
        src = id_remap.get(e["source"], e["source"])
        dst = id_remap.get(e["target"], e["target"])
        sn = nodes_by_id.get(src)
        dn = nodes_by_id.get(dst)
        if e.get("relation_type") == "cross_discipline" or (sn and dn and sn["subject"] != dn["subject"]):
            cross_discipline_edges += 1
        if e.get("relation_type") in {"prerequisite", "structural_basis"}:
            rescue_anchors += 1
        if e.get("relation_type") == "contrast_with":
            contrast_edges += 1
        bt = e.get("bridge_text", "").strip()
        if any(bt.startswith(p) for p in FILLER_BRIDGE_PREFIXES) or len(bt) < 12:
            warnings.append(f"Weak filler bridge_text on edge {e.get('id')}: '{bt}'")

    ungrounded_core = []
    newly_covered_refs = set()
    pre_covered_refs = set()
    delta_added_ids = {id_remap.get(n["id"], n["id"]) for n in delta.get("nodes", [])}
    for nid, nd in nodes_by_id.items():
        if nid not in delta_added_ids:
            pre_covered_refs.update(nd.get("syllabus_refs", []))

    for nid in dict.fromkeys(added_node_ids):
        nd = nodes_by_id[nid]
        ins = in_edges_by_node.get(nid, [])
        outs = out_edges_by_node.get(nid, [])
        refs = nd.get("syllabus_refs", [])
        for r in refs:
            if r not in pre_covered_refs:
                newly_covered_refs.add(r)
        if nd.get("syllabus_tier") in {"core_underlined", "master"} and not refs:
            ungrounded_core.append(nid)
        if not ins and not outs:
            orphan_nodes.append(nid)
        elif nd.get("syllabus_tier") in {"core_underlined", "master"}:
            has_foundation = any(e["relation_type"] in {"prerequisite", "structural_basis", "causal_next"} for e in ins)
            if not has_foundation and not outs:
                unanchored_core.append(nid)
        if nd["node_type"] == "structure_cloze" and len(nd["payload"].get("bullets", [])) > 6:
            warnings.append(f"Kitchen-sink bloat risk in {nid}: >6 cloze bullets (consider splitting)")
        if nd["node_type"] == "mechanism_flow" and len(nd["payload"].get("steps", [])) > 5:
            warnings.append(f"Overlong mechanism_flow in {nid}: >5 steps (consider splitting)")

    if orphan_nodes:
        warnings.append(f"Orphan nodes without any edges: {orphan_nodes}")
    if unanchored_core:
        warnings.append(f"Core nodes missing incoming logic foundation: {unanchored_core}")
    if ungrounded_core:
        warnings.append(f"Core/Master nodes missing syllabus_refs grounding: {ungrounded_core}")

    # Discipline-specific logic grammar checks
    added_subjects = {nodes_by_id[nid]["subject"] for nid in added_node_ids}
    if any(s in added_subjects for s in ("pathology", "pathophys", "pharmacology")):
        has_foundation_anchor = any(
            e.get("relation_type") in {"prerequisite", "structural_basis", "cross_discipline"}
            for e in delta.get("edges", [])
        )
        if added_node_ids and not has_foundation_anchor:
            warnings.append(
                "Discipline logic gap: pathology/pathophys/pharmacology additions should include at least one "
                "prerequisite, structural_basis, or cross_discipline anchor back to normal anatomy/histology/physiology/biochem."
            )

    return {
        "cross_discipline_edges": cross_discipline_edges,
        "rescue_anchors": rescue_anchors,
        "contrast_edges": contrast_edges,
        "newly_covered_syllabus_refs": sorted(newly_covered_refs),
        "orphan_count": len(orphan_nodes),
        "warnings": warnings,
    }


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
            prompt = build_prompt_text(slide_path.name, chunk, syl_candidates, graph)
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
            prompt = build_prompt_text(slide_path.name, chunk, syl_candidates, graph)
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
