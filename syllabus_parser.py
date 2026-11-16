"""Parse Fudan medical course syllabi into structured SyllabusNode weights.

Supports parsing:
1. 《基础医学分论》详细教学大纲 20251221.docx (preserving Word underline markup -> core_underlined = 3.5)
2. 系解教学大纲.pdf (16-week Human Anatomy syllabus with 掌握/熟悉/了解 tiers)
3. 生化教学大纲.pdf (12-chapter Biochemistry syllabus with 重点掌握/掌握/熟悉/了解 tiers)
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"

TIER_PRIORITY = {
    "core_underlined": 4,
    "master": 3,
    "familiar": 2,
    "understand": 1,
}


def slug_id(prefix: str, index: int) -> str:
    return f"{prefix}_{index:03d}"


def infer_tier_from_clauses(text: str, has_underline: bool = False) -> str:
    if has_underline or "重点掌握" in text:
        return "core_underlined"
    if "掌握" in text:
        return "master"
    if "熟悉" in text:
        return "familiar"
    if "了解" in text:
        return "understand"
    return "master"


def split_clauses_with_tiers(text: str, default_tier: str = "master") -> list[tuple[str, str]]:
    """Extract (clause, tier) pairs from Chinese syllabus lines containing 掌握/熟悉/了解."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    parts = [p.strip(" 。；;") for p in re.split(r"[。；;]", cleaned) if p.strip(" 。；;")]
    out = []
    cur_tier = default_tier
    for part in parts:
        stripped = re.sub(r"^(?:[（(]?[一二三四五六七八九十\d]+[）)]?\s*[.．、]?\s*)", "", part)
        tier = cur_tier
        m = re.search(r"(?:^|[：:，,、\s])(重点掌握|掌握|熟悉|了解)", stripped)
        if m:
            kw = m.group(1)
            if kw == "重点掌握":
                tier = "core_underlined"
            elif kw == "掌握":
                tier = "master"
            elif kw == "熟悉":
                tier = "familiar"
            elif kw == "了解":
                tier = "understand"
        elif "重点掌握" in part:
            tier = "core_underlined"
        cur_tier = tier
        out.append((part, tier))
    return out


def parse_jichu_yixue_docx(docx_path: Path) -> list[dict]:
    import docx

    doc = docx.Document(str(docx_path))
    items: list[dict] = []
    section = None
    subsection = ""
    mode = "none"  # objective | content | requirement
    counters = {
        "histology": 0,
        "physiology": 0,
        "pathophys": 0,
        "pathology": 0,
        "pharmacology": 0,
    }
    section_map = {
        "组织胚胎部分": ("histology", "组织学与胚胎学"),
        "生理部分": ("physiology", "生理学"),
        "病理生理部分": ("pathophys", "病理生理学"),
        "病理部分": ("pathology", "病理学"),
        "药理部分": ("pharmacology", "药理学"),
    }

    # For histology/pharmacology, objectives/requirements carry the tier (掌握/熟悉/了解),
    # whereas for physiology/pathophys/pathology, underlines in 【教学内容】 mark core_underlined.
    histo_objective_lines: list[tuple[str, str]] = []
    pharma_current_topic = "药理学总论"

    for p in doc.paragraphs:
        txt = re.sub(r"\s+", " ", p.text).strip()
        if not txt:
            continue
        if txt in section_map:
            section, sec_label = section_map[txt]
            subsection = sec_label
            mode = "none"
            continue
        if not section:
            continue
        if txt.startswith("【教学时数】") or txt.startswith("【注释】") or txt.startswith("【名词解释】"):
            if txt.startswith("【名词解释】"):
                mode = "glossary"
            continue
        if txt.startswith("【教学目的】"):
            mode = "objective"
            continue
        if txt.startswith("【教学内容】") or txt == "教学内容":
            mode = "content"
            continue
        if txt.startswith("【教学要求】") or txt == "教学要求":
            mode = "requirement"
            continue

        has_ul = any(r.underline and r.text.strip() for r in p.runs)
        ul_text = "".join(r.text for r in p.runs if r.underline).strip()

        if section == "pharmacology":
            m_topic = re.match(r"^([一二三四五六七八九十]+、)?(.+?)[（(](?:学时数[：:]\s*)?[\d.]+学时[）)]|^(.+?)[（(]学时数[：:]\s*[\d.]+[）)]", txt)
            if ("学时" in txt and len(txt) < 40) and not re.match(r"^\d+\.", txt):
                pharma_current_topic = re.sub(r"[（(].*?[）)]", "", txt).strip(" ：:")
                continue
            if mode == "requirement":
                tier = infer_tier_from_clauses(txt, has_ul)
                counters[section] += 1
                items.append({
                    "id": slug_id(f"syl_{section}", counters[section]),
                    "course": "基础医学分论",
                    "subject": "pharmacology",
                    "chapter": pharma_current_topic,
                    "title": re.sub(r"^\d+[.、]\s*", "", txt)[:140],
                    "raw_text": txt,
                    "tier": tier,
                    "underlined": has_ul,
                })
            continue

        if section == "histology":
            if mode == "objective":
                tier = infer_tier_from_clauses(txt, has_ul)
                histo_objective_lines.append((re.sub(r"^\d+[.、]\s*", "", txt), tier))
            elif mode == "content":
                for clause, obj_tier in split_clauses_with_tiers(txt, default_tier="master"):
                    pass
                # Split by semicolons into granular topics and inherit the highest matching objective tier
                sub_parts = [s.strip(" 。；;") for s in re.split(r"[；;]", re.sub(r"^\d+[.、]\s*", "", txt)) if s.strip(" 。；;")]
                for sp in sub_parts:
                    matched_tier = None
                    for obj_txt, obj_tier in histo_objective_lines:
                        keywords = [w for w in re.split(r"[、，,。；;\s的与及和]+", obj_txt) if len(w) >= 2]
                        if any(k in sp for k in keywords):
                            if matched_tier is None or TIER_PRIORITY[obj_tier] > TIER_PRIORITY[matched_tier]:
                                matched_tier = obj_tier
                    tier = "core_underlined" if has_ul else (matched_tier or "familiar")
                    counters[section] += 1
                    items.append({
                        "id": slug_id(f"syl_{section}", counters[section]),
                        "course": "基础医学分论",
                        "subject": "histology",
                        "chapter": f"组胚专题 · {sp[:22]}",
                        "title": sp[:95],
                        "raw_text": sp,
                        "tier": tier,
                        "underlined": has_ul,
                    })
            continue

        # Physiology / Pathophys / Pathology: 【教学内容】 has explicit underline marks!
        if mode == "content":
            # Skip bare section headings that have no punctuation if sub-items follow, unless underlined
            if section == "pathology" and txt in {
                "1.细胞适应的概念、类型及其病理学改变。",
                "2.细胞变性的概念、类型及其病理学改变。",
                "三种组织/细胞变性改变的概念、类型及其病理学改变。",
                "3.细胞死亡的类型（包括坏死、凋亡及自噬）。",
            }:
                subsection = txt
                continue
            counters[section] += 1
            title = (ul_text if has_ul and len(ul_text) >= 4 else txt)
            title = re.sub(r"^\d+[.、]\s*", "", title).split("：")[0].split("；")[0][:95]
            if has_ul:
                tier = "core_underlined"
            else:
                # Infer familiar vs understand from objectives/wording
                tier = "familiar" if ("简介" not in txt and "发展简史" not in txt) else "understand"
                if any(k in txt for k in ["常见肿瘤", "淋巴造血", "神经系统常见肿瘤", "致瘤病毒", "凋亡", "自噬", "铁死亡"]):
                    tier = "familiar"
                elif any(k in txt for k in ["研究的基本方法", "发展简史"]):
                    tier = "understand"
            items.append({
                "id": slug_id(f"syl_{section}", counters[section]),
                "course": "基础医学分论",
                "subject": section,
                "chapter": subsection if section != "pathology" else "病理学总论",
                "title": title,
                "raw_text": txt,
                "tier": tier,
                "underlined": has_ul,
                "underlined_span": ul_text if has_ul else "",
            })

    return items


def parse_anatomy_pdf(pdf_path: Path) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    full_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    # Extract the per-week detailed syllabus section
    m = re.search(r"教学内容\s*\(\s*按64学时共计16周.*?(?=六、\s*课程考核)", full_text, re.S)
    body = m.group(0) if m else full_text

    items: list[dict] = []
    cur_week = "第1周"
    counter = 0
    # Split lines and merge broken wrapped sentences
    raw_lines = [line.strip() for line in body.splitlines() if line.strip()]
    merged: list[str] = []
    for line in raw_lines:
        if line in {"教务小助手", "×"} or line.startswith("教学内容(") or line.startswith("教学内容("):
            continue
        if re.match(r"^(第[一二三四五六七八九十\d]+周|绪论[：:]|骨学及骨|躯干骨|颅骨|上肢骨|下肢骨|肌学总论|头颈肌|躯干肌|上肢肌|下肢肌|内脏学总论|消化系|呼吸系|泌尿系|生殖系|腹膜[：:]|脉管学总论|心[：:]|动脉|静脉|淋巴系统|感官|视器|位听器|神经系总论|脊神经|脑神经|自主神经|一、|二、|三、|四、|五、|六、|七、|八、|九、|十、|十一、|十二、|\d+\s*[.．、]|（[一二三四五六七八九十]+）|附[：:]|临床解剖)", line):
            merged.append(line)
        elif merged:
            merged[-1] += line
        else:
            merged.append(line)

    cur_topic = "绪论"
    for entry in merged:
        w_m = re.match(r"^(第[一二三四五六七八九十\d]+周)[（(].*?[）)](.*)$", entry)
        if w_m:
            cur_week = w_m.group(1)
            rest = w_m.group(2).strip()
            if rest:
                cur_topic = rest[:40]
            continue
        if "：" in entry:
            prefix, rest = entry.split("：", 1)
            if len(prefix) <= 32:
                cur_topic = prefix.strip()
        for clause, tier in split_clauses_with_tiers(entry, default_tier="master"):
            if len(clause) < 6 or clause.startswith("临床解剖讲座"):
                continue
            counter += 1
            items.append({
                "id": slug_id("syl_anatomy", counter),
                "course": "人体解剖学",
                "subject": "anatomy",
                "chapter": f"{cur_week} · {cur_topic}",
                "title": re.sub(r"^(掌握|熟悉|了解)", "", clause).strip(" ：:")[:95],
                "raw_text": clause,
                "tier": tier,
                "underlined": False,
            })
    return items


def parse_biochem_pdf(pdf_path: Path) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    full_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    m = re.search(r"第一章\s*氨基酸与蛋白质.*?(?=六、\s*课程考核)", full_text, re.S)
    body = m.group(0) if m else full_text

    items: list[dict] = []
    cur_chapter = "第一章 氨基酸与蛋白质"
    in_objectives = False
    counter = 0
    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines() if line.strip()]
    merged: list[str] = []
    for line in lines:
        if line in {"教务小助手", "×", "+"}:
            continue
        if re.match(r"^(第[一二三四五六七八九十]+章|目的要求|内容|\d+\s*[.．、]|（[一二三四五六七八九十]+）|[一二三四五六七八九十]+、)", line):
            merged.append(line)
        elif merged:
            merged[-1] += line
        else:
            merged.append(line)

    for entry in merged:
        if re.match(r"^第[一二三四五六七八九十]+章", entry):
            cur_chapter = re.sub(r"[（(].*?[）)]", "", entry).strip()
            in_objectives = False
            continue
        if entry == "目的要求":
            in_objectives = True
            continue
        if entry == "内容" or re.match(r"^[一二三四五六七八九十]+、", entry):
            in_objectives = False
            continue
        if in_objectives:
            for clause, tier in split_clauses_with_tiers(entry, default_tier="master"):
                clean = re.sub(r"^\d+\s*[.．、]\s*", "", clause)
                clean = re.sub(r"^(重点掌握|掌握|熟悉|了解)", "", clean).strip(" ：:")
                if len(clean) < 4:
                    continue
                counter += 1
                items.append({
                    "id": slug_id("syl_biochem", counter),
                    "course": "生物化学",
                    "subject": "biochem",
                    "chapter": cur_chapter,
                    "title": clean[:95],
                    "raw_text": clause,
                    "tier": tier,
                    "underlined": (tier == "core_underlined"),
                })
    return items


def build_backbone(source_dir: Path, out_path: Path) -> dict:
    all_items: list[dict] = []
    docx_file = source_dir / "《基础医学分论》详细教学大纲 20251221.docx"
    anat_pdf = source_dir / "系解教学大纲.pdf"
    bio_pdf = source_dir / "生化教学大纲.pdf"

    if docx_file.exists():
        all_items.extend(parse_jichu_yixue_docx(docx_file))
    if anat_pdf.exists():
        all_items.extend(parse_anatomy_pdf(anat_pdf))
    if bio_pdf.exists():
        all_items.extend(parse_biochem_pdf(bio_pdf))

    summary = {}
    for item in all_items:
        subj = item["subject"]
        summary.setdefault(subj, {"total": 0, "core_underlined": 0, "master": 0, "familiar": 0, "understand": 0})
        summary[subj]["total"] += 1
        summary[subj][item["tier"]] += 1

    backbone = {
        "schema_version": 1,
        "source_dir": str(source_dir),
        "summary": summary,
        "items": all_items,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(backbone, ensure_ascii=False, indent=2), encoding="utf-8")
    return backbone


def main():
    parser = argparse.ArgumentParser(description="Build Fudan medical syllabus backbone JSON.")
    parser.add_argument("--source-dir", default=r"c:\Users\32461\Desktop", help="Directory containing syllabus docx/pdf files")
    parser.add_argument("--out", default=str(DATA_DIR / "syllabus_backbone.json"), help="Output JSON path")
    args = parser.parse_args()
    backbone = build_backbone(Path(args.source_dir), Path(args.out))
    print("Syllabus parsed:", json.dumps(backbone["summary"], ensure_ascii=False))
    print(f"Total items: {len(backbone['items'])} -> {args.out}")


if __name__ == "__main__":
    main()
