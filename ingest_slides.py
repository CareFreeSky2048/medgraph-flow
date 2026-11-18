"""Daily incremental courseware compiler for MedGraph Flow.

Supports:
1. Extracting text from .pptx, .pdf, .docx (plus legacy .ppt/.doc via PowerPoint/Word COM on Windows)
2. Matching slide contents to Fudan syllabus_backbone.json items (preserving core_underlined weights)
3. Generating zero-API-key Prompt Packs (--mode prompt-pack) for any free chat/CLI LLM
4. Calling free OpenAI-compatible or Gemini API endpoints via env vars (--mode api)
5. Losslessly merging delta JSON into data/graph.json while validating schema & never touching progress
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
ASSETS_DIR = DATA_DIR / "assets"
INBOX_DIR = APP_DIR / "inbox"

from schema import NODE_TYPES, RELATION_TYPES, SUBJECTS, SYLLABUS_TIERS, validate_graph  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_course_manifest() -> dict:
    manifest_path = DATA_DIR / "course_manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"curricula": [], "lectures": []}


def resolve_lecture_metadata(slide_path: Path, explicit_subject: str | None = None) -> dict:
    """Resolve curriculum_id & declared subject from explicit CLI flag or authoritative course_manifest.json."""
    manifest = load_course_manifest()
    norm_target = str(slide_path).lower()
    stem_target = slide_path.stem.lower()
    matched = None
    for lec in manifest.get("lectures", []):
        lpath = Path(lec["path"])
        if str(lpath).lower() == norm_target or lpath.name.lower() == slide_path.name.lower() or lpath.stem.lower() == stem_target:
            matched = lec
            break

    subject = explicit_subject or (matched["subject"] if matched else None)
    sub_subjects = (matched.get("sub_subjects") if matched else None) or ([subject] if subject else [])
    curriculum_id = matched.get("curriculum_id") if matched else None
    cur_meta = next((c for c in manifest.get("curricula", []) if c["id"] == curriculum_id), None)
    return {
        "primary_subject": subject,
        "sub_subjects": sub_subjects,
        "curriculum_id": curriculum_id,
        "curriculum_name": cur_meta["name"] if cur_meta else None,
        "syllabus_path": cur_meta["syllabus_path"] if cur_meta else None,
        "manifest_hit": bool(matched),
    }


def _iter_pptx_shapes(shapes):
    for shape in shapes:
        yield shape
        if hasattr(shape, "shapes"):
            yield from _iter_pptx_shapes(shape.shapes)


def _inspect_image_blob(raw_bytes: bytes) -> tuple[str, bool]:
    """Return (short_md5, is_nontrivial_size) filtering tiny <120px decorative icons/logos."""
    digest = hashlib.md5(raw_bytes).hexdigest()[:12]
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
        return digest, (max(w, h) >= 120 and min(w, h) >= 32)
    except Exception:
        # Unknown vector/WMF or undecodable stream: keep conservatively if >4 KB
        return digest, len(raw_bytes) >= 4096


def _finalize_slide_pages(raw_pages: list[dict], visual_dir: Path | None = None, prefix: str = "s") -> list[dict]:
    """Deduplicate master-template watermarks appearing on >=40% of slides before flagging MUST_VIEW."""
    total_pages = len(raw_pages)
    hash_page_freq: dict[str, int] = defaultdict(int)
    for rec in raw_pages:
        for h in {img["hash"] for img in rec.get("_images", [])}:
            hash_page_freq[h] += 1

    template_hashes: set[str] = set()
    if total_pages >= 5:
        repeat_thresh = max(3, int(total_pages * 0.40))
        template_hashes = {h for h, cnt in hash_page_freq.items() if cnt >= repeat_thresh}

    pages = []
    for rec in raw_pages:
        text = rec["text"]
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        sig_imgs = [img for img in rec.get("_images", []) if img["hash"] not in template_hashes]
        img_count = len(sig_imgs)
        group_count = rec.get("_group_count", 0)
        short_lines = sum(1 for ln in lines if len(ln) <= 6)
        short_ratio = (short_lines / len(lines)) if lines else 0.0
        label_lines = sum(1 for ln in lines if len(ln) <= 16 and not re.search(r"[。！？；.!?;]", ln))
        label_ratio = (label_lines / len(lines)) if lines else 0.0

        has_vector = any(img.get("is_vector") for img in sig_imgs)

        reasons = []
        if len(text) < 45:
            reasons.append("sparse_or_image_only")
        if img_count > 0:
            reasons.append(f"embedded_images:{img_count}")
        if has_vector:
            reasons.append("wmf_vector_media")
        if group_count >= 2 or rec.get("_complex_group"):
            reasons.append(f"grouped_diagrams:{max(group_count, 1)}")
        if len(lines) >= 10 and (short_ratio >= 0.45 or (label_lines >= 10 and label_ratio >= 0.60)):
            diag_pct = max(short_ratio, label_ratio)
            reasons.append(f"fragmented_diagram:{diag_pct:.0%}")

        img_paths = []
        if visual_dir is not None and sig_imgs:
            visual_dir.mkdir(parents=True, exist_ok=True)
            for k, img in enumerate(sig_imgs, 1):
                if img.get("blob"):
                    ext = img.get("ext", "png")
                    out_img = visual_dir / f"{prefix}{rec['page']:03d}_img{k}.{ext}"
                    blob = img["blob"]
                    try:
                        from PIL import Image

                        with Image.open(io.BytesIO(blob)) as raw_im:
                            if max(raw_im.size) > 2048:
                                resized = raw_im.copy()
                                resized.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                                buf = io.BytesIO()
                                fmt = "JPEG" if ext.lower() in {"jpg", "jpeg"} and resized.mode in {"RGB", "L"} else "PNG"
                                if fmt == "PNG" and out_img.suffix.lower() != ".png":
                                    out_img = visual_dir / f"{prefix}{rec['page']:03d}_img{k}.png"
                                save_kw = {"quality": 90, "optimize": True} if fmt == "JPEG" else {"optimize": True}
                                resized.save(buf, format=fmt, **save_kw)
                                blob = buf.getvalue()
                    except Exception:
                        pass
                    out_img.write_bytes(blob)
                    img_paths.append(str(out_img))

        pages.append({
            "page": rec["page"],
            "text": text,
            "char_count": len(text),
            "heading": lines[0][:60] if lines else rec.get("_empty_heading", "[纯图/无提取文本页]"),
            "embedded_images": img_count,
            "ignored_template_images": len(rec.get("_images", [])) - img_count,
            "needs_vision": bool(reasons),
            "vision_reasons": reasons,
            "visual_files": img_paths,
        })
    return pages


def extract_pptx(path: Path, visual_dir: Path | None = None) -> list[dict]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(str(path))
    raw_pages = []
    for idx, slide in enumerate(prs.slides, 1):
        chunks = []
        sig_images = []
        group_count = 0
        complex_group = False
        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                group_count += 1
                try:
                    if len(list(shape.shapes)) >= 3:
                        complex_group = True
                except Exception:
                    pass
        for shape in _iter_pptx_shapes(slide.shapes):
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
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    img = shape.image
                    img_ext = (img.ext or "png").lower()
                    is_vector = img_ext in {"wmf", "emf"}
                    digest, nontrivial = _inspect_image_blob(img.blob)
                    if nontrivial:
                        sig_images.append({
                            "hash": digest,
                            "ext": img_ext,
                            "is_vector": is_vector,
                            "blob": (img.blob if (visual_dir is not None and not is_vector) else None),
                        })
                except Exception:
                    pass

        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        text = "\n".join(chunks).strip()
        if notes:
            text = f"{text}\n[讲义备注] {notes}".strip()

        raw_pages.append({
            "page": idx,
            "text": text,
            "_images": sig_images,
            "_group_count": group_count,
            "_complex_group": complex_group,
            "_empty_heading": "[纯图/无文本页]",
        })
    return _finalize_slide_pages(raw_pages, visual_dir=visual_dir, prefix="s")


def extract_pdf(path: Path) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    raw_pages = []
    for idx, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        sig_images = []
        try:
            for im in page.images:
                digest, nontrivial = _inspect_image_blob(im.data)
                if nontrivial:
                    sig_images.append({"hash": digest})
        except Exception:
            pass
        raw_pages.append({
            "page": idx,
            "text": text,
            "_images": sig_images,
            "_group_count": 0,
            "_empty_heading": "[纯图/无提取文本页]",
        })
    return _finalize_slide_pages(raw_pages, visual_dir=None)


def _resolve_pdf_render_dpi(pdf_path: Path, explicit_dpi: int | None = None) -> int:
    """Use 280 DPI automatically for portrait PDFs (typically 2-up/4-up multi-slide handouts) so cropped figures remain sharp."""
    if explicit_dpi is not None:
        return int(explicit_dpi)
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        if reader.pages:
            box = reader.pages[0].mediabox
            if float(box.height) > float(box.width) * 1.1:
                return 280
    except Exception:
        pass
    return 150


def render_pdf_visual_pages(pdf_path: Path, pages: list[dict], visual_dir: Path, dpi: int | None = None) -> int:
    """Rasterize flagged diagram/image-heavy PDF pages using Poppler pdftoppm for multimodal reading."""
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return 0
    visual_dir.mkdir(parents=True, exist_ok=True)
    effective_dpi = _resolve_pdf_render_dpi(pdf_path, dpi) if any(p.get("needs_vision") for p in pages) else 150
    rendered = 0
    for p in pages:
        if not p.get("needs_vision"):
            continue
        pg = p["page"]
        target_png = visual_dir / f"p{pg:03d}.png"
        if target_png.exists():
            p.setdefault("visual_files", []).append(str(target_png))
            rendered += 1
            continue
        prefix = visual_dir / f"_tmp_p{pg:03d}"
        try:
            subprocess.run(
                [pdftoppm, "-png", "-r", str(effective_dpi), "-f", str(pg), "-l", str(pg), str(pdf_path), str(prefix)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            matches = sorted(visual_dir.glob(f"_tmp_p{pg:03d}*.png"))
            if matches:
                matches[0].replace(target_png)
                for extra in matches[1:]:
                    extra.unlink(missing_ok=True)
                p.setdefault("visual_files", []).append(str(target_png))
                rendered += 1
        except Exception:
            continue
    return rendered


def crop_asset_image(image_path: Path, out_name: str, box_spec: str = "full", max_width: int = 1200) -> dict:
    """Crop a clean pathway diagram or morphological plate from a rendered slide PNG into data/assets/."""
    from PIL import Image

    if not image_path.exists():
        raise FileNotFoundError(f"Source image not found: {image_path}")
    safe_name = Path(out_name).name
    if not safe_name.lower().endswith((".webp", ".jpg", ".jpeg", ".png")):
        safe_name = f"{Path(safe_name).stem}.webp"

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ASSETS_DIR / safe_name

    with Image.open(image_path) as raw_im:
        has_alpha = raw_im.mode in {"RGBA", "LA"} or ("transparency" in raw_im.info)
        if has_alpha:
            rgba = raw_im.convert("RGBA")
            white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(white_bg, rgba).convert("RGB")
        else:
            im = raw_im.convert("RGB")
        w, h = im.size
        if box_spec and box_spec.lower() != "full":
            parts = [float(x.strip()) for x in box_spec.split(",")]
            if len(parts) != 4:
                raise ValueError("--box must be 'full' or 'x1,y1,x2,y2' normalized in 0..1")
            x1, y1, x2, y2 = parts
            if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
                raise ValueError(f"Normalized crop coordinates out of bounds: {parts}")
            im = im.crop((int(w * x1), int(h * y1), int(w * x2), int(h * y2)))
        cw, ch = im.size
        if cw > max_width:
            nh = max(1, int(ch * (max_width / cw)))
            im = im.resize((max_width, nh), Image.Resampling.LANCZOS)
        fmt = "WEBP" if out_path.suffix.lower() == ".webp" else ("JPEG" if out_path.suffix.lower() in {".jpg", ".jpeg"} else "PNG")
        save_kwargs = {"quality": 86, "method": 5} if fmt == "WEBP" else ({"quality": 88, "optimize": True} if fmt == "JPEG" else {"optimize": True})
        im.save(out_path, format=fmt, **save_kwargs)
        fw, fh = im.size

    return {
        "rel_path": f"assets/{safe_name}",
        "abs_path": str(out_path),
        "width": fw,
        "height": fh,
        "size_kb": round(out_path.stat().st_size / 1024, 1),
    }


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
        pres = None
        try:
            pres = app.Presentations.Open(str(path.resolve()), WithWindow=False)
            pres.SaveAs(str(out_path), 24)  # ppSaveAsOpenXMLPresentation
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        finally:
            if pres is not None:
                try:
                    pres.Close()
                except Exception:
                    pass
            try:
                if app.Presentations.Count == 0:
                    app.Quit()
            except Exception:
                pass
        return out_path
    if suffix == ".doc":
        out_path = tmp_dir / f"{path.stem}.docx"
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = False
        doc = None
        try:
            doc = app.Documents.Open(str(path.resolve()))
            doc.SaveAs2(str(out_path), FileFormat=16)  # wdFormatDocumentDefault
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        finally:
            if doc is not None:
                try:
                    doc.Close()
                except Exception:
                    pass
            try:
                if app.Documents.Count == 0:
                    app.Quit()
            except Exception:
                pass
        return out_path
    shutil.rmtree(tmp_dir, ignore_errors=True)
    raise ValueError(f"Unsupported legacy suffix: {suffix}")


def render_pptx_via_com_pdf(pptx_path: Path, pages: list[dict], visual_dir: Path, dpi: int = 150) -> int:
    """Export PPTX via PowerPoint COM to temp PDF and rasterize flagged slides as composite pXXX.png."""
    if not shutil.which("pdftoppm"):
        return 0
    flagged = [p for p in pages if p.get("needs_vision")]
    if not flagged:
        return 0
    pending = [p for p in flagged if not (visual_dir / f"p{p['page']:03d}.png").exists()]
    if not pending:
        attached = 0
        for p in flagged:
            target_png = visual_dir / f"p{p['page']:03d}.png"
            vf = p.setdefault("visual_files", [])
            if target_png.exists() and str(target_png) not in vf:
                vf.insert(0, str(target_png))
                attached += 1
        return attached

    tmp_dir = Path(tempfile.mkdtemp(prefix="medgraph_pptx_pdf_"))
    tmp_pdf = tmp_dir / f"{pptx_path.stem}.pdf"
    try:
        import win32com.client  # type: ignore

        app = win32com.client.Dispatch("PowerPoint.Application")
        pres = None
        try:
            pres = app.Presentations.Open(str(pptx_path.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
            pres.SaveAs(str(tmp_pdf), 32)  # ppSaveAsPDF
        finally:
            if pres is not None:
                try:
                    pres.Close()
                except Exception:
                    pass
            try:
                if app.Presentations.Count == 0:
                    app.Quit()
            except Exception:
                pass
        if not tmp_pdf.exists():
            return 0
        before_lens = {p["page"]: len(p.get("visual_files", [])) for p in pages}
        rendered = render_pdf_visual_pages(tmp_pdf, pages, visual_dir, dpi=dpi)
        for p in pages:
            vf = p.get("visual_files", [])
            if len(vf) > before_lens.get(p["page"], 0):
                composite_png = vf.pop()
                if composite_png not in vf:
                    vf.insert(0, composite_png)
        return rendered
    except Exception:
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def extract_courseware(path: Path, visual_dir: Path | None = None, render_visuals: bool = False, dpi: int | None = None) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Courseware not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pptx":
        pages = extract_pptx(path, visual_dir=visual_dir if render_visuals else None)
        if render_visuals and visual_dir is not None:
            render_pptx_via_com_pdf(path, pages, visual_dir, dpi=dpi or 150)
        return pages
    if suffix == ".pdf":
        pages = extract_pdf(path)
        if render_visuals and visual_dir is not None:
            render_pdf_visual_pages(path, pages, visual_dir, dpi=dpi)
        return pages
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in {".ppt", ".doc"}:
        converted = convert_legacy_office(path)
        try:
            if suffix == ".ppt":
                pages = extract_pptx(converted, visual_dir=visual_dir if render_visuals else None)
                if render_visuals and visual_dir is not None:
                    render_pptx_via_com_pdf(converted, pages, visual_dir)
                return pages
            return extract_docx(converted)
        finally:
            shutil.rmtree(converted.parent, ignore_errors=True)
    raise ValueError(f"Unsupported courseware extension: {suffix}")


def bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]+", "", text.lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def match_syllabus_candidates(
    pages: list[dict],
    syllabus_items: list[dict],
    top_k: int = 20,
    primary_subject: str | None = None,
) -> list[dict]:
    combined = "\n".join(p.get("text", "") for p in pages[:80])
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
        if primary_subject and item.get("subject") == primary_subject:
            score *= 1.5
        elif item.get("tier") == "core_underlined":
            score *= 1.15
        if score > 0.22:
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


def select_relevant_nodes_and_edges(pages: list[dict], graph: dict, limit: int = 120) -> tuple[list[dict], list[dict]]:
    """Rank existing nodes by relevance to incoming slides + hub connectivity (high ceiling avoids blind clipping)."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return [], []

    combined_bg = bigrams("\n".join(p.get("text", "") for p in pages[:80]))
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
    relevant_edges = [e for e in edges if e["source"] in picked_ids or e["target"] in picked_ids][:90]
    return picked_nodes, relevant_edges


def format_syl_raw_line(s: dict, covered_syl_map: dict[str, list[str]]) -> str:
    cov = covered_syl_map.get(s["id"], [])
    cov_tag = f"已建图: {','.join(cov[:4])}" if cov else "⚠️ 空白"
    tier_mark = "★重点/下划线" if s["tier"] == "core_underlined" else s["tier"]
    raw = str(s.get("raw_text") or s.get("title") or "").strip()
    ul = str(s.get("underlined_span") or "").strip()
    ul_note = f" 〔★下划线核心: {ul}〕" if ul and ul != raw else ""
    return f"- `{s['id']}` ({s['chapter']} | {tier_mark} | {cov_tag}): {raw}{ul_note}"


def format_full_syllabus_locator(
    graph: dict,
    primary_subj: str,
    syllabus_candidates: list[dict],
    covered_syl_map: dict[str, list[str]],
) -> tuple[str, str]:
    """Build untruncated raw syllabus panorama for primary subject + cross-curriculum ownership map."""
    all_syl = graph.get("syllabus", [])
    primary_all = [s for s in all_syl if s.get("subject") == primary_subj]
    cand_ids = {s["id"] for s in syllabus_candidates}

    if primary_subj == "anatomy" and len(primary_all) > 80:
        # Summarise all 413 weekly chapters + expand full raw_text for matched chapters & candidates
        by_chap: dict[str, list[dict]] = defaultdict(list)
        matched_chaps = {s["chapter"] for s in syllabus_candidates if s.get("subject") == "anatomy"}
        for s in primary_all:
            by_chap[s["chapter"]].append(s)
        chap_summary = []
        expanded = []
        for ch, items in by_chap.items():
            cov_n = sum(1 for it in items if it["id"] in covered_syl_map)
            mark = "【◀疑似本课】" if ch in matched_chaps else ""
            chap_summary.append(f"- {ch} ({len(items)}条, 已建{cov_n}条){mark}")
            if ch in matched_chaps:
                expanded.extend(items)
        if not expanded:
            expanded = [s for s in syllabus_candidates if s.get("subject") == "anatomy"] or primary_all[:45]
        primary_block = (
            "#### 解剖学全学期章节概览（未展开章节可按 `syllabus_backbone.json` 检索）：\n"
            + "\n".join(chap_summary)
            + "\n\n#### 命中章节完整原始大纲条目（未截断 `raw_text`）：\n"
            + "\n".join(format_syl_raw_line(s, covered_syl_map) for s in expanded[:60])
        )
    else:
        primary_block = "\n".join(
            format_syl_raw_line(s, covered_syl_map) for s in primary_all
        ) or "- （该学科暂无大纲条目）"

    # Cross-curriculum / sibling-discipline ownership map
    sibling_lines = []
    other_subjs = [s for s in ("histology", "embryology", "physiology", "pathophys", "pathology", "pharmacology", "biochem", "anatomy") if s != primary_subj]
    for subj in other_subjs:
        items = [s for s in all_syl if s.get("subject") == subj]
        if not items:
            continue
        prof = SUBJECT_LOGIC_PROFILES.get(subj, {"label": subj})
        cov_cnt = sum(1 for it in items if it["id"] in covered_syl_map)
        # Highlight chapter overview + candidate/already-covered anchors in sibling subjects
        by_ch: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            by_ch[it["chapter"]].append(it)
        ch_tags = [
            f"{ch}({sum(1 for x in its if x['id'] in covered_syl_map)}/{len(its)})"
            for ch, its in by_ch.items()
        ]
        sibling_lines.append(f"**{prof['label']} (`{subj}`) — 共 {len(items)} 条（已建 {cov_cnt} 条）**：{' · '.join(ch_tags[:12])}")
        # Expand specific sibling items matched by content or already linked in graph
        highlights = [
            it for it in items
            if it["id"] in cand_ids or it["id"] in covered_syl_map
        ][:10]
        for h in highlights:
            sibling_lines.append("  " + format_syl_raw_line(h, covered_syl_map))

    cross_block = "\n".join(sibling_lines) or "- （无他科大纲条目）"
    return primary_block, cross_block


def build_prompt_text(
    slide_name: str,
    pages: list[dict],
    syllabus_candidates: list[dict],
    graph_or_nodes: dict | list[dict],
    primary_subject: str | None = None,
    curriculum_name: str | None = None,
    include_atlas: bool = True,
    chunk_idx: int = 1,
    total_chunks: int = 1,
    all_pages_for_index: list[dict] | None = None,
) -> str:
    graph = graph_or_nodes if isinstance(graph_or_nodes, dict) else {"nodes": graph_or_nodes, "edges": [], "threads": []}
    index_pages = all_pages_for_index or pages
    if primary_subject and primary_subject in SUBJECT_LOGIC_PROFILES:
        heuristic_subjs = [s for s in detect_slide_subjects(index_pages, syllabus_candidates, slide_name) if s != primary_subject]
        detected_subjs = [primary_subject] + heuristic_subjs[:2]
        subject_source = "显式指定 / 课程清单锁定"
    else:
        detected_subjs = detect_slide_subjects(index_pages, syllabus_candidates, slide_name)
        subject_source = "启发式回退识别"
    primary_subj = detected_subjs[0]

    skeleton_lines = []
    page_blocks = []
    for p in pages:
        pg = p["page"]
        chars = p.get("char_count", len(p.get("text", "")))
        imgs = p.get("embedded_images", 0)
        reasons = p.get("vision_reasons", [])
        vfiles = p.get("visual_files", [])
        v_flag = f"🖼️ MUST_VIEW ({', '.join(reasons)})" if p.get("needs_vision") else "文本页"
        skeleton_lines.append(f"- P{pg:02d} | {chars}字 | {imgs}图 | {v_flag} | {p.get('heading', '')}")

        header_meta = f" [{chars}字 | {imgs}图 | {v_flag}]" if (p.get("needs_vision") or imgs or chars < 45) else ""
        vis_note = ""
        if vfiles:
            has_composite = any(
                Path(vf).name.startswith("p") and Path(vf).suffix.lower() == ".png"
                for vf in vfiles
            )
            vis_lines = []
            for vf in vfiles:
                vpath = Path(vf)
                if has_composite and not (vpath.name.startswith("p") and vpath.suffix.lower() == ".png"):
                    vis_lines.append(
                        f"🔎 原始内嵌图备查（复合页已渲染；仅当局部过小/被遮挡或需完整无字底图裁剪时再读）: `{vf}`"
                    )
                elif has_composite:
                    vis_lines.append(f"📷 视觉页/复合幻灯片（务必用 read_file 亲眼读取）: `{vf}`")
                else:
                    vis_lines.append(f"📷 视觉页/内嵌图（务必用 read_file 亲眼读取）: `{vf}`")
            vis_note = "\n" + "\n".join(vis_lines)
        body_txt = p.get("text", "").strip() or "[⚠️ 此页无可用纯文本或极稀疏，必须结合视觉原图读取内容！]"
        page_blocks.append(f"### Page/Slide {pg}{header_meta}{vis_note}\n{body_txt}")

    skeleton_txt = "\n".join(skeleton_lines)
    pages_txt = "\n".join(page_blocks)
    cur_label = curriculum_name or "按大纲与文件属性定位"

    if not include_atlas:
        p_start = pages[0]["page"] if pages else "?"
        p_end = pages[-1]["page"] if pages else "?"
        return f"""# 继续编译 `{slide_name}` · 第 {chunk_idx}/{total_chunks} 分块（P{p_start}–P{p_end}）
> 主学科：`{primary_subj}`（{subject_source}） · 课程：{cur_label}
> **公共头部已省略（防重复占用上下文）**：完整 `Phase 0.5` 原始大纲坐标、子学科拆解语法、已有图谱节点/主线/边参考及 JSON 严格规范请直接查看同目录下 `part1_context.md`。

## Phase 1 · 本分块逐页骨架与视觉待检清单
{skeleton_txt}

## 原始课件正文与页面标引
{pages_txt}
"""

    picked_nodes, relevant_edges = select_relevant_nodes_and_edges(index_pages, graph, limit=120)

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
        for e in relevant_edges[:45]
    )

    covered_syl_map: dict[str, list[str]] = {}
    for n in graph.get("nodes", []):
        for ref in n.get("syllabus_refs", []):
            covered_syl_map.setdefault(ref, []).append(n["id"])

    primary_syl_block, cross_syl_block = format_full_syllabus_locator(
        graph, primary_subj, syllabus_candidates, covered_syl_map
    )

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

    return f"""你是复旦基础医学跨学科知识图谱的**逻辑架构师与多模态编译器**。
严禁直接按 PPT 幻灯片目录逐页转写成“内容清单卡”，也**严禁用同一种模板去硬套所有医学学科**！
你必须以**完整原始教学大纲**定位坐标与学科所有权边界，结合**图文双通道精读（文字 + `read_file` 看机理原图）**忠实还原教授的课堂因果逻辑，最后输出严格合法的增量 JSON（delta_graph.json）与保留的机制原图资产。

## 课件元信息
- 文件：`{slide_name}`
- 主学科：`{primary_subj}`（来源：{subject_source}）
- 所属课程体系：{cur_label}

## Phase 0.5 · 原始教学大纲全量定位图（未经截断 `raw_text` · 只做定位与所有权边界，禁止先入为主硬套）
### 1. 本课主学科 `{primary_subj}` 全量大纲（`★重点/下划线` 与 `master` 必须零遗漏承接；`understand` 折叠；超纲故事剪枝）
{primary_syl_block}

### 2. 他科 / 跨课程大纲所有权边界（避免重复打补丁；若本课件顺带提及他科内容，优先复用或留跨学科桥接边）
{cross_syl_block}

## Sub-Discipline 认知语法（本次构建必须严格遵循）
{playbook_text}

## 已有图谱逻辑主线（优先评估 `thread_extensions` 嫁接延伸）
{existing_threads or "- （暂无已有主线）"}

## 已有概念节点全览（按相关度与枢纽度排序；同概念严禁重复建卡）
{existing_index or "- （暂无已有节点）"}

## 已有逻辑边参考
{existing_edges or "- （暂无已有边）"}

## Phase 1 · 本分块逐页骨架与视觉待检清单
{skeleton_txt}

## 逻辑拆解与高阶测试硬性准则（V2）
1. **看图读拓扑，严禁猜破碎文字**：凡标有 `🖼️ MUST_VIEW` 或含机理流程图/曲线/对比表图/显微照片的页，必须用 `read_file` 直接看图；核心机理总图/曲线/典型显微图用 `ingest_slides.py --mode crop-asset` 裁出保存至 `data/assets/*.webp` 并挂载 `diagram_assets`。
2. **打破 PPT 页序、单卡单步认知负荷与非胶囊关键词精标**：每卡聚焦 1 个因果推进/结构格局/易混对偶（30–45秒读完）。`structure_cloze` $\\le 5$ bullets，`mechanism_flow` 2–4 steps。严禁标题超过 26 字或带 `...`/`…`。非胶囊陈述句（`reveal` / `takeaway` / `comparison_matrix` 单元格 / `anchor_summary` / `trap_note` / `quiz.explain`）可用 `**关键词**` 标出每句 1–2 个真考点（严禁用于 `quiz.options` 防泄题，也严禁在 `{{{{胶囊}}}}` 内加 `**` 或整段加粗）。
3. **可选高阶反转诊断层（防泄题铁律）**：除 `comparison_matrix` 必带 `quiz` 外，建议为核心 `mechanism_flow` / `structure_cloze` 选配 1 道机制反推或临床断点题 `quiz`（可指定 `remedial_node_id` 指向前修根因，答错 🔴 时精准直达回溯）。**所有 4 个选项长度与语法结构必须对称**（把“因为……”留给 `explain`，严禁正确项比干扰项明显更长），且整批题目的 `answer_idx` 必须在 `0/1/2/3` 之间均衡打散（严禁全放 `0`）！
4. **诊断补漏锚点与 `bridge_text` 因果检验**：重要新节点配入向 `prerequisite` / `structural_basis`（若需开荒自动前置，设 `strength >= 0.90`；🔴 诊断回溯至少 `>= 0.85`）；`ordered_steps` 相邻边方向必须 `prev -> next`（仅 `contrast_with` 允许双向）。

## 严格输出格式要求
只输出一个合法 JSON 对象（可用 ```json 包裹），可选新增 `diagram_assets` 与通用 `quiz`：
```json
{{
  "nodes": [
    {{
      "id": "subject_snake_case_id",
      "title": "精炼概念标题（<=22字，禁用省略号）",
      "subject": "anatomy|histology|embryology|physiology|biochem|pathophys|pathology|pharmacology",
      "syllabus_refs": ["syl_xxx_001"],
      "syllabus_tier": "core_underlined|master|familiar|understand",
      "node_type": "mechanism_flow|structure_cloze|comparison_matrix",
      "anchor_summary": "40字以内一句话核心定位",
      "source_slides": ["{slide_name}#P1-P5"],
      "diagram_assets": [{{"path": "assets/example_pathway.webp", "caption": "原图说明", "source_page": 21}}],
      "payload": {{}}
    }}
  ],
  "edges": [
    {{
      "id": "e_src_to_dst",
      "source": "源节点ID",
      "target": "目标节点ID",
      "relation_type": "causal_next|prerequisite|structural_basis|clinical_manifestation|contrast_with|cross_discipline",
      "strength": 0.9,
      "bridge_text": "【必填】用1句话显式说明因果/结构/跨学科联系"
    }}
  ],
  "threads": [],
  "thread_extensions": []
}}
```

三种 `node_type` 的 `payload` 规范：
1. `mechanism_flow`：`{{"steps": [{{"cue": "预测问题", "reveal": "推演结论", "mechanism_tag": "标签"}}], "takeaway": "一句话总结", "quiz": "(可选高阶反转题)"}}`
2. `structure_cloze`：`{{"bullets": ["含{{{{核心词}}}}"], "trap_note": "易错陷阱", "quiz": "(可选功能/临床反转题)"}}`
3. `comparison_matrix`：`{{"columns": [...], "rows": [[...]], "quiz": {{"stem": "...", "options": [...], "answer_idx": 0, "explain": "...", "remedial_node_id": "可选前置节点ID"}}}}`

## 原始课件正文与页面标引
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
    pre_existing_refs = {r for n in graph.get("nodes", []) for r in n.get("syllabus_refs", [])}
    merged = json.loads(json.dumps(graph))
    nodes_by_id = {n["id"]: n for n in merged["nodes"]}
    nodes_by_title = {normalize_title(n["title"]): n for n in merged["nodes"]}
    id_remap: dict[str, str] = {}

    stats = {"nodes_added": 0, "nodes_enriched": 0, "edges_added": 0, "threads_added": 0}
    newly_added_ids: list[str] = []
    enriched_ids: list[str] = []
    enriched_new_quizzes: dict[str, dict] = {}
    enriched_new_assets: dict[str, list[dict]] = {}

    for inc_node in delta.get("nodes", []):
        nid = inc_node["id"]
        norm_t = normalize_title(inc_node["title"]) if inc_node.get("title") else ""
        existing = nodes_by_id.get(nid) or (nodes_by_title.get(norm_t) if norm_t else None)
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
            existing_assets = {a.get("path") for a in existing.get("diagram_assets", []) if isinstance(a, dict)}
            for da in inc_node.get("diagram_assets", []) or []:
                if isinstance(da, dict) and da.get("path") not in existing_assets:
                    existing.setdefault("diagram_assets", []).append(da)
                    existing_assets.add(da.get("path"))
                    enriched_new_assets.setdefault(existing["id"], []).append(da)
            inc_quiz = (inc_node.get("payload") or {}).get("quiz")
            if inc_quiz and isinstance(existing.get("payload"), dict) and (not existing["payload"].get("quiz") or inc_node.get("upgrade_quiz")):
                existing["payload"]["quiz"] = inc_quiz
                enriched_new_quizzes[existing["id"]] = inc_quiz
            enriched_ids.append(existing["id"])
            stats["nodes_enriched"] += 1
        else:
            new_node = json.loads(json.dumps(inc_node))
            new_node.setdefault("added_at", ts)
            new_node.setdefault("syllabus_refs", [])
            new_node.setdefault("source_slides", [])
            merged["nodes"].append(new_node)
            nodes_by_id[nid] = new_node
            if norm_t:
                nodes_by_title[norm_t] = new_node
            id_remap[nid] = nid
            newly_added_ids.append(nid)
            stats["nodes_added"] += 1

    edge_keys = {(e["source"], e["target"], e["relation_type"]) for e in merged["edges"]}
    edge_ids = {e["id"] for e in merged["edges"]}
    edge_id_remap: dict[str, str] = {}
    dropped_dangling_edges: list[str] = []

    for inc_edge in delta.get("edges", []):
        src = id_remap.get(inc_edge["source"], inc_edge["source"])
        dst = id_remap.get(inc_edge["target"], inc_edge["target"])
        if src == dst or src not in nodes_by_id or dst not in nodes_by_id:
            dropped_dangling_edges.append(f"{inc_edge.get('id', '?')}: {src}->{dst}")
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
    stats["logic_audit"] = audit_logic_quality(
        merged,
        delta,
        id_remap,
        dropped_dangling_edges,
        merge_context={
            "pre_existing_refs": pre_existing_refs,
            "newly_added_ids": newly_added_ids,
            "enriched_ids": enriched_ids,
            "enriched_new_quizzes": enriched_new_quizzes,
            "enriched_new_assets": enriched_new_assets,
        },
    )
    return merged, stats


FILLER_BRIDGE_PREFIXES = ("接下来", "下面我们", "然后学习", "此外还有", "本节介绍", "下一节")


def audit_logic_quality(
    graph: dict,
    delta: dict,
    id_remap: dict[str, str],
    dropped_dangling_edges: list[str] | None = None,
    merge_context: dict | None = None,
) -> dict:
    """Inspect structural logic density and highlight weak/unanchored delta artifacts."""
    nodes_by_id = {n["id"]: n for n in graph.get("nodes", [])}
    in_edges_by_node = {n["id"]: [] for n in graph.get("nodes", [])}
    out_edges_by_node = {n["id"]: [] for n in graph.get("nodes", [])}
    for e in graph.get("edges", []):
        in_edges_by_node.setdefault(e["target"], []).append(e)
        out_edges_by_node.setdefault(e["source"], []).append(e)

    if merge_context is not None:
        target_new_ids = [nid for nid in dict.fromkeys(merge_context.get("newly_added_ids", [])) if nid in nodes_by_id]
        target_enriched_ids = [nid for nid in dict.fromkeys(merge_context.get("enriched_ids", [])) if nid in nodes_by_id]
        pre_covered_refs = set(merge_context.get("pre_existing_refs", ()))
        enriched_new_quizzes = merge_context.get("enriched_new_quizzes", {})
        enriched_new_assets = merge_context.get("enriched_new_assets", {})
    else:
        target_new_ids = list(
            dict.fromkeys(
                id_remap.get(n["id"], n["id"])
                for n in delta.get("nodes", [])
                if id_remap.get(n["id"], n["id"]) in nodes_by_id
            )
        )
        target_enriched_ids = []
        delta_id_set = set(target_new_ids)
        pre_covered_refs = {
            r for nid, nd in nodes_by_id.items() if nid not in delta_id_set for r in nd.get("syllabus_refs", [])
        }
        enriched_new_quizzes = {}
        enriched_new_assets = {}

    warnings = []
    orphan_nodes = []
    unanchored_core = []
    bold_sprawl_edges: list[str] = []
    cross_discipline_edges = 0
    rescue_anchors = 0
    contrast_edges = 0
    new_thread_front_ids = {
        id_remap.get(step.get("node_id", ""), step.get("node_id", ""))
        for inc_th in delta.get("threads", [])
        for step in inc_th.get("ordered_steps", [])[:3]
    }

    def _has_bold_sprawl(text: str) -> bool:
        spans = re.findall(r"\*\*([^*\n]+?)\*\*", str(text or ""))
        if not spans:
            return False
        plain_len = len(re.sub(r"\*\*([^*\n]+?)\*\*", r"\1", str(text or "")).strip())
        bold_len = sum(len(sp) for sp in spans)
        return plain_len >= 26 and (bold_len / plain_len > 0.45 or (len(spans) >= 4 and plain_len < 80))

    for e in delta.get("edges", []):
        src = id_remap.get(e["source"], e["source"])
        dst = id_remap.get(e["target"], e["target"])
        sn = nodes_by_id.get(src)
        dn = nodes_by_id.get(dst)
        if e.get("relation_type") == "cross_discipline" or (sn and dn and sn["subject"] != dn["subject"]):
            cross_discipline_edges += 1
        if e.get("relation_type") in {"prerequisite", "structural_basis"}:
            rescue_anchors += 1
        is_prereq_gate = (
            e.get("relation_type") == "prerequisite"
            or (e.get("relation_type") == "structural_basis" and e.get("strength", 0.8) >= 0.9)
        )
        if sn and dn and is_prereq_gate:
            if sn.get("syllabus_tier") == "understand" and dn.get("syllabus_tier") != "understand":
                warnings.append(
                    f"Prerequisite leak risk on edge {e.get('id')}: understand-tier source '{src}' gates mainline target '{dst}' via {e.get('relation_type')}."
                )
            elif (
                sn.get("syllabus_tier") == "familiar"
                and dn.get("syllabus_tier") in {"core_underlined", "master"}
                and dst in new_thread_front_ids
            ):
                warnings.append(
                    f"Familiar prerequisite leak risk on edge {e.get('id')}: familiar source '{src}' gates front-of-thread core target '{dst}' (use causal_next)."
                )
        if e.get("relation_type") == "contrast_with":
            contrast_edges += 1
        bt = e.get("bridge_text", "").strip()
        if any(bt.startswith(p) for p in FILLER_BRIDGE_PREFIXES) or len(bt) < 12:
            warnings.append(f"Weak filler bridge_text on edge {e.get('id')}: '{bt}'")
        elif _has_bold_sprawl(bt):
            bold_sprawl_edges.append(str(e.get("id") or f"{src}->{dst}"))

    ungrounded_core = []
    missing_assets = []
    title_violations = []
    quiz_nodes_added = 0
    remedial_quiz_nodes = 0
    diagram_assets_added = 0
    comparison_cards_added = 0
    quiz_ans_counts: dict[int, int] = defaultdict(int)
    quiz_length_tell_nodes: list[str] = []
    extreme_length_tell_nodes: list[str] = []
    quiz_bilateral_asym_nodes: list[str] = []
    quiz_because_tell_nodes: list[str] = []
    quiz_bold_option_nodes: list[str] = []
    bold_sprawl_nodes: list[str] = []
    bold_in_cloze_nodes: list[str] = []
    dense_card_warnings: list[str] = []
    newly_covered_refs = set()

    def _check_bold_hygiene(nid: str, nd: dict) -> None:
        pay = nd.get("payload") or {}
        prose_blocks: list[str] = [str(nd.get("anchor_summary", ""))]
        if nd.get("node_type") == "mechanism_flow":
            prose_blocks.extend(str(st.get("reveal", "")) for st in pay.get("steps", []))
            prose_blocks.append(str(pay.get("takeaway", "")))
        elif nd.get("node_type") == "comparison_matrix":
            for row in pay.get("rows", []):
                prose_blocks.extend(str(cell) for cell in row)
        elif nd.get("node_type") == "structure_cloze":
            for b in pay.get("bullets", []):
                b_str = str(b)
                if any("**" in inner for inner in re.findall(r"\{\{(.+?)\}\}", b_str)):
                    bold_in_cloze_nodes.append(nid)
                prose_blocks.append(re.sub(r"\{\{.+?\}\}", "", b_str))
            if pay.get("trap_note"):
                prose_blocks.append(str(pay.get("trap_note")))
        if (pay.get("quiz") or {}).get("explain"):
            prose_blocks.append(str(pay["quiz"]["explain"]))
        if any(_has_bold_sprawl(blk) for blk in prose_blocks):
            if nid not in bold_sprawl_nodes:
                bold_sprawl_nodes.append(nid)

    def _tally_quiz(nid: str, quiz_obj: dict | None):
        nonlocal quiz_nodes_added, remedial_quiz_nodes
        if not quiz_obj:
            return
        quiz_nodes_added += 1
        if quiz_obj.get("remedial_node_id"):
            remedial_quiz_nodes += 1
        ans_i = quiz_obj.get("answer_idx")
        opts = quiz_obj.get("options", [])
        if opts and any(re.search(r"\*\*[^*\n]+?\*\*", str(o)) for o in opts):
            quiz_bold_option_nodes.append(nid)
        if isinstance(ans_i, int) and opts and 0 <= ans_i < len(opts):
            quiz_ans_counts[ans_i] += 1
            opt_lens = [len(str(o)) for o in opts]
            dist_lens = sorted(opt_lens[j] for j in range(len(opt_lens)) if j != ans_i)
            med_dist = dist_lens[len(dist_lens) // 2] if dist_lens else 0
            if opt_lens[ans_i] == max(opt_lens) and med_dist > 0 and opt_lens[ans_i] > med_dist * 1.25:
                quiz_length_tell_nodes.append(nid)
                if med_dist >= 8 and (opt_lens[ans_i] - med_dist) >= 10 and opt_lens[ans_i] > med_dist * 1.45:
                    extreme_length_tell_nodes.append(nid)
            min_len = min(opt_lens)
            max_len = max(opt_lens)
            if (min_len > 0 and max_len >= min_len * 2.25 and (max_len - min_len) >= 12) or (
                med_dist >= 10 and opt_lens[ans_i] < med_dist * 0.55 and (med_dist - opt_lens[ans_i]) >= 10
            ):
                quiz_bilateral_asym_nodes.append(nid)
            ans_txt = str(opts[ans_i])
            dist_txts = [str(opts[j]) for j in range(len(opts)) if j != ans_i]
            if len(dist_txts) >= 2 and "因为" in ans_txt and not any("因为" in dt for dt in dist_txts):
                quiz_because_tell_nodes.append(nid)

    def _tally_assets(nid: str, asset_list: list | None):
        nonlocal diagram_assets_added
        for da in asset_list or []:
            diagram_assets_added += 1
            rel = str(da.get("path", "")).replace("\\", "/")
            if not (DATA_DIR / rel).exists():
                missing_assets.append(f"{nid}:{rel}")

    for nid in target_new_ids:
        nd = nodes_by_id[nid]
        ins = in_edges_by_node.get(nid, [])
        outs = out_edges_by_node.get(nid, [])
        refs = nd.get("syllabus_refs", [])
        title = nd.get("title", "")
        if "…" in title or "..." in title or len(title) > 32:
            title_violations.append(nid)
        if nd["node_type"] == "comparison_matrix":
            comparison_cards_added += 1
        _tally_quiz(nid, (nd.get("payload") or {}).get("quiz"))
        _check_bold_hygiene(nid, nd)
        _tally_assets(nid, nd.get("diagram_assets", []))
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
        if nd["node_type"] == "structure_cloze":
            bullets = nd["payload"].get("bullets", [])
            if len(bullets) > 6:
                warnings.append(f"Kitchen-sink bloat risk in {nid}: >6 cloze bullets (consider splitting)")
            if any(len(str(b)) > 190 for b in bullets):
                dense_card_warnings.append(f"{nid}(bullet>190c)")
        if nd["node_type"] == "mechanism_flow":
            steps = nd["payload"].get("steps", [])
            if len(steps) > 5:
                warnings.append(f"Overlong mechanism_flow in {nid}: >5 steps (consider splitting)")
            if any(len(str(st.get("cue", ""))) + len(str(st.get("reveal", ""))) > 250 for st in steps):
                dense_card_warnings.append(f"{nid}(step>250c)")

    for nid in target_enriched_ids:
        nd = nodes_by_id[nid]
        for r in nd.get("syllabus_refs", []):
            if r not in pre_covered_refs:
                newly_covered_refs.add(r)
        _tally_quiz(nid, enriched_new_quizzes.get(nid))
        _tally_assets(nid, enriched_new_assets.get(nid))
        if not in_edges_by_node.get(nid) and not out_edges_by_node.get(nid):
            orphan_nodes.append(nid)

    if quiz_nodes_added >= 4 and quiz_ans_counts:
        top_cnt = max(quiz_ans_counts.values())
        if top_cnt / quiz_nodes_added > 0.60:
            warnings.append(
                f"Quiz position bias: {top_cnt}/{quiz_nodes_added} quizzes share one answer_idx ({dict(quiz_ans_counts)}); spread across A/B/C/D."
            )
    if (quiz_nodes_added >= 4 and len(quiz_length_tell_nodes) / quiz_nodes_added >= 0.50) or extreme_length_tell_nodes:
        flagged_quiz_ids = extreme_length_tell_nodes or quiz_length_tell_nodes
        warnings.append(
            f"Quiz length bias: {len(quiz_length_tell_nodes)}/{quiz_nodes_added} correct options are >25% longer than distractors "
            f"(flagged: {flagged_quiz_ids}); keep options symmetric and put rationale in explain."
        )
    if quiz_bilateral_asym_nodes:
        warnings.append(
            f"Quiz bilateral option asymmetry: extreme option length spread (>=2.25x & >=12c diff or ultra-short key) on "
            f"{quiz_bilateral_asym_nodes}; balance option lengths across A/B/C/D."
        )
    if quiz_because_tell_nodes:
        warnings.append(
            f"Quiz rationale-in-option tell: correct option contains '因为' while distractors do not on "
            f"{quiz_because_tell_nodes}; move causal explanation into quiz.explain."
        )
    if quiz_bold_option_nodes:
        warnings.append(
            f"Quiz markup leak in options: remove '**...**' emphasis inside quiz.options on "
            f"{quiz_bold_option_nodes} (reserve highlights for prose and quiz.explain)."
        )
    if bold_in_cloze_nodes:
        warnings.append(
            f"Bold markup inside cloze pill: do not put '**...**' inside '{{{{...}}}}' on {sorted(set(bold_in_cloze_nodes))}."
        )
    if bold_sprawl_nodes:
        warnings.append(
            f"Bold sprawl risk: over-highlighted >45% of text in {bold_sprawl_nodes}; keep '**...**' to 1-2 pivotal terms per block."
        )
    if bold_sprawl_edges:
        warnings.append(
            f"Bridge bold sprawl risk: over-highlighted >45% of text on edge(s) {bold_sprawl_edges}; keep '**...**' to 1-2 pivotal terms."
        )

    understand_leaks: list[str] = []
    for inc_th in delta.get("threads", []):
        for step in inc_th.get("ordered_steps", []):
            step_nid = id_remap.get(step.get("node_id", ""), step.get("node_id", ""))
            if nodes_by_id.get(step_nid, {}).get("syllabus_tier") == "understand":
                understand_leaks.append(f"{inc_th.get('id')}:{step_nid}")
    for ext in delta.get("thread_extensions", []):
        for step in ext.get("append_steps", []):
            step_nid = id_remap.get(step.get("node_id", ""), step.get("node_id", ""))
            if nodes_by_id.get(step_nid, {}).get("syllabus_tier") == "understand":
                understand_leaks.append(f"{ext.get('thread_id')}:{step_nid}")
    if understand_leaks:
        warnings.append(
            f"Understand-tier node(s) in thread ordered_steps (keep understand nodes on side branches off mainline ordered_steps): {understand_leaks}"
        )
    if dense_card_warnings:
        warnings.append(f"Mobile density risk (overlong card blocks): {dense_card_warnings}")

    if orphan_nodes:
        warnings.append(f"Orphan nodes without any edges: {orphan_nodes}")
    if unanchored_core:
        warnings.append(f"Core nodes missing incoming logic foundation: {unanchored_core}")
    if ungrounded_core:
        warnings.append(f"Core/Master nodes missing syllabus_refs grounding: {ungrounded_core}")
    if missing_assets:
        warnings.append(f"Missing diagram_assets files under data/: {missing_assets}")
    if title_violations:
        warnings.append(f"Node title exceeds 32 chars or contains ellipsis (.../…): {title_violations}")
    if dropped_dangling_edges:
        warnings.append(f"Dropped dangling or self-loop edges referencing missing node IDs: {dropped_dangling_edges}")

    # Discipline-specific logic grammar checks (based on genuinely added nodes)
    added_subjects = {nodes_by_id[nid]["subject"] for nid in target_new_ids}
    if any(s in added_subjects for s in ("pathology", "pathophys", "pharmacology")):
        has_foundation_anchor = any(
            e.get("relation_type") in {"prerequisite", "structural_basis", "cross_discipline"}
            for e in delta.get("edges", [])
        )
        if target_new_ids and not has_foundation_anchor:
            warnings.append(
                "Discipline logic gap: pathology/pathophys/pharmacology additions should include at least one "
                "prerequisite, structural_basis, or cross_discipline anchor back to normal anatomy/histology/physiology/biochem."
            )

    return {
        "cross_discipline_edges": cross_discipline_edges,
        "rescue_anchors": rescue_anchors,
        "contrast_edges": contrast_edges,
        "comparison_cards_added": comparison_cards_added,
        "quiz_nodes_added": quiz_nodes_added,
        "remedial_quiz_nodes": remedial_quiz_nodes,
        "diagram_assets_added": diagram_assets_added,
        "dropped_dangling_edges": len(dropped_dangling_edges or []),
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


def extract_and_write_context(
    slide_path: Path,
    subject: str | None,
    graph: dict,
    out_dir: Path,
    chunk_size: int = 25,
    mode: str = "extract-context",
    render_visuals: bool = False,
    dpi: int | None = None,
) -> dict:
    """Extract courseware pages + visuals and write partitioned markdown context files."""
    meta = resolve_lecture_metadata(slide_path, subject)
    visual_dir = out_dir / "visuals" / slide_path.stem
    pages = extract_courseware(slide_path, visual_dir=visual_dir, render_visuals=render_visuals, dpi=dpi)
    if not pages:
        raise RuntimeError(f"No slides/pages found in {slide_path}.")
    primary_subj = meta["primary_subject"]
    syl_candidates = match_syllabus_candidates(pages, graph.get("syllabus", []), top_k=24, primary_subject=primary_subj)
    chunks = [pages[i : i + chunk_size] for i in range(0, len(pages), chunk_size)]
    generated_files = []
    suffix_tag = "context" if mode == "extract-context" else "prompt_pack"
    for idx, chunk in enumerate(chunks, 1):
        is_context_cont = (mode == "extract-context" and idx > 1)
        prompt = build_prompt_text(
            slide_path.name,
            chunk,
            syl_candidates,
            graph,
            primary_subject=primary_subj,
            curriculum_name=meta.get("curriculum_name"),
            include_atlas=not is_context_cont,
            chunk_idx=idx,
            total_chunks=len(chunks),
            all_pages_for_index=pages,
        )
        out_file = out_dir / f"{slide_path.stem}_part{idx}_{suffix_tag}.md"
        out_file.write_text(prompt, encoding="utf-8")
        generated_files.append(str(out_file))
    vis_pages = [p["page"] for p in pages if p.get("needs_vision")]
    return {
        "pages": len(pages),
        "subject": primary_subj or "auto",
        "vision_flagged": len(vis_pages),
        "files": generated_files,
    }


def batch_extract_pending(
    manifest_path: Path,
    graph: dict,
    out_dir: Path,
    subject: str | None = None,
    chunk_size: int = 25,
    render_visuals: bool = False,
    force: bool = False,
    dpi: int | None = None,
) -> dict:
    """One-shot batch extraction for all pending lectures in course_manifest.json."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pending = [
        lec
        for lec in manifest.get("lectures", [])
        if lec.get("status", "pending") == "pending" and (not subject or lec.get("subject") == subject)
    ]
    extracted, skipped, failed = 0, 0, []
    total = len(pending)
    for idx, lec in enumerate(pending, 1):
        slide_path = Path(lec["path"])
        part1 = out_dir / f"{slide_path.stem}_part1_context.md"
        if part1.exists() and not force:
            skipped += 1
            print(f"[{idx}/{total}] SKIP (cached): {slide_path.name}")
            continue
        try:
            res = extract_and_write_context(
                slide_path,
                lec.get("subject") or subject,
                graph,
                out_dir,
                chunk_size=chunk_size,
                mode="extract-context",
                render_visuals=render_visuals,
                dpi=dpi,
            )
            extracted += 1
            print(
                f"[{idx}/{total}] OK: {slide_path.name} ({res['pages']}p, vis={res['vision_flagged']}, {len(res['files'])} parts)"
            )
        except Exception as err:
            failed.append(f"{slide_path.name}: {err}")
            print(f"[{idx}/{total}] FAIL: {slide_path.name} -> {err}")
    return {"total": total, "extracted": extracted, "skipped": skipped, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description="Compile slides into MedGraph Flow delta JSON and merge safely.")
    parser.add_argument("--slide", help="Path to courseware (.pptx/.ppt/.pdf/.docx)")
    parser.add_argument("--subject", choices=sorted(SUBJECTS), help="Explicit primary subject override (skips fragile auto-guessing)")
    parser.add_argument("--mode", choices=["extract-context", "prompt-pack", "api", "merge", "audit-delta", "crop-asset", "batch-extract"], required=True)
    parser.add_argument("--manifest", default=str(DATA_DIR / "course_manifest.json"), help="Manifest JSON path for --mode batch-extract")
    parser.add_argument("--force", action="store_true", help="Re-extract even if *_part1_context.md exists (for --mode batch-extract)")
    parser.add_argument("--render-visuals", action="store_true", help="Rasterize visual/diagram PDF pages or dump PPTX images into inbox/visuals/")
    parser.add_argument("--dpi", type=int, default=None, help="Override PDF visual rasterization DPI (default: auto 280 portrait / 150 landscape)")
    parser.add_argument("--delta", help="Path to delta_graph.json (for --mode merge or api output)")
    parser.add_argument("--image", help="Source image path for --mode crop-asset")
    parser.add_argument("--box", default="full", help="Normalized crop box 'x1,y1,x2,y2' (0..1) or 'full' for --mode crop-asset")
    parser.add_argument("--out-name", help="Output filename under data/assets/ for --mode crop-asset")
    parser.add_argument("--graph", default=str(DATA_DIR / "graph.json"), help="Target graph.json path")
    parser.add_argument("--chunk-size", type=int, default=25, help="Max slides per prompt pack chunk")
    parser.add_argument("--out-dir", default=str(INBOX_DIR), help="Directory for prompt packs / intermediate outputs")
    args = parser.parse_args()

    if args.mode == "crop-asset":
        if not args.image or not args.out_name:
            raise ValueError("--mode crop-asset requires --image <path> and --out-name <filename.webp>")
        info = crop_asset_image(Path(args.image), args.out_name, args.box)
        print(f"Cropped asset -> {info['rel_path']} ({info['width']}x{info['height']}, {info['size_kb']} KB) at {info['abs_path']}")
        return

    graph_path = Path(args.graph)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = load_graph(graph_path)

    if args.mode == "batch-extract":
        summary = batch_extract_pending(
            Path(args.manifest),
            graph,
            out_dir,
            subject=args.subject,
            chunk_size=args.chunk_size,
            render_visuals=args.render_visuals,
            force=args.force,
            dpi=args.dpi,
        )
        print(f"Batch extract finished: {json.dumps(summary, ensure_ascii=False)}")
        return

    if args.mode in {"merge", "audit-delta"}:
        if not args.delta:
            raise ValueError(f"--mode {args.mode} requires --delta <delta_graph.json>")
        delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
        # Support enriching existing nodes with new quiz or diagram_assets safely
        merged, stats = merge_delta_into_graph(graph, delta)
        if args.mode == "audit-delta":
            print(f"Dry-run audit OK (no files modified): {json.dumps(stats, ensure_ascii=False)}")
            return
        if graph_path.exists():
            shutil.copy2(graph_path, graph_path.with_suffix(".json.bak"))
        graph_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Merged into {graph_path}: {json.dumps(stats, ensure_ascii=False)}")
        return

    if not args.slide:
        raise ValueError(f"--mode {args.mode} requires --slide <path>")
    slide_path = Path(args.slide)

    if args.mode in {"extract-context", "prompt-pack"}:
        res = extract_and_write_context(
            slide_path,
            args.subject,
            graph,
            out_dir,
            chunk_size=args.chunk_size,
            mode=args.mode,
            render_visuals=args.render_visuals,
            dpi=args.dpi,
        )
        suffix_tag = "context" if args.mode == "extract-context" else "prompt_pack"
        print(
            f"Extracted {res['pages']} pages (subject={res['subject']}, vision_flagged={res['vision_flagged']}) "
            f"-> {len(res['files'])} {suffix_tag} file(s):"
        )
        for f in res["files"]:
            print(f"  {f}")
        return

    if args.mode == "api":
        meta = resolve_lecture_metadata(slide_path, args.subject)
        visual_dir = out_dir / "visuals" / slide_path.stem
        pages = extract_courseware(slide_path, visual_dir=visual_dir, render_visuals=args.render_visuals, dpi=args.dpi)
        if not pages:
            raise RuntimeError(f"No slides/pages found in {slide_path}.")
        primary_subj = meta["primary_subject"]
        syl_candidates = match_syllabus_candidates(pages, graph.get("syllabus", []), top_k=24, primary_subject=primary_subj)
        chunks = [pages[i : i + args.chunk_size] for i in range(0, len(pages), args.chunk_size)]
        combined_delta = {"nodes": [], "edges": [], "threads": []}
        for idx, chunk in enumerate(chunks, 1):
            prompt = build_prompt_text(
                slide_path.name,
                chunk,
                syl_candidates,
                graph,
                primary_subject=primary_subj,
                curriculum_name=meta.get("curriculum_name"),
            )
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
