"""Automated tests for syllabus backbone and slide ingestion/merge pipeline."""
import json
import sys
import tempfile
from pathlib import Path
from pptx import Presentation

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from ingest_slides import build_prompt_text, extract_courseware, match_syllabus_candidates, merge_delta_into_graph  # noqa: E402
from schema import validate_graph  # noqa: E402


def main():
    graph_path = APP_DIR / "data" / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    validate_graph(graph)
    assert len(graph["syllabus"]) > 600, "Expected full Fudan syllabus backbone"

    # 1. Create a realistic synthetic PPTX inside a temp dir to test extraction & syllabus matching
    with tempfile.TemporaryDirectory() as tmp:
        pptx_path = Path(tmp) / "炎症反应复习测试.pptx"
        prs = Presentation()
        slide1 = prs.slides.add_slide(prs.slide_layouts[1])
        slide1.shapes.title.text = "急性炎症与白细胞渗出"
        slide1.placeholders[1].text = "变质、渗出、增生三种基本病变；白细胞边集、附壁、游出、趋化、吞噬。"
        prs.save(str(pptx_path))

        pages = extract_courseware(pptx_path)
        assert len(pages) == 1 and "趋化" in pages[0]["text"]

        cands = match_syllabus_candidates(pages, graph["syllabus"], top_k=5)
        assert any(c["subject"] == "pathology" for c in cands), "Expected pathology syllabus match"

        prompt = build_prompt_text(pptx_path.name, pages, cands, graph["nodes"])
        assert "bridge_text" in prompt and "anat_portal_vein_system" in prompt

    # 2. Test lossless incremental merge (1 enriched existing node + 1 brand new node + 1 cross-discipline edge)
    delta = {
        "nodes": [
            {
                "id": "patho_edema_mechanisms",
                "title": "水肿发生机制：Starling 平衡失调与淋巴回流受阻",
                "subject": "pathology",
                "syllabus_refs": ["syl_pathology_021"],
                "syllabus_tier": "core_underlined",
                "node_type": "mechanism_flow",
                "anchor_summary": "duplicate placeholder",
                "source_slides": ["基础医学概论 炎症 基础 20241103上传.pdf#P5"],
                "payload": {
                    "steps": [{"cue": "x", "reveal": "y"}, {"cue": "a", "reveal": "b"}],
                    "takeaway": "z",
                },
            },
            {
                "id": "patho_acute_inflammation_exudate",
                "title": "急性炎症渗出液 vs 漏出液鉴别",
                "subject": "pathology",
                "syllabus_refs": ["syl_pathology_023"],
                "syllabus_tier": "core_underlined",
                "node_type": "comparison_matrix",
                "anchor_summary": "炎性渗出液蛋白>30g/L、细胞多、李凡他试验阳性；非炎性漏出液源于静水压↑或胶体压↓。",
                "source_slides": ["基础医学概论 炎症 基础 20241103上传.pdf#P10-P14"],
                "payload": {
                    "columns": ["指标", "渗出液 (Exudate)", "漏出液 (Transudate)"],
                    "rows": [
                        ["本质与原因", "血管通透性↑（炎症）", "静水压↑或胶体渗透压↓（淤血/低蛋白）"],
                        ["蛋白与比重", "> 30 g/L · 比重 > 1.018 · 李凡他(+)", "< 25 g/L · 比重 < 1.015 · 李凡他(-)"],
                    ],
                    "quiz": {
                        "stem": "下列哪项支持胸水为炎性渗出液而非心衰漏出液？",
                        "options": ["蛋白含量 < 20 g/L", "比重 1.010", "李凡他试验阳性且富含中性粒细胞", "外观清亮不易凝固"],
                        "answer_idx": 2,
                        "explain": "炎性渗出液因微血管通透性升高，含大量纤维蛋白原及白细胞，故李凡他试验阳性且易自凝。",
                    },
                },
            },
        ],
        "edges": [
            {
                "id": "e_edema_to_exudate",
                "source": "patho_edema_mechanisms",
                "target": "patho_acute_inflammation_exudate",
                "relation_type": "contrast_with",
                "strength": 0.92,
                "bridge_text": "水肿与积液需分清“淤血滤出”与“炎症渗出”：前者成漏出液，后者因内皮间隙增宽形成高蛋白渗出液。",
            }
        ],
        "threads": [],
    }

    orig_count = len(graph["nodes"])
    merged, stats = merge_delta_into_graph(graph, delta)
    assert stats["nodes_added"] == 1
    assert stats["nodes_enriched"] == 1
    assert stats["edges_added"] == 1
    assert len(merged["nodes"]) == orig_count + 1
    # Verify existing node payload was NOT overwritten, while new slide citation WAS appended
    edema_node = next(n for n in merged["nodes"] if n["id"] == "patho_edema_mechanisms")
    assert len(edema_node["payload"]["steps"]) == 3
    assert "基础医学概论 炎症 基础 20241103上传.pdf#P5" in edema_node["source_slides"]

    print("PASS test_ingest: syllabus matching, prompt-pack, and lossless incremental merge verified.")


if __name__ == "__main__":
    main()
