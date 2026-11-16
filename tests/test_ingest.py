"""Automated tests for syllabus backbone, logic context export, audit checks, and lossless merge."""
import json
import sys
import tempfile
from pathlib import Path
from pptx import Presentation

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from ingest_slides import (  # noqa: E402
    build_prompt_text,
    crop_asset_image,
    extract_courseware,
    extract_pdf,
    match_syllabus_candidates,
    merge_delta_into_graph,
    resolve_lecture_metadata,
)
from PIL import Image  # noqa: E402
from pypdf import PdfWriter  # noqa: E402
from schema import validate_graph  # noqa: E402


def main():
    graph_path = APP_DIR / "data" / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    validate_graph(graph)
    assert len(graph["syllabus"]) > 600, "Expected full Fudan syllabus backbone"

    # 1. Context export must include relevant nodes, existing threads, and logic edges (not blind [:120] slice)
    with tempfile.TemporaryDirectory() as tmp:
        pptx_path = Path(tmp) / "炎症反应复习测试.pptx"
        prs = Presentation()
        slide1 = prs.slides.add_slide(prs.slide_layouts[1])
        slide1.shapes.title.text = "急性炎症与白细胞渗出"
        slide1.placeholders[1].text = "变质、渗出、增生三种基本病变；白细胞边集、附壁、游出、趋化、吞噬；与水肿漏出液鉴别。"
        prs.save(str(pptx_path))

        pages = extract_courseware(pptx_path)
        assert len(pages) == 1 and "趋化" in pages[0]["text"]

        cands = match_syllabus_candidates(pages, graph["syllabus"], top_k=5)
        assert any(c["subject"] == "pathology" for c in cands), "Expected pathology syllabus match"

        prompt = build_prompt_text(pptx_path.name, pages, cands, graph)
        assert "thread_congestion_portal" in prompt, "Context must expose existing LogicThread spines"
        assert "patho_edema_mechanisms" in prompt, "Context must surface slide-relevant existing nodes"
        assert "thread_extensions" in prompt, "Prompt must document thread_extensions grafting"
        assert "病理学 (`pathology`) 专属拆解语法" in prompt, "Prompt must inject pathology discipline playbook"

        # Also verify pharmacology slide triggers pharmacology-specific playbook
        pharma_prompt = build_prompt_text(
            "解热镇痛药理.pptx",
            [{"page": 1, "text": "药理学：阿司匹林抑制COX-1与COX-2受体机制、临床解热镇痛应用及胃肠道不良反应与半衰期"}],
            [],
            graph,
        )
        assert "药理学 (`pharmacology`) 专属拆解语法" in pharma_prompt, "Prompt must inject pharmacology discipline playbook"

    # 2. Test lossless incremental merge + thread extension + logic audit
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
                "bridge_text": "淤血与低蛋白导致单纯漏出液，而炎症因内皮细胞收缩拉大间隙释放高蛋白纤维素，因而必须用渗出液与漏出液加以区分。",
            },
            {
                "id": "e_edema_prereq_exudate",
                "source": "patho_edema_mechanisms",
                "target": "patho_acute_inflammation_exudate",
                "relation_type": "prerequisite",
                "strength": 0.9,
                "bridge_text": "理解Starling静水压与胶体渗透压平衡，才能解释为何仅靠流体静力压改变形成的漏出液蛋白低于25g/L。",
            },
        ],
        "thread_extensions": [
            {
                "thread_id": "thread_congestion_portal",
                "append_steps": [
                    {
                        "node_id": "patho_acute_inflammation_exudate",
                        "incoming_edge_id": "e_edema_to_exudate",
                    }
                ],
            }
        ],
        "threads": [],
    }

    orig_count = len(graph["nodes"])
    merged, stats = merge_delta_into_graph(graph, delta)
    assert stats["nodes_added"] == 1
    assert stats["nodes_enriched"] == 1
    assert stats["edges_added"] == 2
    assert stats["threads_extended"] == 1
    assert len(merged["nodes"]) == orig_count + 1
    assert stats["logic_audit"]["orphan_count"] == 0
    assert stats["logic_audit"]["rescue_anchors"] == 1
    assert stats["logic_audit"]["contrast_edges"] == 1
    assert "syl_pathology_023" in stats["logic_audit"]["newly_covered_syllabus_refs"]
    assert stats["logic_audit"]["warnings"] == []

    ext_th = next(t for t in merged["threads"] if t["id"] == "thread_congestion_portal")
    assert ext_th["ordered_steps"][-1]["node_id"] == "patho_acute_inflammation_exudate"

    # 3. Verify logic audit catches weak filler bridge_text & orphan nodes
    bad_delta = {
        "nodes": [
            {
                "id": "patho_orphan_demo",
                "title": "孤立测试节点",
                "subject": "pathology",
                "syllabus_refs": [],
                "syllabus_tier": "master",
                "node_type": "structure_cloze",
                "anchor_summary": "测试孤立警告",
                "source_slides": [],
                "payload": {"bullets": ["测试{{胶囊}}"]},
            }
        ],
        "edges": [
            {
                "id": "e_filler_demo",
                "source": "patho_edema_mechanisms",
                "target": "patho_fatty_change",
                "relation_type": "causal_next",
                "strength": 0.7,
                "bridge_text": "接下来学习脂肪变",
            }
        ],
    }
    _, bad_stats = merge_delta_into_graph(graph, bad_delta)
    assert bad_stats["logic_audit"]["orphan_count"] == 1
    assert any("Discipline logic gap" in w for w in bad_stats["logic_audit"]["warnings"])
    assert len(bad_stats["logic_audit"]["warnings"]) >= 3

    # 4. V2 manifest lookup, blank-page preservation + visual flagging, full raw syllabus, and crop/quiz schema
    ct_meta = resolve_lecture_metadata(Path(r"C:\Users\32461\Desktop\结缔组织（基础医学分论，2025秋）V.pdf"))
    assert ct_meta["primary_subject"] == "histology" and ct_meta["manifest_hit"]
    fever_meta = resolve_lecture_metadata(Path(r"C:\Users\32461\Desktop\发热20251030.pdf"))
    assert fever_meta["primary_subject"] == "pathophys" and fever_meta["manifest_hit"]

    with tempfile.TemporaryDirectory() as tmp2:
        pdf_path = Path(tmp2) / "blank_vis_test.pdf"
        w = PdfWriter()
        w.add_blank_page(width=400, height=300)
        with pdf_path.open("wb") as f:
            w.write(f)
        pdf_pages = extract_pdf(pdf_path)
        assert len(pdf_pages) == 1, "Blank/image-only PDF pages must NOT be silently dropped"
        assert pdf_pages[0]["needs_vision"] and "sparse_or_image_only" in pdf_pages[0]["vision_reasons"]

        # Test crop_asset_image & clean cleanup inside ASSETS_DIR
        src_img = Path(tmp2) / "raw_slide.png"
        Image.new("RGB", (800, 600), color=(240, 248, 255)).save(src_img)
        cropped = crop_asset_image(src_img, "__test_v2_crop.webp", "0.1,0.1,0.9,0.9")
        try:
            assert cropped["rel_path"] == "assets/__test_v2_crop.webp"
            assert Path(cropped["abs_path"]).exists()
            quiz_delta = {
                "nodes": [
                    {
                        "id": "patho_test_v2_quiz_asset",
                        "title": "细胞水肿机制测试卡",
                        "subject": "pathology",
                        "syllabus_refs": ["syl_pathology_002"],
                        "syllabus_tier": "core_underlined",
                        "node_type": "mechanism_flow",
                        "anchor_summary": "ATP减少致钠泵衰竭从而引起水钠潴留。",
                        "source_slides": ["test#P1"],
                        "diagram_assets": [{"path": "assets/__test_v2_crop.webp", "caption": "测试通路图", "source_page": 1}],
                        "payload": {
                            "steps": [{"cue": "ATP不足时首先受阻的是？", "reveal": "Na+-K+泵衰竭", "mechanism_tag": "泵衰竭"},
                                      {"cue": "胞内Na+上升导致？", "reveal": "水随渗透压进入引起颗粒变性", "mechanism_tag": "水肿"}],
                            "takeaway": "能量障碍直接引发膜泵失灵与水钠潴留。",
                            "quiz": {
                                "stem": "氰化物抑制氧化磷酸化后早期胞内改变是？",
                                "options": ["细胞内Na+和水量↑", "K+内流骤增"],
                                "answer_idx": 0,
                                "explain": "ATP生成中断令钠泵停摆。",
                                "remedial_node_id": "physio_membrane_transport",
                            },
                        },
                    }
                ],
                "edges": [
                    {
                        "id": "e_transport_to_v2_quiz",
                        "source": "physio_membrane_transport",
                        "target": "patho_test_v2_quiz_asset",
                        "relation_type": "prerequisite",
                        "strength": 0.92,
                        "bridge_text": "Na+-K+泵耗ATP逆梯度泵出Na+维持细胞容积，其崩溃直接造成细胞水肿。",
                    }
                ],
                "threads": [],
            }
            _, v2_stats = merge_delta_into_graph(graph, quiz_delta)
            assert v2_stats["logic_audit"]["quiz_nodes_added"] == 1
            assert v2_stats["logic_audit"]["diagram_assets_added"] == 1
            assert v2_stats["logic_audit"]["warnings"] == []
        finally:
            Path(cropped["abs_path"]).unlink(missing_ok=True)

    full_prompt = build_prompt_text(
        "发热20251030.pdf",
        [{"page": 1, "text": "发热机制", "char_count": 4, "embedded_images": 0, "needs_vision": True, "vision_reasons": ["sparse_or_image_only"], "heading": "发热"}],
        [],
        graph,
        primary_subject="pathophys",
        curriculum_name="基础医学分论（整合课程）",
    )
    assert "体温调节的方式及发热的时相" in full_prompt, "Full raw_text must not be truncated in V2 prompt"
    assert "Phase 0.5" in full_prompt and "crop-asset" in full_prompt

    # 5. Test continuation chunk omits redundant header + template watermark image dedup in multi-slide deck
    cont_prompt = build_prompt_text(
        "糖代谢2026.pdf",
        [{"page": 26, "text": "PFK-1 别构激活", "char_count": 10, "embedded_images": 0, "needs_vision": False, "vision_reasons": [], "heading": "PFK-1"}],
        [],
        graph,
        primary_subject="biochem",
        include_atlas=False,
        chunk_idx=2,
        total_chunks=4,
    )
    assert "公共头部已省略" in cont_prompt and "## Phase 0.5" not in cont_prompt

    with tempfile.TemporaryDirectory() as tmp3:
        wm_pptx = Path(tmp3) / "watermark_dedup.pptx"
        wm_img = Path(tmp3) / "crest_watermark.png"
        uniq_img = Path(tmp3) / "unique_pathway.png"
        Image.new("RGB", (200, 200), color=(220, 220, 220)).save(wm_img)
        Image.new("RGB", (600, 400), color=(20, 110, 160)).save(uniq_img)
        prs_wm = Presentation()
        blank_layout = prs_wm.slide_layouts[1]
        for s_idx in range(1, 6):
            sl = prs_wm.slides.add_slide(blank_layout)
            sl.shapes.title.text = f"Slide {s_idx} Metabolic Step {s_idx}"
            sl.placeholders[1].text = "催化限速酶调控底物磷酸化与能量偶联生成充足ATP驱动代谢通量向前推进" * 2
            sl.shapes.add_picture(str(wm_img), 0, 0)
            if s_idx == 3:
                sl.shapes.add_picture(str(uniq_img), 100000, 100000)
        prs_wm.save(str(wm_pptx))
        wm_pages = extract_courseware(wm_pptx)
        assert [p["embedded_images"] for p in wm_pages] == [0, 0, 1, 0, 0], f"Repeated master logo must be filtered: {[p['embedded_images'] for p in wm_pages]}"

    print("PASS test_ingest: logic-first context, thread grafting, V2 visual/syllabus/quiz audit, and lossless merge verified.")


if __name__ == "__main__":
    main()
